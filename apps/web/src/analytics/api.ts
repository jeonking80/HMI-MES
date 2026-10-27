/**
 * Read-only clients for the reporting APIs defined in docs/work-orders/CODEX_KULS_보강_v1.md
 * (CX-01 trends, CX-02 reports, CX-03 downtime reasons, CX-04 SPC, CX-05 dashboard).
 *
 * Validators reject wrong types instead of coercing them: a missing value stays null and is
 * never turned into 0. Additional response fields are allowed because the contract lets the
 * server add fields without a revision.
 */

export class AnalyticsError extends Error {
  constructor(public readonly status: number, message: string) { super(message) }
}

const fail = (): never => { throw new AnalyticsError(0, '응답 형식을 확인할 수 없습니다.') }
function need(condition: unknown): asserts condition { if (!condition) fail() }
function record(value: unknown, keys: readonly string[]): Record<string, unknown> {
  need(value !== null && typeof value === 'object' && !Array.isArray(value))
  const result = value as Record<string, unknown>
  need(keys.every(key => Object.hasOwn(result, key)))
  return result
}
function list(value: unknown, limit = 20_000): unknown[] {
  need(Array.isArray(value) && value.length <= limit)
  return value as unknown[]
}
/** Mirrors contracts.models.Identifier with the backend's (Python) whitespace set, not JS \s (same as screenState). */
const backendWhitespace = new Set([9, 10, 11, 12, 13, 32, 133, 160, 5760, ...Array.from({ length: 11 }, (_, index) => 8192 + index), 8232, 8233, 8239, 8287, 12288])
const identifier = (value: unknown): value is string => typeof value === 'string' && value.length >= 1 && [...value].length <= 128
  && [...value].every(point => !backendWhitespace.has(point.codePointAt(0)!))
export const isTime = (value: unknown): value is string => typeof value === 'string'
  && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)$(?![\s\S])/.test(value) && Number.isFinite(Date.parse(value))
/** Microseconds since the epoch: the server keeps µs, Date.parse drops below 1 ms. */
const micros = (value: string) => {
  const fraction = /\.(\d+)/.exec(value)?.[1] ?? ''
  return Date.parse(value.replace(/\.\d+/, '')) * 1000 + Number(fraction.padEnd(6, '0').slice(0, 6))
}
const nullableTime = (value: unknown): value is string | null => value === null || isTime(value)
const date = (value: unknown): value is string => typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$(?![\s\S])/.test(value)
/** UInt64 quantities travel as decimal strings to keep full precision. */
export const isQuantity = (value: unknown): value is string => typeof value === 'string' && /^(0|[1-9]\d{0,19})$(?![\s\S])/.test(value)
  && BigInt(value) <= 18446744073709551615n
const finite = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value)
const nullableFinite = (value: unknown): value is number | null => value === null || finite(value)
const ratio = (value: unknown): value is number | null => value === null || (finite(value) && value >= 0 && value <= 1)
/** Durations may be fractional; the server measures segments to sub-second precision. */
const seconds = (value: unknown): value is number => finite(value) && value >= 0
const count = (value: unknown): value is number => finite(value) && value >= 0 && Number.isInteger(value)
const text = (value: unknown, max = 1000): value is string => typeof value === 'string' && [...value].length <= max

export interface Scope { line_id: string; source_id: string; stream_id: string }
export interface Totals { good: string; reject: string; scrap: string }
function totals(value: unknown): Totals {
  const result = record(value, ['good', 'reject', 'scrap'])
  need(isQuantity(result.good) && isQuantity(result.reject) && isQuantity(result.scrap))
  return { good: result.good, reject: result.reject, scrap: result.scrap }
}

// ---- CX-05 dashboard -------------------------------------------------------------------------

