import { useCallback, useEffect, useState } from 'react'
import type { Dispatch, ReactNode, SetStateAction } from 'react'
import type { Session } from '../auth'
import { terminal } from '../commands'
import type { TrackedCommand } from '../commands'
import type { WorkOrder } from '../mesApi'
import { readOverview } from '../orders/orderApi'
import { screenUInt64 } from '../screenState'
import type { WorkOrderWorkflow } from '../useWorkOrderWorkflow'
import { workflowId } from '../workflow'
import type { BindBody } from '../workflow'
import { useLine } from './lineState'
import type { LiveLine, Source } from './lineState'
import { interlocks, stateOf, zones } from './lineModel'
import type { ZoneId } from './lineModel'
import { LampChip, MetricBox, SourcePicker, StateBadge } from './parts'
import { newIntentBlocked, powerLabels, production, toneOf } from './logic'
import { useLineCommands } from './useLineCommands'
import type { LineCommands } from './useLineCommands'
import { CommandButtons, CommandLookup, CommandResult, SpeedControl } from './CommandControls'
import './line.css'

/* The serverless API serves few requests at once; the 1 s screen poll must not queue behind a burst
   of side reads, or the line data turns stale. Periods are spread so their beats rarely meet. */
const POLL_POWER_MS = 9000
const POLL_RUN_MS = 3000
const POLL_ORDERS_MS = 13000

interface Props {
  session: Session; onSessionInvalid: () => void; workflow: WorkOrderWorkflow
  tracked: TrackedCommand | null; setTracked: Dispatch<SetStateAction<TrackedCommand | null>>
  zone: ZoneId | 'Line'; onZone: (zone: ZoneId | 'Line') => void
  selectedOrder: WorkOrder | null; onSelectOrder: (order: WorkOrder | null) => void
  onOpenMonitor: () => void; onOpenOrders: () => void; onOpenReports: () => void
}

type Step = 1 | 2 | 3 | 4 | 5
type StepState = 'done' | 'current' | 'todo' | 'bad' | 'unknown'
interface Power { desired: 'ON' | 'OFF'; run_until: string | null; agent_online: boolean; reported_state: string; reported_detail: string | null }
/** What the server stored for the active execution: its order, load policy and settlement. */
interface ActiveRun {
  executionId: string; workOrderId: string; registeredAt: string; target: number | null
  autoEnd: boolean | null; startWhenReady: boolean | null; loadedBy: string | null
  settlement: 'PENDING' | 'SETTLED' | 'MISMATCH' | 'CONFLICT'
  summary: { good: string; reject: string; endReason: string | null; endedAt: string | null } | null
}

const powerText: Record<string, string> = { OFF: '꺼짐', STARTING: '켜는 중', RUNNING: '켜짐', STOPPING: '끄는 중', ERROR: '오류', UNKNOWN: '호스트 연결 안 됨', NOT_MANAGED: '상시 켜짐' }
const time = (value: string | null | undefined) => value ? new Date(value).toLocaleTimeString('ko-KR', { hour: '2-digit', minute: '2-digit' }) : '—'

function usePowerState(onSessionInvalid: () => void) {
  const [power, setPower] = useState<Power | null>(null)
  const [seenAt, setSeenAt] = useState(0)
  const [managed, setManaged] = useState(true)
  const [error, setError] = useState(false)
  const load = useCallback(async (signal?: AbortSignal) => {
    const response = await fetch('/api/v1/runtime-control', { credentials: 'same-origin', cache: 'no-store', signal })
    if (response.status === 401 || response.status === 403) { onSessionInvalid(); return }
    if (response.status === 404) { setManaged(false); return }
    if (!response.ok) throw new Error('runtime control unavailable')
    setPower(await response.json() as Power); setSeenAt(Date.now()); setError(false)
  }, [onSessionInvalid])
  const state = !managed ? 'NOT_MANAGED' : error ? 'UNKNOWN' : power ? (power.agent_online ? power.reported_state : 'UNKNOWN') : null
  // A request the host has not finished yet: ON not yet RUNNING, or OFF not yet OFF.
  // A start that fails is retried by the host, so an early ERROR is still "turning on".
  const wantOn = power?.desired === 'ON'
  const kind: 'starting' | 'stopping' | null = !power || !managed || state === 'UNKNOWN' ? null
    : wantOn && ['OFF', 'STARTING', 'STOPPING', 'ERROR'].includes(state ?? '') ? 'starting'
      : !wantOn && ['RUNNING', 'STARTING', 'STOPPING'].includes(state ?? '') ? 'stopping' : null
  const [transition, setTransition] = useState<{ kind: 'starting' | 'stopping'; at: number } | null>(null)
  if ((transition?.kind ?? null) !== kind) setTransition(kind ? { kind, at: seenAt } : null)
  const [now, setNow] = useState(seenAt)
  useEffect(() => {
    const controller = new AbortController()
    const tick = () => { load(controller.signal).catch(() => { if (!controller.signal.aborted) setError(true) }) }
    tick()
    // Side reads stay off the 1 s screen poll's beat: few, slow and staggered (see POLL_* below);
    // while the host is switching, follow it closely so the screen shows when it is done.
    const timer = window.setInterval(tick, kind ? 2000 : POLL_POWER_MS)
    const clock = kind ? window.setInterval(() => setNow(Date.now()), 1000) : undefined
    return () => { controller.abort(); window.clearInterval(timer); if (clock) window.clearInterval(clock) }
  }, [load, kind])
  const elapsed = transition ? Math.max(0, Math.round((Math.max(now, seenAt) - transition.at) / 1000)) : 0
  // Past the host's own retries an error is real, not a slow start.
  const failed = kind === 'starting' && state === 'ERROR' && elapsed > 150
  const accept = useCallback((value: Power) => { setPower(value); setSeenAt(Date.now()) }, [])
  return { power, state, managed, reload: load, setPower: accept, switching: failed ? null : kind, elapsed }
}

