import { parseDashboard, query } from './api'
import type { Dashboard } from './api'
import { Legend, ShareBar, StackedBars } from './charts'
import { SERIES_COLORS } from './scale'
import { clock, dateTime, duration, hourLabel, lineStateLabels, percent, quantity, seoulToday, settlementLabels } from './format'
import { useResource, useSources } from './hooks'
import type { Source } from './hooks'
import { Badge, Card, ChartOrTable, EmptyState, KpiTile, PageNotice, ProvisionalBadge, RefreshStatus, ScopePicker } from './ui'
import type { Tone } from './ui'
import { useState } from 'react'

const REFRESH_MS = 10_000

function freshnessNotice(data: Dashboard) {
  if (data.freshness.status === 'LIVE') return null
  const last = data.freshness.last_receipt_at ? `마지막 수신 ${dateTime(data.freshness.last_receipt_at)}.` : '수신 기록이 없습니다.'
  return data.freshness.status === 'NO_DATA'
    ? <PageNotice tone="unknown" title="설비 데이터가 수신되지 않고 있습니다.">{last} 아래 값은 저장된 기록 기준이며 현재 설비 상태를 뜻하지 않습니다.</PageNotice>
    : <PageNotice tone="warn" title="설비 데이터 수신이 지연되고 있습니다.">{last} 현재 상태는 확인되지 않았습니다.</PageNotice>
}

const lineTone = (state: string | null): Tone => state === 'RUNNING' ? 'good' : state === 'FAULT' || state === 'ESTOP' || state === 'RECOVERY_REQUIRED' ? 'bad' : state === 'PAUSED' || state === 'STOPPING' ? 'warn' : state ? 'neutral' : 'unknown'

