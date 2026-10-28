import { useEffect, useState } from 'react'
import type { FormEvent, ReactNode } from 'react'
import type { Session } from '../auth'
import type { RecipeVersion, WorkOrder } from '../mesApi'
import { MesHTTPError, validDate } from '../mesApi'
import { editOrder, endReasonLabels, eventLabels, fieldLabels, phaseLabels, readHistory, readOverview } from './orderApi'
import type { OrderRun } from './orderApi'
import type { EditBody, OrderEvent, OrderPhase, OrderSummary } from './orderApi'

type Filter = OrderPhase | 'ALL'
interface LineChoice { id: string; name: string }
interface Revision { order: string; body: EditBody; phase: 'sending' | 'unknown' | 'conflict' | 'invalid' | 'done'; message: string }

const filters: Filter[] = ['WAITING', 'RUNNING', 'DONE', 'DRAFT', 'CANCELLED', 'ALL']
const time = (value: string | null) => {
  if (!value) return ''
  const parts = Object.fromEntries(new Intl.DateTimeFormat('en-GB', { timeZone: 'Asia/Seoul', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).formatToParts(new Date(value)).map(part => [part.type, part.value]))
  return `${parts.month}/${parts.day} ${parts.hour}:${parts.minute}`
}
const settlementLabels = { PENDING: '정산 전', SETTLED: '정산 완료', MISMATCH: '정산 불일치', CONFLICT: '정산 충돌' }

/** Events and runs on one time line, oldest first; each run replaces its bare "loaded" event. */
function timeline(events: OrderEvent[], runs: OrderRun[]) {
  const ordered = [...runs].sort((a, b) => a.bound_at.localeCompare(b.bound_at))
  return [
    ...events.filter(event => event.action !== 'BIND_EXECUTION').map((event, index) => ({ key: `event-${index}`, at: event.occurred_at, body: <><strong>{eventLabels[event.action] ?? event.action}</strong>{event.changes?.map(change => ` · ${fieldLabels[change.field] ?? change.field} ${change.before} → ${change.after}`).join('')} · {event.actor}</> })),
    ...ordered.map((run, index) => ({ key: run.execution_id, at: run.bound_at, body: <><strong>{index + 1}회째 실행</strong> · {settlementLabels[run.settlement_status]} · 양품 {run.good_quantity ?? '?'} · 불량 {run.reject_quantity ?? '?'} · {run.bound_by}</> })),
  ].sort((a, b) => a.at.localeCompare(b.at))
}

function progressOf(order: OrderSummary) {
  const run = order.execution
  if (order.phase === 'DONE' && run) return `양품 ${run.good_quantity ?? '?'} · 불량 ${run.reject_quantity ?? '?'}`
  if (order.phase === 'RUNNING' && run) return run.good_quantity === null ? '생산 중' : `생산 중 ${run.good_quantity}/${order.target_quantity}`
  if (order.phase === 'WAITING') {
    const waiting = order.queue_position !== null ? `대기열 ${order.queue_position + 1}번` : '불러오기 대기'
    if (!run || order.runs === 0) return waiting
    const last = `${order.runs}회 실행 · 최근 ${run.good_quantity ?? '?'}개 (불량 ${run.reject_quantity ?? '?'})`
    return order.queue_position !== null ? `${waiting} · ${last}` : last
  }
  if (order.phase === 'CANCELLED') return '취소됨'
  return '배포 전'
}

export default function OrderBoard({ session, lines, recipes, canWrite, canBind, refreshKey, onSessionInvalid, onRelease, onCancel, onBind, onOpenControl, onReports, onNew, isBlocked, creating, onCloseCreate, createPanel, focusId = null }: {
  session: Session; lines: LineChoice[] | null; recipes: RecipeVersion[]; canWrite: boolean; canBind: boolean; refreshKey: string
  onSessionInvalid: () => void; onRelease: (order: WorkOrder) => void; onCancel: (order: WorkOrder) => void; onBind: (order: WorkOrder) => void
  onOpenControl: () => void; onReports: () => void; onNew: () => void; isBlocked: (id: string) => boolean; focusId?: string | null
  creating: boolean; onCloseCreate: () => void; createPanel: ReactNode
}) {
  const [items, setItems] = useState<OrderSummary[] | null>(null)
  const [error, setError] = useState('')
  const [filter, setFilter] = useState<Filter>('WAITING')
  const [selectedId, setSelectedId] = useState<string | null>(null)
  // An order just registered elsewhere on this page is shown at once.
  const [seenFocus, setSeenFocus] = useState<string | null>(null)
  if (focusId && focusId !== seenFocus) { setSeenFocus(focusId); setSelectedId(focusId) }
  const [tick, setTick] = useState(0)
  const [history, setHistory] = useState<{ id: string; events: OrderEvent[]; runs: OrderRun[] } | null>(null)
  const [editing, setEditing] = useState<string | null>(null)
  const [form, setForm] = useState({ recipe: '', target: '', date: '' })
  const [revision, setRevision] = useState<Revision | null>(null)

  const lineIds = (lines ?? []).map(line => line.id).join('|')
  useEffect(() => {
    if (!lineIds) return
    const controller = new AbortController()
    Promise.all(lineIds.split('|').map(line => readOverview(line, controller.signal)))
      .then(lists => { setItems(lists.flat()); setError('') })
      .catch(cause => {
        if (controller.signal.aborted) return
        if (cause instanceof MesHTTPError && (cause.status === 401 || cause.status === 403)) onSessionInvalid()
        else setError('작업지시 현황을 불러오지 못했습니다. 잠시 후 자동으로 다시 조회합니다.')
      })
    return () => controller.abort()
  }, [lineIds, refreshKey, tick, onSessionInvalid])
  useEffect(() => { const timer = window.setInterval(() => setTick(value => value + 1), 5000); return () => window.clearInterval(timer) }, [])

  const counts = Object.fromEntries(filters.map(key => [key, key === 'ALL' ? items?.length ?? 0 : items?.filter(item => item.phase === key).length ?? 0]))
  // A picked order whose state changed (e.g. released) moves its tab along with it.
  const picked = selectedId ? items?.find(item => item.id === selectedId) : undefined
  if (picked && filter !== 'ALL' && picked.phase !== filter) setFilter(picked.phase)
  const shown = (items ?? []).filter(item => filter === 'ALL' || item.phase === filter)
  const selected = items?.find(item => item.id === selectedId) ?? shown[0] ?? null
  const historyKey = selected ? `${selected.id}|${selected.row_version}|${selected.runs}|${selected.execution?.settlement_status ?? ''}` : ''
  useEffect(() => {
    if (!historyKey) return
    const id = historyKey.split('|')[0]
    const controller = new AbortController()
    readHistory(id, controller.signal).then(value => setHistory({ id, ...value })).catch(() => {})
    return () => controller.abort()
  }, [historyKey])

  const lineName = (id: string) => lines?.find(line => line.id === id)?.name ?? id
  const recipeOf = (id: string) => recipes.find(item => item.id === id)
  const pendingRevision = revision && ['sending', 'unknown'].includes(revision.phase) ? revision : null

  function pick(order: OrderSummary) {
    onCloseCreate(); setSelectedId(order.id); if (editing !== order.id) setEditing(null)
    // On a narrow screen the detail sits below the list: bring it into view.
    if (window.matchMedia('(max-width: 900px)').matches) window.requestAnimationFrame(() => document.getElementById('wo-detail')?.scrollIntoView({ behavior: 'smooth', block: 'start' }))
  }
  function startEdit(order: OrderSummary) {
    setForm({ recipe: order.recipe_version_id, target: String(order.target_quantity), date: order.planned_date })
    setEditing(order.id)
    if (revision?.phase !== 'unknown' && revision?.phase !== 'sending') setRevision(null)
  }
  async function send(next: Revision) {
    setRevision({ ...next, phase: 'sending', message: '수정 내용을 저장하고 있습니다.' })
    try {
      await editOrder(next.order, next.body, session.csrf_token, new AbortController().signal)
      setRevision({ ...next, phase: 'done', message: `${next.order}를 수정했습니다.` })
      setEditing(null)
      setTick(value => value + 1)
    } catch (cause) {
      const status = cause instanceof MesHTTPError ? cause.status : 0
      if (status === 401 || status === 403) { onSessionInvalid(); return }
      if (status === 409) setRevision({ ...next, phase: 'conflict', message: '수정하지 못했습니다. 바뀐 내용이 없거나, 그 사이 라인에 불러왔거나 다른 사람이 먼저 고쳤습니다. 대기열에 들어 있으면 먼저 대기열에서 빼야 합니다.' })
      else if (status === 422 || status === 404) setRevision({ ...next, phase: 'invalid', message: '입력값을 확인해 주세요.' })
      else setRevision({ ...next, phase: 'unknown', message: '저장 결과를 확인하지 못했습니다. "같은 요청으로 다시 확인"을 누르세요. 같은 요청 ID라 두 번 적용되지 않습니다.' })
    }
  }
  function submit(event: FormEvent<HTMLFormElement>, order: OrderSummary) {
    event.preventDefault()
    const quantity = Number(form.target)
    if (!recipeOf(form.recipe) || !/^[1-9][0-9]*$/.test(form.target) || quantity > 4294967295 || !validDate(form.date)) {
      setRevision({ order: order.id, body: {} as EditBody, phase: 'invalid', message: '공정 설정, 목표 수량(1개 이상), 예정일을 확인해 주세요.' }); return
    }
    if (form.recipe === order.recipe_version_id && quantity === order.target_quantity && form.date === order.planned_date) {
      setRevision({ order: order.id, body: {} as EditBody, phase: 'invalid', message: '바뀐 내용이 없습니다.' }); return
    }
    void send({ order: order.id, phase: 'sending', message: '', body: { edit_id: crypto.randomUUID(), expected_row_version: order.row_version, recipe_version_id: form.recipe, target_quantity: quantity, planned_date: form.date } })
  }

  return <div className="wo-board">
    <div className="pl-toolbar wo-filters" role="tablist" aria-label="작업지시 상태">
      {filters.map(key => <button key={key} type="button" role="tab" aria-selected={filter === key} onClick={() => { setFilter(key); setSelectedId(null); setEditing(null) }}>{key === 'ALL' ? '전체' : phaseLabels[key]} <span>{counts[key]}</span></button>)}
      <span className="wo-spacer" />
      {canWrite && <button type="button" className="button primary" onClick={onNew}>+ 새 작업지시</button>}
    </div>
    {error && <p className="message error" role="alert">{error}</p>}
    {revision?.phase === 'done' && <p className="message success" role="status">{revision.message}</p>}
    <div className="rc-layout">
      <div className="rc-list">
        {!items ? <p role="status">작업지시 현황을 조회하고 있습니다.</p> : shown.length === 0 ? <p className="mes-empty">{filter === 'ALL' ? '등록된 작업지시가 없습니다.' : `${phaseLabels[filter as OrderPhase]} 작업지시가 없습니다.`}</p>
          : <ul id="wo-list">{shown.map(item => <li key={item.id}><button type="button" aria-pressed={!creating && selected?.id === item.id} onClick={() => pick(item)}>
            <span className="rc-name"><strong>{item.id}</strong><em className={`wo-phase wo-${item.phase.toLowerCase()}`}>{phaseLabels[item.phase]}</em></span>
            <small>{item.item_id} · {item.recipe_version_id} · 목표 {item.target_quantity}개</small>
            <small className="wo-progress">{progressOf(item)}</small>
          </button></li>)}</ul>}
      </div>
      <div className="rc-detail">
        {creating ? createPanel : !selected ? <div className="rc-card mes-empty">왼쪽에서 작업지시를 고르세요.</div> : <div className="rc-card" id="wo-detail">
          <div className="rc-card-head"><div>
            <h3>{selected.id} <em className={`wo-phase wo-${selected.phase.toLowerCase()}`}>{phaseLabels[selected.phase]}</em></h3>
            <p>{selected.item_id} · {lineName(selected.line_id)} · 예정일 {selected.planned_date}</p>
          </div></div>
          <div className="rc-params">
            <div><span>공정 설정</span><strong className="wo-small">{selected.recipe_version_id}</strong><small>{recipeOf(selected.recipe_version_id) ? `${recipeOf(selected.recipe_version_id)?.parameters.temperature_c} ℃ · ${recipeOf(selected.recipe_version_id)?.parameters.speed_m_per_min} m/min` : ''}</small></div>
            <div><span>{selected.execution ? (selected.phase === 'RUNNING' ? '양품 / 목표' : `최근 실행 (${selected.runs}회째) 양품 / 목표`) : '목표 양품'}</span><strong>{selected.execution ? <>{selected.execution.good_quantity ?? '?'}<small> / {selected.target_quantity}개</small></> : <>{selected.target_quantity}<small> 개</small></>}</strong>{selected.execution && <small>불량 {selected.execution.reject_quantity ?? '?'} · {settlementLabels[selected.execution.settlement_status]}{selected.execution.end_reason ? ` · ${endReasonLabels[selected.execution.end_reason] ?? selected.execution.end_reason}` : ''}</small>}</div>
          </div>

          {editing === selected.id && canWrite ? <form className="wo-edit" onSubmit={event => submit(event, selected)}>
            <h4>수정</h4>
            <p className="form-help">저장하면 이 작업지시에 바로 반영되고, 바뀐 값은 진행 기록에 남습니다.</p>
            <fieldset disabled={!!pendingRevision}><div className="mes-form-grid">
              <div><label htmlFor="wo-edit-recipe">공정 설정</label><select id="wo-edit-recipe" value={form.recipe} onChange={event => setForm(previous => ({ ...previous, recipe: event.target.value }))}>{recipes.filter(item => item.item_id === selected.item_id).map(item => <option key={item.id} value={item.id}>{item.recipe_id} {item.version} ({item.parameters.temperature_c} ℃ / {item.parameters.speed_m_per_min} m/min)</option>)}</select></div>
              <div><label htmlFor="wo-edit-target">목표 양품 수량 (개)</label><input id="wo-edit-target" type="number" min="1" step="1" value={form.target} onChange={event => setForm(previous => ({ ...previous, target: event.target.value }))} /></div>
              <div><label htmlFor="wo-edit-date">생산 예정일</label><input id="wo-edit-date" type="date" value={form.date} onChange={event => setForm(previous => ({ ...previous, date: event.target.value }))} /></div>
            </div>
            <div className="mes-form-actions"><button type="button" className="button secondary" onClick={() => setEditing(null)}>취소</button><button id="wo-edit-save" type="submit" className="button primary">수정 저장</button></div></fieldset>
          </form> : <div className="wo-actions">
            {selected.phase === 'WAITING' && canBind && <button type="button" className="button primary" disabled={isBlocked(selected.id)} onClick={() => onBind(selected)}>운전 제어에서 불러오기 →</button>}
            {selected.phase === 'DRAFT' && canWrite && <button type="button" className="button primary" disabled={isBlocked(selected.id)} onClick={() => onRelease(selected)}>배포</button>}
            {['DRAFT', 'WAITING'].includes(selected.phase) && canWrite && <button id="wo-edit" type="button" className="button secondary" disabled={!!pendingRevision || isBlocked(selected.id) || selected.queue_position !== null} onClick={() => startEdit(selected)}>수정</button>}
            {['DRAFT', 'WAITING'].includes(selected.phase) && canWrite && <button type="button" className="button secondary danger-text" disabled={!!pendingRevision || isBlocked(selected.id) || selected.queue_position !== null} onClick={() => onCancel(selected)}>취소</button>}
            {selected.queue_position !== null && canWrite && <span className="form-help">대기열에 들어 있어 수정·취소할 수 없습니다. 운전 제어의 대기열에서 먼저 빼 주세요.</span>}
            {selected.phase === 'RUNNING' && <button type="button" className="button primary" onClick={onOpenControl}>운전 제어에서 보기 →</button>}
            {(selected.phase === 'DONE' || selected.runs > 0) && <button type="button" className="button secondary" onClick={onReports}>실적 보기 →</button>}
            {selected.phase === 'RUNNING' && <span className="form-help">실행 중에는 수정·취소할 수 없습니다. 정산이 끝나면 다시 불러오거나 고칠 수 있습니다.</span>}
            {selected.phase === 'DONE' && <span className="form-help">끝난 작업지시입니다.</span>}
          </div>}
          {revision && revision.order === selected.id && revision.phase !== 'done' && <div className={`message ${revision.phase === 'sending' ? '' : revision.phase === 'unknown' ? 'caution' : 'error'}`} role="status">
            <p>{revision.message}</p>
            {revision.phase === 'unknown' && <button type="button" className="button primary" onClick={() => void send(revision)}>같은 요청으로 다시 확인</button>}
          </div>}

          <section className="wo-timeline" aria-label="진행 기록">
            <h4>진행 기록</h4>
            <ol>
              {(history?.id === selected.id ? timeline(history.events, history.runs) : []).map(entry => <li key={entry.key}><span>{time(entry.at)}</span><span>{entry.body}</span></li>)}
              {history?.id !== selected.id && <li><span /><span>기록을 조회하고 있습니다.</span></li>}
            </ol>
          </section>
        </div>}
      </div>
    </div>
  </div>
}