export type Freshness = 'LIVE' | 'STALE' | 'NO_DATA'
export interface HourlyBucket { hour_start: string; good: string; reject: string; running_seconds: number; unknown_seconds: number }
export interface Dashboard {
  date: string
  generated_at: string
  freshness: { last_receipt_at: string | null; status: Freshness }
  production: Totals & { yield_ratio: number | null; basis: string; provisional: boolean }
  active_execution: null | {
    execution_id: string; work_order_id: string; item_id: string | null; target_quantity: number | null
    good: string; progress_ratio: number | null; line_state: string | null; settlement_status: string
  }
  time: { running_seconds: number; stopped_seconds: number; unknown_seconds: number; total_seconds: number; running_ratio: number | null; unknown_ratio: number | null }
  oee_partial: { availability: number | null; quality: number | null; performance: null; note: string }
  alarms: { active_total: number; active_by_severity: Record<string, number>; unacknowledged: number }
  hourly: HourlyBucket[]
}

function hourly(value: unknown): HourlyBucket[] {
  return list(value, 48).map(item => {
    const bucket = record(item, ['hour_start', 'good', 'reject', 'running_seconds', 'unknown_seconds'])
    need(isTime(bucket.hour_start) && isQuantity(bucket.good) && isQuantity(bucket.reject) && seconds(bucket.running_seconds) && seconds(bucket.unknown_seconds))
    return { hour_start: bucket.hour_start, good: bucket.good, reject: bucket.reject, running_seconds: bucket.running_seconds, unknown_seconds: bucket.unknown_seconds }
  })
}

function durations(value: unknown) {
  const result = record(value, ['running', 'stopped', 'unknown', 'total'])
  need(seconds(result.running) && seconds(result.stopped) && seconds(result.unknown) && seconds(result.total))
  return { running: result.running, stopped: result.stopped, unknown: result.unknown, total: result.total }
}

export function parseDashboard(value: unknown): Dashboard {
  const root = record(value, ['date', 'generated_at', 'freshness', 'production', 'active_execution', 'time', 'oee_partial', 'alarms', 'hourly'])
  need(date(root.date) && isTime(root.generated_at))
  const freshness = record(root.freshness, ['last_receipt_at', 'status'])
  need(nullableTime(freshness.last_receipt_at) && ['LIVE', 'STALE', 'NO_DATA'].includes(freshness.status as string))
  const production = record(root.production, ['good', 'reject', 'scrap', 'yield_ratio', 'basis', 'provisional'])
  need(ratio(production.yield_ratio) && identifier(production.basis) && typeof production.provisional === 'boolean')
  let active: Dashboard['active_execution'] = null
  if (root.active_execution !== null) {
    const item = record(root.active_execution, ['execution_id', 'work_order_id', 'item_id', 'target_quantity', 'good', 'progress_ratio', 'line_state', 'settlement_status'])
    need(identifier(item.execution_id) && identifier(item.work_order_id) && (item.item_id === null || identifier(item.item_id)))
    need(item.target_quantity === null || (count(item.target_quantity) && item.target_quantity > 0))
    need(isQuantity(item.good) && nullableFinite(item.progress_ratio) && (item.progress_ratio === null || (item.progress_ratio as number) >= 0))
    need(item.line_state === null || identifier(item.line_state))
    need(identifier(item.settlement_status))
    active = {
      execution_id: item.execution_id, work_order_id: item.work_order_id, item_id: item.item_id as string | null,
      target_quantity: item.target_quantity as number | null, good: item.good, progress_ratio: item.progress_ratio as number | null,
      line_state: item.line_state as string | null, settlement_status: item.settlement_status,
    }
  }
  const time = record(root.time, ['running_seconds', 'stopped_seconds', 'unknown_seconds', 'total_seconds', 'running_ratio', 'unknown_ratio'])
  need(seconds(time.running_seconds) && seconds(time.stopped_seconds) && seconds(time.unknown_seconds) && seconds(time.total_seconds))
  need(ratio(time.running_ratio) && ratio(time.unknown_ratio))
  const oee = record(root.oee_partial, ['availability', 'quality', 'performance', 'note'])
  need(ratio(oee.availability) && ratio(oee.quality) && oee.performance === null && identifier(oee.note))
  const alarms = record(root.alarms, ['active_total', 'active_by_severity', 'unacknowledged'])
  need(count(alarms.active_total) && count(alarms.unacknowledged))
  const severity = record(alarms.active_by_severity, [])
  need(Object.entries(severity).every(([key, amount]) => identifier(key) && count(amount)))
  return {
    date: root.date, generated_at: root.generated_at,
    freshness: { last_receipt_at: freshness.last_receipt_at, status: freshness.status as Freshness },
    production: { ...totals(production), yield_ratio: production.yield_ratio, basis: production.basis, provisional: production.provisional },
    active_execution: active,
    time: {
      running_seconds: time.running_seconds, stopped_seconds: time.stopped_seconds, unknown_seconds: time.unknown_seconds,
      total_seconds: time.total_seconds, running_ratio: time.running_ratio, unknown_ratio: time.unknown_ratio,
    },
    oee_partial: { availability: oee.availability, quality: oee.quality, performance: null, note: oee.note },
    alarms: { active_total: alarms.active_total, active_by_severity: severity as Record<string, number>, unacknowledged: alarms.unacknowledged },
    hourly: hourly(root.hourly),
  }
}

