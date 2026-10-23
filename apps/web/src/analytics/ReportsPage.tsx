import { useState } from 'react'
import type { Session } from '../auth'
import { AnalyticsError, parseDailyReport, parseExecutionReports, parseTimeline, postDowntimeReason, query, STOP_REASONS } from './api'
import type { DowntimeReasonBody, ExecutionReport, Segment } from './api'
import { Legend, ShareBar, StackedBars, StateTimeline } from './charts'
import { SERIES_COLORS } from './scale'
import type { TimelineSegment } from './charts'
import { clock, dateTime, duration, hourLabel, lineStateLabels, percent, quantity, seoulToday, settlementLabels, unknownReasonLabels } from './format'
import { useResource, useSources } from './hooks'
import type { Source } from './hooks'
import { Badge, Card, ChartOrTable, EmptyState, KpiTile, PageNotice, ProvisionalBadge, RefreshStatus, ScopePicker } from './ui'

const reasonLabel = (code: string) => STOP_REASONS.find(item => item.code === code)?.label ?? code
const settlementTone = (status: string) => status === 'SETTLED' ? 'good' : status === 'PENDING' ? 'warn' : 'bad'
function daysAgo(day: string, days: number) {
  const date = new Date(`${day}T00:00:00Z`)
  date.setUTCDate(date.getUTCDate() - days)
  return date.toISOString().slice(0, 10)
}
/** Seoul calendar day [start, end) as UTC instants. */
function seoulRange(fromDay: string, toDay: string) {
  const start = new Date(`${fromDay}T00:00:00+09:00`)
  const end = new Date(`${toDay}T00:00:00+09:00`)
  end.setUTCDate(end.getUTCDate() + 1)
  return { from: start.toISOString().replace('.000Z', 'Z'), to: end.toISOString().replace('.000Z', 'Z') }
}

function segmentTone(segment: Segment): TimelineSegment['tone'] {
  if (segment.class === 'RUNNING') return 'good'
  if (segment.class === 'UNKNOWN') return 'unknown'
  return segment.state === 'FAULT' || segment.state === 'ESTOP' || segment.state === 'RECOVERY_REQUIRED' ? 'bad' : segment.state === 'PAUSED' ? 'warn' : 'stopped'
}
function segmentLabel(segment: Segment) {
  if (segment.class === 'UNKNOWN') return `불명 · ${unknownReasonLabels[segment.unknown_reason ?? ''] ?? '근거 없음'}`
  const state = segment.state ? lineStateLabels[segment.state] ?? segment.state : '상태 불명'
  return segment.class === 'RUNNING' ? '가동' : `정지 · ${state}${segment.fault_code ? ` (고장 ${segment.fault_code})` : ''}`
}

interface Props { session: Session; scope: Source | null; onScope: (source: Source | null) => void; onSessionInvalid: () => void }

export default function ReportsPage(props: Props) {
  const [tab, setTab] = useState<'executions' | 'daily'>('executions')
  return <div className="analytics-page" data-page="reports">
    <div className="page-tabs" role="tablist" aria-label="보고서 종류">
      <button type="button" role="tab" id="report-tab-executions" aria-selected={tab === 'executions'} aria-controls="report-panel" onClick={() => setTab('executions')}>작업별 실적</button>
      <button type="button" role="tab" id="report-tab-daily" aria-selected={tab === 'daily'} aria-controls="report-panel" onClick={() => setTab('daily')}>일일 운영 보고</button>
    </div>
    <div id="report-panel" role="tabpanel" aria-labelledby={`report-tab-${tab}`}>
      {tab === 'executions' ? <ExecutionReports {...props} /> : <DailyReportView {...props} />}
    </div>
  </div>
}