export default function DashboardPage({ scope, onScope, onSessionInvalid }: { scope: Source | null; onScope: (source: Source | null) => void; onSessionInvalid: () => void }) {
  const sources = useSources(onSessionInvalid)
  const [day, setDay] = useState(seoulToday())
  const today = seoulToday()
  const path = scope ? query('/api/v1/dashboard', { line_id: scope.line_id, source_id: scope.source_id, stream_id: scope.stream_id, date: day }) : null
  const dashboard = useResource(path, parseDashboard, onSessionInvalid, day === today ? REFRESH_MS : undefined)
  const data = dashboard.data

  return <div className="analytics-page" data-page="dashboard">
    <div className="filter-row">
      <ScopePicker id="dashboard-scope" sources={sources.data} value={scope} onChange={onScope} />
      <div className="filter-field">
        <label className="filter-label" htmlFor="dashboard-date">날짜 (한국 시간)</label>
        <input id="dashboard-date" type="date" value={day} max={today} onChange={event => event.target.value && setDay(event.target.value)} />
      </div>
      <RefreshStatus loading={dashboard.loading} updatedAt={dashboard.updatedAt} onRefresh={dashboard.refresh} auto={day === today ? '10초마다 갱신' : undefined} />
    </div>
    {sources.error && <PageNotice tone="bad" title="수집원 목록을 조회하지 못했습니다.">{sources.error}</PageNotice>}
    {dashboard.error && <PageNotice tone="bad" title="대시보드를 조회하지 못했습니다.">{dashboard.error}{data ? ' 아래는 이전 조회 결과입니다.' : ''}</PageNotice>}
    {!scope && <EmptyState title="라인을 선택해 주세요.">선택한 라인의 생산·가동·알람 현황을 보여줍니다.</EmptyState>}
    {scope && !data && !dashboard.error && <EmptyState title="현황을 불러오는 중입니다." />}
    {data && <div className={dashboard.loading ? 'is-refreshing' : ''}>
      {freshnessNotice(data)}
      {data.time.unknown_ratio !== null && data.time.unknown_ratio >= 0.5 && <PageNotice tone="unknown" title={`보고 시간의 ${percent(data.time.unknown_ratio)}를 가동·정지로 판정하지 못했습니다.`}>
        시스템이 수집하지 않았거나 수집 시각을 신뢰할 근거가 없는 구간은 불명으로 남깁니다. 불명을 정지나 가동으로 바꾸지 않으므로 가동 비율이 낮게 보일 수 있습니다.
      </PageNotice>}
      <div className="kpi-grid">
        <KpiTile label="양품 수량" value={quantity(data.production.good)} unit="개" tone="neutral">
          <ProvisionalBadge provisional={data.production.provisional} reasons={data.production.provisional ? [data.production.basis] : []} />
        </KpiTile>
        <KpiTile label="수율 (양품 ÷ 양품+불량)" value={percent(data.production.yield_ratio)} tone={data.production.yield_ratio === null ? 'unknown' : data.production.yield_ratio >= 0.95 ? 'good' : 'warn'}>
          불량 {quantity(data.production.reject)}개 · 폐기 {quantity(data.production.scrap)}개
        </KpiTile>
        <KpiTile label="가동 비율" value={percent(data.time.running_ratio)} tone={data.time.running_ratio === null ? 'unknown' : 'neutral'}>
          불명 {percent(data.time.unknown_ratio)} 포함 · 전체 {duration(data.time.total_seconds)}
        </KpiTile>
        <KpiTile label="활성 알람" value={String(data.alarms.active_total)} unit="건" tone={data.alarms.active_total > 0 ? 'bad' : 'good'}>
          미확인 {data.alarms.unacknowledged}건{Object.entries(data.alarms.active_by_severity).map(([severity, amount]) => ` · ${severity} ${amount}`).join('')}
        </KpiTile>
      </div>
      <div className="dashboard-grid">
        <Card title="진행 중인 작업" kicker="CURRENT RUN">
          {data.active_execution ? <div className="run-card">
            <div className="run-head">
              <div><strong>{data.active_execution.work_order_id}</strong><span>{data.active_execution.item_id ?? '품목 미연결'}</span></div>
              <Badge tone={lineTone(data.active_execution.line_state)}>{data.active_execution.line_state ? lineStateLabels[data.active_execution.line_state] ?? data.active_execution.line_state : '상태 불명'}</Badge>
            </div>
            <div className="progress" role="progressbar" aria-label="목표 대비 양품" aria-valuemin={0} aria-valuemax={100} aria-valuenow={data.active_execution.progress_ratio === null ? undefined : Math.round(Math.min(1, data.active_execution.progress_ratio) * 100)}>
              <span style={{ width: `${Math.min(1, data.active_execution.progress_ratio ?? 0) * 100}%` }} />
            </div>
            <p className="run-progress"><b>{quantity(data.active_execution.good)}</b> / {data.active_execution.target_quantity?.toLocaleString('ko-KR') ?? '목표 미등록'}개 · {percent(data.active_execution.progress_ratio, 0)}</p>
            <dl className="run-facts">
              <div><dt>실적 정산</dt><dd>{settlementLabels[data.active_execution.settlement_status] ?? data.active_execution.settlement_status}</dd></div>
              <div><dt>실행 ID</dt><dd title={data.active_execution.execution_id}>{data.active_execution.execution_id.slice(0, 8)}…</dd></div>
            </dl>
          </div> : <EmptyState title="진행 중인 작업이 없습니다.">작업지시를 배포하고 라인에 연결하면 여기에 표시됩니다.</EmptyState>}
        </Card>
        <Card title="가동 · 정지 · 불명 시간" kicker="AVAILABILITY">
          <ShareBar total={data.time.total_seconds} format={duration} items={[
            { id: 'running', label: '가동', value: data.time.running_seconds, tone: 'good' },
            { id: 'stopped', label: '정지', value: data.time.stopped_seconds, tone: 'stopped' },
            { id: 'unknown', label: '불명', value: data.time.unknown_seconds, tone: 'unknown' },
          ]} />
          <div className="oee-partial">
            <div><span>가용률</span><b>{percent(data.oee_partial.availability)}</b></div>
            <span aria-hidden="true">×</span>
            <div><span>품질률</span><b>{percent(data.oee_partial.quality)}</b></div>
            <span aria-hidden="true">×</span>
            <div className="muted"><span>성능효율</span><b>미산정</b></div>
          </div>
          <p className="card-note">OEE는 기준 사이클이 정해진 뒤 산정합니다. 불명 시간은 분모에 포함해 가용률을 부풀리지 않습니다.</p>
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
      <p className="data-footnote">기준 시각 {dateTime(data.generated_at)} · 수량 기준 {data.production.basis === 'RECEIVED_EVENTS' ? '수신된 생산 사건(정산 전 잠정)' : data.production.basis}</p>
    </div>}
  </div>
}