// ---- CX-01 trends ----------------------------------------------------------------------------

export type PointQuality = 'GOOD' | 'PARTIAL' | 'UNCERTAIN' | 'BAD' | 'MISSING' | 'TIME_UNKNOWN' | 'RETENTION_EXPIRED'
export interface TrendPoint { t: string; value: number | null; min: number | null; max: number | null; quality: PointQuality; valid_count: number; expected_count: number }
export interface TrendSeries { tag_id: string; unit: string; points: TrendPoint[] }
export interface Trend { from: string; to: string; resolution: 'second' | 'minute'; policy_version: string; series: TrendSeries[]; generated_at: string }
const qualities: readonly PointQuality[] = ['GOOD', 'PARTIAL', 'UNCERTAIN', 'BAD', 'MISSING', 'TIME_UNKNOWN', 'RETENTION_EXPIRED']

export function parseTrend(value: unknown): Trend {
  const root = record(value, ['scope', 'from', 'to', 'resolution', 'policy_version', 'series', 'generated_at'])
  need(isTime(root.from) && isTime(root.to) && Date.parse(root.from) < Date.parse(root.to) && isTime(root.generated_at))
  need((root.resolution === 'second' || root.resolution === 'minute') && identifier(root.policy_version))
  const series = list(root.series, 4).map(item => {
    const entry = record(item, ['tag_id', 'unit', 'points'])
    need(identifier(entry.tag_id) && text(entry.unit, 16))
    let previous = -Infinity
    const points = list(entry.points, 10_080).map(raw => {
      const point = record(raw, ['t', 'value', 'min', 'max', 'quality', 'valid_count', 'expected_count'])
      need(isTime(point.t) && Date.parse(point.t) > previous)
      previous = Date.parse(point.t)
      need(nullableFinite(point.value) && nullableFinite(point.min) && nullableFinite(point.max))
      need(qualities.includes(point.quality as PointQuality) && count(point.valid_count) && count(point.expected_count))
      need(point.valid_count as number <= (point.expected_count as number))
      // A slot without samples must not carry a value; the chart breaks the line there.
      // A slot whose raw samples were deleted by retention is unknown, never a value.
      if (point.quality === 'MISSING' || point.quality === 'RETENTION_EXPIRED') need(point.value === null && point.min === null && point.max === null)
      if (point.value !== null && point.min !== null && point.max !== null) need((point.min as number) <= (point.max as number))
      return {
        t: point.t, value: point.value as number | null, min: point.min as number | null, max: point.max as number | null,
        quality: point.quality as PointQuality, valid_count: point.valid_count as number, expected_count: point.expected_count as number,
      }
    })
    return { tag_id: entry.tag_id, unit: entry.unit, points }
  })
  return { from: root.from, to: root.to, resolution: root.resolution, policy_version: root.policy_version, series, generated_at: root.generated_at }
}

// ---- CX-02 / CX-03 reports -------------------------------------------------------------------

