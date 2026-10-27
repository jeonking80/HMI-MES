import { useEffect, useRef, useState } from 'react'
import type { Dispatch, SetStateAction } from 'react'
import type { Session } from '../auth'
import type { TrackedCommand } from '../commands'
import type { WorkOrderWorkflow } from '../useWorkOrderWorkflow'
import { useLine } from './lineState'
import type { LiveLine } from './lineState'
import { interlocks, numberText, stateOf, zones } from './lineModel'
import type { Zone, ZoneId } from './lineModel'
import { Drawing, LampChip, MetricBox, SourcePicker, StateBadge, TagDetails } from './parts'
import { issuesOf, newIntentBlocked, powerLabels, production, toneOf, usePower } from './logic'
import { useLineCommands } from './useLineCommands'
import { CommandButtons, CommandResult, SpeedControl } from './CommandControls'
import TrendMini from './TrendMini'
import './line.css'

interface Props {
  session: Session; onSessionInvalid: () => void; workflow: WorkOrderWorkflow
  tracked: TrackedCommand | null; setTracked: Dispatch<SetStateAction<TrackedCommand | null>>
  onOpenControl: (zone: ZoneId | 'Line') => void; onOpenAlarms: () => void
}

/** Line monitoring: the line process at the centre, read-only. A zone opens a control window over it. */
export default function MonitorPage(props: Props) {
  const line = useLine()
  const power = usePower()
  const live = line.live
  return <div className="lc-page">
    <header className="lc-page-head">
      <div className="lc-title"><h2>{line.source?.line_name ?? '라인'}</h2><span>실시간 공정</span></div>
      <div className="lc-chips">
        <SourcePicker />
        <span id="screen-connection" className="lc-chip" data-tone={live?.stream.phase === 'synced' ? (live.blockedReason ? 'unknown' : 'good') : live?.stream.phase === 'stopped' ? 'bad' : 'unknown'} data-state={live?.stream.phase ?? 'connecting'}><i aria-hidden="true" />{live?.stream.phase === 'synced' ? (live.blockedReason ? '화면 연결됨 · 데이터 지연' : '실시간') : live?.stream.phase === 'stopped' ? '화면 연결 끊김' : '연결 중'}</span>
        {live && <span className="lc-chip" data-tone={live.heartbeat === 'GOOD' ? 'good' : live.heartbeat === 'UNKNOWN' ? 'unknown' : 'bad'}>PLC 통신 <strong id="heartbeat-state" data-state={live.heartbeat}>{live.heartbeatLabel}</strong></span>}
        {power.state && power.state !== 'NOT_MANAGED' && <span className="lc-chip" data-tone={power.state === 'RUNNING' ? 'good' : power.state === 'ERROR' ? 'bad' : 'unknown'}>{powerLabels[power.state] ?? power.state}</span>}
        {live?.stream.phase === 'stopped' && <button type="button" className="lc-button" onClick={live.stream.retry}>다시 연결</button>}
        {live?.snapshot && <time className="lc-clock">{new Date(live.snapshot.screen.emitted_at).toLocaleTimeString('ko-KR', { hour12: false })}</time>}
      </div>
    </header>
    {!line.source ? <div className="lc-card lc-empty">{line.sources === null ? '라인 정보를 불러오고 있습니다.' : line.sources.length === 0 ? '등록된 수집원이 없습니다.' : '라인을 선택해 주세요.'}</div>
      : !live ? null : <LiveMonitor {...props} live={live} source={line.source} power={power.state} />}
  </div>
}

