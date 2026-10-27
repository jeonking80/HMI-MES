import { useMemo, useState } from 'react'
import { parseTrend, query } from './api'
import type { PointQuality, Trend, TrendSeries } from './api'
import { Legend, TimeSeriesChart } from './charts'
import { SERIES_COLORS } from './scale'
import type { ChartSeries } from './charts'
import { alignedEnd, analogTags, clock, dateTime, focusWindow, MISSING, tagLabels } from './format'
import { useResource, useSources } from './hooks'
import type { Source } from './hooks'
import { Card, ChartOrTable, EmptyState, PageNotice, RefreshStatus, ScopePicker } from './ui'

const ranges = [
  { id: '15m', label: '15분', ms: 15 * 60e3 },
  { id: '1h', label: '1시간', ms: 3600e3 },
  { id: '8h', label: '8시간', ms: 8 * 3600e3 },
  { id: '24h', label: '24시간', ms: 86400e3 },
  { id: '7d', label: '7일', ms: 7 * 86400e3 },
] as const
const MAX_TAGS = 4
const qualityLabels: Record<PointQuality, string> = {
  GOOD: '정상', PARTIAL: '일부 표본', UNCERTAIN: '품질 불확실', BAD: '품질 나쁨', MISSING: '데이터 없음', TIME_UNKNOWN: '시간 불명', RETENTION_EXPIRED: '보존 기간 지남',
}
const groups = [
  { label: '투입', prefix: 'Infeed.' }, { label: '실링', prefix: 'Sealer.' }, { label: '배출', prefix: 'Outfeed.' },
]

/**
 * Spans where no series in the group has a value, so the chart shows a gap instead of a joined line.
 * A span made only of TIME_UNKNOWN slots is labelled separately: values arrived but their capture
 * time could not be trusted, which is different from no data at all.
 */
function gapsFor(series: TrendSeries[], stepMs: number) {
  // Index once so the 7-day maximum stays linear in the number of points.
  const indexed = series.map(item => new Map(item.points.map(point => [Date.parse(point.t), point])))
  const times = [...new Set(indexed.flatMap(map => [...map.keys()]))].sort((a, b) => a - b)
  const gaps: { from: number; to: number; timeUnknown: boolean }[] = []
  for (const t of times) {
    const points = indexed.map(map => map.get(t))
    if (!points.every(point => !point || point.value === null)) continue
    const timeUnknown = points.some(point => point?.quality === 'TIME_UNKNOWN')
    const last = gaps[gaps.length - 1]
    if (last && last.to >= t && last.timeUnknown === timeUnknown) last.to = t + stepMs
    else gaps.push({ from: t, to: t + stepMs, timeUnknown })
  }
  return gaps.map(gap => ({ from: gap.from, to: gap.to, label: gap.timeUnknown ? '시간 불명' : '데이터 없음' }))
}

function clipGaps(gaps: { from: number; to: number; label: string }[], view: { from: number; to: number }) {
  return gaps.filter(gap => gap.to > view.from && gap.from < view.to).map(gap => ({ ...gap, from: Math.max(gap.from, view.from), to: Math.min(gap.to, view.to) }))
}

function coverage(series: TrendSeries) {
  const valid = series.points.reduce((sum, point) => sum + point.valid_count, 0)
  const expected = series.points.reduce((sum, point) => sum + point.expected_count, 0)
  const missing = series.points.filter(point => point.quality === 'MISSING').length
  const timeUnknown = series.points.filter(point => point.quality === 'TIME_UNKNOWN').length
  return { ratio: expected > 0 ? valid / expected : null, missing, timeUnknown }
}