interface LineStatus { run: ActiveRun | null; unresolved: { command_id: string; kind: string; status: string } | null }

/** The server's single answer for the cycle: the job loaded last, its settlement and any open command. */
function useLineStatus(source: Source, onSessionInvalid: () => void): LineStatus | undefined {
  const [status, setStatus] = useState<{ key: string; value: LineStatus } | undefined>(undefined)
  const key = `${source.line_id}|${source.source_id}|${source.stream_id}`
  useEffect(() => {
    const controller = new AbortController()
    let busy = false
    const tick = async () => {
      if (busy) return
      busy = true
      try {
        const response = await fetch(`/api/v1/line-status?${new URLSearchParams({ line_id: source.line_id, source_id: source.source_id, stream_id: source.stream_id })}`, { credentials: 'same-origin', cache: 'no-store', signal: controller.signal })
        if (response.status === 401) { onSessionInvalid(); return }
        if (!response.ok) return
        const raw = await response.json() as { execution: null | { execution_id: string; work_order_id: string; bound_at: string; bound_by: string; target_quantity: number | null; auto_end: boolean | null; start_when_ready: boolean | null; settlement: ActiveRun['settlement']; good_quantity: number | null; reject_quantity: number | null; end_reason: string | null; ended_at: string | null }; unresolved_command: LineStatus['unresolved'] }
        const e = raw.execution
        if (!controller.signal.aborted) setStatus({ key, value: { unresolved: raw.unresolved_command ?? null, run: e ? {
          executionId: e.execution_id, workOrderId: e.work_order_id, registeredAt: e.bound_at, target: e.target_quantity, autoEnd: e.auto_end, startWhenReady: e.start_when_ready, loadedBy: e.bound_by, settlement: e.settlement,
          summary: e.settlement === 'PENDING' ? null : { good: e.good_quantity === null ? '—' : String(e.good_quantity), reject: e.reject_quantity === null ? '—' : String(e.reject_quantity), endReason: e.end_reason, endedAt: e.ended_at },
        } : null } })
      } catch { /* the next poll retries; the step stays unknown meanwhile */ } finally { busy = false }
    }
    void tick()
    const timer = window.setInterval(() => void tick(), POLL_RUN_MS)
    return () => { controller.abort(); window.clearInterval(timer) }
  }, [key, source.line_id, source.source_id, source.stream_id, onSessionInvalid])
  return status?.key === key ? status.value : undefined
}

/** Which step the line is in, from live tags, the host power report and the stored execution. */
function stepOf(live: LiveLine, powerState: string | null, run: ActiveRun | null | undefined): { current: Step; fault: boolean; unknown: boolean } {
  const state = Number(live.known('Line.State'))
  const fault = [5, 6, 7].includes(state)
  const unknown = live.lineState === null || powerState === null || powerState === 'UNKNOWN' || run === undefined
  if (powerState !== 'RUNNING' && powerState !== 'NOT_MANAGED') return { current: 1, fault, unknown: powerState === null || powerState === 'UNKNOWN' }
  const made = production(live.known)
  const produced = (made.good ?? 0) + (made.reject ?? 0) > 0
  if (run === null || (run && run.settlement !== 'PENDING')) return { current: 2, fault, unknown }
  if ([2, 3].includes(state)) return { current: 4, fault, unknown }
  if (state === 4 || (produced && [0, 1].includes(state))) return { current: 5, fault, unknown }
  return { current: produced ? 4 : 3, fault, unknown }
}

