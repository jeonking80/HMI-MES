import { useEffect, useState } from 'react'
import type { Session } from './auth'
import { mesPaths, mesRequest, orderPage, recipePage } from './mesApi'
import type { WorkOrder } from './mesApi'
import type { WorkspacePage } from './navigation'
import type { WorkOrderWorkflow } from './useWorkOrderWorkflow'

/** Where a guide step sends the user: a page and the card to bring into view. */
export interface GuideTarget { page: WorkspacePage; anchor: string }

interface Facts { recipes: boolean | null; released: boolean | null; runtime: string | null }

interface Step { key: string; label: string; todo: string; action: string; target: GuideTarget; done: boolean | null }

const POLL_MS = 5000

async function readFacts(signal: AbortSignal): Promise<Facts> {
  const [recipes, orders, runtime] = await Promise.allSettled([
    mesRequest(`${mesPaths.recipe}?limit=1`, signal).then(value => recipePage(value).items.length > 0),
    mesRequest(`${mesPaths.order}?limit=50`, signal).then(value => orderPage(value).items.some(order => order.status === 'RELEASED')),
    fetch('/api/v1/runtime-control', { credentials: 'same-origin', cache: 'no-store', signal }).then(async response => {
      // Without the on/off feature the runtime is managed outside this screen.
      if (response.status === 404) return 'NOT_MANAGED'
      if (!response.ok) return null
      const body = await response.json() as { reported_state?: unknown }
      return typeof body.reported_state === 'string' ? body.reported_state : null
    }),
  ])
  return {
    recipes: recipes.status === 'fulfilled' ? recipes.value : null,
    released: orders.status === 'fulfilled' ? orders.value : null,
    runtime: runtime.status === 'fulfilled' ? runtime.value : null,
  }
}

/**
 * The production flow as one guided path: which step is done, what to do now, and a button
 * that opens the right card. Done/not-done comes from the server, never from a click alone;
 * an unknown fact is shown as unknown rather than as done.
 */
export default function GuideFlow({ session, selectedOrder, workflow, onGo }: { session: Session; selectedOrder: WorkOrder | null; workflow: WorkOrderWorkflow; onGo: (target: GuideTarget) => void }) {
  const [facts, setFacts] = useState<Facts>({ recipes: null, released: null, runtime: null })
  const [collapsed, setCollapsed] = useState(false)

  useEffect(() => {
    const controller = new AbortController()
    const tick = () => { readFacts(controller.signal).then(value => { if (!controller.signal.aborted) setFacts(value) }).catch(() => undefined) }
    tick()
    const timer = window.setInterval(tick, POLL_MS)
    return () => { controller.abort(); window.clearInterval(timer) }
  }, [session.csrf_token])

  const orderId = selectedOrder?.id
  const bindAttempt = workflow.attempt?.kind === 'bind' && workflow.attempt.intent.work_order_id === orderId ? workflow.attempt : null
  const bound = !orderId ? false
    : bindAttempt ? bindAttempt.loadResult?.status === 'SUCCEEDED'
      : workflow.history.some(item => item.attempt.kind === 'bind' && item.attempt.intent.work_order_id === orderId && item.attempt.loadResult?.status === 'SUCCEEDED')
  const runtimeOn = facts.runtime === null ? null : facts.runtime === 'RUNNING' || facts.runtime === 'NOT_MANAGED'

  const steps: Step[] = [
    { key: 'recipe', label: '공정 설정', todo: '품목과 온도·속도를 정한 공정 설정을 등록하세요.', action: '공정 설정 등록하러 가기', target: { page: 'drafts', anchor: 'mes-recipes' }, done: facts.recipes },
    { key: 'order', label: '작업지시 배포', todo: '공정 설정을 골라 작업지시를 만들고 "등록하고 바로 배포"를 누르세요.', action: '작업지시 만들러 가기', target: { page: 'drafts', anchor: 'mes-orders' }, done: facts.released },
    { key: 'power', label: '설비 켜기', todo: '설비 전원 카드에서 "설비 켜기"를 누르고 "동작 중"이 될 때까지 기다리세요.', action: '설비 전원으로 가기', target: { page: 'operations', anchor: 'runtime-power' }, done: runtimeOn },
    { key: 'select', label: '작업지시 선택', todo: '배포된 작업지시 줄의 "실행 연결 준비"를 누르세요.', action: '작업지시 목록으로 가기', target: { page: 'drafts', anchor: 'mes-orders' }, done: !!selectedOrder && selectedOrder.status === 'RELEASED' },
    { key: 'bind', label: '라인 연결', todo: `${orderId ?? '작업지시'}를 라인에 연결하세요. "실행 연결 내용 확인" → 체크 2개 → "실행 연결 · 작업 불러오기 요청" 순서입니다.`, action: '실행 연결 카드로 가기', target: { page: 'operations', anchor: 'work-order-binding' }, done: bound },
    { key: 'run', label: '생산 · 결과', todo: '라인 공정 그림에서 양품·불량 수가 오르는지 보고, 끝나면 실적·보고서에서 결과를 확인하세요.', action: '공정 현황 보기', target: { page: 'operations', anchor: 'process-heading' }, done: false },
  ]
  const current = steps.find(step => step.done !== true) ?? steps[steps.length - 1]
  const unknown = current.done === null

  return <section id="guide-flow" className="guide-flow" aria-labelledby="guide-heading">
    <div className="guide-head">
      <h2 id="guide-heading">진행 안내</h2>
      <button type="button" className="guide-toggle" aria-expanded={!collapsed} onClick={() => setCollapsed(value => !value)}>{collapsed ? '펼치기' : '접기'}</button>
    </div>
    <ol className="guide-steps">
      {steps.map((step, index) => <li key={step.key} data-state={step === current ? 'current' : step.done === true ? 'done' : step.done === null ? 'unknown' : 'todo'}>
        <button type="button" onClick={() => onGo(step.target)} aria-current={step === current ? 'step' : undefined}>
          <span className="guide-num" aria-hidden="true">{step.done === true && step !== current ? '✓' : index + 1}</span>
          <span>{step.label}</span>
        </button>
      </li>)}
    </ol>
    {!collapsed && <div className="guide-now" role="status">
      <p><strong>지금 할 일 · {current.label}</strong>{unknown ? ' (상태를 확인하지 못했습니다)' : ''}<br />{current.todo}</p>
      <button type="button" className="button primary" onClick={() => onGo(current.target)}>{current.action}</button>
    </div>}
  </section>
}
