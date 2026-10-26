import { useCallback, useEffect, useState } from 'react'
import type { Session } from './auth'
import { conflictMessage, reasonCodeOf } from './conflictReasons'
import { MesHTTPError, mesRequest, validId } from './mesApi'
import type { WorkOrder } from './mesApi'

interface QueueEntry { entry_id: string; position: number; work_order_id: string; state: 'WAITING' | 'BOUND' | 'REMOVED'; expected_row_version: string }
interface Queue { line_id: string; revision: string; auto_advance: boolean; source_id: string | null; stream_id: string | null; blocked_reason: string | null; entries: QueueEntry[] }
interface Source { line_id: string; source_id: string; stream_id: string }

function parseQueue(value: unknown): Queue {
  const raw = value as Record<string, unknown>
  if (!raw || typeof raw !== 'object' || !validId(raw.line_id) || typeof raw.revision !== 'string' || typeof raw.auto_advance !== 'boolean' || !Array.isArray(raw.entries)) throw new Error('Unconfirmed queue')
  const entries = raw.entries.map(entry => {
    const item = entry as Record<string, unknown>
    if (!validId(item.entry_id) || !validId(item.work_order_id) || !['WAITING', 'BOUND', 'REMOVED'].includes(item.state as string) || typeof item.expected_row_version !== 'string') throw new Error('Unconfirmed queue entry')
    return { entry_id: item.entry_id, position: Number(item.position), work_order_id: item.work_order_id, state: item.state as QueueEntry['state'], expected_row_version: item.expected_row_version }
  })
  return { line_id: raw.line_id, revision: raw.revision, auto_advance: raw.auto_advance, source_id: typeof raw.source_id === 'string' ? raw.source_id : null, stream_id: typeof raw.stream_id === 'string' ? raw.stream_id : null, blocked_reason: typeof raw.blocked_reason === 'string' ? raw.blocked_reason : null, entries }
}

async function putQueue(path: string, csrf: string, body: object): Promise<unknown> {
  const response = await fetch(path, {
    method: 'PUT', credentials: 'same-origin', cache: 'no-store',
    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf }, body: JSON.stringify(body),
    signal: AbortSignal.timeout(12_000),
  })
  if (!response.ok) {
    throw new MesHTTPError(response.status, await reasonCodeOf(response))
  }
  return response.json()
}

const waits: Record<string, string> = {
  ORIGINAL_LOAD_PENDING: '앞 작업의 작업 불러오기 결과를 기다리는 중입니다.',
  PRIOR_EXECUTION_REQUIRED: '첫 작업은 직접 실행 연결해 주세요. 그 작업이 정산되면 다음 작업부터 자동으로 연결합니다.',
  PRIOR_EXECUTION_SETTLEMENT_PENDING: '앞 작업의 실적 정산을 기다리는 중입니다.',
  FRESH_SOURCE_REQUIRED: '설비의 최신 값을 기다리는 중입니다.',
}
const blocks: Record<string, string> = {
  ORIGINAL_LOAD_REQUIRES_HUMAN: '앞 작업의 작업 불러오기가 실패했거나 결과가 불명입니다',
  PRIOR_EXECUTION_NOT_SETTLED: '앞 작업의 정산이 일치하지 않았습니다',
  PHYSICAL_EXECUTION_MISMATCH: '설비에 올라간 작업이 서버 기록과 다릅니다',
  QUEUE_HEAD_CHANGED: '맨 앞 작업지시가 그사이 바뀌었습니다',
  SOURCE_CONFIGURATION_CHANGED: '수집원 설정이 바뀌었습니다',
  PRIOR_EXECUTION_SCOPE_CHANGED: '앞 작업의 라인·수집원이 바뀌었습니다',
  ENABLER_PERMISSION_REVOKED: '자동 연결을 켠 계정의 권한이 없어졌습니다',
  BINDING_REQUIRES_HUMAN: '자동 실행 연결이 거절되었습니다',
}

/** A wait clears by itself; a block keeps the queue stopped until a person re-enables it. */
function queueNotice(reason: string): { wait: boolean; text: string } {
  if (reason in waits) return { wait: true, text: waits[reason] }
  if (reason.startsWith('LOAD_CONDITION_NOT_MET:')) return { wait: true, text: `라인이 다음 작업을 불러올 조건(${reason.slice('LOAD_CONDITION_NOT_MET:'.length)})을 기다리는 중입니다.` }
  return { wait: false, text: blocks[reason] ?? reason }
}