export interface ExecutionReport {
  execution_id: string; work_order_id: string; item_id: string | null; recipe_version: string
  /** The execution's own source scope; absent from older servers. */
  source_id: string | null; stream_id: string | null
  target_quantity: number | null; registered_at: string; started_at: string | null; ended_at: string | null
  totals: Totals; yield_ratio: number | null; settlement_status: string; provisional: boolean; provisional_reasons: string[]
  durations_seconds: { running: number; stopped: number; unknown: number; total: number }
  stopped_by_state: Record<string, number>
}
export interface ExecutionReportPage { items: ExecutionReport[]; next_cursor: string | null }

function reasons(value: unknown): string[] {
  return list(value, 20).map(item => { need(identifier(item)); return item as string })
}

export function parseExecutionReports(value: unknown): ExecutionReportPage {
  const root = record(value, ['items', 'next_cursor'])
  need(root.next_cursor === null || (typeof root.next_cursor === 'string' && root.next_cursor.length <= 512))
  const items = list(root.items, 200).map(raw => {
    const item = record(raw, ['execution_id', 'work_order_id', 'item_id', 'recipe_version', 'target_quantity', 'registered_at', 'started_at', 'ended_at', 'totals', 'yield_ratio', 'settlement_status', 'provisional', 'provisional_reasons', 'durations_seconds', 'stopped_by_state'])
    need(identifier(item.execution_id) && identifier(item.work_order_id) && (item.item_id === null || identifier(item.item_id)) && identifier(item.recipe_version))
    const sourceId = Object.hasOwn(item, 'source_id') ? item.source_id : null
    const streamId = Object.hasOwn(item, 'stream_id') ? item.stream_id : null
    need((sourceId === null && streamId === null) || (identifier(sourceId) && identifier(streamId)))
    need(item.target_quantity === null || (count(item.target_quantity) && item.target_quantity > 0))
    need(isTime(item.registered_at) && nullableTime(item.started_at) && nullableTime(item.ended_at))
    need(ratio(item.yield_ratio) && identifier(item.settlement_status) && typeof item.provisional === 'boolean')
    const stopped = record(item.stopped_by_state, [])
    need(Object.entries(stopped).every(([key, amount]) => identifier(key) && seconds(amount)))
    return {
      execution_id: item.execution_id, work_order_id: item.work_order_id, item_id: item.item_id as string | null, recipe_version: item.recipe_version,
      source_id: sourceId as string | null, stream_id: streamId as string | null,
      target_quantity: item.target_quantity as number | null, registered_at: item.registered_at,
      started_at: item.started_at as string | null, ended_at: item.ended_at as string | null,
      totals: totals(item.totals), yield_ratio: item.yield_ratio, settlement_status: item.settlement_status,
      provisional: item.provisional, provisional_reasons: reasons(item.provisional_reasons),
      durations_seconds: durations(item.durations_seconds), stopped_by_state: stopped as Record<string, number>,
    }
  })
  return { items, next_cursor: root.next_cursor as string | null }
}

export type SegmentClass = 'RUNNING' | 'STOPPED' | 'UNKNOWN'
export interface StopReason { code: string; note: string; recorded_by: string; recorded_at: string }
export interface Segment { from: string; to: string; class: SegmentClass; state: string | null; fault_code: number | null; stop_reason: StopReason | null; unknown_reason: string | null }
export interface Timeline { execution_id: string; from: string; to: string; segments: Segment[]; durations_seconds: { running: number; stopped: number; unknown: number; total: number }; check_ok: boolean }

