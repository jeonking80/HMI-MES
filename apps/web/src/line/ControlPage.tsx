import { useCallback, useEffect, useState } from 'react'
import type { Dispatch, SetStateAction } from 'react'
import type { Session } from '../auth'
import { terminal } from '../commands'
import type { TrackedCommand } from '../commands'
import LoadTracker from '../LoadTracker'
import { mesPaths, mesRequest, orderPage } from '../mesApi'
import type { WorkOrder } from '../mesApi'
import { screenUInt64 } from '../screenState'
import type { WorkOrderWorkflow } from '../useWorkOrderWorkflow'
import { workflowId } from '../workflow'
import type { BindBody } from '../workflow'
import { useLine } from './lineState'
import type { LiveLine, Source } from './lineState'
import { interlocks, stateOf, zones } from './lineModel'
import type { ZoneId } from './lineModel'
import { LampChip, MetricBox, SourcePicker, StateBadge } from './parts'
import { newIntentBlocked, production, toneOf } from './logic'
import { useLineCommands } from './useLineCommands'
import { CommandButtons, CommandLookup, CommandResult, SpeedControl } from './CommandControls'
import './line.css'

interface Props {
  session: Session; onSessionInvalid: () => void; workflow: WorkOrderWorkflow
  tracked: TrackedCommand | null; setTracked: Dispatch<SetStateAction<TrackedCommand | null>>
  zone: ZoneId | 'Line'; onZone: (zone: ZoneId | 'Line') => void
  selectedOrder: WorkOrder | null; onSelectOrder: (order: WorkOrder | null) => void
  onOpenMonitor: () => void
}

