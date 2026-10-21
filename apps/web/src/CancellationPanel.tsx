import { useState } from 'react'
import type { CancellationWorkflow } from './useCancellation'
import { workflowId } from './workflow'

export default function CancellationPanel({ cancellation, login }: { cancellation: CancellationWorkflow; login: string }) {
  const [order, setOrder] = useState('')
  const [id, setId] = useState('')
  const ledger = cancellation.ledger
  return <section className="mes-card" id="cancellation-panel" aria-labelledby="cancellation-heading"><h2 id="cancellation-heading">작업지시 취소 기록</h2>
    <p className="form-help">설비에 연결되지 않은 작업만 취소할 수 있습니다. 원래 요청 ID의 기록만 조회하며 설비 정지·작업 종료를 대신하지 않습니다.</p>
    {ledger ? <>
      <p id="cancellation-status" className="message caution" data-state={ledger.phase} role="status">{ledger.message}</p>
      <dl className="command-facts"><div><dt>작업지시</dt><dd id="cancellation-order">{ledger.reference.work_order_id}</dd></div><div><dt>원래 취소 요청 ID</dt><dd id="cancellation-original-id">{ledger.reference.cancellation_id}</dd></div><div><dt>원래 요청자</dt><dd id="cancellation-actor">{ledger.intent?.actor ?? '저장 기록 확인 전'}</dd></div><div><dt>원래 작업지시 버전</dt><dd>{ledger.intent?.expected_row_version ?? '저장 기록 확인 전'}</dd></div></dl>
      {ledger.intent && ledger.intent.actor !== login && <p className="message caution">다른 계정의 원래 요청입니다. 권한이 허용하는 저장 기록 조회만 가능합니다.</p>}
      {ledger.receipt && <p id="cancellation-receipt">{ledger.phase !== 'CONFIRMED' ? '이전 확인 기록 · ' : ''}취소됨 (CANCELLED) · {ledger.receipt.cancelled_by} · {ledger.receipt.cancelled_at} · 버전 {ledger.receipt.row_version}</p>}
      {ledger.submission?.rejection && <p id="cancellation-rejection-evidence">원래 취소 요청의 거절 응답을 확인했습니다. 이후 저장 기록 조회 결과와 구분해 보관합니다.</p>}
      {ledger.observations && ledger.observations.length > 0 && <details id="cancellation-observations"><summary>원래 요청 확인 이력</summary><ol>{ledger.observations.map((item, index) => <li key={index}>{item.method === 'POST' ? '취소 요청 응답' : '저장 기록 조회'} · {item.outcome === 'UNCONFIRMED' ? '결과 확인 불가' : item.status === 404 ? '이 조회에서 저장 기록을 찾지 못함' : item.method === 'POST' && item.status === 409 ? '요청 거절 응답 확인' : item.status === 200 ? '저장 기록 확인' : '조회 결과 확인 불가'} · {item.observed_at}</li>)}</ol></details>}
      {cancellation.canPrepareAgain && <button id="cancellation-prepare-again" type="button" className="button secondary" onClick={() => void cancellation.prepareAgain()}>거절 기록 보관 후 작업 다시 확인</button>}
      <button id="cancellation-lookup" type="button" className="button secondary" disabled={cancellation.busy} onClick={() => void cancellation.lookup()}>{cancellation.busy ? '원래 취소 기록 조회 중…' : '원래 취소 기록 조회'}</button>
      {ledger.phase === 'CONFIRMED' && ledger.intent?.actor === login && <button id="cancellation-acknowledge" type="button" className="button secondary" onClick={cancellation.acknowledge}>취소 기록 확인 · 이 세션 이력에 보관</button>}
    </> : <form id="cancellation-recovery" className="mes-form-grid" onSubmit={event => { event.preventDefault(); if (workflowId(order) && workflowId(id)) void cancellation.recover(order, id) }}>
      <div><label htmlFor="cancellation-recovery-order">조회할 작업지시 ID</label><input id="cancellation-recovery-order" value={order} required maxLength={128} onChange={event => setOrder(event.target.value)} /></div><div><label htmlFor="cancellation-recovery-id">원래 취소 요청 ID</label><input id="cancellation-recovery-id" value={id} required maxLength={128} onChange={event => setId(event.target.value)} /></div><button id="cancellation-recovery-submit" className="button secondary" type="submit" disabled={cancellation.busy}>취소 저장 기록 조회</button>
    </form>}
    {cancellation.preparationMessage && <p id="cancellation-preparation-message" className="message caution" role="status">{cancellation.preparationMessage}</p>}
    {cancellation.rejectedHistory.length > 0 && <details id="cancellation-rejected-history"><summary>현재 세션의 취소 거절 이력 ({cancellation.rejectedHistory.length}건)</summary><p className="form-help">현재 페이지의 메모리 이력이며 새로고침 후 유지되지 않습니다. 거절 기록 보관은 취소 가능 여부를 확정하지 않습니다.</p>{cancellation.rejectedHistory.map(item => <article key={item.ledger.reference.cancellation_id} data-cancellation-rejected-id={item.ledger.reference.cancellation_id}><h3>{item.ledger.reference.work_order_id}</h3><p>{item.ledger.reference.cancellation_id} · {item.ledger.intent?.actor} · 원래 작업 버전 {item.ledger.intent?.expected_row_version}</p><p>거절 응답 확인 · {item.ledger.submission?.rejection?.observed_at} · 다시 확인한 작업 버전 {item.current_order.row_version}</p><ol>{item.ledger.observations?.map((observation, index) => <li key={index}>{observation.method === 'POST' ? '취소 요청 거절 응답' : observation.status === 404 ? '이 조회에서 저장 기록을 찾지 못함' : '저장 기록 조회'} · {observation.observed_at}</li>)}</ol></article>)}</details>}
    {cancellation.history.length > 0 && <details id="cancellation-history"><summary>현재 세션의 확인된 취소 이력 ({cancellation.history.length}건)</summary><p className="form-help">현재 페이지의 메모리 이력이며 새로고침 후 유지되지 않습니다.</p>{cancellation.history.map(item => <p key={item.reference.cancellation_id} data-cancellation-history-id={item.reference.cancellation_id}>{item.reference.work_order_id} · {item.reference.cancellation_id} · {item.receipt?.cancelled_by} · {item.receipt?.cancelled_at} · 취소됨 (CANCELLED)</p>)}</details>}
  </section>
}
