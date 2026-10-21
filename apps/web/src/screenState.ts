import { tagEnums, tagMetadata } from './tagMetadata'

export interface ScreenScope { line_id: string; source_id: string; stream_id: string }
export interface ScreenSample {
  sample_id: string; tag_id: string; source_timestamp: string | null; server_timestamp: string | null
  acquired_at: string; status_code: number
  reading: { data_type: 'Boolean' | 'Int32' | 'UInt32' | 'UInt64' | 'Double' | 'String'; value: boolean | number | string | null }
}
export interface SampleEvidence {
  tag_id: string; batch_id: string; capture_generation: string; capture_seq: string
  captured_at: string; stored_at: string; fresh: boolean
}
export interface ScreenHealth {
  state: 'NEVER_OBSERVED' | 'STALE' | 'DEGRADED' | 'CAPTURE_FRESH'
  heartbeat: 'UNKNOWN' | 'GOOD' | 'DELAYED' | 'ALARM'
  communication: {
    state: 'UNKNOWN' | 'GOOD' | 'DELAYED' | 'ALARM'; connected: boolean
    read_age_seconds: number | null; heartbeat_ages_seconds: Record<string, number | null>
  } | null
  communication_captured_at: string | null; communication_stored_at: string | null
  source_conflicted: boolean; reconciliation_required: boolean
}
export interface ScreenProjection {
  schema_version: '1.0'; scope: ScreenScope
  screen: {
    schema_version: '1.0'; kind: 'SNAPSHOT' | 'UPDATE'; sync_id: string; revision: string
    base_revision?: string; emitted_at: string
    view: { line_id: string; state: string; execution_id: string | null; control_restricted: boolean; samples: readonly ScreenSample[] }
  }
  health: ScreenHealth; evidence: readonly SampleEvidence[]; missing_tag_ids: readonly string[]
}
export interface ScreenReset {
  schema_version: '1.0'; scope: ScreenScope
  screen: { schema_version: '1.0'; kind: 'RESYNC_REQUIRED'; sync_id: string; reason: 'REVISION_GAP' | 'RECONNECT' | 'BACKPRESSURE' | 'SERVER_RESTART'; emitted_at: string }
}
export type ScreenEnvelope = ScreenProjection | ScreenReset
export interface ScreenState {
  phase: 'awaiting_snapshot' | 'synced' | 'resync_required'
  projection: ScreenProjection | null
}
export const MAX_SCREEN_BYTES = 131072
const MAX_UINT64 = 18446744073709551615n
const heartbeatIds = ['Line.Heartbeat', 'Infeed.LinkHeartbeat', 'Sealer.LinkHeartbeat', 'Outfeed.LinkHeartbeat']
const equipmentStates = [...Object.values(tagEnums.EquipmentState), 'UNKNOWN']
const healthStates = ['NEVER_OBSERVED', 'STALE', 'DEGRADED', 'CAPTURE_FRESH']
const communicationStates = ['UNKNOWN', 'GOOD', 'DELAYED', 'ALARM']
const typesByName: Record<string, ScreenSample['reading']['data_type']> = {}
for (const [dataType, names] of Object.entries({
  Boolean: ['EStop', 'GuardClosed', 'Permissive', 'RunFeedback', 'EntrySensor', 'HeaterOn', 'Ready', 'ExitSensor'],
  Int32: ['State', 'ControlMode', 'OperationMode', 'CheckResult'],
  UInt32: ['FaultCode', 'TargetQuantity', 'WipCount', 'BufferCount'],
  UInt64: ['GoodTotal', 'RejectTotal', 'ScrapTotal', 'Heartbeat', 'ControlVersion', 'EventSeq', 'LinkHeartbeat', 'GoodCounter', 'RejectCounter'],
  Double: ['MotorCurrent', 'ActualSpeed', 'TemperaturePV', 'TemperatureSP', 'AirPressure'],
  String: ['ActiveExecutionId', 'RecipeVersion', 'CounterEpoch'],
})) for (const name of names) typesByName[name] = dataType as ScreenSample['reading']['data_type']
const tags = new Map(tagMetadata.map(tag => [tag.logical_name as string, { dataType: typesByName[tag.name], enumName: tag.enum }]))

