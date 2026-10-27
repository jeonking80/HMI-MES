import { useEffect, useRef, useState } from 'react'
import type { Dispatch, SetStateAction } from 'react'
import type { Session } from '../auth'
import { CommandHTTPError, commandHTTP, commandLookupPath, newCommandRequest, parseResult, sameScope, terminal, uint64String } from '../commands'
import type { CommandKind, CommandRequest, CommandScope, TrackedCommand } from '../commands'
import { conflictMessage } from '../conflictReasons'

/**
 * Line command requests: which commands this session may send now and why not, submitting one
 * with its original ID, and following that ID to a final result. The PLC decides; an interrupted
 * or unknown result keeps the original ID and is never re-sent under a new one.
 * Moved unchanged from the former CommandPanel so the new screens share one implementation.
 */
export interface LineCommandsInput {
  source: CommandScope; session: Session; blockedReason: string; values: Record<string, unknown>
  newIntentBlocked?: string
  tracked: TrackedCommand | null; setTracked: Dispatch<SetStateAction<TrackedCommand | null>>
  onSessionInvalid: () => void
}
const identifier = (value: unknown): value is string => typeof value === 'string' && value.length > 0 && value.length <= 128 && !/\s/.test(value)
export const stateNames: Record<string, string> = { 0: 'STOPPED', 1: 'READY', 2: 'RUNNING', 3: 'PAUSED', 4: 'STOPPING', 5: 'FAULT', 6: 'ESTOP', 7: 'RECOVERY_REQUIRED' }

