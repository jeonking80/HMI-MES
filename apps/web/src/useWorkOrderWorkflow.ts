import { useCallback, useEffect, useRef, useState } from 'react'
import type { SetStateAction } from 'react'
import type { Session } from './auth'
import type { WorkOrder } from './mesApi'
import { MesHTTPError, mesRequest } from './mesApi'
import { conflictMessage } from './conflictReasons'
import { parseWorkflow, workflowBody, workflowIdentity, workflowPath } from './workflow'
import type { WorkflowAttempt, WorkflowIntent, WorkflowKind } from './workflow'
import type { CommandResult } from './commands'
import { archiveBlocked, ExecutionArchiveError, executionPath, freezeArchivedWorkflow, parseExecution } from './executionApi'
import type { ArchivedWorkflow } from './executionApi'
import { sameWorkflowValue } from './workflow'
import type { WorkOrderMutationGuard } from './workOrderMutationGuard'

/** The server function stops by 60s; past this a missing record can no longer be committed late. */
const SERVER_DEADLINE_MS = 70_000
const notStored = (kind: WorkflowKind) => `서버가 이 ${kind === 'release' ? '배포' : '실행 연결'} 요청을 거절했고, 원래 ID로 저장된 기록이 없습니다. 작업지시 상태와 라인 조건을 확인한 뒤 새로 요청할 수 있습니다.`
/** A 404 closes an attempt only once nothing can still commit it: a 409 answer, the server deadline, or a read-only query. */
const settled = (attempt: WorkflowAttempt) => attempt.postStatus === 409 || !attempt.knownIntent || (attempt.sentAt !== undefined && Date.now() - attempt.sentAt > SERVER_DEADLINE_MS)

