import { useEffect, useId, useMemo, useRef, useState } from 'react'
import type { KeyboardEvent, ReactNode } from 'react'
import { clock, MISSING, secondClock } from './format'
import { niceTicks } from './scale'

function useWidth<T extends HTMLElement>(fallback = 640) {
  const ref = useRef<T>(null)
  const [width, setWidth] = useState(fallback)
  useEffect(() => {
    const element = ref.current
    if (!element) return
    const observer = new ResizeObserver(entries => {
      const next = Math.floor(entries[0]?.contentRect.width ?? fallback)
      if (next > 0) setWidth(next)
    })
    observer.observe(element)
    return () => observer.disconnect()
  }, [fallback])
  return [ref, width] as const
}

function timeTicks(from: number, to: number, width: number): number[] {
  // Second steps keep a zoomed-in short run readable (UX: trend focus window).
  const steps = [10e3, 15e3, 30e3, 60e3, 5 * 60e3, 15 * 60e3, 30 * 60e3, 3600e3, 3 * 3600e3, 6 * 3600e3, 12 * 3600e3, 86400e3]
  const target = Math.max(2, Math.floor(width / 110))
  const step = steps.find(candidate => (to - from) / candidate <= target) ?? 86400e3
  const offset = 9 * 3600e3 // align ticks to Seoul wall-clock boundaries
  const ticks: number[] = []
  for (let value = Math.ceil((from + offset) / step) * step - offset; value <= to; value += step) ticks.push(value)
  return ticks
}

/** Seconds appear only when the ticks fall inside a minute, so long spans keep their HH:MM labels. */
function tickLabel(tick: number, ticks: number[]): string {
  return ticks.some(value => value % 60e3 !== 0) ? secondClock(tick) : clock(tick)
}

const formatAxis = (value: number) => Math.abs(value) >= 1000 ? value.toLocaleString('ko-KR', { maximumFractionDigits: 0 }) : String(Number(value.toFixed(2)))

export interface ChartPoint { t: number; value: number | null; min?: number | null; max?: number | null; note?: string }
export interface ChartSeries { id: string; label: string; color: string; points: ChartPoint[] }
export interface Threshold { value: number; label: string; tone: 'limit' | 'center' | 'spec' }
export interface Marker { t: number; value: number; label: string }

interface TimeSeriesChartProps {
  title: string
  unit: string
  from: number
  to: number
  series: ChartSeries[]
  height?: number
  band?: boolean
  thresholds?: Threshold[]
  markers?: Marker[]
  /** Spans where no series has data; drawn as hatched gaps instead of connecting lines. */
  gaps?: { from: number; to: number; label?: string }[]
  /** Labelled spans drawn behind the lines, e.g. which work order ran when. */
  periods?: { from: number; to: number; label: string }[]
  showPoints?: boolean
  /** Smallest y-range drawn, for trends where measurement noise must stay visibly small. */
  minSpan?: number
}