export default function TrendPage({ scope, onScope, onSessionInvalid }: { scope: Source | null; onScope: (source: Source | null) => void; onSessionInvalid: () => void }) {
  const sources = useSources(onSessionInvalid)
  const [tags, setTags] = useState<string[]>(['Sealer.TemperaturePV', 'Sealer.TemperatureSP', 'Sealer.AirPressure'])
  const [range, setRange] = useState<(typeof ranges)[number]['id']>('1h')
  const [anchor, setAnchor] = useState(() => Date.now())
  // Zoom into the part that has values unless the user asks for the whole span.
  const [fullSpan, setFullSpan] = useState(false)
  const span = ranges.find(item => item.id === range)!.ms
  // The server resolves spans over one hour to minute buckets and counts them from the floored
  // start, so a minute-resolution request must sit on minute boundaries or 7 days becomes 10081.
  const to = alignedEnd(anchor, span)
  const from = to - span
  const path = scope && tags.length ? query('/api/v1/trends', {
    line_id: scope.line_id, source_id: scope.source_id, stream_id: scope.stream_id, tag: tags,
    from: new Date(from).toISOString().replace('.000Z', 'Z'), to: new Date(to).toISOString().replace('.000Z', 'Z'), resolution: 'auto',
  }) : null
  const trend = useResource(path, parseTrend, onSessionInvalid)
  const data: Trend | null = trend.data
  // Color follows the tag's selection order, so removing one tag never repaints the others.
  const colorOf = (tag: string) => {
    const index = tags.indexOf(tag)
    const fallback = (data?.series ?? []).findIndex(item => item.tag_id === tag)
    return SERIES_COLORS[(index >= 0 ? index : tags.length + Math.max(0, fallback)) % SERIES_COLORS.length]
  }
  const stepMs = data?.resolution === 'second' ? 1000 : 60e3
  const focus = useMemo(() => {
    if (!data) return null
    const valued = data.series.flatMap(item => item.points.filter(point => point.value !== null).map(point => Date.parse(point.t)))
    return focusWindow(valued, Date.parse(data.from), Date.parse(data.to), stepMs)
  }, [data, stepMs])
  const view = data ? (focus && !fullSpan ? focus : { from: Date.parse(data.from), to: Date.parse(data.to) }) : null
  const byUnit = useMemo(() => {
    const result = new Map<string, TrendSeries[]>()
    for (const series of data?.series ?? []) result.set(series.unit, [...(result.get(series.unit) ?? []), series])
    return [...result.entries()]
  }, [data])

  function toggle(tag: string) {
    setTags(current => current.includes(tag) ? current.filter(item => item !== tag) : current.length >= MAX_TAGS ? current : [...current, tag])
  }

  return <div className="analytics-page" data-page="trends">
    <div className="filter-row">
      <ScopePicker id="trend-scope" sources={sources.data} value={scope} onChange={onScope} />
      <div className="filter-field">
        <span className="filter-label">기간</span>
        <div className="segmented" role="group" aria-label="조회 기간">
          {ranges.map(item => <button key={item.id} type="button" aria-pressed={range === item.id} onClick={() => { setRange(item.id); setAnchor(Date.now()); setFullSpan(false) }}>{item.label}</button>)}
        </div>
      </div>
      <RefreshStatus loading={trend.loading} updatedAt={trend.updatedAt} onRefresh={() => setAnchor(Date.now())} />
    </div>
    <fieldset className="tag-picker">
      <legend>태그 선택 <small>최대 {MAX_TAGS}개 · 단위가 다르면 차트를 나눠 그립니다</small></legend>
      {groups.map(group => <div key={group.label} className="tag-group">
        <span>{group.label}</span>
        {analogTags.filter(tag => tag.startsWith(group.prefix)).map(tag => {
          const checked = tags.includes(tag)
          return <label key={tag} className={`tag-option${checked ? ' is-checked' : ''}`}>
            <input type="checkbox" checked={checked} disabled={!checked && tags.length >= MAX_TAGS} onChange={() => toggle(tag)} />
            {checked && <span className="legend-swatch" style={{ background: colorOf(tag) }} />}
            {tagLabels[tag]}
          </label>
        })}
      </div>)}
    </fieldset>
    {trend.error && <PageNotice tone="bad" title="트렌드를 조회하지 못했습니다.">{trend.error}{data ? ' 아래는 이전 조회 결과입니다.' : ''}</PageNotice>}
    {!scope && <EmptyState title="라인을 선택해 주세요." />}
    {scope && !tags.length && <EmptyState title="태그를 하나 이상 선택해 주세요." />}
    {scope && tags.length > 0 && !data && !trend.error && <EmptyState title="추세를 불러오는 중입니다." />}
    {data && <div className={trend.loading ? 'is-refreshing' : ''}>
      {data.series.some(item => item.points.some(point => point.quality === 'TIME_UNKNOWN')) && <PageNotice tone="unknown" title="수집 시각을 확인할 수 없는 값은 그리지 않았습니다.">
        값은 수집됐지만 설비와 서버의 시각이 일치한다는 근거(시간 정책)가 없어 어느 시점의 값인지 확정할 수 없습니다. 추정 시각에 배치하지 않고 &lsquo;시간 불명&rsquo;으로 표시합니다.
      </PageNotice>}
      <p className="data-footnote">{dateTime(data.from)} ~ {dateTime(data.to)} · {data.resolution === 'second' ? '1초 원시값' : '1분 집계(평균, 음영은 최소~최대)'} · 정책 {data.policy_version}</p>
      {focus && <div className="focus-bar" role="status">
        {fullSpan
          ? <span>조회 기간 전체를 보고 있습니다. 값이 있는 구간이 짧아 차트에서 작게 보입니다.</span>
          : <span><b>값이 있는 구간만 확대해 보고 있습니다</b> ({clock(new Date(focus.from).toISOString())} ~ {clock(new Date(focus.to).toISOString())}). 나머지 기간은 값이 없으며, 아래 유효 표본·누락 수는 조회 기간 전체 기준입니다.</span>}
        <button type="button" className="button secondary compact" aria-pressed={fullSpan} onClick={() => setFullSpan(value => !value)}>{fullSpan ? '값이 있는 구간 확대' : '전체 기간 보기'}</button>
      </div>}
      {byUnit.map(([unit, series]) => {
        const chartSeries: ChartSeries[] = series.map(item => ({
          id: item.tag_id, label: tagLabels[item.tag_id] ?? item.tag_id, color: colorOf(item.tag_id),
          points: item.points.filter(point => !view || (Date.parse(point.t) >= view.from && Date.parse(point.t) < view.to)).map(point => ({ t: Date.parse(point.t), value: point.value, min: point.min, max: point.max, note: point.quality === 'GOOD' ? undefined : `${qualityLabels[point.quality]} (${point.valid_count}/${point.expected_count})` })),
        }))
        return <Card key={unit} title={series.map(item => tagLabels[item.tag_id] ?? item.tag_id).join(' · ')} kicker={`단위 ${unit}`}
          actions={series.length > 1 ? <Legend items={chartSeries.map(item => ({ label: item.label, color: item.color }))} /> : undefined}>
          <ChartOrTable label={unit} chart={<TimeSeriesChart title={series.map(item => tagLabels[item.tag_id]).join(', ')} unit={unit} from={view!.from} to={view!.to} series={chartSeries} band={data.resolution === 'minute'} gaps={clipGaps(gapsFor(series, stepMs), view!)} />}
            table={<table className="data-table">
              <thead><tr><th scope="col">시각</th>{series.map(item => <th key={item.tag_id} scope="col">{tagLabels[item.tag_id]} ({unit})</th>)}<th scope="col">품질</th></tr></thead>
              <tbody>{series[0].points.map((point, index) => <tr key={point.t}>
                <th scope="row">{clock(point.t)}</th>
                {series.map(item => <td key={item.tag_id}>{item.points[index]?.value ?? MISSING}</td>)}
                <td>{series.map(item => qualityLabels[item.points[index]?.quality ?? 'MISSING']).join(' / ')}</td>
              </tr>)}</tbody>
            </table>} />
          <ul className="coverage-list">{series.map(item => {
            const summary = coverage(item)
            return <li key={item.tag_id}><span className="legend-swatch" style={{ background: colorOf(item.tag_id) }} />{tagLabels[item.tag_id]}: 유효 표본 {summary.ratio === null ? MISSING : `${(summary.ratio * 100).toFixed(1)}%`}{summary.missing > 0 && <b> · 누락 {summary.missing}구간</b>}{summary.timeUnknown > 0 && <b> · 시간 불명 {summary.timeUnknown}구간</b>}</li>
          })}</ul>
        </Card>
      })}
    </div>}
  </div>
}
