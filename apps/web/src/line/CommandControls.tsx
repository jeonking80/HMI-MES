import { useState } from 'react'
import type { ReactNode } from 'react'
import { commandLabels, statusLabels } from '../commands'
import type { CommandKind } from '../commands'
import { stepGuide } from '../commandGuide'
import type { LineCommands, Setpoint } from './useLineCommands'

const icons: Record<CommandKind, ReactNode> = {
  START: <path d="M7 4L20 12L7 20Z" fill="currentColor" />,
  PAUSE: <><rect x="6" y="4" width="4" height="16" fill="currentColor" /><rect x="14" y="4" width="4" height="16" fill="currentColor" /></>,
  END_RUN: <rect x="5" y="5" width="14" height="14" rx="1.5" fill="currentColor" />,
  RESET_FAULT: <path d="M5 12a7 7 0 1 0 2-5M5 4v4h4" fill="none" stroke="currentColor" strokeWidth="2.2" />,
  SET_PARAMETERS: <path d="M4 12h16" stroke="currentColor" strokeWidth="2" />,
}
const shortLabels: Partial<Record<CommandKind, string>> = { START: '시작 · 재개', PAUSE: '일시 정지', END_RUN: '작업 종료', RESET_FAULT: '고장 리셋' }

/** The line commands as large buttons. The one the PLC state calls for is emphasised; the rest stay visible with the reason they are unavailable. */
export function CommandButtons({ cmd, values, compact = false, only }: { cmd: LineCommands; values: Record<string, unknown>; compact?: boolean; only?: CommandKind[] }) {
  const kinds = only ? cmd.kinds.filter(kind => only.includes(kind)) : cmd.kinds
  if (!kinds.length) return <p className="lc-note">{cmd.kinds.length ? '지금 이 단계에서 보낼 명령이 없습니다.' : '이 계정은 운전 명령 권한이 없습니다. 결과 조회만 할 수 있습니다.'}</p>
  const recommended = stepGuide(values).recommended
  const reasons = kinds.map(kind => cmd.disabledReason(kind))
  const recommendedReason = recommended && kinds.includes(recommended) ? reasons[kinds.indexOf(recommended)] : ''
  const allBlocked = reasons.every(Boolean)
  return <div className="lc-commands">
    <div className={`lc-command-grid${compact ? ' compact' : ''}`} data-control-version={cmd.controlVersion}>
      {kinds.map((kind, index) => <button key={kind} id={`command-${kind}`} type="button" title={reasons[index] || undefined}
        className={`lc-command kind-${kind}${recommended === kind && !reasons[index] ? ' recommended' : ''}`} disabled={!!reasons[index]}
        onClick={() => kind === 'END_RUN' ? cmd.askEnd() : void cmd.submit(kind)}>
        <svg width="22" height="22" viewBox="0 0 24 24" aria-hidden="true">{icons[kind]}</svg>{shortLabels[kind] ?? commandLabels[kind]}
      </button>)}
    </div>
    {/* Explain a blocked button only when it is the one to press now, or when nothing can be pressed. */}
    {(allBlocked || recommendedReason) && <p className="lc-reason" role="status">{allBlocked ? reasons[0] : recommendedReason}</p>}
    {cmd.confirmEnd && <div className="lc-confirm" role="group" aria-labelledby="end-confirm-heading">
      <strong id="end-confirm-heading">현재 작업을 종료할까요?</strong>
      <span>신규 투입을 멈추고 재공을 정리한 뒤 끝냅니다. 종료되면 이 실행은 다시 시작할 수 없습니다.</span>
      {cmd.endConfirmationReason && <span className="lc-reason">{cmd.endConfirmationReason}</span>}
      <div><button id="command-END_RUN-confirm" type="button" className="lc-button danger" disabled={!!cmd.endConfirmationReason} onClick={() => void cmd.submit('END_RUN')}>종료 요청</button><button type="button" className="lc-button" onClick={cmd.cancelEnd}>취소</button></div>
    </div>}
  </div>
}

const setpointRows: { kind: Setpoint; label: string; scope: string; unit: string; step: number; tag: string }[] = [
  { kind: 'speed', label: '라인 속도', scope: '투입 · 실링 · 배출 공통', unit: 'm/min', step: 0.5, tag: 'Sealer.ActualSpeed' },
  { kind: 'temperature', label: '실링 온도', scope: '02 실링기 설정', unit: '℃', step: 1, tag: 'Sealer.TemperatureSP' },
  { kind: 'pressure', label: '실링 공압', scope: '02 실링기 설정', unit: 'bar', step: 0.1, tag: 'Sealer.AirPressure' },
]