export function parseTimeline(value: unknown): Timeline {
  const root = record(value, ['execution_id', 'from', 'to', 'segments', 'durations_seconds', 'check_ok'])
  need(identifier(root.execution_id) && isTime(root.from) && isTime(root.to) && typeof root.check_ok === 'boolean')
  let cursor = micros(root.from)
  const segments = list(root.segments, 100_000).map(raw => {
    const segment = record(raw, ['from', 'to', 'class', 'state', 'fault_code', 'stop_reason'])
    need(isTime(segment.from) && isTime(segment.to) && micros(segment.from) >= cursor && micros(segment.from) < micros(segment.to))
    cursor = micros(segment.to)
    need(['RUNNING', 'STOPPED', 'UNKNOWN'].includes(segment.class as string))
    need(segment.state === null || identifier(segment.state))
    need(segment.fault_code === null || count(segment.fault_code))
    let reason: StopReason | null = null
    if (segment.stop_reason !== null) {
      need(segment.class === 'STOPPED')
      const entry = record(segment.stop_reason, ['code', 'note', 'recorded_by', 'recorded_at'])
      need(identifier(entry.code) && text(entry.note, 200) && identifier(entry.recorded_by) && isTime(entry.recorded_at))
      reason = { code: entry.code, note: entry.note, recorded_by: entry.recorded_by, recorded_at: entry.recorded_at }
    }
    const unknownReason = Object.hasOwn(segment, 'unknown_reason') ? segment.unknown_reason : null
    need(unknownReason === null || identifier(unknownReason))
    if (segment.class === 'UNKNOWN') need(segment.state === null)
    return {
      from: segment.from, to: segment.to, class: segment.class as SegmentClass, state: segment.state as string | null,
      fault_code: segment.fault_code as number | null, stop_reason: reason, unknown_reason: unknownReason as string | null,
    }
  })
  need(cursor <= micros(root.to))
  return { execution_id: root.execution_id, from: root.from, to: root.to, segments, durations_seconds: durations(root.durations_seconds), check_ok: root.check_ok }
}

export interface DailyReport {
  date: string; from: string; to: string
  durations_seconds: { running: number; stopped: number; unknown: number; total: number }
  running_ratio: number | null; unknown_ratio: number | null
  stopped_by_reason: Record<string, number>; totals: Totals; hourly: HourlyBucket[]
  unacknowledged_alarms: number; executions: string[]; provisional: boolean; provisional_reasons: string[]
}

export function parseDailyReport(value: unknown): DailyReport {
  const root = record(value, ['date', 'from', 'to', 'durations_seconds', 'running_ratio', 'unknown_ratio', 'stopped_by_reason', 'totals', 'hourly', 'unacknowledged_alarms', 'executions', 'provisional', 'provisional_reasons'])
  need(date(root.date) && isTime(root.from) && isTime(root.to) && ratio(root.running_ratio) && ratio(root.unknown_ratio))
  const stopped = record(root.stopped_by_reason, [])
  need(Object.entries(stopped).every(([key, amount]) => identifier(key) && seconds(amount)))
  need(count(root.unacknowledged_alarms) && typeof root.provisional === 'boolean')
  const executions = list(root.executions, 20_000).map(item => { need(identifier(item)); return item as string })
  return {
    date: root.date, from: root.from, to: root.to, durations_seconds: durations(root.durations_seconds),
    running_ratio: root.running_ratio, unknown_ratio: root.unknown_ratio, stopped_by_reason: stopped as Record<string, number>,
    totals: totals(root.totals), hourly: hourly(root.hourly), unacknowledged_alarms: root.unacknowledged_alarms,
    executions, provisional: root.provisional, provisional_reasons: reasons(root.provisional_reasons),
  }
}

export const STOP_REASONS: readonly { code: string; label: string }[] = [
  { code: 'MATERIAL_WAIT', label: '자재 대기' },
  { code: 'EQUIPMENT', label: '설비 문제' },
  { code: 'CHANGEOVER', label: '작업 전환' },
  { code: 'PLANNED', label: '계획 정지' },
  { code: 'UNCLASSIFIED', label: '미분류' },
]

// ---- CX-04 SPC -------------------------------------------------------------------------------

export interface ControlLimits { ucl: number; cl: number; lcl: number }
export interface Subgroup { t: string; mean: number; range: number; n: number; violations: string[] }
export interface Spc {
  tag_id: string; unit: string; method: string; subgroup_size: number; rules_version: string
  status: 'OK' | 'INSUFFICIENT_DATA'
  /** Set when retention removed the run's original prefix, so no later data can certify a baseline. */
  baseline_limitation: string | null
  baseline: { from: string; to: string; subgroups: number } | null
  limits: { xbar: ControlLimits; range: ControlLimits } | null
  spec: { usl: number; lsl: number; target: number | null; source: string } | null
  capability: { cp: number; cpk: number; subgroups: number } | null
  subgroups: Subgroup[]
  excluded: { count: number; reasons: Record<string, number> }
  trend_warning: { method: string; slope_per_minute: number | null; r2: number | null; projected_cross: { limit: string; at: string } | null } | null
  generated_at: string
}

