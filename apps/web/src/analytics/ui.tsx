import { useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import { scopeKey } from './hooks'
import type { Source } from './hooks'
import './analytics.css'
import { clock, provisionalLabels } from './format'

/** Line/source selector shared by the reporting pages. A single registered source is selected automatically. */
export function ScopePicker({ id, sources, value, onChange }: { id: string; sources: Source[] | null; value: Source | null; onChange: (source: Source | null) => void }) {
  useEffect(() => {
    if (sources && sources.length === 1 && (!value || scopeKey(value) !== scopeKey(sources[0]))) onChange(sources[0])
  }, [sources, value, onChange])
  if (sources === null) return <div className="filter-field"><span className="filter-label">라인 · 수집원</span><span className="filter-static">확인 중…</span></div>
  if (sources.length === 0) return <div className="filter-field"><span className="filter-label">라인 · 수집원</span><span className="filter-static">등록된 수집원 없음</span></div>
  return <div className="filter-field">
    <label className="filter-label" htmlFor={id}>라인 · 수집원</label>
    <select id={id} value={value ? scopeKey(value) : ''} onChange={event => onChange(sources.find(item => scopeKey(item) === event.target.value) ?? null)}>
      <option value="" disabled>선택해 주세요</option>
      {sources.map(source => <option key={scopeKey(source)} value={scopeKey(source)}>{source.line_name} · {source.source_id} / {source.stream_id}</option>)}
    </select>
  </div>
}

export function RefreshStatus({ loading, updatedAt, onRefresh, auto }: { loading: boolean; updatedAt: number | null; onRefresh: () => void; auto?: string }) {
  return <div className="refresh-status">
    <span aria-live="polite">{loading ? '조회 중…' : updatedAt ? `${auto ? `${auto} · ` : ''}마지막 조회 ${clock(updatedAt)}` : ''}</span>
    <button type="button" className="button secondary compact" onClick={onRefresh} disabled={loading}>새로 조회</button>
  </div>
}

export type Tone = 'good' | 'warn' | 'bad' | 'unknown' | 'info' | 'neutral'

export function Badge({ tone, children, title }: { tone: Tone; children: ReactNode; title?: string }) {
  return <span className={`badge ${tone}`} title={title}>{children}</span>
}

export function ProvisionalBadge({ provisional, reasons }: { provisional: boolean; reasons?: string[] }) {
  if (!provisional) return <Badge tone="good">확정</Badge>
  const detail = reasons?.map(reason => provisionalLabels[reason] ?? reason).join(', ')
  return <Badge tone="warn" title={detail}>잠정{detail ? ` · ${detail}` : ''}</Badge>
}

export function KpiTile({ label, value, unit, tone = 'neutral', children }: { label: string; value: string; unit?: string; tone?: Tone; children?: ReactNode }) {
  return <div className={`kpi-tile ${tone}`}>
    <span className="kpi-label">{label}</span>
    <strong className="kpi-value">{value}{unit && value !== '—' && <small>{unit}</small>}</strong>
    {children && <div className="kpi-detail">{children}</div>}
  </div>
}

export function Card({ title, kicker, actions, children, className = '' }: { title: string; kicker?: string; actions?: ReactNode; children: ReactNode; className?: string }) {
  return <section className={`panel-card ${className}`}>
    <header className="panel-card-head">
      <div>{kicker && <p className="section-kicker">{kicker}</p>}<h2>{title}</h2></div>
      {actions && <div className="panel-card-actions">{actions}</div>}
    </header>
    {children}
  </section>
}

/** Every chart has a table twin; the toggle keeps both reachable. */
export function ChartOrTable({ label, chart, table }: { label: string; chart: ReactNode; table: ReactNode }) {
  const [showTable, setShowTable] = useState(false)
  return <div className="chart-or-table">
    <div className="view-toggle" role="group" aria-label={`${label} 보기 방식`}>
      <button type="button" aria-pressed={!showTable} onClick={() => setShowTable(false)}>차트</button>
      <button type="button" aria-pressed={showTable} onClick={() => setShowTable(true)}>표</button>
    </div>
    {showTable ? <div className="table-scroll">{table}</div> : chart}
  </div>
}

export function PageNotice({ tone, title, children }: { tone: 'info' | 'warn' | 'bad' | 'unknown'; title: string; children?: ReactNode }) {
  return <div className={`page-notice ${tone}`} role={tone === 'bad' ? 'alert' : 'status'}><strong>{title}</strong>{children && <span>{children}</span>}</div>
}

export function EmptyState({ title, children }: { title: string; children?: ReactNode }) {
  return <div className="empty-state"><strong>{title}</strong>{children && <p>{children}</p>}</div>
}
