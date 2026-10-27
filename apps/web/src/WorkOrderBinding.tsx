import { useState } from 'react'
import type { Session } from './auth'
import { terminal } from './commands'
import type { CommandScope, TrackedCommand } from './commands'
import type { WorkOrder } from './mesApi'
import { screenUInt64 } from './screenState'
import type { WorkOrderWorkflow } from './useWorkOrderWorkflow'
import { workflowId } from './workflow'
import type { BindBody } from './workflow'

export default function WorkOrderBinding({ order, source, session, blockedReason, values, workflow, tracked }: { order: WorkOrder | null; source: CommandScope; session: Session; blockedReason: string; values: Record<string, unknown>; workflow: WorkOrderWorkflow; tracked: TrackedCommand | null }) {
  const [confirmation, setConfirmation] = useState<(BindBody & { order_id: string; epoch: string }) | null>(null)
  // PLC-owned job policies (CX-33/34): end at the good target, start once warm-up reaches READY.
  const [autoEnd, setAutoEnd] = useState(true)
  const [startWhenReady, setStartWhenReady] = useState(true)
  if (!session.roles.some(role => ['operator', 'production_manager'].includes(role))) return null
  const reason = session.must_change_password ? '비밀번호를 먼저 변경해 주세요.' : workflow.attempt || (tracked && !(terminal(tracked.result) || tracked.phase === 'REJECTED')) ? '기존 요청의 원래 기록과 결과를 먼저 확인해 주세요.' : order && workflow.orderBlocked(order.id) ? '같은 작업지시의 원래 요청 기록을 먼저 확인해 주세요.' : !order || order.status !== 'RELEASED' ? '작업지시 화면에서 배포된 작업지시를 선택해 주세요.' : order.line_id !== source.line_id ? '선택한 작업지시의 라인과 현재 조회 라인이 다릅니다.' : blockedReason || (values['Line.ControlMode'] !== 1 ? '원격 제어 모드가 필요합니다.' : values['Line.State'] !== 0 || values['Line.WipCount'] !== 0 ? '정지 상태와 재공 0개를 확인해야 합니다.' : !screenUInt64(values['Line.ControlVersion']) || !workflowId(values['Line.CounterEpoch']) ? '제어 버전과 카운터 구간을 확인해야 합니다.' : '')
  const changed = confirmation && (confirmation.order_id !== order?.id || confirmation.epoch !== values['Line.CounterEpoch'] || confirmation.line_id !== source.line_id || confirmation.source_id !== source.source_id || confirmation.stream_id !== source.stream_id || confirmation.expected_work_order_version !== order?.row_version || confirmation.expected_control_version !== values['Line.ControlVersion'])
  return <section id="work-order-binding" className="mes-card" aria-labelledby="binding-heading"><h2 id="binding-heading">작업지시 실행 연결</h2>
    <p className="form-help">배포된 작업지시를 현재 라인에 연결하고 작업 불러오기를 요청합니다. 운전 시작과 PLC 실행 완료는 별도로 확인합니다. 기존 실행의 정산·예약은 서버가 다시 검증합니다.</p>
    {order ? <div id="binding-order" className="binding-selected"><span>선택한 작업지시</span><strong>{order.id}</strong><small>{order.item_id} · {order.recipe_version_id} · 목표 {order.target_quantity.toLocaleString('ko-KR')}개</small>{!reason && !confirmation && <p>다음 단계: 아래 "실행 연결 내용 확인"을 누르세요.</p>}</div> : <p className="binding-empty">아직 고른 작업지시가 없습니다. 작업지시 화면에서 배포된 작업지시의 "실행 연결 준비"를 누르세요.</p>}
    <button id="binding-prepare" className="button primary" type="button" disabled={!!reason} onClick={() => setConfirmation({ command_id: crypto.randomUUID(), order_id: order!.id, epoch: values['Line.CounterEpoch'] as string, line_id: source.line_id, source_id: source.source_id, stream_id: source.stream_id, expected_work_order_version: order!.row_version, expected_control_version: values['Line.ControlVersion'] as string })}>실행 연결 내용 확인</button>
    {reason && order && <p id="binding-reason" className="message caution">지금은 연결할 수 없습니다: {reason}</p>}
    {confirmation && <div role="group" aria-label="실행 연결 확인"><p>작업지시 {order?.id}를 현재 라인에 연결하고 설정·목표 수량을 불러오시겠습니까?</p><p>현재 수집원 {source.source_id} / {source.stream_id}</p><fieldset className="binding-options"><legend>작업 자동화</legend><label className="check"><input id="binding-auto-end" type="checkbox" checked={autoEnd} onChange={event => setAutoEnd(event.target.checked)} /> 목표 양품 수량에 닿으면 PLC가 자동으로 종료</label><label className="check"><input id="binding-start-when-ready" type="checkbox" checked={startWhenReady} onChange={event => setStartWhenReady(event.target.checked)} /> 예열이 끝나 준비 상태가 되면 PLC가 자동으로 운전 시작</label><p className="input-help">자동 시작은 예열 중 안전 입력이나 모드가 바뀌면 PLC가 취소합니다.</p></fieldset>{changed && <p className="message caution">확인 중 라인·작업지시·제어 버전이 바뀌었습니다. 취소 후 다시 확인해 주세요.</p>}<button id="binding-submit" className="button primary" type="button" disabled={!!reason || !!changed} onClick={() => { if (!reason && !changed && order) { void workflow.submit('bind', { command_id: confirmation.command_id, line_id: confirmation.line_id, source_id: confirmation.source_id, stream_id: confirmation.stream_id, expected_work_order_version: confirmation.expected_work_order_version, expected_control_version: confirmation.expected_control_version, auto_end_at_target: autoEnd, start_when_ready: startWhenReady, work_order_id: confirmation.order_id, actor: session.login }, order); setConfirmation(null) } }}>실행 연결 · 작업 불러오기 요청</button><button id="binding-cancel-confirm" className="button secondary" type="button" onClick={() => setConfirmation(null)}>확인 닫기</button></div>}
  </section>
}