function limits(value: unknown): ControlLimits {
  const result = record(value, ['ucl', 'cl', 'lcl'])
  need(finite(result.ucl) && finite(result.cl) && finite(result.lcl) && result.lcl <= result.cl && result.cl <= result.ucl)
  return { ucl: result.ucl, cl: result.cl, lcl: result.lcl }
}

export function parseSpc(value: unknown): Spc {
  const root = record(value, ['tag_id', 'unit', 'method', 'subgroup_size', 'rules_version', 'status', 'baseline', 'limits', 'spec', 'capability', 'subgroups', 'excluded', 'trend_warning', 'generated_at'])
  need(identifier(root.tag_id) && text(root.unit, 16) && identifier(root.method) && count(root.subgroup_size) && root.subgroup_size >= 2)
  need(identifier(root.rules_version) && (root.status === 'OK' || root.status === 'INSUFFICIENT_DATA') && isTime(root.generated_at))
  const limitation = Object.hasOwn(root, 'baseline_limitation') ? root.baseline_limitation : null
  need(limitation === null || identifier(limitation))
  let baseline: Spc['baseline'] = null
  if (root.baseline !== null) {
    const item = record(root.baseline, ['from', 'to', 'subgroups'])
    need(isTime(item.from) && isTime(item.to) && count(item.subgroups))
    baseline = { from: item.from, to: item.to, subgroups: item.subgroups }
  }
  const limitsValue = root.limits === null ? null : (() => {
    const item = record(root.limits, ['xbar', 'range'])
    return { xbar: limits(item.xbar), range: limits(item.range) }
  })()
  // Without a baseline there are no control limits to draw.
  if (root.status === 'INSUFFICIENT_DATA') need(limitsValue === null)
  else need(limitsValue !== null)
  let spec: Spc['spec'] = null
  if (root.spec !== null) {
    const item = record(root.spec, ['usl', 'lsl', 'target', 'source'])
    need(finite(item.usl) && finite(item.lsl) && item.lsl < item.usl && nullableFinite(item.target) && text(item.source, 64))
    spec = { usl: item.usl, lsl: item.lsl, target: item.target as number | null, source: item.source }
  }
  let capability: Spc['capability'] = null
  if (root.capability !== null) {
    const item = record(root.capability, ['cp', 'cpk', 'subgroups'])
    need(finite(item.cp) && finite(item.cpk) && count(item.subgroups) && spec !== null)
    capability = { cp: item.cp, cpk: item.cpk, subgroups: item.subgroups }
  }
  let previous = -Infinity
  const subgroups = list(root.subgroups, 20_000).map(raw => {
    const item = record(raw, ['t', 'mean', 'range', 'n', 'violations'])
    need(isTime(item.t) && Date.parse(item.t) > previous && finite(item.mean) && finite(item.range) && item.range >= 0 && count(item.n))
    previous = Date.parse(item.t)
    const violations = list(item.violations, 8).map(rule => { need(identifier(rule)); return rule as string })
    return { t: item.t, mean: item.mean, range: item.range, n: item.n, violations }
  })
  const excluded = record(root.excluded, ['count', 'reasons'])
  need(count(excluded.count))
  const excludedReasons = record(excluded.reasons, [])
  need(Object.entries(excludedReasons).every(([key, amount]) => identifier(key) && count(amount)))
  let trend: Spc['trend_warning'] = null
  if (root.trend_warning !== null) {
    const item = record(root.trend_warning, ['method', 'slope_per_minute', 'r2', 'projected_cross'])
    need(identifier(item.method) && nullableFinite(item.slope_per_minute) && ratio(item.r2))
    let cross: { limit: string; at: string } | null = null
    if (item.projected_cross !== null) {
      const entry = record(item.projected_cross, ['limit', 'at'])
      need(identifier(entry.limit) && isTime(entry.at))
      cross = { limit: entry.limit, at: entry.at }
    }
    trend = { method: item.method, slope_per_minute: item.slope_per_minute as number | null, r2: item.r2, projected_cross: cross }
  }
  return {
    tag_id: root.tag_id, unit: root.unit, method: root.method, subgroup_size: root.subgroup_size, rules_version: root.rules_version,
    status: root.status, baseline_limitation: limitation as string | null, baseline, limits: limitsValue, spec, capability, subgroups,
    excluded: { count: excluded.count, reasons: excludedReasons as Record<string, number> }, trend_warning: trend, generated_at: root.generated_at,
  }
}

