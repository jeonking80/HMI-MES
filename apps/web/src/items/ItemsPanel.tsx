import { useState } from 'react'
import type { FormEvent } from 'react'
import type { Session } from '../auth'
import { itemsPath, MesHTTPError, mesRequest, parameterText } from '../mesApi'
import type { ItemMaster, RecipeVersion, WorkOrder } from '../mesApi'
import { nextItemId, UNITS } from './itemRules'

interface Attempt { body: ItemMaster; phase: 'sending' | 'done' | 'conflict' | 'invalid' | 'unknown'; message: string }
export default function ItemsPanel({ session, canWrite, items, loaded, recipes, orders, onChanged, onNewRecipe, onSessionInvalid }: {
  session: Session; canWrite: boolean; items: ItemMaster[]; loaded: boolean; recipes: RecipeVersion[]; orders: WorkOrder[]
  onChanged: () => void; onNewRecipe: (itemId: string) => void; onSessionInvalid: () => void
}) {
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [adding, setAdding] = useState(false)
  const [form, setForm] = useState({ name: '', unit: '개' })
  const [attempt, setAttempt] = useState<Attempt | null>(null)
  const [query, setQuery] = useState('')

  const shown = items.filter(item => !query.trim() || `${item.id} ${item.name}`.toLowerCase().includes(query.trim().toLowerCase()))
  const selected = adding ? null : items.find(item => item.id === selectedId) ?? shown[0] ?? null
  const recipesOf = (id: string) => recipes.filter(recipe => recipe.item_id === id)
  const ordersOf = (id: string) => orders.filter(order => order.item_id === id)

  async function send(body: ItemMaster) {
    setAttempt({ body, phase: 'sending', message: '품목을 등록하고 있습니다.' })
    try {
      await mesRequest(itemsPath, new AbortController().signal, session.csrf_token, body)
      setAttempt({ body, phase: 'done', message: `품목 ${body.id}(${body.name})를 등록했습니다.` })
      setAdding(false)
      setSelectedId(body.id)
      setForm({ name: '', unit: '개' })
      onChanged()
    } catch (cause) {
      const status = cause instanceof MesHTTPError ? cause.status : 0
      if (status === 409) setAttempt({ body, phase: 'conflict', message: `그 사이 ${body.id}가 다른 품목으로 등록됐습니다. 목록을 새로 불러온 뒤 다시 등록해 주세요.` })
      else if (status === 422) setAttempt({ body, phase: 'invalid', message: '입력값을 확인해 주세요.' })
      else if (status === 401) { setAttempt(null); onSessionInvalid() }
      else if (status === 403) setAttempt({ body, phase: 'invalid', message: '품목 등록 권한이 없거나 보안 확인에 실패했습니다. 생산 담당자 계정으로 다시 로그인해 주세요.' })
      else setAttempt({ body, phase: 'unknown', message: '등록 결과를 확인하지 못했습니다. "같은 내용으로 다시 확인"을 누르세요. 같은 내용이면 두 번 등록되지 않습니다.' })
    }
  }
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const body = { id: nextItemId(items), name: form.name.trim(), unit: form.unit, enabled: true }
    if (!body.name || body.name.length > 128 || !UNITS.includes(body.unit)) {
      setAttempt({ body, phase: 'invalid', message: '품목명을 입력하고 단위를 골라 주세요.' }); return
    }
    if (items.some(item => item.name === body.name)) {
      setAttempt({ body, phase: 'invalid', message: `품목명 "${body.name}"은 이미 등록되어 있습니다.` }); return
    }
    void send(body)
  }

  return <>
  <div className="pl-toolbar">
    <label htmlFor="item-search" className="sr-only">품목 검색</label>
    <input id="item-search" className="item-search" placeholder="품목명 · ID 검색" value={query} onChange={event => setQuery(event.target.value)} />
    <span className="pl-count">품목 {items.length}개</span>
    <span className="wo-spacer" />
    {canWrite && <button id="item-new" type="button" className="button primary" onClick={() => { setAdding(true); setAttempt(null) }}>+ 새 품목</button>}
  </div>
  <div className="rc-layout">
    <div className="rc-list">
      {!loaded ? <p role="status">품목을 조회하고 있습니다.</p> : shown.length === 0 ? <p className="mes-empty">{items.length ? '검색 결과가 없습니다.' : '등록된 품목이 없습니다. "+ 새 품목"으로 등록하세요.'}</p>
        : <ul id="item-list">{shown.map(item => <li key={item.id}><button type="button" aria-pressed={!adding && selected?.id === item.id} onClick={() => { setAdding(false); setSelectedId(item.id) }}>
          <span className="rc-name"><strong>{item.name}</strong><em>{item.enabled ? item.unit : '사용 중지'}</em></span>
          <small>{item.id} · 공정 설정 {recipesOf(item.id).length} · 작업지시 {ordersOf(item.id).length}</small>
        </button></li>)}</ul>}
    </div>
    <div className="rc-detail">
      {adding && canWrite ? <form id="item-form" className="rc-card" onSubmit={submit}>
        <h3>새 품목</h3>
        <p className="form-help">품목 ID는 규칙(ITM-일련번호)에 따라 자동으로 붙습니다. 등록한 품목은 공정 설정에서 고를 수 있습니다.</p>
        <fieldset disabled={attempt?.phase === 'sending' || attempt?.phase === 'unknown'}><div className="mes-form-grid">
          <div><span className="field-label">품목 ID (자동)</span><p id="item-id" className="item-auto-id">{attempt?.phase === 'unknown' ? attempt.body.id : nextItemId(items)}</p></div>
          <div><label htmlFor="item-name">품목명</label><input id="item-name" required maxLength={128} placeholder="예: 파우치 200g" value={form.name} onChange={event => setForm(previous => ({ ...previous, name: event.target.value }))} /></div>
          <div><label htmlFor="item-unit">단위</label><select id="item-unit" required value={form.unit} onChange={event => setForm(previous => ({ ...previous, unit: event.target.value }))}>{UNITS.map(unit => <option key={unit} value={unit}>{unit}</option>)}</select></div>
        </div>
        <div className="mes-form-actions"><button type="button" className="button secondary" onClick={() => { setAdding(false); setAttempt(null) }}>취소</button><button id="item-submit" type="submit" className="button primary">품목 등록</button></div></fieldset>
        {attempt && attempt.phase !== 'done' && <div className={`message ${attempt.phase === 'sending' ? '' : attempt.phase === 'unknown' ? 'caution' : 'error'}`} role="status"><p>{attempt.message}</p>{attempt.phase === 'unknown' && <button type="button" className="button primary" onClick={() => void send(attempt.body)}>같은 내용으로 다시 확인</button>}</div>}
      </form> : selected ? <div className="rc-card" id="item-detail">
        {attempt?.phase === 'done' && attempt.body.id === selected.id && <p className="message success" role="status">{attempt.message}</p>}
        <div className="rc-card-head"><div><h3>{selected.name}</h3><p>품목 ID {selected.id} · 단위 {selected.unit} · {selected.enabled ? '사용 중' : '사용 중지'}</p></div>
          {canWrite && selected.enabled && <button type="button" className="button primary" onClick={() => onNewRecipe(selected.id)}>이 품목으로 공정 설정 만들기 →</button>}</div>
        <div className="wo-timeline">
          <h4>공정 설정 {recipesOf(selected.id).length}</h4>
          {recipesOf(selected.id).length === 0 ? <p className="form-help">아직 이 품목의 공정 설정이 없습니다.</p>
            : <ol>{recipesOf(selected.id).map(recipe => <li key={recipe.id}><span>{recipe.version}</span><span><strong>{recipe.recipe_id}</strong> · {parameterText(recipe.parameters)}</span></li>)}</ol>}
        </div>
        <div className="wo-timeline">
          <h4>작업지시 {ordersOf(selected.id).length}</h4>
          {ordersOf(selected.id).length === 0 ? <p className="form-help">아직 이 품목의 작업지시가 없습니다.</p>
            : <ol>{ordersOf(selected.id).map(order => <li key={order.id}><span>{order.planned_date.slice(5)}</span><span><strong>{order.id}</strong> · 목표 {order.target_quantity}{selected.unit}{order.status === 'CANCELLED' ? ' · 취소' : order.status === 'DRAFT' ? ' · 작성 중' : ''}</span></li>)}</ol>}
        </div>
      </div> : <div className="rc-card mes-empty">왼쪽에서 품목을 고르거나 새 품목을 등록하세요.</div>}
    </div>
  </div>
  </>
}
