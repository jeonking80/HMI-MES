import { useEffect, useState } from 'react'
import { parseTrend, query } from '../analytics/api'
import { TimeSeriesChart } from '../analytics/charts'
import { useResource } from '../analytics/hooks'
import type { Source } from './lineState'

const SPAN = 10 * 60_000

/** Sealer temperature against its setpoint over the last ten minutes, from the stored history. */
export default function TrendMini({ source, onSessionInvalid }: { source: Source; onSessionInvalid: () => void }) {
  // Re-anchor every 15 s on a whole second so the path (and cache key) moves in steps.
  const [to, setTo] = useState(() => Math.floor(Date.now() / 1000) * 1000)
  useEffect(() => {
    const timer = window.setInterval(() => setTo(Math.floor(Date.now() / 1000) * 1000), 15_000)
    return () => window.clearInterval(timer)
  }, [])
  const from = to - SPAN
  const path = query('/api/v1/trends', {
    line_id: source.line_id, source_id: source.source_id, stream_id: source.stream_id, tag: ['Sealer.TemperaturePV', 'Sealer.TemperatureSP'],
    from: new Date(from).toISOString().replace('.000Z', 'Z'), to: new Date(to).toISOString().replace('.000Z', 'Z'), resolution: 'auto',
  })
  const trend = useResource(path, parseTrend, onSessionInvalid)
  const series = trend.data?.series ?? []
  const pv = series.find(item => item.tag_id === 'Sealer.TemperaturePV')
  const sp = series.find(item => item.tag_id === 'Sealer.TemperatureSP')
  const hasValues = !!pv?.points.some(point => point.value !== null)
  return <section className="lc-card lc-trend" aria-labelledby="trend-mini-heading">
    <div className="lc-card-head"><h3 id="trend-mini-heading">실링 온도 · 최근 10분</h3><span>{trend.error ? '조회 실패' : trend.loading && !trend.data ? '불러오는 중' : ''}</span></div>
    {hasValues ? <TimeSeriesChart title="실링 온도" unit="℃" from={from} to={to} height={170} minSpan={10} series={[
      { id: 'pv', label: '현재 온도', color: '#2d74b0', points: pv!.points.map(point => ({ t: Date.parse(point.t), value: point.value })) },
      ...(sp ? [{ id: 'sp', label: '설정 온도', color: '#9aa8b8', points: sp.points.map(point => ({ t: Date.parse(point.t), value: point.value })) }] : []),
    ]} /> : <p className="lc-empty-chart">{trend.error || (trend.data ? '최근 10분 동안 저장된 온도 값이 없습니다.' : '불러오는 중입니다.')}</p>}
  </section>
}