function ExecutionReports({ session, scope, onScope, onSessionInvalid }: Props) {
  const sources = useSources(onSessionInvalid)
  const today = seoulToday()
  const [fromDay, setFromDay] = useState(daysAgo(today, 6))
  const [toDay, setToDay] = useState(today)
  const [selected, setSelected] = useState<ExecutionReport | null>(null)
  const range = seoulRange(fromDay, toDay)
  const params = scope ? { line_id: scope.line_id, from: range.from, to: range.to } : null
  const reports = useResource(params ? query('/api/v1/reports/executions', { ...params, limit: '50' }) : null, parseExecutionReports, onSessionInvalid)
  const items = reports.data?.items ?? []
  return <>
    <div className="filter-row">
      <ScopePicker id="report-scope" sources={sources.data} value={scope} onChange={value => { onScope(value); setSelected(null) }} />
      <div className="filter-field"><label className="filter-label" htmlFor="report-from">시작일</label><input id="report-from" type="date" value={fromDay} max={toDay} onChange={event => event.target.value && setFromDay(event.target.value)} /></div>
      <div className="filter-field"><label className="filter-label" htmlFor="report-to">종료일</label><input id="report-to" type="date" value={toDay} min={fromDay} max={today} onChange={event => event.target.value && setToDay(event.target.value)} /></div>
      <RefreshStatus loading={reports.loading} updatedAt={reports.updatedAt} onRefresh={reports.refresh} />
      {params && <a className="button secondary compact download" href={query('/api/v1/reports/executions.csv', params)} download>CSV 내려받기</a>}
    </div>
    {reports.error && <PageNotice tone="bad" title="작업별 실적을 조회하지 못했습니다.">{reports.error}</PageNotice>}
    {!scope && <EmptyState title="라인을 선택해 주세요." />}
    {scope && reports.data && items.length === 0 && <EmptyState title="기간 안에 등록된 실행이 없습니다.">작업지시를 라인에 연결하면 실행 단위로 실적이 쌓입니다.</EmptyState>}
    {items.length > 0 && <Card title="작업별 실적" kicker="EXECUTIONS">
      <div className="table-scroll"><table className="data-table selectable">
        <thead><tr><th scope="col">작업지시 · 품목</th><th scope="col">설정</th><th scope="col">목표</th><th scope="col">양품</th><th scope="col">불량</th><th scope="col">폐기</th><th scope="col">수율</th><th scope="col">가동 비율</th><th scope="col">시작</th><th scope="col">정산</th></tr></thead>
        <tbody>{items.map(item => <tr key={item.execution_id} className={selected?.execution_id === item.execution_id ? 'is-selected' : ''}>
          <th scope="row"><button type="button" className="link-button" aria-pressed={selected?.execution_id === item.execution_id} onClick={() => setSelected(item)}>{item.work_order_id}</button><small>{item.item_id ?? '품목 미연결'}</small></th>
          <td>{item.recipe_version}</td>
          <td>{item.target_quantity?.toLocaleString('ko-KR') ?? '—'}</td>
          <td>{quantity(item.totals.good)}</td><td>{quantity(item.totals.reject)}</td><td>{quantity(item.totals.scrap)}</td>
          <td>{percent(item.yield_ratio)}</td>
          <td>{percent(item.durations_seconds.total > 0 ? item.durations_seconds.running / item.durations_seconds.total : null)}{item.durations_seconds.unknown > 0 && <small>불명 {duration(item.durations_seconds.unknown)}</small>}</td>
          <td>{dateTime(item.started_at)}</td>
          <td><Badge tone={settlementTone(item.settlement_status)}>{settlementLabels[item.settlement_status] ?? item.settlement_status}</Badge>{item.provisional && <small>잠정</small>}</td>
        </tr>)}</tbody>
      </table></div>
      {reports.data?.next_cursor && <p className="card-note">기간 안의 실행이 더 있습니다. 기간을 좁혀 조회해 주세요.</p>}
    </Card>}
    {selected && scope && <ExecutionDetail key={selected.execution_id} report={selected} scope={scope} session={session} onSessionInvalid={onSessionInvalid} />}
  </>
}