// ---- transport -------------------------------------------------------------------------------

// The largest legal trend (4 tags x 10,080 minute points) is about 7 MiB of JSON.
const MAX_BYTES = 16 * 1024 * 1024

// Reports over a long run can take tens of seconds; the serverless function allows 60 seconds.
const READ_TIMEOUT_MS = 45_000

export async function readJson<T>(path: string, parse: (value: unknown) => T, signal: AbortSignal): Promise<T> {
  let response: Response
  try {
    response = await fetch(path, { credentials: 'same-origin', cache: 'no-store', signal: AbortSignal.any([signal, AbortSignal.timeout(READ_TIMEOUT_MS)]) })
  } catch (cause) {
    if (signal.aborted) throw cause
    throw new AnalyticsError(0, '서버 응답을 확인할 수 없습니다. 연결을 확인해 주세요.')
  }
  if (!response.ok) {
    void response.body?.cancel().catch(() => {})
    const messages: Record<number, string> = {
      401: '세션이 만료되었습니다.', 403: '조회 권한이 없습니다.', 404: '조회 대상이 없습니다.',
      422: '조회 조건을 확인해 주세요.', 503: '운영 데이터 서비스를 사용할 수 없습니다.',
    }
    throw new AnalyticsError(response.status, messages[response.status] ?? '데이터를 조회하지 못했습니다.')
  }
  const body = await response.text()
  if (body.length > MAX_BYTES) throw new AnalyticsError(0, '응답이 너무 큽니다.')
  let value: unknown
  try { value = JSON.parse(body) } catch { throw new AnalyticsError(0, '응답 형식을 확인할 수 없습니다.') }
  return parse(value)
}

export function query(path: string, params: Record<string, string | string[]>): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    for (const item of Array.isArray(value) ? value : [value]) search.append(key, item)
  }
  return `${path}?${search}`
}

export interface DowntimeReasonBody {
  request_id: string; execution_id: string; line_id: string; source_id: string; stream_id: string
  segment_from: string; reason_code: string; note: string
}

/** CX-03 write. The caller keeps request_id so a lost response can be retried with the same body. */
export async function postDowntimeReason(body: DowntimeReasonBody, csrf: string): Promise<void> {
  let response: Response
  try {
    response = await fetch('/api/v1/downtime-reasons', {
      method: 'POST', credentials: 'same-origin', cache: 'no-store',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf, 'X-HMI-Request': '1' },
      body: JSON.stringify(body), signal: AbortSignal.timeout(15_000),
    })
  } catch {
    throw new AnalyticsError(0, '저장 결과를 확인할 수 없습니다. 같은 요청으로 다시 시도해 주세요.')
  }
  void response.body?.cancel().catch(() => {})
  if (!response.ok) {
    const messages: Record<number, string> = {
      401: '세션이 만료되었습니다.', 403: '정지 사유를 입력할 권한이 없습니다.',
      409: '정지 구간이 바뀌었거나 같은 요청 ID로 다른 내용이 저장되어 있습니다. 타임라인을 새로 조회해 주세요.',
      422: '입력 내용을 확인해 주세요. 불명 구간에는 사유를 넣을 수 없습니다.',
    }
    throw new AnalyticsError(response.status, messages[response.status] ?? '정지 사유를 저장하지 못했습니다.')
  }
}