/** Operation control: the step bar shows where the line is; the open step lists what can be done now. */
export default function ControlPage(props: Props) {
  const line = useLine()
  const live = line.live
  return <div className="lc-page">
    <header className="lc-page-head">
      <div className="lc-title"><h2>{line.source?.line_name ?? '라인'}</h2><span>운전 제어</span></div>
      <div className="lc-chips">
        <SourcePicker />
        {live && <span className="lc-chip" data-tone={live.known('Line.ControlMode') === 1 ? 'good' : live.known('Line.ControlMode') === 0 ? 'warn' : 'unknown'}><i aria-hidden="true" />{live.known('Line.ControlMode') === 1 ? '원격 제어 가능' : live.known('Line.ControlMode') === 0 ? '현장 제어 중' : '제어 권한 불명'}</span>}
        <span className="lc-chip">{props.session.login}</span>
      </div>
    </header>
    {!line.source || !live ? <div className="lc-card lc-empty">{line.sources === null ? '라인 정보를 불러오고 있습니다.' : '라인을 선택해 주세요.'}</div>
      : <LiveControl {...props} live={live} source={line.source} />}
  </div>
}

function LiveControl({ live, source, session, onSessionInvalid, workflow, tracked, setTracked, zone, onZone, selectedOrder, onSelectOrder, onOpenMonitor, onOpenOrders, onOpenReports }: Props & { live: LiveLine; source: Source }) {
  const known = live.known
  const made = production(known)
  const power = usePowerState(onSessionInvalid)
  const lineStatus = useLineStatus(source, onSessionInvalid)
  const run = lineStatus === undefined ? undefined : lineStatus.run
  // With the plant powered off the real reason is the power, not stale tag quality.
  const powerOff = power.managed && power.state !== null && power.state !== 'RUNNING'
  const blockedReason = powerOff ? `${powerLabels[power.state ?? ''] ?? '설비 전원 상태 확인 필요'} · ① 전원에서 켜 주세요.` : live.blockedReason
  const cmd = useLineCommands({ source, session, blockedReason, newIntentBlocked: newIntentBlocked(workflow, live.controlValues), values: live.controlValues, tracked, setTracked, onSessionInvalid })
  const step = stepOf(live, power.state, run)
  // A picked step stays open only while the line is still in the step it was picked from.
  const [picked, setPicked] = useState<{ step: Step; from: Step } | null>(null)
  const open = picked && picked.from === step.current ? picked.step : null
  const setOpen = (value: Step | null) => setPicked(value === null ? null : { step: value, from: step.current })
  const shown: Step = open ?? step.current
  const stateOfStep = (n: Step): StepState => n < step.current ? 'done' : n > step.current ? (n === 5 && step.current === 2 && run && run.settlement !== 'PENDING' ? 'done' : 'todo') : step.fault ? 'bad' : step.unknown ? 'unknown' : 'current'
  const speed = known('Sealer.ActualSpeed')
  const summaries: Record<Step, [string, string]> = {
    1: [power.switching === 'starting' ? '켜는 중…' : power.switching === 'stopping' ? '끄는 중…' : powerText[power.state ?? 'UNKNOWN'] ?? '확인 중', power.power?.desired === 'ON' && power.power.run_until ? `${time(power.power.run_until)} 자동 꺼짐` : '설비 호스트'],
    2: step.current > 2 && run ? [`${run.workOrderId.replace(/^WO-\d{8}-/, 'WO-')} 불러옴`, `${time(run.registeredAt)}${run.loadedBy ? ` · ${run.loadedBy}` : ''}`] : ['작업을 불러오세요', '작업지시 선택'],
    3: step.current > 3 ? ['예열 완료', run?.startWhenReady ? '자동 시작' : '시작함'] : step.current === 3 ? [`${numberOr(known('Sealer.TemperaturePV'), 1)} / ${numberOr(known('Sealer.TemperatureSP'), 0)} ℃`, known('Sealer.Ready') === true ? '준비 완료' : '예열 중'] : ['대기', '작업을 불러오면 시작'],
    4: step.current >= 4 ? [`${Number(known('Line.State')) === 3 && step.current === 4 ? '일시 정지 · ' : ''}양품 ${made.good ?? '—'} / ${made.target ?? '—'}`, `불량 ${made.reject ?? '—'} · ${typeof speed === 'number' ? speed.toFixed(1) : '—'} m/min`] : ['대기', '예열 후 시작'],
    5: step.current === 5 ? ['정산 대기', '종료 중 · 기록 확인 중'] : step.current === 2 && run && run.settlement !== 'PENDING' ? [`직전 ${run.workOrderId.replace(/^WO-\d{8}-/, 'WO-')} ${run.settlement === 'SETTLED' ? '완료' : '확인 필요'}`, run.summary ? `양품 ${run.summary.good} · 불량 ${run.summary.reject}` : ''] : ['대기', run?.autoEnd ? '목표 도달 시 자동 종료' : '종료 요청 시'],
  }
  const labels: Record<Step, string> = { 1: '① 설비 전원', 2: '② 작업 준비', 3: '③ 예열', 4: '④ 생산', 5: step.current === 2 ? '⑤ 직전 작업 정산' : '⑤ 종료 · 정산' }
  return <>
    <div className="lc-strip">
      <StateBadge id="line-state" state={live.lineState ?? 'UNKNOWN'} />
      <span className="lc-strip-job">{run ? `${run.workOrderId}` : '불러온 작업 없음'}</span>
      <span className="lc-spacer" />
      <span>PLC 통신 <strong data-tone={live.heartbeat === 'GOOD' ? undefined : 'bad'}>{live.heartbeatLabel}</strong></span>
      <span id="snapshot-health" data-tone={live.healthTone}>데이터 {live.health}</span>
      <button type="button" className="lc-link light" onClick={onOpenMonitor}>모니터링 →</button>
    </div>

    <section className="lc-card cs-bar" aria-label="진행 상태">
      <ol>{([1, 2, 3, 4, 5] as Step[]).map(n => <li key={n}>
        <button type="button" id={`step-${n}`} data-state={stateOfStep(n)} aria-pressed={shown === n} onClick={() => setOpen(n === step.current ? null : n)}>
          <span>{labels[n]}{stateOfStep(n) === 'done' ? ' ✓' : n === step.current ? (step.fault ? ' · 문제' : ' · 지금') : ''}</span>
          <strong>{summaries[n][0]}</strong><small>{summaries[n][1]}</small>
        </button>
      </li>)}</ol>
      <div className="cs-track"><span style={{ width: `${(step.current - 1) * 25 + 5}%` }} /></div>
    </section>

    <div className="lc-control">
      <section className="lc-card cs-panel" aria-labelledby="step-panel-heading" data-current={shown === step.current}>
        <div className="cs-panel-head"><h3 id="step-panel-heading">{labels[shown]}</h3><span>{shown === step.current ? '지금 단계 · 할 수 있는 것' : stateOfStep(shown) === 'done' ? '지난 단계 · 결과' : '다음 단계 · 이렇게 되면 넘어옵니다'}{open !== null && <> · <button type="button" className="lc-link" onClick={() => setOpen(null)}>지금 단계로</button></>}</span></div>
        {step.fault && <FaultBox live={live} cmd={cmd} />}
        {shown === 1 && <PowerStep session={session} power={power} onSessionInvalid={onSessionInvalid} producing={step.current >= 3 && step.current <= 5} />}
        {shown === 2 && (step.current <= 2
          ? <JobStep live={live} source={source} session={session} workflow={workflow} tracked={tracked} selectedOrder={selectedOrder} onSelectOrder={onSelectOrder} onSessionInvalid={onSessionInvalid} onOpenOrders={onOpenOrders} powerOn={step.current === 2} lineStatus={lineStatus} />
          : <Facts rows={[['불러온 작업지시', run?.workOrderId ?? '불명'], ['불러온 시각', run ? new Date(run.registeredAt).toLocaleString('ko-KR') : '—'], ['처리자', run?.loadedBy ?? '—'], ['목표 도달 시 자동 종료', run?.autoEnd === null || run === undefined ? '불명' : run?.autoEnd ? '예' : '아니요'], ['예열 후 자동 시작', run?.startWhenReady === null || run === undefined ? '불명' : run?.startWhenReady ? '예' : '아니요']]}
              note="생산 중인 작업은 바꿀 수 없습니다. 다음 작업은 작업지시의 대기열에 예약하면, 이 작업이 정산된 뒤 자동으로 연결됩니다." action={<button type="button" className="lc-button" onClick={onOpenOrders}>다음 작업 예약 →</button>} />)}
        {shown === 3 && (step.current < 3 ? <Pending text="작업을 불러오면 실링 예열이 시작됩니다." />
          : step.current === 3 ? <HeatStep live={live} cmd={cmd} autoStart={run?.startWhenReady ?? null} />
            : <Facts rows={[['실링 온도', `${numberOr(known('Sealer.TemperaturePV'), 1)} ℃ (설정 ${numberOr(known('Sealer.TemperatureSP'), 0)} ℃)`], ['시작', run?.startWhenReady ? '예열 완료 후 자동 시작' : '작업자가 시작']]} />)}
        {shown === 4 && (step.current < 4 ? <Pending text="예열이 끝나고 시작하면 생산 단계로 넘어갑니다." />
          : <RunStep live={live} cmd={cmd} ended={step.current === 5} />)}
        {shown === 5 && <EndStep live={live} cmd={cmd} run={run} current={step.current} onNext={() => setOpen(2)} onOpenReports={onOpenReports} />}
        {cmd.tracked && <div className="cs-result"><h4>방금 보낸 요청</h4><CommandResult cmd={cmd} /></div>}
      </section>

      <aside className="lc-zones" aria-label="구역 상태">
        <div className="lc-card lc-zone-list">
          {(['Line', ...zones.map(item => item.id)] as (ZoneId | 'Line')[]).map((id, index) => {
            const state = stateOf(known, id)
            return <button key={id} type="button" aria-pressed={zone === id} onClick={() => onZone(id)}>
              <span>{id === 'Line' ? '라인 전체' : `0${index} ${zones[index - 1].label}`}</span><i data-tone={toneOf(state)} aria-label={state} />
            </button>
          })}
        </div>
        <ZoneDetail live={live} zone={zone} />
        <CommandLookup cmd={cmd} />
      </aside>
    </div>
  </>
}

