import { useCallback, useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import type { Session } from './auth'
import { itemPage, itemsPath, MesHTTPError, mesDetailPath, mesPaths, mesRequest, orderPage, recipePage, registrationFrom, sameDefinition, validDate, validId } from './mesApi'
import type { Definition, OrderDefinition, Page, RecipeDefinition, RecipeVersion, Registration, RegistrationKind, WorkOrder } from './mesApi'
import type { WorkOrderWorkflow } from './useWorkOrderWorkflow'
import type { CancellationWorkflow } from './useCancellation'
import { conflictMessage } from './conflictReasons'
import LineQueuePanel from './LineQueuePanel'
import OrderBoard from './orders/OrderBoard'
import ItemsPanel from './items/ItemsPanel'
import { DEFAULT_PARAMETERS, DEFAULT_TARGET, knownItems, parameterProblem, recipeGroups, SPEED_RANGE, suggestOrderId, suggestRecipe, TEMPERATURE_RANGE, todayKst } from './mesAutofill'

interface Attempt {
  definition: Definition
  phase: 'sending' | 'uncertain' | 'conflict' | 'confirmed' | 'rejected'
  message: string
  stored: Registration | null
}
interface LineChoice { id: string; name: string }
const recipeEmpty = { id: '', family: '', version: '', item: '', temperature: String(DEFAULT_PARAMETERS.temperature_c), speed: String(DEFAULT_PARAMETERS.speed_m_per_min) }
const orderEmpty = { id: '', line: '', target: String(DEFAULT_TARGET), date: '' }
const CHOICE_PAGES = 10
const displayTime = (value: string) => new Date(value).toLocaleString('ko-KR', { timeZone: 'Asia/Seoul', hour12: false })

function usePage<T>(path: string, parse: (value: unknown) => Page<T>, onSessionInvalid: () => void, refreshKey = '') {
  const [page, setPage] = useState<Page<T> | null>(null)
  const [error, setError] = useState('')
  const [cursor, setCursor] = useState<string | null>(null)
  const [revision, setRevision] = useState(0)
  useEffect(() => {
    const controller = new AbortController()
    const query = new URLSearchParams({ limit: '20', ...(cursor ? { after_id: cursor } : {}) })
    void mesRequest(`${path}?${query}`, controller.signal).then(value => {
      if (!controller.signal.aborted) { setPage(parse(value)); setError('') }
    }).catch(cause => {
      if (controller.signal.aborted) return
      if (cause instanceof MesHTTPError && (cause.status === 401 || cause.status === 403)) { onSessionInvalid(); return }
      setError('목록을 불러오지 못했습니다. 잠시 후 다시 조회해 주세요.')
    })
    return () => controller.abort()
  }, [path, parse, cursor, revision, refreshKey, onSessionInvalid])
  const refresh = useCallback(() => { setPage(null); setError(''); setCursor(null); setRevision(value => value + 1) }, [])
  function next() { if (page?.next_cursor) { setCursor(page.next_cursor); setPage(null); setError('') } }
  return { page, error, cursor, refresh, next }
}

/** Every registered entry (bounded) so forms can offer choices and next IDs without typing. */
function useAll<T>(path: string, parse: (value: unknown) => Page<T>, refreshKey: string) {
  const [state, setState] = useState<{ items: T[]; loaded: boolean }>({ items: [], loaded: false })
  useEffect(() => {
    const controller = new AbortController()
    void (async () => {
      const collected: T[] = []
      let cursor: string | null = null
      for (let page = 0; page < CHOICE_PAGES; page += 1) {
        const query = new URLSearchParams({ limit: '20', ...(cursor ? { after_id: cursor } : {}) })
        const value = parse(await mesRequest(`${path}?${query}`, controller.signal))
        collected.push(...value.items)
        cursor = value.next_cursor
        if (!cursor) break
      }
      if (!controller.signal.aborted) setState({ items: collected, loaded: true })
    })().catch(() => {})
    return () => controller.abort()
  }, [path, parse, refreshKey])
  return state
}

export type DraftsTab = 'recipes' | 'items' | 'orders' | 'queue'
const tabs: { id: DraftsTab; label: string }[] = [{ id: 'recipes', label: '공정 설정' }, { id: 'items', label: '품목' }, { id: 'orders', label: '작업지시' }, { id: 'queue', label: '작업 대기열' }]
const planningTabs: DraftsTab[] = ['recipes', 'items']

export default function MesDraftPanel({ session, onSessionInvalid, workflow, cancellation, onSelectOrder, tab, onTab, onOpenControl, onOpenReports }: { onOpenControl: () => void; onOpenReports: () => void; tab: DraftsTab; onTab: (tab: DraftsTab) => void; session: Session; onSessionInvalid: () => void; workflow: WorkOrderWorkflow; cancellation: CancellationWorkflow; onSelectOrder: (order: WorkOrder) => void }) {
  const [chosenCancelOrder, setCancelOrder] = useState<WorkOrder | null>(null)
  const cancelOrder = cancellation.preparedOrder ?? chosenCancelOrder
  const cancelHeading = useRef<HTMLHeadingElement>(null)
  useEffect(() => { if (cancelOrder) cancelHeading.current?.focus() }, [cancelOrder])
  function closeCancellation() {
    if (cancelOrder) document.getElementById(`cancel-order-${encodeURIComponent(cancelOrder.id)}`)?.focus()
    setCancelOrder(null)
    cancellation.dismissPrepared()
  }
  const [releaseOrder, setReleaseOrder] = useState<WorkOrder | null>(null)
  const canWrite = session.roles.includes('production_manager') && !session.must_change_password
  const recipes = usePage(mesPaths.recipe, recipePage, onSessionInvalid)
  const orders = usePage(mesPaths.order, orderPage, onSessionInvalid, cancellation.ledger?.receipt?.cancellation_id ?? '')
  const [recipe, setRecipe] = useState(recipeEmpty)
  const [order, setOrder] = useState(() => ({ ...orderEmpty, date: todayKst() }))
  const [selectedRecipe, setSelectedRecipe] = useState<RecipeVersion | null>(null)
  // Typing an ID or a parameter by hand stops the suggestions from overwriting it.
  const [idsManual, setIdsManual] = useState(false)
  const [paramsManual, setParamsManual] = useState(false)
  // Which existing group the form continues; '' means a brand-new process setting.
  const [baseGroup, setBaseGroup] = useState('')
  const [recipeMode, setRecipeMode] = useState<'view' | 'edit'>('view')
  const [viewRecipe, setViewRecipe] = useState<RecipeVersion | null>(null)
  const [orderIdManual, setOrderIdManual] = useState(false)
  const [choiceRevision, setChoiceRevision] = useState(0)
  const { items: allRecipes } = useAll(mesPaths.recipe, recipePage, `recipes-${choiceRevision}`)
  // A confirmed release changes an order to RELEASED, so the queue choices reload with it.
  const releasedKey = workflow.attempt?.kind === 'release' && workflow.attempt.phase === 'confirmed' ? workflow.attempt.intent.work_order_id : ''
  const { items: allOrders } = useAll(mesPaths.order, orderPage, `orders-${choiceRevision}-${releasedKey}-${cancellation.ledger?.receipt?.cancellation_id ?? ''}`)
  const groups = recipeGroups(allRecipes)
  // Item master (CX-37); an empty list on a server without it keeps the earlier free-text behaviour.
  const { items: masterItems, loaded: masterLoaded } = useAll(itemsPath, itemPage, `items-${choiceRevision}`)
  const masterIds = new Set(masterItems.map(item => item.id))
  const items = [...new Set([...masterItems.filter(item => item.enabled).map(item => item.id), ...knownItems(allRecipes, allOrders)])]
  // An empty but confirmed master list still needs its first item registered; an unavailable one does not.
  const newItem = masterLoaded && recipe.item.trim() !== '' && !masterIds.has(recipe.item.trim())
  const releaseAfterRegister = useRef(false)
  const [moving, setMoving] = useState('')
  const [lines, setLines] = useState<LineChoice[] | null>(null)
  const [lineError, setLineError] = useState('')
  const [lineRevision, setLineRevision] = useState(0)
  const [attempts, setAttempts] = useState<Record<RegistrationKind, Attempt | null>>({ recipe: null, order: null })
  const [checking, setChecking] = useState({ recipe: false, order: false })
  const [validation, setValidation] = useState({ recipe: '', order: '' })
  const controllers = useRef<Partial<Record<RegistrationKind, AbortController>>>({})
  const inFlight = useRef({ recipe: false, order: false })
  const alive = useRef(true)
  useEffect(() => {
    alive.current = true
    const activeControllers = controllers.current
    return () => { alive.current = false; Object.values(activeControllers).forEach(controller => controller?.abort()) }
  }, [])
  useEffect(() => {
    const controller = new AbortController()
    void mesRequest('/api/v1/sources', controller.signal).then(value => {
      if (controller.signal.aborted) return
      if (typeof value !== 'object' || value === null || !('sources' in value) || !Array.isArray(value.sources)) throw new Error('Unconfirmed lines')
      const choices = new Map<string, LineChoice>()
      for (const source of value.sources) {
        if (!source || !validId(source.line_id) || typeof source.line_name !== 'string') throw new Error('Unconfirmed line')
        choices.set(source.line_id, { id: source.line_id, name: source.line_name })
      }
      const list = [...choices.values()]
      setLines(list)
      setLineError('')
      if (list.length === 1) setOrder(previous => previous.line ? previous : { ...previous, line: list[0].id })
    }).catch(cause => {
      if (controller.signal.aborted) return
      if (cause instanceof MesHTTPError && (cause.status === 401 || cause.status === 403)) { onSessionInvalid(); return }
      setLineError('선택할 라인을 불러오지 못했습니다.')
    })
    return () => controller.abort()
  }, [lineRevision, onSessionInvalid])

  // Suggestions are derived each render; nothing is written back until the operator edits or submits.
  const orderId = orderIdManual ? order.id : validDate(order.date) ? suggestOrderId(allOrders, order.date) : order.id
  const latestRecipe = allRecipes.length ? [...allRecipes].sort((a, b) => b.created_at.localeCompare(a.created_at))[0] : null
  const chosenRecipe = selectedRecipe ?? latestRecipe
  function suggestFor(item: string, group: string, base: RecipeVersion | null) {
    setRecipe(previous => {
      const next = { ...previous, item }
      if (!idsManual) {
        const ids = suggestRecipe(allRecipes, group, item)
        Object.assign(next, { id: ids.id, family: ids.recipe_id, version: ids.version })
      }
      if (base && (!paramsManual || group)) Object.assign(next, { temperature: String(base.parameters.temperature_c), speed: String(base.parameters.speed_m_per_min) })
      return next
    })
  }
  function chooseItem(item: string) {
    const group = groups.find(entry => entry.item_id === item.trim())
    setBaseGroup(group?.recipe_id ?? '')
    suggestFor(item, group?.recipe_id ?? '', group?.latest ?? null)
  }
  function resetRecipe() {
    setBaseGroup('')
    setIdsManual(false)
    setParamsManual(false)
    setRecipe(recipeEmpty)
  }
  // Bring a form into view after a row action: open its folded box, scroll, mark it and focus the changed field.
  function reveal(formId: string, fieldId: string) {
    // Two frames: the tab switch renders first, then the form can be measured and scrolled to.
    window.requestAnimationFrame(() => window.requestAnimationFrame(() => {
      const form = document.getElementById(formId)
      if (!form) return
      const box = form.closest('details')
      if (box) box.open = true
      form.scrollIntoView({ behavior: 'smooth', block: 'center' })
      form.classList.remove('guide-focus')
      void form.offsetWidth
      form.classList.add('guide-focus')
      window.setTimeout(() => form.classList.remove('guide-focus'), 2600)
      document.getElementById(fieldId)?.focus({ preventScroll: true })
    }))
  }
  function pickForOrder(item: RecipeVersion) {
    setSelectedRecipe(item)
    onTab('orders')
    reveal('mes-order-form', 'mes-order-recipe')
  }
  // "Edit" keeps the stored version unchanged and starts its next version from its values.
  function editFrom(item: RecipeVersion) {
    setBaseGroup(item.recipe_id)
    setIdsManual(false)
    setParamsManual(false)
    suggestFor(item.item_id, item.recipe_id, item)
    reveal('mes-recipe-form', 'mes-recipe-temperature')
  }
  function chooseBase(recipeId: string) {
    const group = groups.find(entry => entry.recipe_id === recipeId)
    if (!group) return resetRecipe()
    setBaseGroup(group.recipe_id)
    setParamsManual(false)
    suggestFor(group.item_id, group.recipe_id, group.latest)
  }
  function refillIds() {
    setIdsManual(false)
    const ids = suggestRecipe(allRecipes, groups.find(entry => entry.item_id === recipe.item.trim())?.recipe_id ?? '', recipe.item)
    setRecipe(previous => ({ ...previous, id: ids.id, family: ids.recipe_id, version: ids.version }))
  }
  async function moveToBinding(id: string) {
    setMoving(id)
    try {
      const stored = registrationFrom('order', await mesRequest(mesDetailPath('order', id), new AbortController().signal)) as WorkOrder
      if (workflow.attempt?.kind === 'release' && workflow.attempt.phase === 'confirmed') workflow.acknowledgeRelease()
      orders.refresh()
      onSelectOrder(stored)
    } catch (cause) {
      if (cause instanceof MesHTTPError && (cause.status === 401 || cause.status === 403)) onSessionInvalid()
    } finally { setMoving('') }
  }
  const locked = (kind: RegistrationKind) => checking[kind] || ['sending', 'uncertain', 'conflict', 'confirmed'].includes(attempts[kind]?.phase ?? '')
  function confirmed(kind: RegistrationKind, stored: Registration) {
    setChoiceRevision(value => value + 1)
    if (kind === 'recipe') { setSelectedRecipe(stored as RecipeVersion); recipes.refresh(); return }
    orders.refresh()
    const order = stored as WorkOrder
    if (releaseAfterRegister.current && order.status === 'DRAFT' && !workflow.attempt && !workflow.orderBlocked(order.id)) {
      void workflow.submit('release', { work_order_id: order.id, actor: session.login, release_id: crypto.randomUUID(), expected_row_version: order.row_version })
    }
    releaseAfterRegister.current = false
  }
  async function submit(kind: RegistrationKind, definition: Definition) {
    if (!canWrite || inFlight.current[kind] || locked(kind)) return
    inFlight.current[kind] = true
    setValidation(previous => ({ ...previous, [kind]: '' }))
    setAttempts(previous => ({ ...previous, [kind]: { definition, phase: 'sending', message: '등록 내용을 확인하고 있습니다.', stored: null } }))
    const controller = new AbortController()
    controllers.current[kind] = controller
    try {
      const response = await mesRequest(mesPaths[kind], controller.signal, session.csrf_token, definition)
      const stored = registrationFrom(kind, response)
      if (!alive.current) return
      const matches = sameDefinition(kind, stored, definition)
      setAttempts(previous => ({ ...previous, [kind]: { definition, stored, phase: matches ? 'confirmed' : 'conflict', message: matches ? '등록 내용을 확인했습니다.' : '저장된 내용이 요청한 내용과 다릅니다. 원래 등록 내용을 확인해 주세요.' } }))
      if (matches) confirmed(kind, stored)
    } catch (cause) {
      if (!alive.current) return
      if (cause instanceof MesHTTPError && (cause.status === 401 || cause.status === 403)) { onSessionInvalid(); return }
      const status = cause instanceof MesHTTPError ? cause.status : 0
      const phase = status === 409 ? 'conflict' : [400, 404, 413, 415, 422, 429].includes(status) ? 'rejected' : 'uncertain'
      const message = phase === 'conflict' ? conflictMessage(cause instanceof MesHTTPError ? cause.reasonCode : null) ?? '같은 ID 또는 설정 그룹·버전에 다른 내용이 이미 등록되어 있습니다. 기존 내용을 덮어쓰지 않았습니다.'
        : phase === 'rejected' ? status === 404 ? '선택한 라인이나 공정 설정을 찾을 수 없습니다. 목록과 입력을 확인해 주세요.' : '등록 요청이 거절되었습니다. 입력 조건을 확인해 주세요.'
          : '등록 결과를 확인할 수 없습니다. 입력과 ID를 그대로 보관했습니다. 원래 ID로 조회해 주세요.'
      setAttempts(previous => ({ ...previous, [kind]: { definition, stored: null, phase, message } }))
    } finally { inFlight.current[kind] = false }
  }
  async function lookup(kind: RegistrationKind) {
    const original = attempts[kind]
    if (!original || inFlight.current[kind]) return
    inFlight.current[kind] = true
    setChecking(previous => ({ ...previous, [kind]: true }))
    const controller = new AbortController()
    controllers.current[kind] = controller
    try {
      const response = await mesRequest(mesDetailPath(kind, original.definition.id), controller.signal)
      const stored = registrationFrom(kind, response)
      if (!alive.current) return
      const matches = sameDefinition(kind, stored, original.definition)
      setAttempts(previous => ({ ...previous, [kind]: { ...original, stored, phase: matches ? 'confirmed' : 'conflict', message: matches ? '원래 ID로 등록 내용을 확인했습니다.' : '이 ID에 저장된 내용이 요청한 내용과 다릅니다. 아래 기존 내용을 확인해 주세요.' } }))
      if (matches) confirmed(kind, stored)
    } catch (cause) {
      if (!alive.current) return
      if (cause instanceof MesHTTPError && (cause.status === 401 || cause.status === 403)) { onSessionInvalid(); return }
      const missing = cause instanceof MesHTTPError && cause.status === 404
      setAttempts(previous => ({ ...previous, [kind]: { ...original, message: missing ? '현재 이 ID의 등록 내용을 찾지 못했습니다. 등록되지 않았다고 단정할 수 없어 입력과 ID를 유지합니다.' : '원래 등록 내용을 조회하지 못했습니다. 연결을 확인한 뒤 같은 ID로 다시 조회해 주세요.' } }))
    } finally {
      inFlight.current[kind] = false
      if (alive.current) setChecking(previous => ({ ...previous, [kind]: false }))
    }
  }
  function submitRecipe(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const temperature = recipe.temperature === '' ? NaN : Number(recipe.temperature)
    const speed = recipe.speed === '' ? NaN : Number(recipe.speed)
    if (![recipe.id, recipe.family, recipe.version, recipe.item].every(validId) || !Number.isFinite(temperature) || temperature < 0 || !Number.isFinite(speed) || speed <= 0) {
      setValidation(previous => ({ ...previous, recipe: 'ID는 공백 없이 1~128자, 온도는 0 이상, 속도는 0보다 큰 유한한 수로 입력해 주세요.' })); return
    }
    const problem = parameterProblem(temperature, speed)
    if (problem) { setValidation(previous => ({ ...previous, recipe: problem })); return }
    const definition: RecipeDefinition = { id: recipe.id, recipe_id: recipe.family, version: recipe.version, item_id: recipe.item, parameters: { temperature_c: temperature, speed_m_per_min: speed } }
    if (!newItem) { void submit('recipe', definition); return }
    // A first-time item is registered just before its recipe; an existing identical item is accepted as is.
    void mesRequest(itemsPath, new AbortController().signal, session.csrf_token, { id: recipe.item.trim(), name: recipe.item.trim(), unit: '개', enabled: true })
      .then(() => { setChoiceRevision(value => value + 1); void submit('recipe', definition) })
      .catch(cause => {
        if (cause instanceof MesHTTPError && (cause.status === 401 || cause.status === 403)) { onSessionInvalid(); return }
        setValidation(previous => ({ ...previous, recipe: cause instanceof MesHTTPError && cause.status === 409 ? '같은 품목 ID가 다른 이름·단위로 이미 등록되어 있습니다. 품목 목록에서 골라 주세요.' : '새 품목을 등록하지 못했습니다. 잠시 후 다시 시도해 주세요.' }))
      })
  }
  function submitOrder(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    releaseAfterRegister.current = (event.nativeEvent as SubmitEvent).submitter?.id === 'mes-order-submit-release'
    const quantity = Number(order.target)
    if (!validId(orderId) || !chosenRecipe || !lines?.some(line => line.id === order.line) || !/^[1-9][0-9]*$/.test(order.target) || !Number.isSafeInteger(quantity) || quantity > 4294967295 || !validDate(order.date)) {
      setValidation(previous => ({ ...previous, order: '작업지시 ID, 공정 설정, 등록된 라인, 예정일과 목표 양품 수량 1~4,294,967,295개를 확인해 주세요.' })); return
    }
    const definition: OrderDefinition = { id: orderId, item_id: chosenRecipe.item_id, line_id: order.line, recipe_version_id: chosenRecipe.id, target_quantity: quantity, planned_date: order.date }
    void submit('order', definition)
  }
  function reset(kind: RegistrationKind) {
    const wasConfirmed = attempts[kind]?.phase === 'confirmed'
    setAttempts(previous => ({ ...previous, [kind]: null }))
    setValidation(previous => ({ ...previous, [kind]: '' }))
    if (wasConfirmed) {
      if (kind === 'recipe') resetRecipe()
      else { setOrder(previous => ({ ...orderEmpty, line: previous.line, date: previous.date })); setOrderIdManual(false) }
    }
  }
  const choices = recipes.page?.items ?? []
  const shownRecipe = viewRecipe ?? recipes.page?.items[0] ?? null
  const recipeChoices = selectedRecipe && !choices.some(item => item.id === selectedRecipe.id) ? [selectedRecipe, ...choices] : choices
  const orderRecipeChoices = [...new Map([...recipeChoices, ...allRecipes].map(item => [item.id, item])).values()].sort((a, b) => b.created_at.localeCompare(a.created_at))
  const released = workflow.attempt?.kind === 'release' && workflow.attempt.phase === 'confirmed' ? workflow.attempt.intent.work_order_id : null
  return <section id="mes-drafts" className="mes-panel" aria-labelledby="mes-heading">
    <h2 id="mes-heading" className="sr-only">공정 설정 · 작업지시</h2>
    <div className="mes-tabs" role="tablist" aria-label={planningTabs.includes(tab) ? '공정 설정 보기' : '작업지시 보기'}>{tabs.filter(item => planningTabs.includes(item.id) === planningTabs.includes(tab)).map(item => <button key={item.id} id={`mes-tab-${item.id}`} type="button" role="tab" aria-selected={tab === item.id} aria-controls={item.id === 'queue' ? 'mes-queue' : `mes-${item.id}`} onClick={() => onTab(item.id)}>{item.label}</button>)}</div>
    {!canWrite && <p className="message caution">현재 계정은 등록된 내용을 조회할 수 있습니다. 공정 설정과 작업지시 등록은 생산 담당자가 진행합니다.</p>}
    <section id="mes-recipes" className="mes-card" aria-labelledby="mes-recipes-heading" hidden={tab !== 'recipes'}>
      <h3 id="mes-recipes-heading" className="sr-only">공정 설정</h3>
      <div className="rc-layout">
        <div className="rc-list">
          <div className="rc-list-head"><strong>공정 설정 {recipes.page?.items.length ?? 0}개</strong>{canWrite && <button id="mes-recipe-new" type="button" className="button primary" disabled={locked('recipe')} onClick={() => { resetRecipe(); setRecipeMode('edit'); reveal('mes-recipe-form', 'mes-recipe-item') }}>+ 새 공정 설정</button>}</div>
          {recipes.error ? <p className="message error" role="alert">{recipes.error}</p> : !recipes.page ? <p role="status">공정 설정을 조회하고 있습니다.</p> : recipes.page.items.length === 0 ? <p className="mes-empty">등록된 공정 설정이 없습니다. "+ 새 공정 설정"으로 만드세요.</p>
            : <ul id="mes-recipe-list">{recipes.page.items.map(item => <li key={item.id}><button type="button" aria-pressed={recipeMode === 'view' && shownRecipe?.id === item.id} onClick={() => { setViewRecipe(item); setRecipeMode('view') }}><span className="rc-name"><strong>{item.recipe_id}</strong><em>{item.version}</em></span><small>{item.item_id} · {item.parameters.temperature_c} ℃ · {item.parameters.speed_m_per_min} m/min</small></button></li>)}</ul>}
          {(recipes.cursor || recipes.page?.next_cursor) && <div className="mes-pagination"><button id="mes-recipes-first" className="button secondary" type="button" disabled={!recipes.cursor} onClick={recipes.refresh}>처음</button><button id="mes-recipes-next" className="button secondary" type="button" disabled={!recipes.page?.next_cursor} onClick={recipes.next}>다음</button></div>}
        </div>
        <div className="rc-detail">
          {recipeMode === 'edit' && canWrite ? <div className="rc-card">
            <h3>{baseGroup ? `${baseGroup} 새 버전` : '새 공정 설정'}</h3>
            <p className="form-help">{baseGroup ? '기존 버전은 그대로 두고 새 버전으로 저장합니다.' : '품목과 온도·속도를 정하면 ID는 자동으로 붙습니다.'}</p>
        <form id="mes-recipe-form" onSubmit={submitRecipe}><fieldset disabled={locked('recipe')}><legend className="sr-only">공정 설정 등록 내용</legend><div className="mes-form-grid">
          <div><label htmlFor="mes-recipe-base">기존 설정에서 시작 (선택)</label><select id="mes-recipe-base" value={baseGroup} onChange={event => chooseBase(event.target.value)}><option value="">새 공정 설정 (처음부터 입력)</option>{groups.map(group => <option key={group.recipe_id} value={group.recipe_id}>{group.item_id} · {group.recipe_id} (최신 {group.latest.version}, {group.latest.parameters.temperature_c} ℃ / {group.latest.parameters.speed_m_per_min} m/min)</option>)}</select></div>
          <div><label htmlFor="mes-recipe-item">품목</label><input id="mes-recipe-item" required maxLength={128} list="mes-item-choices" placeholder="목록에서 고르거나 새 품목 입력" value={recipe.item} onChange={event => chooseItem(event.target.value)} /><datalist id="mes-item-choices">{items.map(item => <option key={item} value={item}>{masterItems.find(entry => entry.id === item)?.name}</option>)}</datalist>{newItem && <p className="input-help">처음 쓰는 품목입니다. 공정 설정을 등록할 때 품목 목록에도 함께 등록합니다(단위: 개).</p>}</div>
          <div><label htmlFor="mes-recipe-temperature">설정 온도 (℃)</label><input id="mes-recipe-temperature" type="number" required min={TEMPERATURE_RANGE.min} max={TEMPERATURE_RANGE.max} step="any" value={recipe.temperature} onChange={event => { setParamsManual(true); setRecipe(previous => ({ ...previous, temperature: event.target.value })) }} /><p className="input-help">허용 범위 {TEMPERATURE_RANGE.min}~{TEMPERATURE_RANGE.max} ℃</p></div>
          <div><label htmlFor="mes-recipe-speed">설정 속도 (m/min)</label><input id="mes-recipe-speed" type="number" required min={SPEED_RANGE.min} max={SPEED_RANGE.max} step="any" value={recipe.speed} onChange={event => { setParamsManual(true); setRecipe(previous => ({ ...previous, speed: event.target.value })) }} /><p className="input-help">허용 범위 {SPEED_RANGE.min}~{SPEED_RANGE.max} m/min</p></div>
        </div>
        <details className="mes-generated-ids"><summary>ID 직접 지정 (선택){idsManual ? ' · 직접 수정함' : ' · 자동으로 채워집니다'}</summary><div className="mes-form-grid">
          <div><label htmlFor="mes-recipe-family">설정 그룹 ID</label><input id="mes-recipe-family" required maxLength={128} value={recipe.family} onChange={event => { setIdsManual(true); setRecipe(previous => ({ ...previous, family: event.target.value })) }} /></div>
          <div><label htmlFor="mes-recipe-version">버전</label><input id="mes-recipe-version" required maxLength={128} value={recipe.version} onChange={event => { setIdsManual(true); setRecipe(previous => ({ ...previous, version: event.target.value })) }} /></div>
          <div><label htmlFor="mes-recipe-id">설정 ID</label><input id="mes-recipe-id" required maxLength={128} value={recipe.id} onChange={event => { setIdsManual(true); setRecipe(previous => ({ ...previous, id: event.target.value })) }} /></div>
        </div>{idsManual && <button type="button" className="button text" onClick={refillIds}>ID 자동으로 다시 채우기</button>}</details>
        <div className="mes-form-actions"><button id="mes-recipe-submit" className="button primary" type="submit">{baseGroup ? '새 버전으로 등록' : '새 공정 설정 등록'}</button><button id="mes-recipe-reset" className="button secondary" type="button" onClick={resetRecipe}>입력 초기화</button><button type="button" className="button text" onClick={() => setRecipeMode('view')}>닫기</button></div></fieldset></form>
          </div> : shownRecipe ? <div className="rc-card" id="mes-recipe-detail">
            <div className="rc-card-head"><div><h3>{shownRecipe.recipe_id} · {shownRecipe.version}</h3><p>{shownRecipe.item_id} · 등록 {shownRecipe.created_by} · {new Date(shownRecipe.created_at).toLocaleString('ko-KR', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' })}</p></div>
              {canWrite && <button type="button" className="button secondary" disabled={locked('recipe')} onClick={() => { editFrom(shownRecipe); setRecipeMode('edit') }} aria-label={`${shownRecipe.id} 값으로 새 버전 만들기`}>수정 → 새 버전</button>}</div>
            <div className="rc-params"><div><span>실링 온도</span><strong>{shownRecipe.parameters.temperature_c}<small> ℃</small></strong></div><div><span>운전 속도</span><strong>{shownRecipe.parameters.speed_m_per_min}<small> m/min</small></strong></div></div>
            {canWrite && <div className="rc-next"><span>이 설정으로 생산하려면</span><button type="button" className="button primary" disabled={locked('order')} onClick={() => pickForOrder(shownRecipe)} aria-label={`${shownRecipe.id} 공정 설정을 작업지시에 사용`}>작업지시 만들기 →</button></div>}
          </div> : <div className="rc-card mes-empty">왼쪽에서 공정 설정을 고르세요.</div>}
        </div>
      </div>
    </section>
    <section id="mes-orders" className="mes-card" aria-labelledby="mes-orders-heading" hidden={tab !== 'orders'}>
      <h3 id="mes-orders-heading" className="sr-only">작업지시</h3>
      {released && <div id="mes-release-next" className="message success" role="status"><p>작업지시 {released} 배포를 확인했습니다. 다음 단계는 운전 제어에서 작업을 불러오는 것입니다.</p><button id="mes-release-bind" type="button" className="button primary" disabled={!!moving} onClick={() => void moveToBinding(released)}>{moving ? '이동 중…' : '운전 제어에서 불러오기'}</button></div>}
      {cancelOrder && <div id="cancellation-confirmation" className="message caution" role="group" aria-labelledby="cancellation-confirm-heading"><h3 id="cancellation-confirm-heading" ref={cancelHeading} tabIndex={-1}>작업지시를 취소하시겠습니까?</h3><p>설비에 연결되지 않은 작업만 취소할 수 있습니다. 이미 설비에 연결되었거나 조회 후 내용이 변경된 작업은 취소할 수 없습니다.</p><p>라인 {lines?.find(line => line.id === cancelOrder.line_id)?.name ?? cancelOrder.line_id}</p><p>{cancelOrder.id} · {cancelOrder.item_id} · {cancelOrder.recipe_version_id} · 목표 {cancelOrder.target_quantity.toLocaleString('ko-KR')}개</p><p>취소 후 작성 중 상태로 되돌리지 않습니다. 설비 정지·작업 종료·연결된 실행 취소는 이 요청에 포함되지 않습니다.</p><button id="cancellation-submit" type="button" className="button danger" disabled={cancellation.blocked(cancelOrder.id)} onClick={() => { if (!cancellation.blocked(cancelOrder.id)) { void cancellation.submit(cancelOrder); setCancelOrder(null) } }}>작업지시 취소 요청</button><button id="cancellation-close-confirm" type="button" className="button secondary" onClick={closeCancellation}>확인 닫기</button></div>}
      {releaseOrder && <div id="release-confirmation" className="message caution" role="group" aria-label="작업지시 배포 확인"><p>작업지시 {releaseOrder.id}의 품목·공정 설정·목표 수량을 배포하시겠습니까? 배포 후 작성 중 상태로 되돌리는 기능은 제공하지 않습니다. 설비 운전은 시작되지 않습니다.</p><button id="release-submit" type="button" className="button primary" disabled={!!workflow.attempt || workflow.orderBlocked(releaseOrder.id)} onClick={() => { if (!workflow.attempt && !workflow.orderBlocked(releaseOrder.id)) { void workflow.submit('release', { work_order_id: releaseOrder.id, actor: session.login, release_id: crypto.randomUUID(), expected_row_version: releaseOrder.row_version }); setReleaseOrder(null) } }}>작업지시 배포 요청</button><button type="button" className="button secondary" onClick={() => setReleaseOrder(null)}>확인 닫기</button></div>}
      <OrderBoard session={session} lines={lines} recipes={orderRecipeChoices} canWrite={canWrite} canBind={session.roles.some(role => ['operator', 'production_manager'].includes(role))} refreshKey={`${cancellation.ledger?.receipt?.cancellation_id ?? ''}|${released ?? ''}|${choiceRevision}`} onSessionInvalid={onSessionInvalid} onRelease={setReleaseOrder} onCancel={setCancelOrder} onBind={onSelectOrder} onOpenControl={onOpenControl} onReports={onOpenReports} onNew={() => reveal('mes-order-form', 'mes-order-id')} isBlocked={id => !!workflow.attempt || workflow.orderBlocked(id) || cancellation.blocked(id)} />
      {canWrite && <details className="mes-editor" id="mes-order-editor"><summary>새 작업지시 작성</summary><p className="form-help">선택한 공정 설정의 품목을 사용합니다. 등록 후 상태는 작성 중(DRAFT)입니다.</p>
        {validation.order && <p className="message error" role="alert">{validation.order}</p>}
        <form id="mes-order-form" onSubmit={submitOrder}><fieldset disabled={locked('order')}><legend className="sr-only">작성 중 작업지시 등록 내용</legend><div className="mes-form-grid">
          <div><label htmlFor="mes-order-id">작업지시 ID</label><input id="mes-order-id" required maxLength={128} value={orderId} onChange={event => { setOrderIdManual(true); setOrder(previous => ({ ...previous, id: event.target.value })) }} /><p className="input-help">{orderIdManual ? <button type="button" className="button text" onClick={() => setOrderIdManual(false)}>날짜 기준 번호로 다시 채우기</button> : '예정일 기준으로 다음 번호를 자동으로 붙입니다.'}</p></div>
          <div><label htmlFor="mes-order-recipe">공정 설정</label><select id="mes-order-recipe" required value={chosenRecipe?.id ?? ''} onChange={event => setSelectedRecipe(recipeChoices.find(item => item.id === event.target.value) ?? null)}><option value="" disabled>공정 설정을 선택해 주세요</option>{orderRecipeChoices.map(item => <option key={item.id} value={item.id}>{item.item_id} · {item.recipe_id} {item.version} ({item.parameters.temperature_c} ℃ / {item.parameters.speed_m_per_min} m/min)</option>)}</select><p className="input-help">{selectedRecipe ? '공정 설정 목록에서 고른 설정입니다.' : '가장 최근에 등록한 설정을 기본으로 고릅니다.'}</p></div>
          <div><label htmlFor="mes-order-item">품목 ID</label><input id="mes-order-item" readOnly value={chosenRecipe?.item_id ?? ''} aria-describedby="mes-order-item-help" /><p id="mes-order-item-help" className="input-help">선택한 공정 설정의 품목입니다.</p></div>
          <div><label htmlFor="mes-order-line">생산 라인</label><select id="mes-order-line" required value={order.line} onChange={event => setOrder(previous => ({ ...previous, line: event.target.value }))}><option value="" disabled>라인을 선택해 주세요</option>{lines?.map(line => <option key={line.id} value={line.id}>{line.name}</option>)}</select>{lineError ? <p className="input-help">{lineError}</p> : lines?.length === 0 ? <p className="input-help">선택할 라인이 없습니다. 설비 관리자에게 라인 등록과 수집원 연결을 확인해 주세요.</p> : null}</div>
          <div><label htmlFor="mes-order-target">목표 양품 수량 (개)</label><input id="mes-order-target" type="number" required min="1" max="4294967295" step="1" value={order.target} onChange={event => setOrder(previous => ({ ...previous, target: event.target.value }))} /></div>
          <div><label htmlFor="mes-order-date">생산 예정일</label><input id="mes-order-date" type="date" required value={order.date} onChange={event => setOrder(previous => ({ ...previous, date: event.target.value }))} /></div>
        </div><div className="tracker-actions"><button id="mes-order-submit-release" className="button primary" type="submit" disabled={!chosenRecipe || !lines?.some(line => line.id === order.line) || !!workflow.attempt}>등록하고 바로 배포</button><button id="mes-order-submit" className="button secondary" type="submit" disabled={!chosenRecipe || !lines?.some(line => line.id === order.line)}>작성 중 작업지시 등록</button></div></fieldset></form>
        {lineError && <button className="button secondary" type="button" onClick={() => setLineRevision(value => value + 1)}>라인 다시 조회</button>}
        <AttemptResult kind="order" attempt={attempts.order} checking={checking.order} onLookup={() => void lookup('order')} onReset={() => reset('order')} />
      </details>}
    </section>
    <section id="mes-items" className="mes-card" aria-label="품목" hidden={tab !== 'items'}><ItemsPanel session={session} canWrite={canWrite} items={masterItems} loaded={masterLoaded} recipes={allRecipes} orders={allOrders} onChanged={() => setChoiceRevision(value => value + 1)} onNewRecipe={item => { onTab('recipes'); resetRecipe(); chooseItem(item); setRecipeMode('edit'); reveal('mes-recipe-form', 'mes-recipe-temperature') }} /></section>
    <div id="mes-queue" hidden={tab !== 'queue'}><LineQueuePanel session={session} orders={allOrders} onSessionInvalid={onSessionInvalid} /></div>
  </section>
}

function AttemptResult({ kind, attempt, checking, onLookup, onReset }: { kind: RegistrationKind; attempt: Attempt | null; checking: boolean; onLookup: () => void; onReset: () => void }) {
  if (!attempt) return null
  const record = attempt.stored
  return <div id={`mes-${kind}-result`} className="mes-registration-result">
    <p id={`mes-${kind}-status`} data-state={attempt.phase} className={`message ${attempt.phase === 'confirmed' ? 'success' : attempt.phase === 'conflict' || attempt.phase === 'rejected' ? 'error' : 'caution'}`} role={attempt.phase === 'conflict' || attempt.phase === 'rejected' ? 'alert' : 'status'}>{attempt.message}</p>
    <p className="form-help">등록 ID: <strong id={`mes-${kind}-original-id`}>{attempt.definition.id}</strong></p>
    {record && <dl className="mes-result-facts"><div><dt>저장된 품목</dt><dd>{record.item_id}</dd></div>{kind === 'recipe' ? <><div><dt>설정 그룹 · 버전</dt><dd>{(record as RecipeVersion).recipe_id} · {(record as RecipeVersion).version}</dd></div><div><dt>온도 · 속도</dt><dd>{(record as RecipeVersion).parameters.temperature_c} ℃ · {(record as RecipeVersion).parameters.speed_m_per_min} m/min</dd></div></> : <><div><dt>저장 상태</dt><dd>{(record as WorkOrder).status === 'DRAFT' ? '작성 중 (DRAFT)' : (record as WorkOrder).status === 'RELEASED' ? '배포됨 (RELEASED)' : '취소됨 (CANCELLED)'}</dd></div><div><dt>목표 양품 · 예정일</dt><dd>{(record as WorkOrder).target_quantity.toLocaleString('ko-KR')}개 · {(record as WorkOrder).planned_date}</dd></div></>}<div><dt>등록자 · 등록 시각</dt><dd>{record.created_by} · {displayTime(record.created_at)}</dd></div></dl>}
    <div className="tracker-actions">{['uncertain', 'conflict'].includes(attempt.phase) && <button id={`mes-${kind}-lookup`} type="button" className="button secondary" disabled={checking} onClick={onLookup}>{checking ? '원래 등록 조회 중…' : '원래 ID로 등록 내용 조회'}</button>}{['confirmed', 'conflict', 'rejected'].includes(attempt.phase) && <button id={`mes-${kind}-reset`} type="button" className="button secondary" disabled={checking} onClick={onReset}>{attempt.phase === 'confirmed' ? '다른 등록 작성' : '입력 다시 검토'}</button>}</div>
  </div>
}
