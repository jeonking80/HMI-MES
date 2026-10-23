/* Validated categorical order (blue, orange, aqua, violet): adjacent CVD ΔE ≥ 9.2 on the light surface. */
export const SERIES_COLORS = ['#2a78d6', '#eb6834', '#1baf7a', '#6250d6'] as const

/** Round tick values covering [min, max]. */
export function niceTicks(min: number, max: number, count = 5): number[] {
  if (!Number.isFinite(min) || !Number.isFinite(max)) return []
  if (min === max) { min -= 1; max += 1 }
  const raw = (max - min) / Math.max(1, count)
  const magnitude = 10 ** Math.floor(Math.log10(raw))
  const step = [1, 2, 2.5, 5, 10].map(factor => factor * magnitude).find(candidate => candidate >= raw) ?? raw
  const start = Math.floor(min / step) * step
  const ticks: number[] = []
  // The last tick always reaches the maximum so no data point is drawn above the axis.
  for (let value = start; ticks.length < 20; value += step) {
    ticks.push(Number(value.toFixed(10)))
    if (value >= max - step * 1e-9) break
  }
  return ticks
}

