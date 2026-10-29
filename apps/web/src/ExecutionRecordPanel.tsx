import { useEffect, useRef, useState } from 'react'
import { MesHTTPError, mesRequest, parameterText } from './mesApi'
import { archiveBlocked, ExecutionArchiveError, executionPath, parseExecution } from './executionApi'
import type { ExecutionView } from './executionApi'
import type { WorkOrderWorkflow } from './useWorkOrderWorkflow'
import type { BindReceipt, WorkflowAttempt } from './workflow'

const statusLabels = { PENDING: '최종 저장 정산 대기', SETTLED: '마지막 저장 정산 일치', MISMATCH: '마지막 저장 정산 불일치', CONFLICT: '마지막 저장 정산 충돌' }
export default function ExecutionRecordPanel({ original, login, workflow, archived = false, onSessionInvalid }: { original: WorkflowAttempt; login: string; workflow: WorkOrderWorkflow; archived?: boolean; onSessionInvalid: () => void }) {
  const receipt = original.record?.receipt
  const binding = receipt && 'execution_id' in receipt ? receipt as BindReceipt : null
  const [view, setView] = useState<ExecutionView | null>(null)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const [currentRead, setCurrentRead] = useState(false)
  const controller = useRef<AbortController | null>(null)
  const active = useRef(false)
  useEffect(() => () => { controller.current?.abort() }, [binding, login])
  if (!binding) return null
  const reason = !currentRead || !view ? '실행 기록을 수동 조회한 뒤 저장된 정산 근거를 확인해 주세요.' : archiveBlocked(view, original, login)
  async function read() {
    if (!binding || active.current) return
    active.current = true
    const request = new AbortController()
    controller.current = request
    setBusy(true); setCurrentRead(false); setMessage('')
    try {
      const response = await mesRequest(executionPath(binding), request.signal)
      if (request.signal.aborted) return
      const parsed = parseExecution(response, binding)
      setView(parsed); setCurrentRead(true)
      if (!archived && parsed.load) workflow.recordLoadResult(parsed.load.result)
    } catch (cause) {
      if (request.signal.aborted) return
      setMessage('실행 조회 결과를 확인할 수 없습니다. 이전 표시는 과거 조회 기록이며 원래 요청을 보존합니다.')
      if (cause instanceof MesHTTPError && [401, 403].includes(cause.status)) onSessionInvalid()
    } finally { active.current = false; setBusy(false) }
  }
  async function archive() {
    if (active.current || reason || login !== original.intent.actor) return
    active.current = true
    setBusy(true); setCurrentRead(false); setMessage('')
    try { await workflow.archiveExecution() }
    catch (cause) { setMessage(cause instanceof ExecutionArchiveError ? cause.message : '저장 정산 재조회의 원래 실행 근거를 확인하지 못해 보관하지 않았습니다. 원래 요청을 유지합니다.') }
    finally { active.current = false; setBusy(false) }
  }
  return <section className="execution-record" data-execution-record={binding.execution_id} aria-label="원래 실행과 저장 정산">
    <h3>원래 실행 · 저장 정산</h3><p>실행 {binding.execution_id}</p><p className="form-help">수동 조회한 저장 기록입니다. 실시간 운전 허가나 새 작업 허가를 뜻하지 않습니다. 다음 연결은 최신 설비 증거와 서버 정산을 다시 검증합니다.</p>
    <button id={archived ? undefined : 'execution-refresh'} type="button" className="button secondary" disabled={busy || workflow.checking} onClick={() => void read()}>원래 실행 기록 조회</button>
    {message && <p className="message caution" role="status">{message}</p>}
    {view && <div data-execution-details data-current-read={currentRead}>
      {!currentRead && <p className="message caution">과거 조회 기록 · 현재 조회 확인 불가</p>}
      <p data-settlement-status={view.settlement.status}>{statusLabels[view.settlement.status]}</p>
      <dl className="command-facts"><div><dt>작업지시 · 설정</dt><dd>{view.execution.work_order_id} · {view.execution.recipe_version}</dd></div><div><dt>원래 카운터 구간</dt><dd>{view.execution.counter_epoch}</dd></div><div><dt>공정 조건</dt><dd>{parameterText(view.execution.parameters)}</dd></div><div><dt>마지막 정산 확인 시각</dt><dd>{view.settlement.checked_at ?? '저장된 확인 기록 없음'}</dd></div><div><dt>최종 요약 수신 시각</dt><dd>{view.settlement.received_at ?? '수신 기록 없음'}</dd></div><div><dt>원래 작업 불러오기</dt><dd>{view.load?.result.status ?? '관리 연결 기록 없음'}{view.load?.collision_latched ? ' · 충돌 기록' : ''}</dd></div><div><dt>현재 저장 집계 · 양품 / 불량 / 폐기</dt><dd data-execution-totals>{view.totals.good_quantity} / {view.totals.reject_quantity} / {view.totals.scrap_quantity}</dd></div><div><dt>마지막 요약 · 양품 / 불량 / 폐기</dt><dd>{view.settlement.summary ? `${view.settlement.summary.good_quantity} / ${view.settlement.summary.reject_quantity} / ${view.settlement.summary.scrap_quantity}` : '요약 없음'}</dd></div></dl>
      {view.settlement.summary && <p>마지막 요약의 구간·설정: {view.settlement.summary.counter_epoch} · {view.settlement.summary.recipe_version} · {parameterText(view.settlement.summary.parameters, ' / ')} · {view.settlement.summary.final ? '최종 요약' : '진행 중 요약'} · 종료 {view.settlement.summary.ended_at ?? '미확정'}</p>}
      <p>저장 정산 사유: {view.settlement.reason_code}</p><p>조회 시점 수집원: {view.source.control_restricted ? '제한 있음' : '저장된 제한 플래그 없음'} · 대조 완료 {view.source.verified_through_seq} / 수신 {view.source.latest_ingested_seq}</p>
    </div>}
    {!archived && <><p className="form-help">{reason || '원래 계정의 명시적 클릭에서 다시 조회하고, 일치하는 최종 기록만 이 세션 이력에 보관합니다.'}</p><button id="execution-archive" type="button" className="button secondary" disabled={busy || workflow.checking || !!reason || login !== original.intent.actor} onClick={() => void archive()}>저장 정산 재확인 후 기록 보관</button></>}
  </section>
}