/** Line chart with one y-axis. Null values break the line; they are never drawn as zero. */
export function TimeSeriesChart({ title, unit, from, to, series, height = 240, band = false, thresholds = [], markers = [], gaps = [], periods = [], showPoints = false, minSpan = 0 }: TimeSeriesChartProps) {
  const [ref, width] = useWidth<HTMLDivElement>()
  const patternId = useId().replace(/:/g, '')
  const [active, setActive] = useState<number | null>(null)
  const margin = { top: 24, right: 64, bottom: 28, left: 52 }
  const plotWidth = Math.max(10, width - margin.left - margin.right)
  const plotHeight = height - margin.top - margin.bottom
  const values = series.flatMap(item => item.points.flatMap(point => [point.value, band ? point.min : null, band ? point.max : null]))
    .filter((value): value is number => typeof value === 'number' && Number.isFinite(value))
    .concat(thresholds.map(item => item.value), markers.map(item => item.value))
  // A steady value must not fill the plot: a 0.1 ℃ wobble drawn floor-to-ceiling reads as a fault.
  const span = minSpan
  let low = values.length ? Math.min(...values) : 0, high = values.length ? Math.max(...values) : 1
  if (high - low < span) { const middle = (high + low) / 2; low = middle - span / 2; high = middle + span / 2 }
  const ticks = values.length ? niceTicks(low, high) : []
  const yMin = ticks[0] ?? 0
  const yMax = ticks[ticks.length - 1] ?? 1
  const x = (t: number) => margin.left + ((t - from) / Math.max(1, to - from)) * plotWidth
  const y = (value: number) => margin.top + plotHeight - ((value - yMin) / Math.max(1e-9, yMax - yMin)) * plotHeight
  const times = useMemo(() => [...new Set(series.flatMap(item => item.points.map(point => point.t)))].sort((a, b) => a - b), [series])

  function path(points: ChartPoint[], pick: (point: ChartPoint) => number | null | undefined) {
    let d = ''
    let open = false
    for (const point of points) {
      const value = pick(point)
      if (typeof value !== 'number' || !Number.isFinite(value)) { open = false; continue }
      d += `${open ? 'L' : 'M'}${x(point.t).toFixed(1)},${y(value).toFixed(1)}`
      open = true
    }
    return d
  }
  function area(points: ChartPoint[]) {
    const runs: ChartPoint[][] = []
    let run: ChartPoint[] = []
    for (const point of points) {
      if (typeof point.min === 'number' && typeof point.max === 'number') run.push(point)
      else if (run.length) { runs.push(run); run = [] }
    }
    if (run.length) runs.push(run)
    return runs.map(items => `M${items.map(point => `${x(point.t).toFixed(1)},${y(point.max as number).toFixed(1)}`).join('L')}L${[...items].reverse().map(point => `${x(point.t).toFixed(1)},${y(point.min as number).toFixed(1)}`).join('L')}Z`).join('')
  }
  function locate(clientX: number, element: Element) {
    const box = element.getBoundingClientRect()
    const t = from + ((clientX - box.left - margin.left) / plotWidth) * (to - from)
    if (!times.length) return null
    let best = 0
    for (let index = 1; index < times.length; index++) if (Math.abs(times[index] - t) < Math.abs(times[best] - t)) best = index
    return best
  }
  function onKey(event: KeyboardEvent<SVGSVGElement>) {
    if (!times.length) return
    if (event.key === 'ArrowRight') { event.preventDefault(); setActive(value => Math.min(times.length - 1, (value ?? -1) + 1)) }
    if (event.key === 'ArrowLeft') { event.preventDefault(); setActive(value => Math.max(0, (value ?? times.length) - 1)) }
    if (event.key === 'Escape') setActive(null)
  }
  const activeTime = active === null ? null : times[active]
  const tooltipLeft = activeTime === null ? 0 : Math.min(Math.max(x(activeTime) + 12, 0), width - 200)

  return <div className="chart" ref={ref}>
    <svg width={width} height={height} role="img" aria-label={`${title} 추세 차트. 좌우 화살표로 시점을 이동합니다.`} tabIndex={0}
      onMouseMove={event => setActive(locate(event.clientX, event.currentTarget))} onMouseLeave={() => setActive(null)}
      onKeyDown={onKey} onBlur={() => setActive(null)}>
      <defs>
        <pattern id={`gap-${patternId}`} width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
          <rect width="6" height="6" className="chart-gap-fill" /><line x1="0" y1="0" x2="0" y2="6" className="chart-gap-line" />
        </pattern>
      </defs>
      {ticks.map(tick => <g key={tick}>
        <line className="chart-grid" x1={margin.left} x2={margin.left + plotWidth} y1={y(tick)} y2={y(tick)} />
        <text className="chart-tick" x={margin.left - 8} y={y(tick)} dy="0.32em" textAnchor="end">{formatAxis(tick)}</text>
      </g>)}
      <text className="chart-unit" x={margin.left - 8} y={10} textAnchor="end">{unit}</text>
      {timeTicks(from, to, plotWidth).map((tick, _index, ticks) => <text key={tick} className="chart-tick" x={x(tick)} y={height - 8} textAnchor="middle">{tickLabel(tick, ticks)}</text>)}
      <line className="chart-axis" x1={margin.left} x2={margin.left + plotWidth} y1={margin.top + plotHeight} y2={margin.top + plotHeight} />
      {periods.map(period => <g key={`period-${period.from}`}>
        <rect className="chart-period" x={x(Math.max(from, period.from))} y={margin.top} width={Math.max(1, x(Math.min(to, period.to)) - x(Math.max(from, period.from)))} height={plotHeight} />
      </g>)}
      {gaps.map(gap => <g key={gap.from}>
        <rect x={x(gap.from)} y={margin.top} width={Math.max(1, x(gap.to) - x(gap.from))} height={plotHeight} fill={`url(#gap-${patternId})`} />
        {x(gap.to) - x(gap.from) > 64 && <text className="chart-gap-label" x={(x(gap.from) + x(gap.to)) / 2} y={margin.top + 14} textAnchor="middle">{gap.label ?? '데이터 없음'}</text>}
      </g>)}
      {periods.reduce<{ at: number; label: string; key: number }[]>((shown, period) => {
        // Short runs side by side: label only where there is room, never on top of another label.
        const at = x(Math.max(from, period.from)) + 4
        if (!shown.length || at - shown[shown.length - 1].at > 64) shown.push({ at, label: period.label, key: period.from })
        return shown
      }, []).map(label => <text key={`period-label-${label.key}`} className="chart-period-label" x={label.at} y={margin.top + plotHeight - 6}>{label.label}</text>)}
      {thresholds.map(item => <g key={`${item.label}-${item.value}`}>
        <line className={`chart-threshold ${item.tone}`} x1={margin.left} x2={margin.left + plotWidth} y1={y(item.value)} y2={y(item.value)} />
        <text className="chart-threshold-label" x={margin.left + plotWidth + 6} y={y(item.value)} dy="0.32em">{item.label}</text>
      </g>)}
      {band && series.map(item => <path key={`band-${item.id}`} d={area(item.points)} fill={item.color} opacity={0.14} />)}
      {series.map(item => <path key={item.id} d={path(item.points, point => point.value)} fill="none" stroke={item.color} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />)}
      {showPoints && series.map(item => item.points.map(point => typeof point.value === 'number'
        ? <circle key={`${item.id}-${point.t}`} cx={x(point.t)} cy={y(point.value)} r={2.5} fill={item.color} /> : null))}
      {markers.map(item => <circle key={`marker-${item.t}`} className="chart-violation" cx={x(item.t)} cy={y(item.value)} r={6} />)}
      {series.length === 1 && thresholds.length === 0 && (() => {
        const last = [...series[0].points].reverse().find(point => typeof point.value === 'number')
        return last && typeof last.value === 'number' ? <text className="chart-end-label" x={x(last.t) + 6} y={y(last.value)} dy="0.32em">{formatAxis(last.value)}</text> : null
      })()}
      {activeTime !== null && <g>
        <line className="chart-crosshair" x1={x(activeTime)} x2={x(activeTime)} y1={margin.top} y2={margin.top + plotHeight} />
        {series.map(item => {
          const point = item.points.find(entry => entry.t === activeTime)
          return point && typeof point.value === 'number' ? <circle key={item.id} cx={x(activeTime)} cy={y(point.value)} r={4.5} fill={item.color} className="chart-focus-point" /> : null
        })}
      </g>}
    </svg>
    {activeTime !== null && <div className="chart-tooltip" role="status" style={{ left: tooltipLeft, top: margin.top }}>
      <strong>{secondClock(activeTime)}</strong>
      {series.map(item => {
        const point = item.points.find(entry => entry.t === activeTime)
        return <div key={item.id} className="chart-tooltip-row">
          <span className="legend-swatch" style={{ background: item.color }} />
          <span>{item.label}</span>
          <b>{point && typeof point.value === 'number' ? `${formatAxis(point.value)} ${unit}` : MISSING}</b>
          {point?.note && <small>{point.note}</small>}
        </div>
      })}
      {markers.filter(item => item.t === activeTime).map(item => <div key={item.label} className="chart-tooltip-alert">{item.label}</div>)}
    </div>}
  </div>
}