function LiveMonitor({ live, source, power, session, onSessionInvalid, workflow, tracked, setTracked, onOpenControl, onOpenAlarms }: Props & { live: LiveLine; source: NonNullable<ReturnType<typeof useLine>['source']>; power: string | null }) {
  const [open, setOpen] = useState<ZoneId | null>(null)
  const known = live.known
  const made = production(known)
  const issues = issuesOf(live)
  const execution = live.executionId
  const recipe = known('Line.RecipeVersion')
  const zoneIssue = (zone: ZoneId) => issues.find(item => item.zone === zone)
  const powerOff = power !== null && power !== 'NOT_MANAGED' && power !== 'RUNNING'
  // With the plant powered off the real reason is the power, not stale tag quality.
  const blockedReason = powerOff ? `${powerLabels[power] ?? '설비 전원 상태 확인 필요'} · 운전 제어 ①에서 전원을 켜 주세요.` : live.blockedReason
  const cmd = useLineCommands({ source, session, blockedReason, newIntentBlocked: newIntentBlocked(workflow, live.controlValues), values: live.controlValues, tracked, setTracked, onSessionInvalid })
  return <>
    {issues.length > 0 && !powerOff && <div className="lc-alert" role="alert">
      <svg width="18" height="18" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3L22 20H2Z" fill="none" stroke="currentColor" strokeWidth="2" /><path d="M12 10v5M12 17.5v.5" stroke="currentColor" strokeWidth="2" /></svg>
      <strong>{issues[0].text}</strong>{issues.length > 1 && <span>외 {issues.length - 1}건</span>}
      <span className="lc-spacer" />
      {issues[0].zone !== 'Line' ? <button type="button" className="lc-link" onClick={() => setOpen(issues[0].zone as ZoneId)}>눌러서 조치</button> : <button type="button" className="lc-link" onClick={onOpenAlarms}>알람 보기</button>}
    </div>}
    {powerOff && <div className="lc-notice">설비 전원이 꺼져 있어 값이 갱신되지 않습니다. 운전 제어에서 설비를 켜세요. <button type="button" className="lc-link" onClick={() => onOpenControl('Line')}>운전 제어로 →</button></div>}

    <section className="lc-mimic" aria-labelledby="process-heading">
      <h3 id="process-heading" className="sr-only">라인 공정</h3>
      <div className="lc-mimic-head">
        <div className="lc-mimic-state">
          <StateBadge id="line-state" state={live.lineState ?? 'UNKNOWN'} large />
          <div className="lc-job"><span>현재 실행</span><strong>{execution ? <>{execution.length > 18 ? `${execution.slice(0, 18)}…` : execution}{typeof recipe === 'string' && recipe ? <small> · {recipe}</small> : null}</> : '없음'}</strong></div>
        </div>
        <div className="lc-mimic-totals">
          <div className="lc-progress"><div><span>양품 진행</span><strong data-process-value="Line.GoodTotal">{made.good ?? '—'} / {made.target ?? '—'}</strong></div><div className="lc-bar"><span style={{ width: `${(made.ratio ?? 0) * 100}%` }} /></div></div>
          <div className="lc-total"><span>불량</span><strong data-tone="bad" data-process-value="Line.RejectTotal">{made.reject ?? '—'}</strong></div>
          <div className="lc-total"><span>재공</span><strong data-process-value="Line.WipCount">{made.wip ?? '—'}</strong></div>
        </div>
      </div>

      <ol className="lc-flow">{zones.map((zone, index) => {
        const state = stateOf(known, zone.id)
        const issue = zoneIssue(zone.id)
        return <li key={zone.id} data-equipment={zone.id} data-state={state}>
          <button type="button" className="lc-zone" data-tone={issue ? 'bad' : toneOf(state)} aria-label={`${zone.label} · ${state} · 눌러서 제어`} onClick={() => setOpen(zone.id)}>
            <span className="lc-zone-head"><span><small>0{index + 1}</small>{zone.label}</span><StateBadge state={state} /></span>
            <Drawing zone={zone.id} />
            <span className="lc-zone-metrics"><MetricBox known={known} zone={zone} index={0} /><MetricBox known={known} zone={zone} index={1} /></span>
            <span className="lc-zone-lamps">{zone.lamps.slice(0, 3).map(lamp => <LampChip key={lamp.tag} known={known} lamp={lamp} />)}</span>
            <span className="lc-zone-open">{issue ? issue.text : '눌러서 조치 · 변경'} →</span>
          </button>
        </li>
      })}</ol>

      <div className="lc-interlocks" aria-label="인터록"><span>인터록</span>{interlocks.map(lamp => <LampChip key={lamp.tag} known={known} lamp={lamp} />)}</div>
    </section>

    <div className="lc-lower">
      <TrendMini source={source} onSessionInvalid={onSessionInvalid} />
      <section className="lc-card lc-issues" aria-labelledby="issues-heading">
        <div className="lc-card-head"><h3 id="issues-heading">현재 이상</h3><span>{issues.length}건</span></div>
        {issues.length === 0 ? <p className="lc-ok">이상 없음</p> : <ul>{issues.map(item => <li key={item.key}>{item.zone !== 'Line' ? <button type="button" className="lc-link" onClick={() => setOpen(item.zone as ZoneId)}>{item.text} →</button> : item.text}</li>)}</ul>}
        <button type="button" className="lc-link" onClick={onOpenAlarms}>알람 센터 →</button>
      </section>
    </div>

    <TagDetails live={live} />
    <p className="lc-foot"><span id="snapshot-health" data-tone={live.healthTone}>데이터 {live.health}</span> · 미수신 태그 {live.snapshot?.missing_tag_ids.length ?? '—'}개</p>

    {open && <ZoneWindow zone={zones.find(item => item.id === open)!} live={live} cmd={cmd} issue={zoneIssue(open)?.text ?? null} onClose={() => setOpen(null)} onOpenControl={() => { setOpen(null); onOpenControl(open) }} />}
  </>
}

