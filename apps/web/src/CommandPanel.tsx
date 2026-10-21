import { useEffect, useRef, useState } from 'react'
import type { Dispatch, SetStateAction } from 'react'
import type { Session } from './auth'
import { CommandHTTPError, commandHTTP, commandLookupPath, commandLabels, newCommandRequest, parseResult, sameScope, statusLabels, terminal, uint64String } from './commands'
import type { CommandKind, CommandRequest, CommandScope, TrackedCommand } from './commands'

interface Props {
  source: CommandScope; session: Session; blockedReason: string; values: Record<string, unknown>
  newIntentBlocked?: string
  tracked: TrackedCommand | null; setTracked: Dispatch<SetStateAction<TrackedCommand | null>>
  onSessionInvalid: () => void
}
const identifier = (value: unknown): value is string => typeof value === 'string' && value.length > 0 && value.length <= 128 && !/\s/.test(value)

export default function CommandPanel({ source, session, blockedReason, newIntentBlocked = '', values, tracked, setTracked, onSessionInvalid }: Props) {
  const [confirmEnd, setConfirmEnd] = useState<{ execution: string; version: string; epoch: string } | null>(null)
  const [lookupId, setLookupId] = useState('')
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
    if ((kind === 'END_RUN' ? !operator && !producer : !operator)) return '이 명령을 요청할 역할이 없습니다.'
    if (['START', 'RESET_FAULT'].includes(kind) && newIntentBlocked) return newIntentBlocked
    if (tracked) return '기존 명령의 결과를 확인한 뒤 다음 요청을 준비해 주세요.'
    if (blockedReason) return blockedReason
    if (values['Line.ControlMode'] !== 1) return '현장 모드에서는 원격 명령을 요청할 수 없습니다.'
    if (!identifier(values['Line.ActiveExecutionId']) || !identifier(values['Line.CounterEpoch'])) return '현재 실행 ID와 카운터 구간을 확인할 수 없습니다.'
    if (!uint64String(values['Line.ControlVersion'])) return '현재 제어 버전을 정확히 확인할 수 없습니다.'
    if (kind === 'START' && (![1, 3].includes(Number(state)) || values['Line.OperationMode'] !== 1
      || values['Line.EStop'] !== false || values['Line.GuardClosed'] !== true || values['Line.FaultCode'] !== 0
      || values['Line.Permissive'] !== true || values['Sealer.Ready'] !== true)) return '시작에는 준비·일시정지 상태와 자동 모드, 안전 입력 및 준비 완료가 필요합니다.'
    if (kind === 'PAUSE' && state !== 2) return '일시 정지는 운전 중인 현재 실행에만 요청할 수 있습니다.'
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

  async function submit(kind: CommandKind) {
    if (submitting.current || disabledReason(kind) || (kind === 'END_RUN' && (!confirmEnd || endConfirmationReason))) return
    submitting.current = true
    setConfirmEnd(null)
    setLookupMessage('')
    const request = newCommandRequest(kind, values['Line.ActiveExecutionId'] as string, values['Line.ControlVersion'] as string, session.login)
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
      if (cause instanceof CommandHTTPError && (cause.status === 401 || cause.status === 403)) {
        if (!controller.signal.aborted) onSessionInvalid()
        return
      }
      const knownRejection = cause instanceof CommandHTTPError && [400, 404, 409, 413, 415, 422, 429].includes(cause.status)
      setTracked(previous => previous && previous.commandId === request.command_id ? {
        ...previous, phase: knownRejection ? 'REJECTED' : 'UNCERTAIN',
        message: knownRejection ? 'API가 이 요청을 거절했습니다. 최신 조건을 확인해 주세요. PLC 실행 성공이 아닙니다.'
          : '요청 응답 불명 · 서버에 저장되었을 수 있습니다. 원래 ID만 조회하며 새 ID로 다시 보내지 않습니다.',
      } : previous)
    } finally { submitting.current = false }
  }

  function lookupOriginal() {
    if (tracked) {
      if (!scoped || posting) return
      setLookupMessage('')
      setLookupAttempt(value => value + 1)
    } else if (identifier(lookupId)) {
      setTracked({ commandId: lookupId, scope: source, request: null, counterEpoch: null, result: null, phase: 'TRACKING', message: '' })
      setLookupMessage('')
    }
  }
  const kinds: CommandKind[] = operator ? ['START', 'PAUSE', 'END_RUN', 'RESET_FAULT'] : producer ? ['END_RUN'] : []
  const result = tracked?.result
  const feedbackMatches = !blockedReason && result?.evidence?.state !== undefined
    && result.evidence.state === ({ 0: 'STOPPED', 1: 'READY', 2: 'RUNNING', 3: 'PAUSED', 4: 'STOPPING', 5: 'FAULT', 6: 'ESTOP', 7: 'RECOVERY_REQUIRED' } as Record<string, string>)[String(state)]
    && result.evidence.execution_id === values['Line.ActiveExecutionId']
  return <section id="command-panel" className="command-panel" aria-labelledby="command-heading">
    <div className="monitor-heading"><div><p className="section-kicker">COMMAND REQUEST</p><h2 id="command-heading">현재 실행 제어 요청</h2></div><span className="read-only-tag">서버 · PLC 재검증</span></div>
    <p className="form-help">요청 저장과 PLC 접수는 실행 완료가 아닙니다. 현재 실행에 대한 요청만 제공합니다. 작업지시 화면에서 배포 후 실행 연결을 준비할 수 있습니다.</p>
    {kinds.length ? <div className="command-actions">{kinds.map(kind => {
      const reason = disabledReason(kind)
      return <div key={kind}><button id={`command-${kind}`} type="button" className={`button ${kind === 'START' ? 'primary' : kind === 'END_RUN' ? 'danger' : 'secondary'}`} disabled={!!reason} aria-describedby={`command-${kind}-reason`} onClick={() => kind === 'END_RUN' ? setConfirmEnd({ execution: String(values['Line.ActiveExecutionId']), version: String(values['Line.ControlVersion']), epoch: String(values['Line.CounterEpoch']) }) : void submit(kind)}>{commandLabels[kind]}</button><p id={`command-${kind}-reason`} className="command-reason">{reason || '요청 시 서버와 PLC가 조건을 다시 확인합니다.'}</p></div>
    })}</div> : <p className="message caution">현재 역할은 운전 요청 권한이 없습니다. 명령 결과 조회는 가능합니다.</p>}
    {confirmEnd && <div className="end-confirm" role="group" aria-labelledby="end-confirm-heading">
      <h3 id="end-confirm-heading">현재 작업의 종료를 요청하시겠습니까?</h3><p>실행 {confirmEnd.execution}의 신규 투입을 중지하고 재공 정리 후 종료하도록 요청합니다. 종료가 확정되면 이 실행을 다시 시작할 수 없습니다. MES 실적 정산 완료와는 구분됩니다.</p>
      {endConfirmationReason && <p className="message caution">{endConfirmationReason}</p>}<div><button id="command-END_RUN-confirm" className="button danger" type="button" disabled={!!endConfirmationReason} onClick={() => void submit('END_RUN')}>현재 실행 종료 요청</button><button className="button secondary" type="button" onClick={() => setConfirmEnd(null)}>취소</button></div>
    </div>}
    {tracked && <div id="command-tracker" className="command-tracker">
      <p className="section-kicker">ORIGINAL COMMAND</p><h3 id="command-status" data-status={tracked.phase === 'REJECTED' ? 'HTTP_REJECTED' : tracked.phase === 'SUBMITTING' ? 'SUBMITTING' : result?.status ?? 'UNKNOWN'}>{tracked.phase === 'REJECTED' ? 'API 요청 거절' : tracked.phase === 'SUBMITTING' ? '요청 응답 확인 중' : result ? statusLabels[result.status] : '요청 결과 불명'}</h3>
      <dl className="command-facts"><div><dt>원래 명령 ID</dt><dd id="original-command-id">{tracked.commandId}</dd></div><div><dt>요청 종류 · 실행</dt><dd>{tracked.request ? `${commandLabels[tracked.request.command.kind] ?? tracked.request.command.kind} · ${tracked.request.execution_id ?? '없음'}` : '원래 요청 조회 중'}</dd></div><div><dt>수집원</dt><dd>{tracked.scope.source_id} / {tracked.scope.stream_id}</dd></div>{tracked.request && <div><dt>요청 당시 버전 · 구간</dt><dd>{tracked.request.expected_control_version} · {tracked.counterEpoch ?? '별도 조회 필요'}</dd></div>}</dl>
      {tracked.message && <p className="message caution" role="status">{tracked.message}</p>}
      {!scoped && <p className="message caution">다른 수집원의 명령을 보관하고 있습니다. 원래 라인·수집원을 선택하면 같은 ID의 추적을 이어갈 수 있습니다.</p>}
      {result && <div className="command-result"><p>서버 기록: {result.status} · {result.updated_at}</p>{result.reason_code && <p>사유 코드: {result.reason_code}</p>}{result.reason && <p>{result.reason}</p>}{result.status === 'SUCCEEDED' && <><p>실행 완료 근거: {result.evidence?.state} · 제어 버전 {result.applied_control_version}</p><p>{feedbackMatches ? '현재 정상 관측과 결과 상태·실행 ID가 일치합니다.' : '현재 정상 관측과 실행 결과의 대조를 확인할 수 없습니다.'}</p>{tracked.request?.command.kind === 'END_RUN' && <p>설비 종료 기록입니다. MES 실적 정산 완료는 아직 확인하지 않았습니다.</p>}</>}</div>}
      {lookupMessage && <p className="message caution" role="status">{lookupMessage}</p>}
      <div className="tracker-actions"><button id="command-lookup-original" className="button secondary" type="button" disabled={!scoped || posting || (polling && !finished)} onClick={lookupOriginal}>{polling && !finished ? '원래 ID 조회 중…' : '원래 ID 결과 다시 조회'}</button>{finished && <button id="command-acknowledge" className="button secondary" type="button" onClick={() => { setTracked(null); setLookupMessage(''); setPolling(false) }}>확정 결과 확인 · 다음 요청 준비</button>}</div>
    </div>}
    {!tracked && <form className="command-lookup-form" onSubmit={event => { event.preventDefault(); lookupOriginal() }}><label htmlFor="command-lookup-id">기존 명령 ID로 결과 조회</label><div><input id="command-lookup-id" required maxLength={128} value={lookupId} onChange={event => setLookupId(event.target.value)} /><button id="command-lookup-submit" className="button secondary" type="submit">결과 조회</button></div></form>}
    <p className="form-help">결과 불명일 때 새 명령을 생성하거나 자동 재전송하지 않습니다. 원래 명령 ID를 기록해 주세요. 새로고침·로그아웃 후에는 재로그인하고 이 ID로 다시 조회할 수 있습니다.</p>
  </section>
}