function ExecutionDetail({ report, scope: selectedScope, session, onSessionInvalid }: { report: ExecutionReport; scope: Source; session: Session; onSessionInvalid: () => void }) {
  // A line-wide list can hold runs from other sources; detail requests use the run's own scope.
  const scope: Source = report.source_id && report.stream_id ? { ...selectedScope, source_id: report.source_id, stream_id: report.stream_id } : selectedScope
  const path = query(`/api/v1/reports/executions/${encodeURIComponent(report.execution_id)}/timeline`, { line_id: scope.line_id, source_id: scope.source_id, stream_id: scope.stream_id })
  const timeline = useResource(path, parseTimeline, onSessionInvalid)
  const [segmentKey, setSegmentKey] = useState<string | null>(null)
  const canAnnotate = session.roles.some(role => role === 'operator' || role === 'production_manager')
  const data = timeline.data
  const segment = data?.segments.find(item => item.from === segmentKey) ?? null
  return <Card title={`${report.work_order_id} 실행 상세`} kicker="RUN DETAIL" actions={<ProvisionalBadge provisional={report.provisional} reasons={report.provisional_reasons} />}>
    <div className="kpi-grid compact">
      <KpiTile label="양품 / 목표" value={quantity(report.totals.good)} unit={report.target_quantity ? ` / ${report.target_quantity.toLocaleString('ko-KR')}개` : '개'} />
      <KpiTile label="가동" value={duration(report.durations_seconds.running)} tone="good" />
      <KpiTile label="정지" value={duration(report.durations_seconds.stopped)} />
      <KpiTile label="불명" value={duration(report.durations_seconds.unknown)} tone={report.durations_seconds.unknown > 0 ? 'unknown' : 'neutral'} />
    </div>
    {timeline.error && <PageNotice tone="bad" title="상태 타임라인을 조회하지 못했습니다.">{timeline.error}</PageNotice>}
    {data && <>
      {!data.check_ok && <PageNotice tone="warn" title="시간 검산이 맞지 않습니다.">가동·정지·불명 합계가 전체 시간과 다릅니다. 이 실행의 시간 값은 확정하지 않습니다.</PageNotice>}
      <Legend items={[{ label: '가동', color: 'var(--status-good)' }, { label: '정지', color: '#8796a8' }, { label: '일시 정지', color: 'var(--status-warn)' }, { label: '고장 · 비상정지', color: 'var(--status-bad)' }, { label: '불명', color: '', pattern: true }]} />
      <StateTimeline from={Date.parse(data.from)} to={Date.parse(data.to)} selected={segmentKey} onSelect={key => setSegmentKey(key === segmentKey ? null : key)}
        segments={data.segments.map(item => ({
          key: item.from, from: Date.parse(item.from), to: Date.parse(item.to), tone: segmentTone(item), label: segmentLabel(item),
          detail: item.stop_reason ? `사유: ${reasonLabel(item.stop_reason.code)}${item.stop_reason.note ? ` · ${item.stop_reason.note}` : ''}` : item.class === 'STOPPED' ? '사유 미입력' : undefined,
          selectable: item.class === 'STOPPED',
        }))} />
      <p className="card-note">정지 구간을 선택하면 사유를 확인하거나 입력할 수 있습니다. 불명 구간에는 사유를 넣지 않습니다.</p>
      <div className="table-scroll"><table className="data-table">
        <thead><tr><th scope="col">구간</th><th scope="col">분류</th><th scope="col">길이</th><th scope="col">정지 사유</th></tr></thead>
        <tbody>{data.segments.filter(item => item.class !== 'RUNNING').map(item => <tr key={item.from} className={segmentKey === item.from ? 'is-selected' : ''}>
          <th scope="row">{clock(item.from)} ~ {clock(item.to)}</th>
          <td>{segmentLabel(item)}</td>
          <td>{duration((Date.parse(item.to) - Date.parse(item.from)) / 1000)}</td>
          <td>{item.class === 'STOPPED'
            ? <button type="button" className="link-button" onClick={() => setSegmentKey(item.from)}>{item.stop_reason ? reasonLabel(item.stop_reason.code) : '미입력 · 입력하기'}</button>
            : '—'}</td>
        </tr>)}</tbody>
      </table></div>
      {segment && segment.class === 'STOPPED' && <StopReasonEditor key={segment.from} segment={segment} report={report} scope={scope} session={session} canAnnotate={canAnnotate} onSaved={timeline.refresh} onSessionInvalid={onSessionInvalid} />}
    </>}
  </Card>
}