/** A control window over the monitoring screen for one zone: values, settings, commands and their result. */
function ZoneWindow({ zone, live, cmd, issue, onClose, onOpenControl }: { zone: Zone; live: LiveLine; cmd: ReturnType<typeof useLineCommands>; issue: string | null; onClose: () => void; onOpenControl: () => void }) {
  const close = useRef<HTMLButtonElement>(null)
  useEffect(() => { close.current?.focus() }, [])
  const known = live.known
  const state = stateOf(known, zone.id)
  const index = zones.indexOf(zone)
  const setpoint = numberText(known, { tag: 'Sealer.TemperatureSP', label: '', unit: '', digits: 0 })
  return <div className="lc-overlay" onMouseDown={event => { if (event.target === event.currentTarget) onClose() }}>
    <section className="lc-window" role="dialog" aria-modal="true" aria-labelledby="zone-window-title" onKeyDown={event => { if (event.key === 'Escape') onClose() }}>
      <header className="lc-window-head">
        <span className="lc-window-title"><small>0{index + 1}</small><h2 id="zone-window-title">{zone.label} 제어</h2><StateBadge state={state} /></span>
        <button ref={close} type="button" aria-label="닫기" onClick={onClose}>×</button>
      </header>
      <div className="lc-window-body">
        {issue && <div className="lc-problem"><strong>{issue}</strong><span>아래 실행에서 조치할 수 있는 버튼이 강조됩니다.</span></div>}
        <div className="lc-window-values">
          {zone.metrics.map((metric, at) => <MetricBox key={metric.tag} known={known} zone={zone} index={at} />)}
        </div>
        <div className="lc-zone-lamps">{zone.lamps.map(lamp => <LampChip key={lamp.tag} known={known} lamp={lamp} />)}</div>

        <section className="lc-window-section" aria-labelledby="zone-settings">
          <h3 id="zone-settings">설정 변경</h3>
          {zone.id === 'Sealer' && <p className="lc-hint">목표 온도 {setpoint ?? '—'} ℃ · 운전 중 온도는 작업에 불러온 공정 설정값으로 고정됩니다. 바꾸려면 공정 설정의 새 버전으로 다음 작업을 불러오세요.</p>}
          {cmd.canSetSpeed ? <SpeedControl cmd={cmd} /> : <p className="lc-hint">{cmd.producer ? '운전 속도는 운전 중이거나 일시 정지 중일 때 바꿀 수 있습니다.' : '운전 속도 변경은 생산 담당자 권한이 필요합니다.'}</p>}
        </section>

        <section className="lc-window-section" aria-labelledby="zone-actions">
          <h3 id="zone-actions">실행 <small>라인 전체에 적용</small></h3>
          <CommandButtons cmd={cmd} values={live.controlValues} compact />
        </section>

        {cmd.tracked && <section className="lc-window-section" aria-labelledby="zone-result"><h3 id="zone-result">요청 결과</h3><CommandResult cmd={cmd} /></section>}
      </div>
      <footer className="lc-window-foot"><button type="button" className="lc-link" onClick={onOpenControl}>운전 제어 화면에서 전체 보기 →</button></footer>
    </section>
  </div>
}