export function Legend({ items }: { items: { label: string; color: string; pattern?: boolean }[] }) {
  return <ul className="chart-legend">{items.map(item => <li key={item.label}>
    <span className={`legend-swatch${item.pattern ? ' pattern' : ''}`} style={item.pattern ? undefined : { background: item.color }} />{item.label}
  </li>)}</ul>
}

export interface BarDatum { key: string; label: string; segments: { id: string; value: number; color: string; label: string }[]; detail?: ReactNode }

/** Stacked vertical bars with a 2px surface gap between segments and a per-bar tooltip. */
export function StackedBars({ title, data, height = 200, unit }: { title: string; data: BarDatum[]; height?: number; unit: string }) {
  const [ref, width] = useWidth<HTMLDivElement>()
  const [active, setActive] = useState<number | null>(null)
  const margin = { top: 12, right: 12, bottom: 26, left: 44 }
  const plotWidth = Math.max(10, width - margin.left - margin.right)
  const plotHeight = height - margin.top - margin.bottom
  const totals = data.map(item => item.segments.reduce((sum, segment) => sum + segment.value, 0))
  const ticks = niceTicks(0, Math.max(1, ...totals), 4)
  const yMax = ticks[ticks.length - 1] ?? 1
  const slot = plotWidth / Math.max(1, data.length)
  const barWidth = Math.max(2, Math.min(28, slot - 4))
  const y = (value: number) => margin.top + plotHeight - (value / yMax) * plotHeight
  const labelEvery = Math.ceil(data.length / Math.max(1, Math.floor(plotWidth / 34)))
  const current = active === null ? null : data[active]
  return <div className="chart" ref={ref}>
    <svg width={width} height={height} role="img" aria-label={`${title} 막대 차트`} onMouseLeave={() => setActive(null)}>
      {ticks.map(tick => <g key={tick}>
        <line className="chart-grid" x1={margin.left} x2={margin.left + plotWidth} y1={y(tick)} y2={y(tick)} />
        <text className="chart-tick" x={margin.left - 8} y={y(tick)} dy="0.32em" textAnchor="end">{formatAxis(tick)}</text>
      </g>)}
      <line className="chart-axis" x1={margin.left} x2={margin.left + plotWidth} y1={margin.top + plotHeight} y2={margin.top + plotHeight} />
      {data.map((item, index) => {
        const left = margin.left + index * slot + (slot - barWidth) / 2
        let base = 0
        return <g key={item.key} onMouseEnter={() => setActive(index)} onFocus={() => setActive(index)} onBlur={() => setActive(null)} tabIndex={0} aria-label={`${item.label} ${item.segments.map(segment => `${segment.label} ${segment.value}`).join(', ')}`}>
          <rect x={margin.left + index * slot} y={margin.top} width={slot} height={plotHeight} fill="transparent" />
          {item.segments.map(segment => {
            if (segment.value <= 0) return null
            const top = y(base + segment.value)
            const bottom = y(base)
            base += segment.value
            return <rect key={segment.id} x={left} y={top} width={barWidth} height={Math.max(1, bottom - top - 2)} rx={2} fill={segment.color} />
          })}
          {index % labelEvery === 0 && <text className="chart-tick" x={left + barWidth / 2} y={height - 8} textAnchor="middle">{item.label}</text>}
        </g>
      })}
    </svg>
    {current && active !== null && <div className="chart-tooltip" role="status" style={{ left: Math.min(margin.left + active * slot + slot, width - 180), top: margin.top }}>
      <strong>{current.label}</strong>
      {current.segments.map(segment => <div key={segment.id} className="chart-tooltip-row"><span className="legend-swatch" style={{ background: segment.color }} /><span>{segment.label}</span><b>{segment.value.toLocaleString('ko-KR')} {unit}</b></div>)}
      {current.detail}
    </div>}
  </div>
}