/** Operation control: pick a zone to see its detail, then power, job, commands and their results in order. */
export default function ControlPage(props: Props) {
  const line = useLine()
  const live = line.live
  return <div className="lc-page">
    <header className="lc-page-head">
      <div className="lc-title"><h2>{line.source?.line_name ?? '라인'}</h2><span>원격 조작</span></div>
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

function LiveControl({ live, source, session, onSessionInvalid, workflow, tracked, setTracked, zone, onZone, selectedOrder, onSelectOrder, onOpenMonitor }: Props & { live: LiveLine; source: Source }) {
  const known = live.known
  const made = production(known)
  const cmd = useLineCommands({ source, session, blockedReason: live.blockedReason, newIntentBlocked: newIntentBlocked(workflow, live.controlValues), values: live.controlValues, tracked, setTracked, onSessionInvalid })
  const current = zones.find(item => item.id === zone)
  return <>
    <div className="lc-strip">
      <StateBadge id="line-state" state={live.lineState ?? 'UNKNOWN'} />
      <span className="lc-strip-job">{live.executionId ? `실행 ${live.executionId.length > 18 ? `${live.executionId.slice(0, 18)}…` : live.executionId}` : '불러온 작업 없음'}</span>
      <span className="lc-spacer" />
      <span>양품 <strong>{made.good ?? '—'} / {made.target ?? '—'}</strong></span>
      <span>불량 <strong data-tone="bad">{made.reject ?? '—'}</strong></span>
      <span id="snapshot-health" data-tone={live.healthTone}>데이터 {live.health}</span>
      <button type="button" className="lc-link light" onClick={onOpenMonitor}>모니터링 →</button>
    </div>

    <div className="lc-control">
      <aside className="lc-zones" aria-label="구역 선택">
        <div className="lc-card lc-zone-list">
          {(['Line', ...zones.map(item => item.id)] as (ZoneId | 'Line')[]).map((id, index) => {
            const state = stateOf(known, id)
            return <button key={id} type="button" aria-pressed={zone === id} onClick={() => onZone(id)}>
              <span>{id === 'Line' ? '라인 전체' : `0${index} ${zones[index - 1].label}`}</span><i data-tone={toneOf(state)} aria-label={state} />
            </button>
          })}
        </div>
        <section className="lc-card lc-zone-detail" aria-labelledby="zone-detail-heading">
          <div className="lc-card-head"><h3 id="zone-detail-heading">{current ? `${current.label} 구역` : '라인 전체'}</h3><StateBadge state={stateOf(known, zone)} /></div>
          {current ? <>
            <div className="lc-window-values">{current.metrics.map((metric, at) => <MetricBox key={metric.tag} known={known} zone={current} index={at} />)}</div>
            <div className="lc-zone-lamps">{current.lamps.map(lamp => <LampChip key={lamp.tag} known={known} lamp={lamp} />)}</div>
          </> : <div className="lc-zone-lamps">{interlocks.map(lamp => <LampChip key={lamp.tag} known={known} lamp={lamp} />)}</div>}
          <p className="lc-hint">운전 명령은 PLC 규격상 라인 전체에 적용됩니다.</p>
        </section>
      </aside>

      <div className="lc-steps-col">
        <PowerCard session={session} onSessionInvalid={onSessionInvalid} />
        <JobCard live={live} source={source} session={session} workflow={workflow} tracked={tracked} selectedOrder={selectedOrder} onSelectOrder={onSelectOrder} onSessionInvalid={onSessionInvalid} />
        <section id="command-panel" className="lc-card lc-step" aria-labelledby="command-heading">
          <div className="lc-step-head"><span className="lc-num">3</span><h3 id="command-heading">운전 명령</h3></div>
          <CommandButtons cmd={cmd} values={live.controlValues} />
          <SpeedControl cmd={cmd} />
        </section>
        <section className="lc-card lc-step" aria-labelledby="result-heading">
          <div className="lc-step-head"><h3 id="result-heading">명령 결과</h3></div>
          {cmd.tracked ? <CommandResult cmd={cmd} /> : <p className="lc-hint">보낸 명령의 진행이 여기에 표시됩니다.</p>}
          <CommandLookup cmd={cmd} />
        </section>
      </div>
    </div>
  </>
}

interface Power { desired: 'ON' | 'OFF'; run_until: string | null; agent_online: boolean; reported_state: string; reported_detail: string | null }
const powerText: Record<string, string> = { OFF: '꺼짐', STARTING: '켜는 중', RUNNING: '켜짐', STOPPING: '끄는 중', ERROR: '오류', UNKNOWN: '설비 호스트 연결 안 됨' }

/** Plant runtime power: equipment admins request a bounded start or a stop; the host agent reports what it did. */
function PowerCard({ session, onSessionInvalid }: { session: Session; onSessionInvalid: () => void }) {
  const [power, setPower] = useState<Power | null>(null)
  const [hidden, setHidden] = useState(false)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const canChange = session.roles.includes('equipment_admin') && !session.must_change_password
  const load = useCallback(async (signal?: AbortSignal) => {
    const response = await fetch('/api/v1/runtime-control', { credentials: 'same-origin', cache: 'no-store', signal })
    if (response.status === 401 || response.status === 403) { onSessionInvalid(); return }
    if (response.status === 404) { setHidden(true); return }
    if (!response.ok) throw new Error('runtime control unavailable')
    setPower(await response.json() as Power)
  }, [onSessionInvalid])
  useEffect(() => {
    const controller = new AbortController()
    const tick = () => { load(controller.signal).catch(() => { if (!controller.signal.aborted) setMessage('설비 전원 상태를 확인하지 못했습니다.') }) }
    tick()
    const timer = window.setInterval(tick, 5000)
    return () => { controller.abort(); window.clearInterval(timer) }
  }, [load])
  async function change(action: 'start' | 'stop') {
    setBusy(true); setMessage('')
    try {
      const response = await fetch('/api/v1/runtime-control', { method: 'POST', credentials: 'same-origin', cache: 'no-store', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': session.csrf_token }, body: JSON.stringify({ action }), signal: AbortSignal.timeout(12_000) })
      if (response.status === 401 || response.status === 403) { onSessionInvalid(); return }
      if (!response.ok) throw new Error('rejected')
      setPower(await response.json() as Power)
      setMessage(action === 'start' ? '켜기 요청을 보냈습니다. 보통 30초~1분 걸립니다.' : '끄기 요청을 보냈습니다.')
    } catch { setMessage('요청 결과를 확인하지 못했습니다. 잠시 뒤 상태를 다시 확인해 주세요.') } finally { setBusy(false) }
  }
  if (hidden) return null
  const state = power?.agent_online ? power.reported_state : 'UNKNOWN'
  const on = power?.desired === 'ON'
  const until = on && power?.run_until ? new Date(power.run_until).toLocaleTimeString('ko-KR', { hour: '2-digit', minute: '2-digit' }) : null
  return <section id="runtime-power" className="lc-card lc-step lc-power" aria-labelledby="runtime-power-heading" data-state={state}>
    <div className="lc-step-head"><span className="lc-num">1</span><div><h3 id="runtime-power-heading">설비 전원</h3>
      <p id="runtime-power-state" role="status"><strong data-tone={state === 'RUNNING' ? 'good' : state === 'ERROR' ? 'bad' : 'unknown'}>{powerText[state] ?? state}</strong>{until ? ` · ${until}에 자동으로 꺼짐` : ''}{state === 'ERROR' && power?.reported_detail ? ` · ${power.reported_detail}` : ''}</p></div></div>
    {canChange ? <div className="lc-switch" role="group" aria-label="설비 전원">
      <button id="runtime-power-start" type="button" aria-pressed={on} disabled={busy || on} onClick={() => void change('start')}>ON</button>
      <button id="runtime-power-stop" type="button" aria-pressed={!on} disabled={busy || !on} onClick={() => void change('stop')}>OFF</button>
    </div> : <p className="lc-hint">설비 관리자만 켜고 끌 수 있습니다.</p>}
    {message && <p className="lc-hint lc-full" role="status">{message}</p>}
  </section>
}

/** Pick a released work order and load it onto the line (LOAD_JOB through the work-order workflow). */
function JobCard({ live, source, session, workflow, tracked, selectedOrder, onSelectOrder, onSessionInvalid }: { live: LiveLine; source: Source; session: Session; workflow: WorkOrderWorkflow; tracked: TrackedCommand | null; selectedOrder: WorkOrder | null; onSelectOrder: (order: WorkOrder | null) => void; onSessionInvalid: () => void }) {
  const [orders, setOrders] = useState<WorkOrder[] | null>(null)
  const [revision, setRevision] = useState(0)
  const [confirmation, setConfirmation] = useState<(BindBody & { order_id: string; epoch: string }) | null>(null)
  const [autoEnd, setAutoEnd] = useState(true)
  const [startWhenReady, setStartWhenReady] = useState(true)
  useEffect(() => {
    const controller = new AbortController()
    mesRequest(`${mesPaths.order}?limit=50`, controller.signal).then(value => {
      setOrders(orderPage(value).items.filter(item => item.status === 'RELEASED' && item.line_id === source.line_id))
    }).catch(cause => { if (!controller.signal.aborted && (cause?.status === 401 || cause?.status === 403)) onSessionInvalid() })
    return () => controller.abort()
  }, [source.line_id, revision, onSessionInvalid])
  const allowed = session.roles.some(role => ['operator', 'production_manager'].includes(role))
  const values = live.controlValues
  const order = selectedOrder && selectedOrder.status === 'RELEASED' ? selectedOrder : null
  const reason = !allowed ? '이 계정은 작업을 불러올 권한이 없습니다.' : session.must_change_password ? '비밀번호를 먼저 변경해 주세요.'
    : workflow.attempt || (tracked && !(terminal(tracked.result) || tracked.phase === 'REJECTED')) ? '앞선 요청의 결과를 먼저 확인해 주세요.'
      : order && workflow.orderBlocked(order.id) ? '같은 작업지시의 원래 요청 기록을 먼저 확인해 주세요.'
        : !order ? '불러올 작업지시를 고르세요.' : order.line_id !== source.line_id ? '작업지시의 라인과 현재 라인이 다릅니다.'
          : live.blockedReason || (values['Line.ControlMode'] !== 1 ? '원격 제어 모드가 필요합니다.' : values['Line.State'] !== 0 || values['Line.WipCount'] !== 0 ? '라인이 정지 상태이고 재공이 0개여야 새 작업을 불러올 수 있습니다.' : !screenUInt64(values['Line.ControlVersion']) || !workflowId(values['Line.CounterEpoch']) ? '제어 버전과 카운터 구간을 확인해야 합니다.' : '')
  const changed = confirmation && (confirmation.order_id !== order?.id || confirmation.epoch !== values['Line.CounterEpoch'] || confirmation.line_id !== source.line_id || confirmation.source_id !== source.source_id || confirmation.stream_id !== source.stream_id || confirmation.expected_work_order_version !== order?.row_version || confirmation.expected_control_version !== values['Line.ControlVersion'])
  const loaded = workflow.attempt?.kind === 'bind' && workflow.attempt.loadResult?.status === 'SUCCEEDED'
  return <section id="work-order-binding" className="lc-card lc-step" aria-labelledby="binding-heading">
    <div className="lc-step-head"><span className="lc-num">2</span><h3 id="binding-heading">작업</h3>{loaded && <span className="lc-chip" data-tone="good">불러옴</span>}</div>
    <div className="lc-job-row">
      <div className="lc-field"><label htmlFor="binding-order-select">배포된 작업지시</label>
        <select id="binding-order-select" value={order?.id ?? ''} onChange={event => { onSelectOrder(orders?.find(item => item.id === event.target.value) ?? null); setConfirmation(null) }}>
          <option value="">{orders === null ? '불러오는 중…' : orders.length ? '작업지시를 고르세요' : '배포된 작업지시가 없습니다'}</option>
          {orders?.map(item => <option key={item.id} value={item.id}>{item.id} · {item.item_id} · {item.target_quantity.toLocaleString('ko-KR')}개</option>)}
        </select>
      </div>
      <button type="button" className="lc-link" onClick={() => setRevision(value => value + 1)}>새로 조회</button>
      <button id="binding-prepare" type="button" className="lc-button primary" disabled={!!reason} onClick={() => order && setConfirmation({ command_id: crypto.randomUUID(), order_id: order.id, epoch: values['Line.CounterEpoch'] as string, line_id: source.line_id, source_id: source.source_id, stream_id: source.stream_id, expected_work_order_version: order.row_version, expected_control_version: values['Line.ControlVersion'] as string })}>작업 불러오기</button>
    </div>
    {order && <p id="binding-order" className="lc-hint">{order.id} · {order.item_id} · {order.recipe_version_id} · 목표 {order.target_quantity.toLocaleString('ko-KR')}개</p>}
    {reason && order && <p id="binding-reason" className="lc-reason">{reason}</p>}
    <div className="lc-checks">
      <label><input id="binding-auto-end" type="checkbox" checked={autoEnd} onChange={event => setAutoEnd(event.target.checked)} /> 목표 도달 시 자동 종료</label>
      <label><input id="binding-start-when-ready" type="checkbox" checked={startWhenReady} onChange={event => setStartWhenReady(event.target.checked)} /> 준비되면 자동 시작</label>
    </div>
    {confirmation && <div className="lc-confirm" role="group" aria-label="작업 불러오기 확인">
      <strong>{order?.id}를 라인에 불러올까요?</strong>
      <span>공정 설정과 목표 수량을 PLC로 보냅니다.{startWhenReady ? ' 예열이 끝나면 자동으로 운전을 시작합니다.' : ''}</span>
      {changed && <span className="lc-reason">확인하는 동안 라인·작업지시·제어 버전이 바뀌었습니다. 취소 후 다시 확인해 주세요.</span>}
      <div><button id="binding-submit" type="button" className="lc-button primary" disabled={!!reason || !!changed} onClick={() => { if (!reason && !changed && order) { void workflow.submit('bind', { command_id: confirmation.command_id, line_id: confirmation.line_id, source_id: confirmation.source_id, stream_id: confirmation.stream_id, expected_work_order_version: confirmation.expected_work_order_version, expected_control_version: confirmation.expected_control_version, auto_end_at_target: autoEnd, start_when_ready: startWhenReady, work_order_id: confirmation.order_id, actor: session.login }, order); setConfirmation(null) } }}>불러오기 요청</button>
        <button id="binding-cancel-confirm" type="button" className="lc-button" onClick={() => setConfirmation(null)}>취소</button></div>
    </div>}
    <LoadTracker source={source} workflow={workflow} onSessionInvalid={onSessionInvalid} />
  </section>
}