/** Lives above authenticated components. No request retry or browser persistence. */
export function useWorkOrderWorkflow(session: Session | null, onSessionInvalid: () => void, onArchived: () => void, guard: WorkOrderMutationGuard, onReleased?: (workOrderId: string) => void) {
  const [{ attempt, history }, setLedger] = useState<{ attempt: WorkflowAttempt | null; history: readonly ArchivedWorkflow[] }>({ attempt: null, history: [] })
  const setAttempt = useCallback((update: SetStateAction<WorkflowAttempt | null>) => setLedger(previous => ({ ...previous, attempt: typeof update === 'function' ? update(previous.attempt) : update })), [])
  const [checking, setChecking] = useState(false)
  // Confirmed releases close their attempt at once; this count lets lists reload after one.
  const [released, setReleased] = useState<{ count: number; id: string }>({ count: 0, id: '' })
  const active = useRef<AbortController | null>(null)
  const locked = useRef(false)
  useEffect(() => () => { active.current?.abort() }, [session])
  const recordLoadResult = useCallback((result: CommandResult) => setAttempt(previous => previous?.record && 'command_id' in previous.record.receipt && previous.record.receipt.command_id === result.command_id ? { ...previous, loadResult: result } : previous), [setAttempt])
  async function perform(kind: WorkflowKind, intent: WorkflowIntent, post: boolean, previous?: WorkflowAttempt, order?: WorkOrder) {
    if (!session || session.must_change_password || locked.current) return
    if (post && (intent.actor !== session.login || attempt || !(kind === 'release' ? session.roles.includes('production_manager') : session.roles.some(role => ['operator', 'production_manager'].includes(role))))) return
    if (post && !guard.acquire(intent.work_order_id, 'workflow', workflowIdentity(intent))) return
    if (!post) guard.retain(intent.work_order_id, 'workflow', workflowIdentity(intent))
    locked.current = true
    setChecking(true)
    const controller = new AbortController()
    active.current = controller
    const initial: WorkflowAttempt = previous ?? { kind, intent: Object.freeze({ ...intent }), knownIntent: post, expectedOrder: order ? { recipe_version_id: order.recipe_version_id, target_quantity: order.target_quantity } : null, phase: post ? 'sending' : 'uncertain', record: null, message: '', ...(post ? { sentAt: Date.now() } : {}) }
    setAttempt(initial)
    try {
      const response = await mesRequest(workflowPath(kind, intent.work_order_id, post ? undefined : workflowIdentity(intent)), controller.signal, post ? session.csrf_token : undefined, post ? workflowBody(kind, intent) : undefined)
      const record = parseWorkflow(kind, post ? { intent, receipt: response } : response, intent.work_order_id, workflowIdentity(intent), initial.knownIntent ? intent : undefined, initial.expectedOrder)
      // A confirmed release needs no further action: close it and report it in place.
      if (kind === 'release') { guard.release(record.intent.work_order_id, 'workflow', workflowIdentity(record.intent)); setAttempt(null); setReleased(previous => ({ count: previous.count + 1, id: record.intent.work_order_id })); onReleased?.(record.intent.work_order_id); return }
      setAttempt({ ...initial, intent: record.intent, knownIntent: true, phase: 'confirmed', record, message: '실행 연결 등록을 확인했습니다. 작업 불러오기의 PLC 결과를 별도로 확인해 주세요.' })
    } catch (cause) {
      const status = cause instanceof MesHTTPError ? cause.status : 0
      const reason = conflictMessage(cause instanceof MesHTTPError ? cause.reasonCode : null)
      const tried = post ? { ...initial, postStatus: status } : initial
      if (post && status === 409) {
        // A 409 is a server answer, not a lost response: read the original ID once to tell a stored duplicate from a rejection.
        try {
          const stored = parseWorkflow(kind, await mesRequest(workflowPath(kind, intent.work_order_id, workflowIdentity(intent)), controller.signal), intent.work_order_id, workflowIdentity(intent), intent, initial.expectedOrder)
          if (kind === 'release') { guard.release(stored.intent.work_order_id, 'workflow', workflowIdentity(stored.intent)); setAttempt(null); setReleased(previous => ({ count: previous.count + 1, id: stored.intent.work_order_id })); onReleased?.(stored.intent.work_order_id); return }
          setAttempt({ ...tried, intent: stored.intent, knownIntent: true, phase: 'confirmed', record: stored, message: '원래 ID의 저장 기록을 확인했습니다.' })
        } catch (lookup) {
          const missing = lookup instanceof MesHTTPError && lookup.status === 404
          setAttempt({ ...tried, phase: missing ? 'rejected' : 'conflict', message: missing ? (reason ? `${reason} (저장된 요청은 없습니다.)` : notStored(kind)) : '원래 ID의 요청이 충돌했습니다. 원래 기록을 조회하고 내용을 확인해 주세요.' })
        }
      } else if (!post && status === 404 && settled(initial)) {
        setAttempt({ ...initial, phase: 'rejected', message: initial.postStatus === 409 ? notStored(initial.kind) : initial.sentAt !== undefined ? `요청 후 ${Math.round(SERVER_DEADLINE_MS / 1000)}초가 지나도 저장 기록이 없습니다. 서버 처리 시한이 지났으므로 저장되지 않은 요청으로 확정합니다.` : '조회한 ID의 저장 기록이 없습니다. 조회를 닫을 수 있습니다.' })
      } else {
        setAttempt({ ...tried, phase: status === 409 ? 'conflict' : 'uncertain', message: status === 409 ? '원래 ID의 요청이 충돌했습니다. 원래 기록을 조회하고 내용을 확인해 주세요.' : status === 404 ? '원래 ID의 저장 기록을 아직 확인하지 못했습니다. 요청이 뒤늦게 저장될 수 있어 새 요청을 보내지 않습니다.' : '응답 결과를 확인할 수 없습니다. 원래 ID와 요청 내용을 보존하며 조회만 가능합니다.' })
      }
      if (!controller.signal.aborted && (status === 401 || status === 403)) onSessionInvalid()
    } finally { locked.current = false; setChecking(false) }
  }
  async function archiveExecution() {
    if (!session || session.must_change_password || locked.current || !attempt?.record || !('execution_id' in attempt.record.receipt) || session.login !== attempt.intent.actor || attempt.phase !== 'confirmed') throw new ExecutionArchiveError('원래 요청 계정과 확인된 연결 기록이 필요합니다.')
    const original = attempt
    const receipt = attempt.record.receipt
    const controller = new AbortController()
    active.current = controller
    locked.current = true
    setChecking(true)
    try {
      // Always read again on the explicit archive click; displayed evidence is not authority.
      const response = await mesRequest(executionPath(receipt), controller.signal)
      if (controller.signal.aborted) throw new ExecutionArchiveError('로그인 상태가 바뀌어 보관하지 않았습니다.')
      const view = parseExecution(response, receipt)
      const reason = archiveBlocked(view, original, session.login)
      if (reason) throw new ExecutionArchiveError(reason)
      setLedger(previous => {
        if (!previous.attempt || !sameWorkflowValue(previous.attempt.record, original.record) || previous.attempt.phase !== 'confirmed') return previous
        const entry = freezeArchivedWorkflow({ attempt: { ...previous.attempt }, execution: view })
        const exists = previous.history.some(item => item.attempt.record && sameWorkflowValue(item.attempt.record, original.record))
        return { attempt: null, history: exists ? previous.history : Object.freeze([...previous.history, entry]) }
      })
      guard.release(original.intent.work_order_id, 'workflow', workflowIdentity(original.intent))
      onArchived()
      return view
    } catch (cause) {
      if (!controller.signal.aborted && cause instanceof MesHTTPError && [401, 403].includes(cause.status)) onSessionInvalid()
      throw cause
    } finally { locked.current = false; setChecking(false) }
  }
  return {
    attempt, history, checking, archiveExecution, released,
    submit: (kind: WorkflowKind, intent: WorkflowIntent, order?: WorkOrder) => perform(kind, intent, true, undefined, order),
    lookup: () => attempt ? perform(attempt.kind, attempt.intent, false, attempt) : Promise.resolve(),
    recover: (kind: WorkflowKind, order: string, id: string) => {
      if (attempt) return Promise.resolve()
      // Placeholders exist only until the read returns the stored intent; never sent as POST.
      const intent: WorkflowIntent = kind === 'release' ? { work_order_id: order, actor: session?.login ?? '', release_id: id, expected_row_version: '1' } : { work_order_id: order, actor: session?.login ?? '', command_id: id, line_id: '', source_id: '', stream_id: '', expected_work_order_version: '1', expected_control_version: '0' }
      return perform(kind, intent, false)
    },
    dismissRejected: () => { if (!locked.current && attempt?.phase === 'rejected') { guard.release(attempt.intent.work_order_id, 'workflow', workflowIdentity(attempt.intent)); setAttempt(null) } },
    acknowledgeRelease: () => { if (!locked.current && attempt?.kind === 'release' && attempt.phase === 'confirmed') { guard.release(attempt.intent.work_order_id, 'workflow', workflowIdentity(attempt.intent)); setAttempt(null) } },
    orderBlocked: (id: string) => guard.blocked(id),
    recordLoadResult,
  }
}
export type WorkOrderWorkflow = ReturnType<typeof useWorkOrderWorkflow>