function StopReasonEditor({ segment, report, scope, session, canAnnotate, onSaved, onSessionInvalid }: { segment: Segment; report: ExecutionReport; scope: Source; session: Session; canAnnotate: boolean; onSaved: () => void; onSessionInvalid: () => void }) {
  const [code, setCode] = useState(segment.stop_reason?.code ?? 'UNCLASSIFIED')
  const [note, setNote] = useState(segment.stop_reason?.note ?? '')
  // The request body (with its id) is fixed on first submit so a lost response is retried unchanged.
  const [pending, setPending] = useState<DowntimeReasonBody | null>(null)
  const [message, setMessage] = useState<{ tone: 'good' | 'bad'; text: string } | null>(null)
  const [busy, setBusy] = useState(false)
  async function submit(body: DowntimeReasonBody) {
    setBusy(true)
    setMessage(null)
    try {
      await postDowntimeReason(body, session.csrf_token)
      setPending(null)
      setMessage({ tone: 'good', text: '정지 사유를 저장했습니다.' })
      onSaved()
    } catch (cause) {
      if (cause instanceof AnalyticsError && cause.status === 401) { onSessionInvalid(); return }
      if (cause instanceof AnalyticsError && cause.status !== 0) setPending(null)
      setMessage({ tone: 'bad', text: cause instanceof AnalyticsError ? cause.message : '정지 사유를 저장하지 못했습니다.' })
    } finally { setBusy(false) }
  }
  const changed = pending !== null && (pending.reason_code !== code || pending.note !== note.trim())
  return <form className="reason-editor" onSubmit={event => {
    event.preventDefault()
    const body = pending && !changed ? pending : { request_id: crypto.randomUUID(), execution_id: report.execution_id, line_id: scope.line_id, source_id: scope.source_id, stream_id: scope.stream_id, segment_from: segment.from, reason_code: code, note: note.trim() }
    setPending(body)
    void submit(body)
  }}>
    <h3>정지 사유 · {clock(segment.from)} ~ {clock(segment.to)}</h3>
    {segment.stop_reason && <p className="card-note">현재: {reasonLabel(segment.stop_reason.code)} · {segment.stop_reason.recorded_by} · {dateTime(segment.stop_reason.recorded_at)}</p>}
    <fieldset disabled={!canAnnotate || busy}>
      <div className="reason-options" role="radiogroup" aria-label="정지 사유">
        {STOP_REASONS.map(item => <label key={item.code} className={code === item.code ? 'is-checked' : ''}><input type="radio" name="stop-reason" value={item.code} checked={code === item.code} onChange={() => setCode(item.code)} />{item.label}</label>)}
      </div>
      <label htmlFor="stop-reason-note">메모 <small>(선택, 200자 이내)</small></label>
      <input id="stop-reason-note" maxLength={200} value={note} onChange={event => setNote(event.target.value)} />
      <button type="submit" className="button primary">{busy ? '저장 중…' : pending && !changed ? '같은 요청으로 다시 저장' : '사유 저장'}</button>
    </fieldset>
    {!canAnnotate && <p className="card-note">작업자 또는 생산 담당자만 정지 사유를 입력할 수 있습니다.</p>}
    {message && <p className={`inline-message ${message.tone}`} role="status">{message.text}</p>}
  </form>
}