const runningSpeed = new Map<string, number>()

/** Running setpoints near the LOAD recipe, sent together as one SET_PARAMETERS; the PLC judges them. */
export function SetpointControl({ cmd, known, only }: { cmd: LineCommands; known: (tag: string) => unknown; only?: Setpoint[] }) {
  const [inputs, setInputs] = useState<Partial<Record<Setpoint, string>>>({})
  if (!cmd.canSetSpeed) return null
  const base = cmd.setpointBase
  const current = (kind: Setpoint, tag: string): number | null => {
    const value = known(tag)
    if (typeof value === 'number' && Number.isFinite(value) && (kind !== 'speed' || value > 0)) {
      const rounded = Math.round(value * 10) / 10
      if (kind === 'speed' && base) runningSpeed.set(base.execution, rounded)
      return rounded
    }
    // Paused: the belt reads 0 m/min, so keep the speed it last ran at for this execution.
    if (kind === 'speed' && base && runningSpeed.has(base.execution)) return runningSpeed.get(base.execution)!
    return base ? (kind === 'speed' ? base.speed : kind === 'temperature' ? base.temperature : base.pressure) : null
  }
  const rows = setpointRows.filter(row => (!only || only.includes(row.kind)) && !(row.kind === 'pressure' && base && base.pressure === null))
  const values = Object.fromEntries(setpointRows.map(row => {
    const now = current(row.kind, row.tag)
    return [row.kind, inputs[row.kind] ?? (now === null ? '' : String(now))]
  })) as Record<Setpoint, string>
  const changed = rows.filter(row => values[row.kind] !== '' && Number(values[row.kind]) !== current(row.kind, row.tag))
  const problems = rows.map(row => changed.includes(row) ? cmd.setpointProblem(row.kind, values[row.kind]) : '').filter(Boolean)
  const blocked = cmd.disabledReason('SET_PARAMETERS')
  const step = (row: (typeof setpointRows)[number], delta: number) => {
    const range = cmd.ranges[row.kind]
    const next = Math.round((Number(values[row.kind] || 0) + delta) * 10) / 10
    setInputs(previous => ({ ...previous, [row.kind]: String(range ? Math.min(range.max, Math.max(range.min, next)) : next) }))
  }
  return <form id="setpoint-adjust" className="lc-speed" onSubmit={event => {
    event.preventDefault()
    cmd.setSetpoints({ speed: values.speed, temperature: values.temperature, pressure: base?.pressure === null ? null : values.pressure })
    setInputs({})
  }}>
    {rows.map(row => {
      const range = cmd.ranges[row.kind], now = current(row.kind, row.tag)
      return <div key={row.kind} className="lc-setpoint">
        <label htmlFor={`setpoint-${row.kind}`}>{row.label}<small>{row.scope} · 현재 {now ?? '—'} {row.unit}{range ? ` · 허용 ${range.min}~${range.max}` : ''}</small></label>
        <div className="lc-stepper">
          <button type="button" aria-label={`${row.label} ${row.step} 낮추기`} onClick={() => step(row, -row.step)}>−</button>
          <input id={`setpoint-${row.kind}`} type="number" step="0.1" inputMode="decimal" min={range?.min} max={range?.max} value={values[row.kind]} onChange={event => setInputs(previous => ({ ...previous, [row.kind]: event.target.value }))} />
          <span>{row.unit}</span>
          <button type="button" aria-label={`${row.label} ${row.step} 높이기`} onClick={() => step(row, row.step)}>+</button>
        </div>
      </div>
    })}
    <button id="command-SET_PARAMETERS" type="submit" className="lc-button primary" disabled={!!blocked || problems.length > 0 || changed.length === 0}>{changed.length ? `${changed.map(row => `${row.label} ${values[row.kind]}${row.unit}`).join(' · ')} 적용` : '변경 적용'}</button>
    <p className="lc-hint">{blocked || problems[0] || (base ? `작업 기준 ${base.speed} m/min · ${base.temperature} ℃${base.pressure === null ? '' : ` · ${base.pressure} bar`} · 실행 여부는 PLC가 판정합니다` : '현재 실행의 기준 설정값을 확인하고 있습니다.')}</p>
  </form>
}