class ScreenProtocolError extends Error { constructor() { super('Invalid screen protocol message') } }
function need(condition: unknown): asserts condition { if (!condition) throw new ScreenProtocolError() }
function object(value: unknown, keys: readonly string[]): Record<string, unknown> {
  need(typeof value === 'object' && value !== null && !Array.isArray(value))
  const result = value as Record<string, unknown>
  need(Object.keys(result).length === keys.length && keys.every(key => Object.hasOwn(result, key)))
  return result
}
function scalarString(value: unknown, max: number): value is string {
  return typeof value === 'string' && [...value].length <= max
    && ![...value].some(point => { const code = point.codePointAt(0)!; return code >= 0xd800 && code <= 0xdfff })
}
const backendWhitespace = new Set([9, 10, 11, 12, 13, 32, 133, 160, 5760, ...Array.from({ length: 11 }, (_, index) => 8192 + index), 8232, 8233, 8239, 8287, 12288])
function identifier(value: unknown): value is string {
  // Match the backend's Unicode White_Space class, including NEL (not JS \s).
  return scalarString(value, 128) && value.length >= 1
    && [...value].every(point => !backendWhitespace.has(point.codePointAt(0)!))
}
export function screenUInt64(value: unknown): value is string {
  return typeof value === 'string' && /^(0|[1-9][0-9]{0,19})$(?![\s\S])/.test(value) && BigInt(value) <= MAX_UINT64
}
function timestamp(value: unknown): value is string {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})$(?![\s\S])/.test(value)) return false
  const year = Number(value.slice(0, 4)), month = Number(value.slice(5, 7)), day = Number(value.slice(8, 10))
  const days = [31, year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0) ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
  return year > 0 && month >= 1 && month <= 12 && day >= 1 && day <= days[month - 1]
    && Number(value.slice(11, 13)) <= 23 && Number(value.slice(14, 16)) <= 59 && Number(value.slice(17, 19)) <= 59 && Number.isFinite(Date.parse(value))
}
const nullableTimestamp = (value: unknown) => value === null || timestamp(value)
const integer = (value: unknown, min: number, max: number) => typeof value === 'number' && Number.isInteger(value) && value >= min && value <= max
function scope(value: unknown, expected: ScreenScope) {
  const result = object(value, ['line_id', 'source_id', 'stream_id'])
  need(identifier(result.line_id) && identifier(result.source_id) && identifier(result.stream_id))
  need(result.line_id === expected.line_id && result.source_id === expected.source_id && result.stream_id === expected.stream_id)
}
function sample(value: unknown): ScreenSample {
  const result = object(value, ['sample_id', 'tag_id', 'source_timestamp', 'server_timestamp', 'acquired_at', 'status_code', 'reading'])
  need(identifier(result.sample_id) && typeof result.tag_id === 'string' && tags.has(result.tag_id))
  need(nullableTimestamp(result.source_timestamp) && nullableTimestamp(result.server_timestamp) && timestamp(result.acquired_at) && integer(result.status_code, 0, 4294967295))
  const reading = object(result.reading, ['data_type', 'value'])
  const tag = tags.get(result.tag_id)!
  need(reading.data_type === tag.dataType)
  const valueRaw = reading.value
  if (valueRaw === null) need(((result.status_code as number) >>> 30) !== 0)
  else {
    switch (reading.data_type) {
      case 'Boolean': need(typeof valueRaw === 'boolean'); break
      case 'Int32': need(integer(valueRaw, -2147483648, 2147483647)); break
      case 'UInt32': need(integer(valueRaw, 0, 4294967295)); break
      case 'UInt64': need(screenUInt64(valueRaw)); break
      case 'Double': need(typeof valueRaw === 'number' && Number.isFinite(valueRaw)); break
      case 'String': need(scalarString(valueRaw, 128)); break
      default: throw new ScreenProtocolError()
    }
    if (tag.enumName !== null) need(Object.hasOwn(tagEnums[tag.enumName], String(valueRaw)))
  }
  return result as unknown as ScreenSample
}
function health(value: unknown) {
  const result = object(value, ['state', 'heartbeat', 'communication', 'communication_captured_at', 'communication_stored_at', 'source_conflicted', 'reconciliation_required'])
  need(healthStates.includes(result.state as string) && communicationStates.includes(result.heartbeat as string))
  need(typeof result.source_conflicted === 'boolean' && typeof result.reconciliation_required === 'boolean')
  need(nullableTimestamp(result.communication_captured_at) && nullableTimestamp(result.communication_stored_at))
  if (result.communication !== null) {
    const communication = object(result.communication, ['state', 'connected', 'read_age_seconds', 'heartbeat_ages_seconds'])
    need(communicationStates.includes(communication.state as string) && typeof communication.connected === 'boolean')
    const heartbeatAges = object(communication.heartbeat_ages_seconds, heartbeatIds)
    const ages = [communication.read_age_seconds, ...Object.values(heartbeatAges)]
    need(ages.every(age => age === null || (typeof age === 'number' && Number.isFinite(age) && age >= 0)))
    const oldest = Math.max(0, ...ages.filter((age): age is number => typeof age === 'number'))
    if (!(communication.state === 'UNKNOWN' && oldest < 3)) {
      const expected = oldest >= 5 ? 'ALARM' : oldest >= 3 ? 'DELAYED' : !communication.connected || ages.some(age => age === null) ? 'UNKNOWN' : 'GOOD'
      need(communication.state === expected)
    }
  }
}
function freeze<T>(value: T): T {
  if (value !== null && typeof value === 'object') {
    for (const item of Object.values(value)) freeze(item)
    Object.freeze(value)
  }
  return value
}
/** Decode a complete frame. Errors contain no payload, credential or source value. */
export function decodeScreenFrame(raw: string, expected: ScreenScope): ScreenEnvelope {
  need(typeof raw === 'string' && new TextEncoder().encode(raw).byteLength <= MAX_SCREEN_BYTES)
  let decoded: unknown
  try { decoded = JSON.parse(raw) } catch { throw new ScreenProtocolError() }
  need(typeof decoded === 'object' && decoded !== null && 'screen' in decoded)
  const rawScreen = (decoded as Record<string, unknown>).screen
  need(typeof rawScreen === 'object' && rawScreen !== null && 'kind' in rawScreen)
  const reset = rawScreen.kind === 'RESYNC_REQUIRED'
  const result = object(decoded, reset ? ['schema_version', 'scope', 'screen'] : ['schema_version', 'scope', 'screen', 'health', 'evidence', 'missing_tag_ids'])
  need(result.schema_version === '1.0')
  scope(result.scope, expected)
  const kind = rawScreen.kind
  const screen = object(rawScreen, reset ? ['schema_version', 'kind', 'sync_id', 'reason', 'emitted_at']
    : kind === 'UPDATE' ? ['schema_version', 'kind', 'sync_id', 'base_revision', 'revision', 'emitted_at', 'view']
      : ['schema_version', 'kind', 'sync_id', 'revision', 'emitted_at', 'view'])
  need(screen.schema_version === '1.0' && identifier(screen.sync_id) && timestamp(screen.emitted_at))
  if (reset) need(['REVISION_GAP', 'RECONNECT', 'BACKPRESSURE', 'SERVER_RESTART'].includes(screen.reason as string))
  else {
    need((kind === 'SNAPSHOT' || kind === 'UPDATE') && screenUInt64(screen.revision))
    if (kind === 'UPDATE') need(screenUInt64(screen.base_revision) && BigInt(screen.revision) === BigInt(screen.base_revision) + 1n)
    const view = object(screen.view, ['line_id', 'state', 'execution_id', 'control_restricted', 'samples'])
    need(view.line_id === expected.line_id && equipmentStates.includes(view.state as string))
    need((view.execution_id === null || identifier(view.execution_id)) && typeof view.control_restricted === 'boolean')
    need(view.state !== 'UNKNOWN' || view.control_restricted === true)
    need(Array.isArray(view.samples) && view.samples.length <= 48)
    const samples = view.samples.map(sample)
    const sampleTags = new Set(samples.map(item => item.tag_id))
    need(sampleTags.size === samples.length)
    health(result.health)
    need(Array.isArray(result.evidence) && result.evidence.length === samples.length)
    const evidenceTags = new Set<string>()
    for (const item of result.evidence) {
      const evidence = object(item, ['tag_id', 'batch_id', 'capture_generation', 'capture_seq', 'captured_at', 'stored_at', 'fresh'])
      need(typeof evidence.tag_id === 'string' && sampleTags.has(evidence.tag_id) && !evidenceTags.has(evidence.tag_id))
      need(identifier(evidence.batch_id) && screenUInt64(evidence.capture_generation) && screenUInt64(evidence.capture_seq))
      need(timestamp(evidence.captured_at) && timestamp(evidence.stored_at) && typeof evidence.fresh === 'boolean')
      evidenceTags.add(evidence.tag_id)
    }
    need(Array.isArray(result.missing_tag_ids) && result.missing_tag_ids.length <= 48)
    const missing = new Set<string>()
    for (const id of result.missing_tag_ids) {
      need(typeof id === 'string' && tags.has(id) && !sampleTags.has(id) && !missing.has(id))
      missing.add(id)
    }
  }
  return freeze(result) as unknown as ScreenEnvelope
}
export function beginScreen(projection: ScreenProjection | null = null): ScreenState { return { phase: 'awaiting_snapshot', projection } }
/** A gap retains observations as historical display only; a new connection must begin again. */
export function acceptScreen(state: ScreenState, frame: ScreenEnvelope): ScreenState {
  const bad: ScreenState = { phase: 'resync_required', projection: state.projection }
  if (frame.screen.kind === 'RESYNC_REQUIRED') return bad
  const projection = frame as ScreenProjection
  if (projection.screen.kind === 'SNAPSHOT') {
    if (state.phase !== 'awaiting_snapshot' || state.projection?.screen.sync_id === projection.screen.sync_id) return bad
    return { phase: 'synced', projection }
  }
  const previous = state.projection
  if (state.phase !== 'synced' || !previous || previous.screen.sync_id !== projection.screen.sync_id
    || previous.scope.line_id !== projection.scope.line_id || previous.scope.source_id !== projection.scope.source_id || previous.scope.stream_id !== projection.scope.stream_id
    || previous.screen.revision !== projection.screen.base_revision
    || BigInt(projection.screen.revision) !== BigInt(previous.screen.revision) + 1n) return bad
  return { phase: 'synced', projection }
}
export function screenWatchdogExpired(lastArrival: number, now: number): boolean {
  return !Number.isFinite(lastArrival) || !Number.isFinite(now) || now < lastArrival || now - lastArrival >= 3000
}
