import { useLayoutEffect, useRef, useState } from 'react'
import type { Session } from './auth'
import type { WorkOrder } from './mesApi'
import { MesHTTPError } from './mesApi'
import { cancellationCanWrite, cancellationFailure, cancellationReference, cancellationRequest, freezeCancellation, CancellationRejection, CancellationRecoveryConflict, cancellationReprepareBlocked, archiveRejectedCancellation, readCancellationOrder } from './cancellationApi'
import type { CancellationIntent, CancellationLedger, CancellationReference, CancellationObservation, RejectedCancellationArchive } from './cancellationApi'
import type { WorkOrderMutationGuard } from './workOrderMutationGuard'

/** App-owned memory. A lookup may verify an intent but can never authorize a POST retry. */
export function useCancellation(session: Session | null, onSessionInvalid: () => void, guard: WorkOrderMutationGuard, knownLinked: (order: string) => boolean, onPrepared: () => void) {
  const [ledger, setLedger] = useState<CancellationLedger | null>(null)
  const [history, setHistory] = useState<readonly CancellationLedger[]>([])
  const [busy, setBusy] = useState(false)
  const [rejectedHistory, setRejectedHistory] = useState<readonly RejectedCancellationArchive[]>([])
  const [preparedOrder, setPreparedOrder] = useState<WorkOrder | null>(null)
  const [preparationMessage, setPreparationMessage] = useState('')
  const [ownership, setOwnership] = useState({ session, generation: 0 })
  if (ownership.session !== session) { setOwnership({ session, generation: ownership.generation + 1 }); setPreparedOrder(null) }
  const currentGeneration = useRef(ownership.generation)
  const active = useRef<AbortController | null>(null)
  const locked = useRef(false)
  useLayoutEffect(() => { currentGeneration.current = ownership.generation; return () => { active.current?.abort() } }, [ownership.generation])
  async function perform(reference: CancellationReference, intent: CancellationIntent | null, post: boolean) {
    if (!session || session.must_change_password || locked.current) return
    if (post && (ledger || !cancellationCanWrite(session) || intent?.actor !== session.login || knownLinked(reference.work_order_id))) return
    if (post && !guard.acquire(reference.work_order_id, 'cancellation', reference.cancellation_id)) return
    if (!post) guard.retain(reference.work_order_id, 'cancellation', reference.cancellation_id)
    locked.current = true
    const controller = new AbortController()
    active.current = controller
    setBusy(true)
    const generation = ownership.generation
    setPreparationMessage('')
    const original = freezeCancellation({ recovery_conflicted: ledger?.recovery_conflicted ?? false, submission: post ? { actor: session.login, generation, send_count: 1 as const, rejection: null, indeterminate: false } : ledger?.submission ?? null, observations: ledger?.observations ?? [], reference: { ...reference }, intent: intent ? { ...intent } : null, receipt: ledger?.receipt ?? null })
    setLedger({ ...original, phase: post ? 'SENDING' : 'UNKNOWN', message: post ? '작업지시 취소 응답을 확인하고 있습니다.' : '원래 취소 기록을 조회하고 있습니다.' })
    try {
      const parsed = await cancellationRequest(reference, controller.signal, post ? intent : null, post ? session.csrf_token : undefined)
      if (!post && intent && (parsed.intent.actor !== intent.actor || parsed.intent.expected_row_version !== intent.expected_row_version)) throw new CancellationRecoveryConflict()
      const observation: CancellationObservation = { method: post ? 'POST' : 'GET', status: 200, outcome: 'HTTP', observed_at: new Date().toISOString() }
      setLedger(freezeCancellation({ ...original, observations: [...original.observations, observation], reference: original.reference, intent: parsed.intent, receipt: parsed.receipt, phase: 'CONFIRMED', message: '작업지시 취소 기록을 확인했습니다. 설비 정지·실행 종료·예약 해제를 수행한 것이 아닙니다.' }))
    } catch (cause) {
      const status = cause instanceof MesHTTPError ? cause.status : 0
      const confirmed = post && cause instanceof CancellationRejection && !controller.signal.aborted && generation === currentGeneration.current
      const observation: CancellationObservation = { method: post ? 'POST' : 'GET', status: confirmed ? 409 : status || null, outcome: confirmed || (!post && status > 0) ? 'HTTP' : 'UNCONFIRMED', observed_at: confirmed ? cause.observedAt : new Date().toISOString() }
      const submission = post && original.submission ? { ...original.submission, rejection: confirmed ? observation : null, indeterminate: !confirmed } : original.submission
      setLedger(cancellationFailure({ ...original, recovery_conflicted: original.recovery_conflicted || cause instanceof CancellationRecoveryConflict, submission, observations: [...original.observations, observation] }, status))
      if (!controller.signal.aborted && (status === 401 || status === 403)) onSessionInvalid()
    } finally { locked.current = false; setBusy(false) }
  }
  function submit(order: WorkOrder) {
    if (!session || ledger || !['DRAFT', 'RELEASED'].includes(order.status) || guard.blocked(order.id) || knownLinked(order.id) || !cancellationCanWrite(session)) return Promise.resolve()
    const reference = cancellationReference(order.id, crypto.randomUUID())
    const intent = freezeCancellation({ ...reference, actor: session.login, expected_row_version: order.row_version })
    setPreparedOrder(null)
    return perform(reference, intent, true)
  }
  function blockedReason(order: string) {
    if (ledger) return '원래 취소 요청의 저장 기록을 먼저 확인해 주세요.'
    if (knownLinked(order)) return '실행 연결 기록이 있는 작업은 이 화면에서 취소할 수 없습니다.'
    if (guard.blocked(order)) return '같은 작업지시의 원래 요청 기록을 먼저 확인해 주세요.'
    return ''
  }
  async function prepareAgain() {
    if (!ledger || locked.current || cancellationReprepareBlocked(ledger, session, ownership.generation)) return
    const original = ledger
    const generation = ownership.generation
    const controller = new AbortController()
    active.current = controller
    locked.current = true
    setBusy(true)
    setPreparationMessage('현재 작업 내용을 다시 확인하고 있습니다.')
    try {
      const order = await readCancellationOrder(original.reference, controller.signal)
      if (controller.signal.aborted || currentGeneration.current !== generation) throw new Error('Session changed')
      const archived = archiveRejectedCancellation(original, order, session, generation, new Date().toISOString())
      setRejectedHistory(previous => previous.some(item => item.ledger.reference.cancellation_id === original.reference.cancellation_id) ? previous : Object.freeze([...previous, archived]))
      guard.release(original.reference.work_order_id, 'cancellation', original.reference.cancellation_id)
      setLedger(null)
      setPreparedOrder(order)
      setPreparationMessage('거절 기록을 보관했습니다. 갱신된 작업 내용을 확인한 뒤 별도로 취소를 요청해 주세요.')
      onPrepared()
    } catch (cause) {
      setPreparationMessage('현재 작업 내용을 확인하지 못해 새 취소를 준비하지 않았습니다. 원래 요청 기록을 유지합니다.')
      if (!controller.signal.aborted && cause instanceof MesHTTPError && [401, 403].includes(cause.status)) onSessionInvalid()
    } finally { locked.current = false; setBusy(false) }
  }
  return {
    ledger, history, busy, submit, rejectedHistory, preparedOrder, preparationMessage, prepareAgain,
    canPrepareAgain: !busy && !cancellationReprepareBlocked(ledger, session, ownership.generation),
    dismissPrepared: () => setPreparedOrder(null),
    blocked: (order: string) => blockedReason(order) !== '', blockedReason,
    lookup: () => ledger ? perform(ledger.reference, ledger.intent, false) : Promise.resolve(),
    recover: (order: string, id: string) => !ledger ? perform(cancellationReference(order, id), null, false) : Promise.resolve(),
    acknowledge: () => {
      if (locked.current || ledger?.phase !== 'CONFIRMED' || !ledger.intent || session?.login !== ledger.intent.actor) return
      // Retain this work-order guard: a known CANCELLED order cannot be released/bound.
      setHistory(previous => Object.freeze([...previous, ledger])); setLedger(null)
    },
  }
}
export type CancellationWorkflow = ReturnType<typeof useCancellation>