const steps = ['요청 저장', 'PLC 접수', '실행 완료']
function progress(cmd: LineCommands): { reached: number; tone: 'active' | 'done' | 'bad' | 'unknown'; label: string } {
  const tracked = cmd.tracked!
  if (tracked.phase === 'REJECTED') return { reached: 0, tone: 'bad', label: '서버가 요청을 거절했습니다' }
  if (tracked.phase === 'SUBMITTING') return { reached: 0, tone: 'active', label: '요청 보내는 중' }
  const status = cmd.result?.status
  if (!status) return { reached: 0, tone: 'unknown', label: '요청 결과 불명' }
  if (status === 'SUCCEEDED') return { reached: 3, tone: 'done', label: statusLabels[status] }
  if (status === 'ACCEPTED') return { reached: 2, tone: 'active', label: statusLabels[status] }
  if (status === 'REQUESTED' || status === 'DELIVERED') return { reached: 1, tone: 'active', label: statusLabels[status] }
  if (status === 'OUTCOME_UNKNOWN') return { reached: 2, tone: 'unknown', label: statusLabels[status] }
  return { reached: status === 'EXPIRED' ? 1 : 2, tone: 'bad', label: statusLabels[status] }
}

/** The original command's progress, beside the controls that sent it. */
export function CommandResult({ cmd }: { cmd: LineCommands }) {
  const tracked = cmd.tracked
  if (!tracked) return null
  const view = progress(cmd)
  const kind = tracked.request?.command.kind
  return <div id="command-tracker" className="lc-result" data-tone={view.tone}>
    <div className="lc-result-head">
      <strong id="command-status" data-status={tracked.phase === 'REJECTED' ? 'HTTP_REJECTED' : tracked.phase === 'SUBMITTING' ? 'SUBMITTING' : cmd.result?.status ?? 'UNKNOWN'}>{view.label}</strong>
      <span>{kind ? commandLabels[kind] ?? kind : '원래 요청 조회 중'}</span>
    </div>
    <ol className="lc-steps">{steps.map((label, index) => <li key={label} data-state={index < view.reached ? (view.tone === 'bad' && index === view.reached - 1 ? 'bad' : 'done') : index === view.reached && view.tone !== 'done' ? view.tone : 'todo'}>{label}</li>)}</ol>
    {tracked.message && <p className="lc-reason" role="status">{tracked.message}</p>}
    {cmd.result?.reason && <p className="lc-reason">{cmd.result.reason}</p>}
    {cmd.result?.status === 'SUCCEEDED' && <p className="lc-hint">{cmd.feedbackMatches ? '현재 설비 상태와 결과가 일치합니다.' : '현재 설비 상태와 결과의 대조를 아직 확인하지 못했습니다.'}{kind === 'END_RUN' ? ' 실적 정산 완료는 실적 화면에서 확인합니다.' : ''}</p>}
    {cmd.lookupMessage && <p className="lc-reason" role="status">{cmd.lookupMessage}</p>}
    {!cmd.scoped && <p className="lc-reason">다른 수집원의 명령입니다. 원래 라인을 선택하면 이어서 조회합니다.</p>}
    <div className="lc-result-foot">
      <span className="lc-id">ID <code id="original-command-id">{tracked.commandId}</code></span>
      <button id="command-lookup-original" type="button" className="lc-link" disabled={!cmd.scoped || cmd.posting || (cmd.polling && !cmd.finished)} onClick={cmd.lookupAgain}>{cmd.polling && !cmd.finished ? '조회 중…' : '다시 조회'}</button>
      {cmd.finished && <button id="command-acknowledge" type="button" className="lc-button" onClick={cmd.acknowledge}>확인 · 다음 요청</button>}
    </div>
  </div>
}

/** Look up an earlier command by its ID (after a reload or from another screen). */
export function CommandLookup({ cmd }: { cmd: LineCommands }) {
  const [id, setId] = useState('')
  if (cmd.tracked) return null
  return <form className="lc-lookup" onSubmit={event => { event.preventDefault(); cmd.lookupId(id.trim()) }}>
    <label htmlFor="command-lookup-id">이전 명령 ID로 결과 조회</label>
    <div><input id="command-lookup-id" required maxLength={128} value={id} onChange={event => setId(event.target.value)} /><button id="command-lookup-submit" type="submit" className="lc-button">조회</button></div>
  </form>
}