function DailyReportView({ scope, onScope, onSessionInvalid }: Props) {
  const sources = useSources(onSessionInvalid)
  const today = seoulToday()
  const [day, setDay] = useState(today)
  const params = scope ? { line_id: scope.line_id, date: day } : null
  const report = useResource(params ? query('/api/v1/reports/daily', params) : null, parseDailyReport, onSessionInvalid)
  const data = report.data
  const stopped = data ? Object.entries(data.stopped_by_reason).sort((a, b) => b[1] - a[1]) : []
  const maxStopped = Math.max(1, ...stopped.map(([, value]) => value))
  return <>
    <div className="filter-row">
      <ScopePicker id="daily-scope" sources={sources.data} value={scope} onChange={onScope} />
      <div className="filter-field"><label className="filter-label" htmlFor="daily-date">날짜 (한국 시간)</label><input id="daily-date" type="date" value={day} max={today} onChange={event => event.target.value && setDay(event.target.value)} /></div>
      <RefreshStatus loading={report.loading} updatedAt={report.updatedAt} onRefresh={report.refresh} />
      {params && <a className="button secondary compact download" href={query('/api/v1/reports/daily.csv', params)} download>CSV 내려받기</a>}
    </div>
    {report.error && <PageNotice tone="bad" title="일일 보고를 조회하지 못했습니다.">{report.error}</PageNotice>}
    {!scope && <EmptyState title="라인을 선택해 주세요." />}
    {data && <div className={report.loading ? 'is-refreshing' : ''}>
      <div className="report-status"><ProvisionalBadge provisional={data.provisional} reasons={data.provisional_reasons} /><span>{dateTime(data.from)} ~ {dateTime(data.to)} · 실행 {data.executions.length}건</span></div>
      <div className="kpi-grid">
        <KpiTile label="양품" value={quantity(data.totals.good)} unit="개" />
        <KpiTile label="불량 · 폐기" value={quantity(data.totals.reject)} unit="개">폐기 {quantity(data.totals.scrap)}개</KpiTile>
        <KpiTile label="가동 비율" value={percent(data.running_ratio)} tone={data.running_ratio === null ? 'unknown' : 'neutral'}>불명 {percent(data.unknown_ratio)} 포함</KpiTile>
        <KpiTile label="미확인 알람" value={String(data.unacknowledged_alarms)} unit="건" tone={data.unacknowledged_alarms > 0 ? 'warn' : 'good'} />
      </div>
      <div className="dashboard-grid">
        <Card title="가동 · 정지 · 불명 시간" kicker="TIME BALANCE">
          <ShareBar total={data.durations_seconds.total} format={duration} items={[
            { id: 'running', label: '가동', value: data.durations_seconds.running, tone: 'good' },
            { id: 'stopped', label: '정지', value: data.durations_seconds.stopped, tone: 'stopped' },
            { id: 'unknown', label: '불명', value: data.durations_seconds.unknown, tone: 'unknown' },
          ]} />
          <p className="card-note">검산: 가동 + 정지 + 불명 = 보고 대상 {duration(data.durations_seconds.total)}</p>
        </Card>
        <Card title="정지 사유별 시간" kicker="DOWNTIME REASONS">
          {stopped.every(([, value]) => value === 0) ? <EmptyState title="정지 시간이 없습니다." /> : <ul className="reason-bars">{stopped.map(([code, value]) => <li key={code}>
            <span>{reasonLabel(code)}</span>
            <span className="reason-bar"><span style={{ width: `${(value / maxStopped) * 100}%` }} /></span>
            <b>{duration(value)}</b>
          </li>)}</ul>}
          <p className="card-note">사유를 입력하지 않은 정지는 미분류로 집계합니다.</p>
        </Card>
      </div>
      <Card title="시간대별 생산" kicker="HOURLY OUTPUT" actions={<Legend items={[{ label: '양품', color: SERIES_COLORS[0] }, { label: '불량', color: SERIES_COLORS[1] }]} />}>
        <ChartOrTable label="시간대별 생산" chart={<StackedBars title="시간대별 양품·불량" unit="개" data={data.hourly.map(bucket => ({
          key: bucket.hour_start, label: hourLabel(bucket.hour_start),
          segments: [
            { id: 'good', label: '양품', value: Number(bucket.good), color: SERIES_COLORS[0] },
            { id: 'reject', label: '불량', value: Number(bucket.reject), color: SERIES_COLORS[1] },
          ],
          detail: <small>가동 {duration(bucket.running_seconds)} · 불명 {duration(bucket.unknown_seconds)}</small>,
        }))} />} table={<table className="data-table">
          <thead><tr><th scope="col">시간</th><th scope="col">양품</th><th scope="col">불량</th><th scope="col">가동</th><th scope="col">불명</th></tr></thead>
          <tbody>{data.hourly.map(bucket => <tr key={bucket.hour_start}><th scope="row">{clock(bucket.hour_start)}</th><td>{quantity(bucket.good)}</td><td>{quantity(bucket.reject)}</td><td>{duration(bucket.running_seconds)}</td><td>{duration(bucket.unknown_seconds)}</td></tr>)}</tbody>
        </table>} />
      </Card>
    </div>}
  </>
}