export interface ShareItem { id: string; label: string; value: number; tone: 'good' | 'stopped' | 'unknown' }

/** Part-to-whole bar for running / stopped / unknown time. Unknown keeps its own hatched segment. */
export function ShareBar({ items, total, format }: { items: ShareItem[]; total: number; format: (value: number) => string }) {
  return <div className="share">
    <div className="share-bar" role="img" aria-label={items.map(item => `${item.label} ${format(item.value)}`).join(', ')}>
      {total > 0 && items.map(item => item.value > 0 && <span key={item.id} className={`share-segment ${item.tone}`} style={{ flexGrow: item.value }} />)}
      {total <= 0 && <span className="share-empty">집계할 시간이 없습니다</span>}
    </div>
    <ul className="share-legend">{items.map(item => <li key={item.id}>
      <span className={`share-swatch ${item.tone}`} />{item.label}<b>{format(item.value)}</b><small>{total > 0 ? `${((item.value / total) * 100).toFixed(1)}%` : MISSING}</small>
    </li>)}</ul>
  </div>
}

export interface TimelineSegment { key: string; from: number; to: number; tone: 'good' | 'warn' | 'bad' | 'stopped' | 'unknown'; label: string; detail?: string; selectable?: boolean }

/** Horizontal state timeline. Unknown spans stay visibly separate (hatched) and are never merged into running or stopped. */
export function StateTimeline({ from, to, segments, selected, onSelect }: { from: number; to: number; segments: TimelineSegment[]; selected?: string | null; onSelect?: (key: string) => void }) {
  const [ref, width] = useWidth<HTMLDivElement>()
  const [active, setActive] = useState<TimelineSegment | null>(null)
  const patternId = useId().replace(/:/g, '')
  const margin = { left: 8, right: 8 }
  const plotWidth = Math.max(10, width - margin.left - margin.right)
  const x = (t: number) => margin.left + ((t - from) / Math.max(1, to - from)) * plotWidth
  return <div className="chart" ref={ref}>
    <svg width={width} height={74} role="group" aria-label="상태 타임라인" onMouseLeave={() => setActive(null)}>
      <defs>
        <pattern id={`unknown-${patternId}`} width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
          <rect width="6" height="6" className="timeline-unknown-fill" /><line x1="0" y1="0" x2="0" y2="6" className="timeline-unknown-line" />
        </pattern>
      </defs>
      {segments.map(segment => {
        const left = x(segment.from)
        const segmentWidth = Math.max(1, x(segment.to) - left - 2)
        const isSelected = selected === segment.key
        return <g key={segment.key} className={`timeline-segment ${segment.tone}${isSelected ? ' is-selected' : ''}${segment.selectable ? ' selectable' : ''}`}
          tabIndex={segment.selectable ? 0 : -1} role={segment.selectable ? 'button' : undefined} aria-pressed={segment.selectable ? isSelected : undefined}
          aria-label={`${segment.label} ${secondClock(segment.from)}~${secondClock(segment.to)}${segment.detail ? ` ${segment.detail}` : ''}`}
          onMouseEnter={() => setActive(segment)} onFocus={() => setActive(segment)}
          onClick={() => segment.selectable && onSelect?.(segment.key)}
          onKeyDown={event => { if (segment.selectable && (event.key === 'Enter' || event.key === ' ')) { event.preventDefault(); onSelect?.(segment.key) } }}>
          <rect x={left} y={8} width={segmentWidth} height={34} rx={3} fill={segment.tone === 'unknown' ? `url(#unknown-${patternId})` : undefined} />
        </g>
      })}
      {(() => { const ticks = timeTicks(from, to, plotWidth); return ticks.map(tick => <text key={tick} className="chart-tick" x={x(tick)} y={66} textAnchor="middle">{tickLabel(tick, ticks)}</text>) })()}
    </svg>
    {active && <div className="chart-tooltip" role="status" style={{ left: Math.min(Math.max(0, x(active.from)), width - 220), top: 46 }}>
      <strong>{active.label}</strong>
      <div>{secondClock(active.from)} ~ {secondClock(active.to)}</div>
      {active.detail && <small>{active.detail}</small>}
    </div>}
  </div>
}