export function useLineCommands({ source, session, blockedReason, newIntentBlocked = '', values, tracked, setTracked, onSessionInvalid }: LineCommandsInput) {
  const [confirmEnd, setConfirmEnd] = useState<{ execution: string; version: string; epoch: string } | null>(null)
  const [lookupAttempt, setLookupAttempt] = useState(0)
  const [lookupMessage, setLookupMessage] = useState('')
  const [polling, setPolling] = useState(false)
  const postController = useRef<AbortController | null>(null)
  const submitting = useRef(false)
  const operator = session.roles.includes('operator')
  const producer = session.roles.includes('production_manager')
  const scoped = tracked !== null && sameScope(tracked.scope, source)
  const commandId = tracked?.commandId
  const posting = tracked?.phase === 'SUBMITTING'
  const finished = tracked !== null && (terminal(tracked.result) || tracked.phase === 'REJECTED')
  const endRun = tracked?.request?.command.kind === 'END_RUN'
  const state = values['Line.State']
  // CX-35: speed may move within ±20 % of the LOAD baseline (and 1–60 m/min) while running or paused.
  const activeExecution = typeof values['Line.ActiveExecutionId'] === 'string' && values['Line.ActiveExecutionId'] ? values['Line.ActiveExecutionId'] as string : null
  const [baseline, setBaseline] = useState<{ execution: string; temperature: number; speed: number; recipe: string } | null>(null)
  useEffect(() => {
    if (!activeExecution || !producer) return
    const controller = new AbortController()
    const query = new URLSearchParams({ execution_id: activeExecution, line_id: source.line_id, source_id: source.source_id, stream_id: source.stream_id })
    void commandHTTP(`/api/v1/execution-record?${query}`, controller.signal).then(value => {
      const execution = (value as { execution?: { recipe_version?: unknown; parameters?: { temperature_c?: unknown; speed_m_per_min?: unknown } } }).execution
      const temperature = execution?.parameters?.temperature_c, speed = execution?.parameters?.speed_m_per_min
      if (typeof temperature === 'number' && typeof speed === 'number' && typeof execution?.recipe_version === 'string') setBaseline({ execution: activeExecution, temperature, speed, recipe: execution.recipe_version })
    }).catch(() => {})
    return () => controller.abort()
  }, [activeExecution, producer, source.line_id, source.source_id, source.stream_id])
  const speedBase = baseline && baseline.execution === activeExecution ? baseline : null
  const speedRange = speedBase ? { min: Math.max(1, Math.ceil(speedBase.speed * 0.8 * 10) / 10), max: Math.min(60, Math.floor(speedBase.speed * 1.2 * 10) / 10) } : null
  const speedProblem = (input: string) => {
    const value = Number(input)
    return !speedRange ? '현재 실행의 기준 속도를 확인하고 있습니다.' : input === '' || !Number.isFinite(value) || value < speedRange.min || value > speedRange.max ? `${speedRange.min}~${speedRange.max} m/min 안에서 입력해 주세요.` : ''
  }

  useEffect(() => () => { postController.current?.abort() }, [])
  useEffect(() => {
    if (!commandId || !scoped || posting || (finished && lookupAttempt === 0)) return
    const controller = new AbortController()
    const deadline = Date.now() + (endRun ? 60_000 : 10_000)
    let timer: number | undefined
    async function poll() {
      setPolling(true)
      try {
        const response = await commandHTTP(commandLookupPath(commandId!, source), controller.signal)
        if (controller.signal.aborted) return
        if (typeof response !== 'object' || response === null || !('request' in response) || !('result' in response)) throw new Error('Unconfirmed command lookup')
        const result = parseResult(response.result, commandId!)
        const request = response.request as CommandRequest
        if (request?.command_id !== commandId || typeof request.requested_by !== 'string' || !request.command?.kind) throw new Error('Unconfirmed original request')
        setTracked(previous => previous && previous.commandId === commandId ? { ...previous, result, request, phase: 'TRACKING', message: '' } : previous)
        setLookupMessage('')
        if (terminal(result)) { setPolling(false); return }
      } catch (cause) {
        if (controller.signal.aborted) return
        if (cause instanceof CommandHTTPError && (cause.status === 401 || cause.status === 403)) { onSessionInvalid(); return }
        const message = cause instanceof CommandHTTPError && cause.status === 404
          ? '현재 범위에서 원래 ID의 저장 기록을 확인하지 못했습니다. 미실행으로 단정할 수 없습니다.'
          : '명령 조회 결과를 확인할 수 없습니다. 원래 ID를 유지하며 새 명령을 보내지 않습니다.'
        setTracked(previous => previous && previous.commandId === commandId ? { ...previous, phase: 'UNCERTAIN', message } : previous)
      }
      if (Date.now() >= deadline) {
        setPolling(false)
        setLookupMessage('자동 조회 시간이 끝났습니다. 실행 실패를 뜻하지 않습니다. 원래 ID로 다시 조회해 주세요.')
      } else if (!controller.signal.aborted) timer = window.setTimeout(() => void poll(), 1000)
    }
    void poll()
    return () => { controller.abort(); window.clearTimeout(timer) }
  }, [commandId, scoped, posting, finished, endRun, lookupAttempt, source, setTracked, onSessionInvalid])

  function disabledReason(kind: CommandKind): string {
    if (session.must_change_password) return '초기 비밀번호를 변경해 주세요.'
    // Speed changes need parameters_manage (production_manager, CX-35); END_RUN either role.
    if (kind === 'SET_PARAMETERS' ? !producer : kind === 'END_RUN' ? !operator && !producer : !operator) return '이 명령을 요청할 역할이 없습니다.'
    if (['START', 'RESET_FAULT'].includes(kind) && newIntentBlocked) return newIntentBlocked
    // A terminal result is final; only an open or unknown request blocks the next command.
    if (tracked && !finished) return '기존 명령의 결과를 확인한 뒤 다음 요청을 준비해 주세요.'
    if (blockedReason) return blockedReason
    if (values['Line.ControlMode'] !== 1) return '현장 모드에서는 원격 명령을 요청할 수 없습니다.'
    if (!identifier(values['Line.ActiveExecutionId']) || !identifier(values['Line.CounterEpoch'])) return '현재 실행 ID와 카운터 구간을 확인할 수 없습니다.'
    if (!uint64String(values['Line.ControlVersion'])) return '현재 제어 버전을 정확히 확인할 수 없습니다.'
    if (kind === 'START' && (![1, 3].includes(Number(state)) || values['Line.OperationMode'] !== 1
      || values['Line.EStop'] !== false || values['Line.GuardClosed'] !== true || values['Line.FaultCode'] !== 0
      || values['Line.Permissive'] !== true || values['Sealer.Ready'] !== true)) return '시작에는 준비·일시정지 상태와 자동 모드, 안전 입력 및 준비 완료가 필요합니다.'
    if (kind === 'PAUSE' && state !== 2) return '일시 정지는 운전 중인 현재 실행에만 요청할 수 있습니다.'
    if (kind === 'SET_PARAMETERS' && ![2, 3].includes(Number(state))) return '속도는 운전 중이거나 일시 정지한 현재 실행에서만 바꿀 수 있습니다.'
    if (kind === 'END_RUN' && ![0, 1, 2, 3, 5, 6].includes(Number(state))) return '현재 설비 상태에서는 작업 종료를 요청할 수 없습니다.'
    if (kind === 'END_RUN' && [5, 6].includes(Number(state))
      && ['Infeed', 'Sealer', 'Outfeed'].some(group => values[`${group}.RunFeedback`] !== false || values[`${group}.ActualSpeed`] !== 0)) return '고장·비상정지 중 종료는 모든 구동 정지를 확인해야 요청할 수 있습니다.'
    if (kind === 'END_RUN' && ![5, 6].includes(Number(state)) && Number(values['Line.WipCount']) > 0
      && values['Line.Permissive'] !== true) return '재공 배출을 위한 운전 허가를 확인해야 작업 종료를 요청할 수 있습니다.'
    if (kind === 'RESET_FAULT' && (state !== 5 || values['Line.EStop'] !== false || values['Line.GuardClosed'] !== true
      || values['Sealer.GuardClosed'] !== true || ['Infeed', 'Sealer', 'Outfeed'].some(group => values[`${group}.RunFeedback`] !== false || values[`${group}.ActualSpeed`] !== 0))) return '고장 상태에서 비상정지 해제·안전문 닫힘·모든 구동 정지를 확인해야 합니다.'
    return ''
  }

  const endConfirmationReason = disabledReason('END_RUN') || (confirmEnd && (confirmEnd.execution !== values['Line.ActiveExecutionId']
    || confirmEnd.version !== values['Line.ControlVersion'] || confirmEnd.epoch !== values['Line.CounterEpoch']) ? '확인 중 실행 또는 제어 버전이 바뀌었습니다. 취소 후 다시 확인해 주세요.' : '')

  async function submit(kind: CommandKind, args: Record<string, unknown> = {}) {
    if (submitting.current || disabledReason(kind) || (kind === 'END_RUN' && (!confirmEnd || endConfirmationReason))) return
    submitting.current = true
    setConfirmEnd(null)
    setLookupMessage('')
    const request = newCommandRequest(kind, values['Line.ActiveExecutionId'] as string, values['Line.ControlVersion'] as string, session.login, args)
    const intent: TrackedCommand = {
      commandId: request.command_id, scope: { line_id: source.line_id, source_id: source.source_id, stream_id: source.stream_id },
      request, counterEpoch: values['Line.CounterEpoch'] as string, result: null, phase: 'SUBMITTING', message: '',
    }
    setTracked(intent)
    const controller = new AbortController()
    postController.current = controller
    try {
      const value = await commandHTTP('/api/v1/commands', controller.signal, { ...intent.scope, request }, session.csrf_token)
      const result = parseResult(value, request.command_id)
      setTracked(previous => previous && previous.commandId === request.command_id ? { ...previous, result, phase: 'TRACKING' } : previous)
    } catch (cause) {
      // An interrupted POST can have committed. Keep its original ID even after navigation.
      // 401/403 are decided before anything is stored: close this request so it never blocks the next one.
      if (cause instanceof CommandHTTPError && (cause.status === 401 || cause.status === 403)) {
        setTracked(previous => previous && previous.commandId === request.command_id ? {
          ...previous, phase: 'REJECTED', message: cause.status === 401 ? '로그인이 만료되어 요청이 거절됐습니다. 서버에 저장되지 않았습니다.' : '권한이 없거나 보안 확인에 실패해 요청이 거절됐습니다. 서버에 저장되지 않았습니다.',
        } : previous)
        if (!controller.signal.aborted) onSessionInvalid()
        return
      }
      const knownRejection = cause instanceof CommandHTTPError && [400, 404, 409, 413, 415, 422, 429].includes(cause.status)
      setTracked(previous => previous && previous.commandId === request.command_id ? {
        ...previous, phase: knownRejection ? 'REJECTED' : 'UNCERTAIN',
        message: knownRejection ? conflictMessage(cause.reasonCode) ?? 'API가 이 요청을 거절했습니다. 최신 조건을 확인해 주세요. PLC 실행 성공이 아닙니다.'
          : '요청 응답 불명 · 서버에 저장되었을 수 있습니다. 원래 ID만 조회하며 새 ID로 다시 보내지 않습니다.',
      } : previous)
    } finally { submitting.current = false }
  }

  function askEnd() {
    setConfirmEnd({ execution: String(values['Line.ActiveExecutionId']), version: String(values['Line.ControlVersion']), epoch: String(values['Line.CounterEpoch']) })
  }
  function setSpeed(input: string) {
    if (!speedProblem(input) && !disabledReason('SET_PARAMETERS') && speedBase) void submit('SET_PARAMETERS', { recipe_version: speedBase.recipe, parameters: { temperature_c: speedBase.temperature, speed_m_per_min: Number(input) } })
  }
  function lookupAgain() {
    if (!tracked || !scoped || posting) return
    setLookupMessage('')
    setLookupAttempt(value => value + 1)
  }
  function lookupId(id: string) {
    if (tracked || !identifier(id)) return
    setTracked({ commandId: id, scope: source, request: null, counterEpoch: null, result: null, phase: 'TRACKING', message: '' })
    setLookupMessage('')
  }
  function acknowledge() { setTracked(null); setLookupMessage(''); setPolling(false) }

  const kinds: CommandKind[] = operator ? ['START', 'PAUSE', 'END_RUN', 'RESET_FAULT'] : producer ? ['END_RUN'] : []
  const result = tracked?.result
  const feedbackMatches = !blockedReason && result?.evidence?.state !== undefined
    && result.evidence.state === stateNames[String(state)]
    && result.evidence.execution_id === values['Line.ActiveExecutionId']
  return {
    operator, producer, kinds, disabledReason, submit, askEnd, confirmEnd, cancelEnd: () => setConfirmEnd(null), endConfirmationReason,
    speedBase, speedRange, speedProblem, setSpeed, canSetSpeed: producer && [2, 3].includes(Number(state)),
    tracked, scoped, posting, finished, polling, lookupMessage, lookupAgain, lookupId, acknowledge, result, feedbackMatches,
    controlVersion: uint64String(values['Line.ControlVersion']) ? String(values['Line.ControlVersion']) : undefined,
  }
}
export type LineCommands = ReturnType<typeof useLineCommands>