const numberOr = (value: unknown, digits: number) => typeof value === 'number' && Number.isFinite(value) ? value.toFixed(digits) : '—'

function Facts({ rows, note, action }: { rows: [string, string][]; note?: string; action?: ReactNode }) {
  return <div className="cs-facts"><dl>{rows.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>{note && <p className="lc-hint">{note}</p>}{action}</div>
}
function Pending({ text }: { text: string }) { return <p className="cs-pending">{text}</p> }

function ZoneDetail({ live, zone }: { live: LiveLine; zone: ZoneId | 'Line' }) {
  const current = zones.find(item => item.id === zone)
  return <section className="lc-card lc-zone-detail" aria-labelledby="zone-detail-heading">
    <div className="lc-card-head"><h3 id="zone-detail-heading">{current ? `${current.label} 구역` : '라인 전체'}</h3><StateBadge state={stateOf(live.known, zone)} /></div>
    {current ? <>
      <div className="lc-window-values">{current.metrics.map((metric, at) => <MetricBox key={metric.tag} known={live.known} zone={current} index={at} />)}</div>
      <div className="lc-zone-lamps">{current.lamps.map(lamp => <LampChip key={lamp.tag} known={live.known} lamp={lamp} />)}</div>
    </> : <div className="lc-zone-lamps">{interlocks.map(lamp => <LampChip key={lamp.tag} known={live.known} lamp={lamp} />)}</div>}
    <p className="lc-hint">운전 명령은 PLC 규격상 라인 전체에 적용됩니다.</p>
  </section>
}

function FaultBox({ live, cmd }: { live: LiveLine; cmd: LineCommands }) {
  const state = Number(live.known('Line.State'))
  const reason = cmd.disabledReason('RESET_FAULT')
  return <div className="cs-fault" role="alert">
    <strong>{state === 6 ? '비상정지 상태입니다' : state === 7 ? '복구 확인이 필요합니다' : `고장 상태입니다 (코드 ${String(live.known('Line.FaultCode') ?? '?')})`}</strong>
    <span>{state === 6 ? '비상정지는 현장에서 안전을 확인한 뒤 해제해야 합니다.' : '현장에서 원인을 해소한 뒤 고장 리셋을 누릅니다.'}</span>
    {cmd.operator && state === 5 && <div><button id="command-RESET_FAULT" type="button" className="lc-button primary" disabled={!!reason} onClick={() => void cmd.submit('RESET_FAULT')}>고장 리셋</button>{reason && <span className="lc-reason">{reason}</span>}</div>}
  </div>
}

function PowerStep({ session, power, onSessionInvalid, producing }: { session: Session; power: ReturnType<typeof usePowerState>; onSessionInvalid: () => void; producing: boolean }) {
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const canChange = session.roles.includes('equipment_admin') && !session.must_change_password
  if (!power.managed) return <Facts rows={[['설비 전원', '이 환경은 설비가 상시 켜져 있습니다']]} />
  async function change(action: 'start' | 'stop') {
    setBusy(true); setMessage('')
    try {
      const response = await fetch('/api/v1/runtime-control', { method: 'POST', credentials: 'same-origin', cache: 'no-store', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': session.csrf_token }, body: JSON.stringify({ action }), signal: AbortSignal.timeout(12_000) })
      if (response.status === 401 || response.status === 403) { onSessionInvalid(); return }
      if (!response.ok) throw new Error('rejected')
      power.setPower(await response.json() as Power)
      setMessage(action === 'start' && on ? '자동 꺼짐 시각을 60분 뒤로 늦췄습니다.' : '')
    } catch { setMessage('요청 결과를 확인하지 못했습니다. 잠시 뒤 상태를 다시 확인해 주세요.') } finally { setBusy(false) }
  }
  const p = power.power
  const on = p?.desired === 'ON'
  const state = power.state ?? 'UNKNOWN'
  return <div id="runtime-power" className="cs-step" data-state={state}>
    <Facts rows={[['상태', power.switching === 'starting' ? '켜는 중' : power.switching === 'stopping' ? '끄는 중' : powerText[state] ?? state], ['자동 꺼짐', on && p?.run_until ? new Date(p.run_until).toLocaleTimeString('ko-KR') : '—'], ...(state === 'ERROR' && !power.switching && p?.reported_detail ? [['오류', p.reported_detail] as [string, string]] : [])]} />
    <p id="runtime-power-state" className="sr-only" role="status">{power.switching === 'starting' ? '켜는 중' : power.switching === 'stopping' ? '끄는 중' : powerText[state] ?? state}</p>
    {(busy || power.switching) && <div className="cs-progress" role="status"><span className="cs-spinner" aria-hidden="true" /><span><strong>{busy ? '요청을 보내고 있습니다…' : power.switching === 'starting' ? '설비를 켜는 중입니다…' : '설비를 끄는 중입니다…'}</strong><small>{busy ? '' : `${power.elapsed}초 경과 · ${power.switching === 'starting' ? '보통 30초~1분 걸립니다' : '보통 10초 안팎 걸립니다'}${power.switching === 'starting' && state === 'ERROR' ? ' · 시작에 실패해 자동으로 다시 시도하고 있습니다' : ''}`}</small></span></div>}
    {canChange ? <div className="cs-actions">
      {!on && <button id="runtime-power-start" type="button" className="lc-button primary big" disabled={busy || !!power.switching} onClick={() => void change('start')}>설비 켜기<small>60분 뒤 자동으로 꺼집니다</small></button>}
      {on && <button type="button" className="lc-button big" disabled={busy || !!power.switching} onClick={() => void change('start')}>60분 연장<small>자동 꺼짐을 지금부터 60분 뒤로</small></button>}
      {on && <button id="runtime-power-stop" type="button" className="lc-button danger big" disabled={busy || producing || !!power.switching} title={producing ? '생산 중에는 끌 수 없습니다' : undefined} onClick={() => void change('stop')}>설비 끄기<small>{producing ? '생산 중에는 끌 수 없습니다. 먼저 작업을 종료하세요' : '시뮬레이터와 게이트웨이를 멈춥니다'}</small></button>}
    </div> : <p className="lc-hint">설비 전원은 설비 관리자만 켜고 끌 수 있습니다.</p>}
    {message && <p className="lc-hint" role="status">{message}</p>}
  </div>
}

/** Pick a released order not yet run and load it (LOAD_JOB through the work-order workflow). */
function JobStep({ live, source, session, workflow, tracked, selectedOrder, onSelectOrder, onSessionInvalid, onOpenOrders, powerOn, lineStatus }: { live: LiveLine; source: Source; session: Session; workflow: WorkOrderWorkflow; tracked: TrackedCommand | null; selectedOrder: WorkOrder | null; onSelectOrder: (order: WorkOrder | null) => void; onSessionInvalid: () => void; onOpenOrders: () => void; powerOn: boolean; lineStatus: LineStatus | undefined }) {
  const [orders, setOrders] = useState<WorkOrder[] | null>(null)
  const [revision, setRevision] = useState(0)
  const [confirmation, setConfirmation] = useState<(BindBody & { order_id: string; epoch: string }) | null>(null)
  const [autoEnd, setAutoEnd] = useState(true)
  // Starting on readiness needs operate permission; a production manager loads and an operator starts.
  const canAutoStart = session.roles.includes('operator')
  const [startWhenReady, setStartWhenReady] = useState(canAutoStart)
  useEffect(() => {
    const controller = new AbortController()
    let busy = false
    const load = async () => {
      if (busy) return
      busy = true
      try {
        // One light read: the overview already knows which released orders were never run.
        const items = await readOverview(source.line_id, controller.signal)
        if (!controller.signal.aborted) setOrders(items.filter(item => item.phase === 'WAITING'))
      } catch (cause) {
        if (!controller.signal.aborted && ((cause as { status?: number })?.status === 401 || (cause as { status?: number })?.status === 403)) onSessionInvalid()
      } finally { busy = false }
    }
    void load()
    // Orders registered or finished elsewhere show up without a manual refresh; a slow read is never cut off.
    const timer = window.setInterval(() => void load(), POLL_ORDERS_MS)
    return () => { controller.abort(); window.clearInterval(timer) }
  }, [source.line_id, revision, onSessionInvalid])
  // Pick the first order ready to run, so the next step is one click.
  useEffect(() => {
    if (!orders) return
    if (!selectedOrder || !orders.some(item => item.id === selectedOrder.id)) onSelectOrder(orders[0] ?? null)
  }, [orders, selectedOrder, onSelectOrder])
  const allowed = session.roles.some(role => ['operator', 'production_manager'].includes(role))
  const values = live.controlValues
  const order = selectedOrder && selectedOrder.status === 'RELEASED' ? selectedOrder : null
  const checks: [string, boolean | null][] = [
    ['설비 전원 켜짐', powerOn],
    ['원격 제어 모드', live.known('Line.ControlMode') === null ? null : live.known('Line.ControlMode') === 1],
    ['라인 정지 · 재공 0개', live.known('Line.State') === null ? null : values['Line.State'] === 0 && values['Line.WipCount'] === 0],
    ['데이터 정상 수집', live.blockedReason === ''],
    ['직전 작업 정산 완료', lineStatus === undefined ? null : !lineStatus.run || lineStatus.run.settlement !== 'PENDING'],
    // The server knows every stored command; only a request this screen has not heard back about adds to it.
    ['직전 요청 정리', lineStatus === undefined ? null : !lineStatus.unresolved && !workflow.attempt && !(tracked && !(terminal(tracked.result) || tracked.phase === 'REJECTED'))],
  ]
  const reason = !allowed ? '이 계정은 작업을 불러올 권한이 없습니다.' : session.must_change_password ? '비밀번호를 먼저 변경해 주세요.'
    : checks.find(([, ok]) => ok !== true) ? `${checks.find(([, ok]) => ok !== true)![0]} 조건을 확인해야 합니다.`
      : order && workflow.orderBlocked(order.id) ? '같은 작업지시의 원래 요청 기록을 먼저 확인해 주세요.'
        : !order ? '불러올 작업지시를 고르세요.' : !screenUInt64(values['Line.ControlVersion']) || !workflowId(values['Line.CounterEpoch']) ? '제어 버전과 카운터 구간을 확인해야 합니다.' : ''
  const changed = confirmation && (confirmation.order_id !== order?.id || confirmation.epoch !== values['Line.CounterEpoch'] || confirmation.expected_work_order_version !== order?.row_version || confirmation.expected_control_version !== values['Line.ControlVersion'])
  return <div id="work-order-binding" className="cs-step">
    <div className="cs-job">
      <div className="cs-orders" role="radiogroup" aria-label="불러올 작업지시">
        {orders === null ? <p className="lc-hint">작업지시를 불러오고 있습니다.</p>
          : orders.length === 0 ? <div className="cs-pending">불러올 작업지시가 없습니다.<button type="button" className="lc-button primary" onClick={onOpenOrders}>작업지시 만들기 →</button></div>
            : orders.map(item => <label key={item.id} data-checked={order?.id === item.id}>
              <input type="radio" name="binding-order" value={item.id} checked={order?.id === item.id} onChange={() => { onSelectOrder(item); setConfirmation(null) }} />
              <span><strong>{item.id}</strong><small>{item.item_id} · {item.recipe_version_id} · 목표 {item.target_quantity.toLocaleString('ko-KR')}개</small></span>
            </label>)}
        <div className="cs-orders-foot"><button type="button" className="lc-link" onClick={() => setRevision(value => value + 1)}>목록 새로 조회</button>{!!orders?.length && <button type="button" className="lc-link" onClick={onOpenOrders}>+ 다른 작업지시 만들기</button>}</div>
      </div>
      <ul className="cs-checks" aria-label="불러오기 조건">{checks.map(([label, ok]) => <li key={label} data-ok={ok === true ? 'yes' : ok === null ? 'unknown' : 'no'}>{ok === true ? '✓' : ok === null ? '?' : '✗'} {label}</li>)}</ul>
    </div>
    {/* Nothing to load: the empty card above is the only action, no dead button. */}
    {!(orders !== null && orders.length === 0 && !confirmation) && <>
    <div className="lc-checks">
      <label><input id="binding-auto-end" type="checkbox" checked={autoEnd} onChange={event => setAutoEnd(event.target.checked)} /> 목표 도달 시 자동 종료</label>
      {canAutoStart ? <label><input id="binding-start-when-ready" type="checkbox" checked={startWhenReady} onChange={event => setStartWhenReady(event.target.checked)} /> 예열 끝나면 자동 시작</label> : <span className="lc-hint">자동 시작은 작업자 권한이 필요합니다. 불러온 뒤 작업자가 시작합니다.</span>}
    </div>
    {!confirmation ? <button id="binding-prepare" type="button" className="lc-button primary big" disabled={!!reason} onClick={() => order && setConfirmation({ command_id: crypto.randomUUID(), order_id: order.id, epoch: values['Line.CounterEpoch'] as string, line_id: source.line_id, source_id: source.source_id, stream_id: source.stream_id, expected_work_order_version: order.row_version, expected_control_version: values['Line.ControlVersion'] as string })}>{order ? `${order.id} 불러오기` : '작업 불러오기'}<small>누르면 확인 후 공정 설정과 목표 수량을 PLC로 보냅니다</small></button>
      : <div className="lc-confirm" role="group" aria-label="작업 불러오기 확인">
        <strong>{order?.id}를 라인에 불러올까요?</strong>
        <span>공정 설정과 목표 {order?.target_quantity}개를 PLC로 보냅니다.{canAutoStart && startWhenReady ? ' 예열이 끝나면 자동으로 운전을 시작합니다.' : ''}</span>
        {changed && <span className="lc-reason">확인하는 동안 상태가 바뀌었습니다. 취소 후 다시 확인해 주세요.</span>}
        <div><button id="binding-submit" type="button" className="lc-button primary" disabled={!!reason || !!changed} onClick={() => { if (!reason && !changed && order) { void workflow.submit('bind', { command_id: confirmation.command_id, line_id: confirmation.line_id, source_id: confirmation.source_id, stream_id: confirmation.stream_id, expected_work_order_version: confirmation.expected_work_order_version, expected_control_version: confirmation.expected_control_version, auto_end_at_target: autoEnd, start_when_ready: canAutoStart && startWhenReady, work_order_id: confirmation.order_id, actor: session.login }, order); setConfirmation(null) } }}>불러오기 요청</button>
          <button id="binding-cancel-confirm" type="button" className="lc-button" onClick={() => setConfirmation(null)}>취소</button></div>
      </div>}
    {reason && <p id="binding-reason" className="lc-reason">{reason}</p>}
    </>}
    {lineStatus?.unresolved && <p className="lc-hint" role="status">{lineStatus.unresolved.kind === 'LOAD_JOB' ? '작업 불러오기를 PLC가 처리하고 있습니다.' : '앞선 명령을 PLC가 처리하고 있습니다.'} ({lineStatus.unresolved.status})</p>}
  </div>
}

function HeatStep({ live, cmd, autoStart }: { live: LiveLine; cmd: LineCommands; autoStart: boolean | null }) {
  const pv = live.known('Sealer.TemperaturePV'), sp = live.known('Sealer.TemperatureSP')
  const ratio = typeof pv === 'number' && typeof sp === 'number' && sp > 0 ? Math.min(1, pv / sp) : null
  const ready = live.known('Sealer.Ready') === true
  return <div className="cs-step">
    <div className="cs-gauge"><div><span>실링 온도</span><strong>{numberOr(pv, 1)}<small> / {numberOr(sp, 0)} ℃</small></strong></div><div className="lc-bar light"><span style={{ width: `${(ratio ?? 0) * 100}%` }} /></div><span>{ready ? '준비 완료' : '예열 중'} · {autoStart === null ? '자동 시작 여부 불명' : autoStart ? '준비되면 자동으로 시작합니다' : '준비되면 시작을 누르세요'}</span></div>
    <CommandButtons cmd={cmd} values={live.controlValues} only={['START', 'END_RUN']} />
  </div>
}

function RunStep({ live, cmd, ended }: { live: LiveLine; cmd: LineCommands; ended: boolean }) {
  const made = production(live.known)
  return <div className="cs-step">
    <div className="cs-gauge"><div><span>양품 / 목표</span><strong>{made.good ?? '—'}<small> / {made.target ?? '—'}</small></strong><span>불량 <b>{made.reject ?? '—'}</b></span></div><div className="lc-bar light"><span style={{ width: `${(made.ratio ?? 0) * 100}%` }} /></div></div>
    {ended ? <p className="lc-hint">생산이 끝났습니다. ⑤ 단계에서 정산을 확인하세요.</p> : <>
      <CommandButtons cmd={cmd} values={live.controlValues} only={['PAUSE', 'START', 'END_RUN']} />
      <SpeedControl cmd={cmd} />
    </>}
  </div>
}

function EndStep({ live, cmd, run, current, onNext, onOpenReports }: { live: LiveLine; cmd: LineCommands; run: ActiveRun | null | undefined; current: Step; onNext: () => void; onOpenReports: () => void }) {
  if (current < 5 && !(current === 2 && run && run.settlement !== 'PENDING')) {
    return <div className="cs-step">
      <Facts rows={[['종료 방식', run?.autoEnd === true ? '목표 수량에 닿으면 PLC가 자동 종료' : run?.autoEnd === false ? '자동 종료 꺼짐 · 종료 요청이 필요합니다' : '작업을 불러오면 정해집니다']]} />
      {current === 4 && run?.autoEnd === false && <CommandButtons cmd={cmd} values={live.controlValues} only={['END_RUN']} />}
    </div>
  }
  if (!run || run.settlement === 'PENDING') return <div className="cs-step"><Pending text="설비가 재공을 정리하고 실적을 확정하는 중입니다. 정산이 기록되면 결과가 여기에 표시됩니다." /></div>
  return <div className="cs-step">
    <Facts rows={[['작업지시', run.workOrderId], ['정산', run.settlement === 'SETTLED' ? '완료' : '확인 필요 (불일치·충돌)'], ['양품 / 불량', run.summary ? `${run.summary.good} / ${run.summary.reject}` : '—'], ['종료 사유', run.summary?.endReason === 'TARGET_REACHED' ? '목표 도달' : run.summary?.endReason === 'OPERATOR' ? '작업자 종료' : '—'], ['종료 시각', run.summary?.endedAt ? new Date(run.summary.endedAt).toLocaleString('ko-KR') : '—']]} />
    <div className="cs-actions"><button type="button" className="lc-button" onClick={onOpenReports}>실적 보기 →</button><button type="button" className="lc-button primary" onClick={onNext}>다음 작업 준비 →</button></div>
  </div>
}
