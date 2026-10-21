import { useState } from 'react'
import type { WorkOrderWorkflow } from './useWorkOrderWorkflow'
import { workflowIdentity, workflowId } from './workflow'
import type { WorkflowKind } from './workflow'
import ExecutionRecordPanel from './ExecutionRecordPanel'

export default function WorkflowTracker({ workflow, login, onSessionInvalid }: { workflow: WorkOrderWorkflow; login: string; onSessionInvalid: () => void }) {
  const [kind, setKind] = useState<WorkflowKind>('release')
  const [order, setOrder] = useState('')
  const [id, setId] = useState('')
  const attempt = workflow.attempt
  return <section className="mes-card" id="workflow-tracker" aria-labelledby="workflow-heading">
    <h2 id="workflow-heading">작업지시 요청 기록</h2>
    <p className="form-help">응답 불명일 때 원래 ID만 조회합니다. 배포와 연결 등록은 PLC 실행 완료가 아닙니다. 새로고침 후에는 작업지시와 원래 요청 ID로 조회해 주세요.</p>
    {attempt ? <>
      <p id="workflow-status" className="message caution" role="status" data-state={attempt.phase}>{attempt.phase === 'sending' ? '요청 응답 확인 중' : attempt.message}</p>
      <dl className="command-facts"><div><dt>요청 종류 · 작업지시</dt><dd>{attempt.kind === 'release' ? '배포' : '실행 연결'} · {attempt.intent.work_order_id}</dd></div><div><dt>원래 요청 ID</dt><dd id="workflow-original-id">{workflowIdentity(attempt.intent)}</dd></div><div><dt>원래 요청자</dt><dd id="workflow-actor">{attempt.knownIntent ? attempt.intent.actor : '저장 기록 확인 전'}</dd></div></dl>
      {attempt.knownIntent && login !== attempt.intent.actor && <p className="message caution">다른 계정의 원래 요청입니다. 권한이 허용하는 저장 기록 조회만 가능합니다.</p>}
      {attempt.record && 'execution_id' in attempt.record.receipt && <p>연결된 실행: <strong id="workflow-execution">{attempt.record.receipt.execution_id}</strong> · 작업 불러오기 결과는 원래 명령 ID로 확인합니다.</p>}
      <button id="workflow-lookup" type="button" className="button secondary" disabled={workflow.checking} onClick={() => void workflow.lookup()}>{workflow.checking ? '원래 기록 조회 중…' : '원래 요청 기록 조회'}</button>
      {attempt.kind === 'release' && attempt.phase === 'confirmed' && <button id="workflow-acknowledge" type="button" className="button secondary" onClick={workflow.acknowledgeRelease}>배포 기록 확인 · 다음 업무 준비</button>}
      {attempt.record && 'execution_id' in attempt.record.receipt && <ExecutionRecordPanel key={attempt.record.receipt.command_id} original={attempt} login={login} workflow={workflow} onSessionInvalid={onSessionInvalid} />}
    </> : <form id="workflow-recovery" className="mes-form-grid" onSubmit={event => { event.preventDefault(); if (workflowId(order) && workflowId(id)) void workflow.recover(kind, order, id) }}>
      <div><label htmlFor="workflow-recovery-kind">조회할 요청</label><select id="workflow-recovery-kind" value={kind} onChange={event => setKind(event.target.value as WorkflowKind)}><option value="release">배포</option><option value="bind">실행 연결</option></select></div>
      <div><label htmlFor="workflow-recovery-order">작업지시 ID</label><input id="workflow-recovery-order" required maxLength={128} value={order} onChange={event => setOrder(event.target.value)} /></div>
      <div><label htmlFor="workflow-recovery-id">원래 요청 ID</label><input id="workflow-recovery-id" required maxLength={128} value={id} onChange={event => setId(event.target.value)} /></div>
      <button id="workflow-recovery-submit" type="submit" className="button secondary" disabled={workflow.checking}>저장 기록 조회</button>
    </form>}
    {workflow.history.length > 0 && <section id="workflow-history" aria-label="현재 세션의 보관 이력"><h3>현재 세션의 보관 이력</h3><p className="form-help">원래 요청과 조회 근거를 이 세션 메모리에 보관합니다. 새로고침 후 영구 보존되는 브라우저 이력이 아닙니다.</p>{workflow.history.map(item => <details key={workflowIdentity(item.attempt.intent)} data-archived-command={workflowIdentity(item.attempt.intent)}><summary>{item.attempt.intent.work_order_id} · {workflowIdentity(item.attempt.intent)}</summary><p>원래 요청자 {item.attempt.intent.actor} · 실행 {item.execution.execution.execution_id}</p><p>보관 당시 정산 확인 {item.execution.settlement.checked_at} · 양품 {item.execution.totals.good_quantity} / 불량 {item.execution.totals.reject_quantity} / 폐기 {item.execution.totals.scrap_quantity}</p><ExecutionRecordPanel original={item.attempt} login={login} workflow={workflow} archived onSessionInvalid={onSessionInvalid} /></details>)}</section>}
  </section>
}