/** CX-36 line queue: released orders wait in order and the server binds the next one after settlement. */
export default function LineQueuePanel({ session, orders, onSessionInvalid }: { session: Session; orders: readonly WorkOrder[]; onSessionInvalid: () => void }) {
  const canWrite = session.roles.includes('production_manager') && !session.must_change_password
  const [source, setSource] = useState<Source | null>(null)
  const [queue, setQueue] = useState<Queue | null>(null)
  const [draft, setDraft] = useState<string[] | null>(null)
  const [autoAdvance, setAutoAdvance] = useState<boolean | null>(null)
  const [message, setMessage] = useState('')
  const [unsupported, setUnsupported] = useState(false)
  const [saving, setSaving] = useState(false)
  const [revision, setRevision] = useState(0)
  const reload = useCallback(() => { setDraft(null); setAutoAdvance(null); setRevision(value => value + 1) }, [])

  useEffect(() => {
    const controller = new AbortController()
    void (async () => {
      const sources = await mesRequest('/api/v1/sources', controller.signal) as { sources?: Source[] }
      const first = sources.sources?.[0]
      if (!first) return
      setSource(first)
      setQueue(parseQueue(await mesRequest(`/api/v1/lines/${encodeURIComponent(first.line_id)}/queue`, controller.signal)))
    })().catch(cause => {
      if (controller.signal.aborted) return
      if (cause instanceof MesHTTPError && (cause.status === 401 || cause.status === 403)) { onSessionInvalid(); return }
      if (cause instanceof MesHTTPError && (cause.status === 404 || cause.status === 503)) { setUnsupported(true); return }
      setMessage('작업 대기열을 불러오지 못했습니다.')
    })
    return () => controller.abort()
  }, [revision, onSessionInvalid])

  if (unsupported) return null
  const waiting = queue?.entries.filter(entry => entry.state === 'WAITING').sort((a, b) => a.position - b.position).map(entry => entry.work_order_id) ?? []
  const list = draft ?? waiting
  const auto = autoAdvance ?? queue?.auto_advance ?? false
  const queued = new Set(queue?.entries.filter(entry => entry.state !== 'REMOVED').map(entry => entry.work_order_id) ?? [])
  const candidates = orders.filter(order => order.status === 'RELEASED' && !queued.has(order.id) && !list.includes(order.id) && order.line_id === source?.line_id)
  const changed = draft !== null || autoAdvance !== null
  const move = (index: number, step: number) => setDraft(() => { const next = [...list]; const [item] = next.splice(index, 1); next.splice(index + step, 0, item); return next })

  async function save() {
    if (!queue || !source || saving) return
    const versions = new Map(orders.map(order => [order.id, order.row_version]))
    const existing = new Map(queue.entries.map(entry => [entry.work_order_id, entry.expected_row_version]))
    const entries = list.map(id => ({ work_order_id: id, expected_row_version: existing.get(id) ?? versions.get(id) ?? '' }))
    if (entries.some(entry => !entry.expected_row_version)) { setMessage('작업지시 목록을 새로 조회한 뒤 다시 저장해 주세요.'); return }
    setSaving(true); setMessage('')
    try {
      setQueue(parseQueue(await putQueue(`/api/v1/lines/${encodeURIComponent(source.line_id)}/queue`, session.csrf_token, {
        request_id: crypto.randomUUID(), expected_revision: queue.revision, source_id: source.source_id, stream_id: source.stream_id, auto_advance: auto, entries,
      })))
      setDraft(null); setAutoAdvance(null); setMessage('대기열을 저장했습니다.')
    } catch (cause) {
      if (cause instanceof MesHTTPError && (cause.status === 401 || cause.status === 403)) { onSessionInvalid(); return }
      setMessage(cause instanceof MesHTTPError && cause.status === 409 ? conflictMessage(cause.reasonCode) ?? '대기열이 그사이 바뀌었거나 넣을 수 없는 작업지시가 있습니다. 새로 조회한 뒤 다시 저장해 주세요.' : '대기열 저장 결과를 확인하지 못했습니다. 새로 조회해 주세요.')
    } finally { setSaving(false) }
  }

  return <section className="mes-card" id="line-queue" aria-labelledby="line-queue-heading">
    <div className="mes-card-heading"><div><h3 id="line-queue-heading">작업 대기열</h3><p>배포된 작업지시를 순서대로 세워 두면, 앞 작업이 끝나 정산되는 대로 서버가 다음 작업을 라인에 연결합니다.</p></div><button className="button secondary" type="button" onClick={reload}>새로 조회</button></div>
    {!queue ? <p role="status">{message || '대기열을 조회하고 있습니다.'}</p> : <>
      <label className="check"><input id="line-queue-auto" type="checkbox" checked={auto} disabled={!canWrite} onChange={event => setAutoAdvance(event.target.checked)} /> 앞 작업이 정산되면 다음 작업을 자동으로 연결</label>
      {queue.blocked_reason && (() => { const notice = queueNotice(queue.blocked_reason); return notice.wait ? <p id="line-queue-wait" className="form-help" role="status">{notice.text}</p> : <p id="line-queue-blocked" className="message caution">자동 연결이 멈춰 있습니다: {notice.text}. 원인을 확인한 뒤 다시 켜 주세요.</p> })()}
      {list.length === 0 ? <p className="mes-empty">대기 중인 작업이 없습니다.</p> : <ol id="line-queue-list" className="queue-list">{list.map((id, index) => <li key={id}><span>{index + 1}. {id}</span>{canWrite && <span className="queue-actions"><button type="button" className="button secondary" disabled={index === 0} onClick={() => move(index, -1)} aria-label={`${id} 위로`}>▲</button><button type="button" className="button secondary" disabled={index === list.length - 1} onClick={() => move(index, 1)} aria-label={`${id} 아래로`}>▼</button><button type="button" className="button secondary" onClick={() => setDraft(list.filter(item => item !== id))}>빼기</button></span>}</li>)}</ol>}
      {canWrite && candidates.length > 0 && <div className="queue-add"><span>배포된 작업지시 추가:</span>{candidates.map(order => <button key={order.id} type="button" className="button secondary" onClick={() => setDraft([...list, order.id])}>+ {order.id}</button>)}</div>}
      {canWrite && <div className="tracker-actions"><button id="line-queue-save" type="button" className="button primary" disabled={!changed || saving} onClick={() => void save()}>{saving ? '저장 중…' : '대기열 저장'}</button>{changed && <button type="button" className="button secondary" onClick={() => { setDraft(null); setAutoAdvance(null) }}>변경 취소</button>}</div>}
      {message && <p className="form-help" role="status">{message}</p>}
    </>}
  </section>
}
