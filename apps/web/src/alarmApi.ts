/** Exact-instance alarm HTTP boundary. Receipt recovery is always a passive GET. */
import type { Dispatch, SetStateAction } from 'react'
import type { Session } from './auth'

export interface AlarmSource { line_id: string; source_id: string; stream_id: string }
export type AlarmAsset = 'Sealer' | 'Line'
export interface AlarmScope extends AlarmSource { asset_id: AlarmAsset }
export type AlarmKind = 'TEMPERATURE_DEVIATION' | 'PRESSURE_LOW' | 'PLC_FAULT'
export type AlarmStatus = 'ACTIVE_UNACKNOWLEDGED' | 'ACTIVE_ACKNOWLEDGED' | 'RETURNED_UNACKNOWLEDGED' | 'RETURNED_ACKNOWLEDGED'
export interface AlarmEvidence {
  observed_at: string; references: string[]
  readings: { name: string; value: boolean | number | string | null }[]
  quality: 'KNOWN' | 'UNKNOWN'; time_quality: 'KNOWN' | 'UNKNOWN'
}
export interface AlarmInstance {
  alarm_id: string; scope: AlarmScope; rule: { rule_id: string; version: string; severity: string }
  raised_evidence: AlarmEvidence; condition_closed_at: string | null
  closure_reason: 'NORMAL_RETURN' | 'CONFIG_CHANGED' | null; closure_event_id: string | null
  return_evidence: AlarmEvidence | null; acknowledged_at: string | null; acknowledged_by: string | null
}
export interface AlarmRow { instance: AlarmInstance; status: AlarmStatus }
export interface AlarmAction {
  action_id: string; alarm_id: string; actor: string; at: string
  kind: 'ACKNOWLEDGE' | 'NOTE_ADD' | 'NOTE_EDIT' | 'RULE_CHANGE_CLOSE'
  note_id: string | null; previous_action_id: string | null; before: string | null; after: string | null
}
interface IntentBase { action_id: string; scope: AlarmScope; rule_id: string; alarm_id: string; actor: string }
export type AlarmIntent = IntentBase & (
  { kind: 'ACKNOWLEDGE' } | { kind: 'NOTE_ADD'; note_id: string; text: string }
  | { kind: 'NOTE_EDIT'; note_id: string; text: string; expected_previous_action_id: string }
)
export interface AlarmReceipt {
  intent: AlarmIntent; recorded_at: string
  receipt: { input_id: string; disposition: string; created_alarm_id: string | null; changed_alarm_id: string | null; replayed: boolean; notify_current: boolean }
  action: AlarmAction | null
}
export type AlarmReference = Pick<IntentBase, 'action_id' | 'scope' | 'rule_id' | 'alarm_id'>
export interface AlarmLedger {
  reference: AlarmReference; intent: AlarmIntent | null; phase: 'UNKNOWN' | 'CONFIRMED' | 'REJECTED'; message: string
  receipt: AlarmReceipt | null
}
export interface AlarmPanelProps {
  session: Session; source: AlarmSource; onSessionInvalid: () => void
  ledger: AlarmLedger | null; setLedger: Dispatch<SetStateAction<AlarmLedger | null>>
}
export interface AlarmCoverage {
  scope: AlarmScope; implemented_kinds: AlarmKind[]
  installed_channel_count: number
  installed_channels: { rule_id: string; rule_version: string; kind: AlarmKind; severity: string }[]
  has_more_installed_channels: boolean; consumer_status: 'UNKNOWN'; runtime_configuration_status: 'UNKNOWN'
}
export interface AlarmPage { coverage: AlarmCoverage; items: AlarmRow[]; has_more: boolean; next_before_alarm_id: string | null }
export interface ActionPage { items: AlarmAction[]; has_more: boolean; next_before_action_id: string | null }

export const alarmStatusLabels: Record<AlarmStatus, string> = {
  ACTIVE_UNACKNOWLEDGED: '발생 미확인', ACTIVE_ACKNOWLEDGED: '발생 확인',
  RETURNED_UNACKNOWLEDGED: '복귀 미확인', RETURNED_ACKNOWLEDGED: '복귀 확인',
}
export class AlarmHTTPError extends Error { constructor(public readonly status: number) { super('Alarm request was not confirmed') } }
function freeze<T>(value: T): T {
  if (value !== null && typeof value === 'object') {
    Object.values(value).forEach(freeze)
    Object.freeze(value)
  }
  return value
}
function need(value: unknown): asserts value { if (!value) throw new Error('Invalid alarm response') }
function object(value: unknown, keys: readonly string[]): Record<string, unknown> {
  need(value !== null && typeof value === 'object' && !Array.isArray(value))
  const row = value as Record<string, unknown>
  need(Object.keys(row).length === keys.length && keys.every(key => Object.hasOwn(row, key)))
  return row
}
function scalar(value: unknown, max: number): value is string {
  return typeof value === 'string' && [...value].length <= max
    && ![...value].some(point => { const code = point.codePointAt(0)!; return code >= 0xd800 && code <= 0xdfff })
}
export function alarmIdentifier(value: unknown): value is string {
  return scalar(value, 128) && value.length > 0 && ![...value].some(point => /[\s\u0085]/u.test(point) || (point.codePointAt(0)! >= 28 && point.codePointAt(0)! <= 31))
}
export function alarmText(value: unknown): value is string {
  return scalar(value, 1000) && [...value].some(point => !/[\s\u0085]/u.test(point) && !(point.codePointAt(0)! >= 28 && point.codePointAt(0)! <= 31))
}
function timestamp(value: unknown): value is string {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$(?![\s\S])/.test(value)) return false
  const year = Number(value.slice(0, 4)), month = Number(value.slice(5, 7)), day = Number(value.slice(8, 10))
  const days = [31, year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0) ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
  return year > 0 && month >= 1 && month <= 12 && day >= 1 && day <= days[month - 1]
    && Number(value.slice(11, 13)) <= 23 && Number(value.slice(14, 16)) <= 59
    && Number(value.slice(17, 19)) <= 59 && Number.isFinite(Date.parse(value))
}
const nullableId = (value: unknown) => value === null || alarmIdentifier(value)
const nullableTime = (value: unknown) => value === null || timestamp(value)
export function sameAlarmScope(a: AlarmScope, b: AlarmScope): boolean {
  return a.line_id === b.line_id && a.source_id === b.source_id && a.stream_id === b.stream_id && a.asset_id === b.asset_id
}
function parseScope(value: unknown, expected: AlarmScope): AlarmScope {
  const row = object(value, ['line_id', 'source_id', 'stream_id', 'asset_id'])
  need(['Line', 'Sealer'].includes(expected.asset_id) && ['Line', 'Sealer'].includes(row.asset_id as string))
  need(Object.values(row).every(alarmIdentifier) && sameAlarmScope(row as unknown as AlarmScope, expected))
  return row as unknown as AlarmScope
}
function evidence(value: unknown): AlarmEvidence {
  const row = object(value, ['observed_at', 'references', 'readings', 'quality', 'time_quality'])
  need(timestamp(row.observed_at) && ['KNOWN', 'UNKNOWN'].includes(row.quality as string) && ['KNOWN', 'UNKNOWN'].includes(row.time_quality as string))
  need(Array.isArray(row.references) && row.references.length > 0 && row.references.every(alarmIdentifier))
  need(Array.isArray(row.readings))
  row.readings.forEach(item => {
    const reading = object(item, ['name', 'value'])
    need(alarmIdentifier(reading.name))
    need(reading.value === null || typeof reading.value === 'boolean' || typeof reading.value === 'string'
      || typeof reading.value === 'number' && Number.isFinite(reading.value))
  })
  return row as unknown as AlarmEvidence
}
export function originalLineFaultCode(instance: AlarmInstance): number | null {
  if (instance.scope.asset_id !== 'Line') return null
  const readings = instance.raised_evidence.readings.filter(row => row.name === 'Line.FaultCode')
  if (readings.length !== 1) return null
  const value = readings[0].value
  return typeof value === 'number' && Number.isInteger(value) && value >= 1 && value <= 0xffff_ffff ? value : null
}
export function parseAlarmRow(value: unknown, expected: AlarmScope): AlarmRow {
  const row = object(value, ['instance', 'status'])
  const instance = object(row.instance, ['alarm_id', 'scope', 'rule', 'raised_evidence', 'condition_closed_at', 'closure_reason', 'closure_event_id', 'return_evidence', 'acknowledged_at', 'acknowledged_by'])
  need(alarmIdentifier(instance.alarm_id))
  parseScope(instance.scope, expected)
  const rule = object(instance.rule, ['rule_id', 'version', 'severity'])
  need(Object.values(rule).every(alarmIdentifier))
  const raised = evidence(instance.raised_evidence)
  need(raised.quality === 'KNOWN' && raised.time_quality === 'KNOWN')
  need(nullableTime(instance.condition_closed_at) && nullableTime(instance.acknowledged_at)
    && nullableId(instance.acknowledged_by) && nullableId(instance.closure_event_id))
  const closed = instance.condition_closed_at !== null, acknowledged = instance.acknowledged_at !== null
  need(acknowledged === (instance.acknowledged_by !== null))
  need(closed === (instance.closure_event_id !== null) && closed === (instance.closure_reason !== null))
  need(instance.closure_reason === null || ['NORMAL_RETURN', 'CONFIG_CHANGED'].includes(instance.closure_reason as string))
  need((instance.closure_reason === 'NORMAL_RETURN') === (instance.return_evidence !== null))
  if (instance.return_evidence !== null) {
    const returned = evidence(instance.return_evidence)
    need(returned.quality === 'KNOWN' && returned.time_quality === 'KNOWN' && returned.observed_at === instance.condition_closed_at)
  }
  for (const at of [instance.condition_closed_at, instance.acknowledged_at]) need(at === null || Date.parse(at as string) >= Date.parse(raised.observed_at))
  const status: AlarmStatus = closed ? acknowledged ? 'RETURNED_ACKNOWLEDGED' : 'RETURNED_UNACKNOWLEDGED' : acknowledged ? 'ACTIVE_ACKNOWLEDGED' : 'ACTIVE_UNACKNOWLEDGED'
  need(row.status === status)
  return row as unknown as AlarmRow
}
function page(value: unknown, cursorName: string, extra: string[] = []): Record<string, unknown> {
  const row = object(value, ['items', 'has_more', cursorName, ...extra])
  need(Array.isArray(row.items) && row.items.length <= 50 && typeof row.has_more === 'boolean')
  need(nullableId(row[cursorName]) && row.has_more === (row[cursorName] !== null))
  need(!row.has_more || row.items.length > 0)
  return row
}
export function parseAlarmPage(value: unknown, expected: AlarmScope): AlarmPage {
  const row = page(value, 'next_before_alarm_id', ['coverage'])
  const coverage = object(row.coverage, ['scope', 'implemented_kinds', 'installed_channel_count', 'installed_channels', 'has_more_installed_channels', 'consumer_status', 'runtime_configuration_status'])
  parseScope(coverage.scope, expected)
  const kinds = expected.asset_id === 'Line' ? ['PLC_FAULT'] : ['TEMPERATURE_DEVIATION', 'PRESSURE_LOW']
  need(Array.isArray(coverage.implemented_kinds) && coverage.implemented_kinds.length === kinds.length
    && kinds.every(kind => (coverage.implemented_kinds as unknown[]).includes(kind)))
  need(Number.isSafeInteger(coverage.installed_channel_count) && (coverage.installed_channel_count as number) >= 0
    && Array.isArray(coverage.installed_channels) && coverage.installed_channels.length <= 100
    && coverage.installed_channels.length <= (coverage.installed_channel_count as number)
    && typeof coverage.has_more_installed_channels === 'boolean'
    && coverage.has_more_installed_channels === (coverage.installed_channels.length < (coverage.installed_channel_count as number))
    && coverage.consumer_status === 'UNKNOWN' && coverage.runtime_configuration_status === 'UNKNOWN')
  const channels = coverage.installed_channels as unknown[]
  const rules = new Set<string>()
  channels.forEach(value => {
    const channel = object(value, ['rule_id', 'rule_version', 'kind', 'severity'])
    need(alarmIdentifier(channel.rule_id) && alarmIdentifier(channel.rule_version)
      && alarmIdentifier(channel.severity) && kinds.includes(channel.kind as string) && !rules.has(channel.rule_id))
    rules.add(channel.rule_id as string)
  })
  const items = (row.items as unknown[]).map(item => parseAlarmRow(item, expected))
  need(new Set(items.map(item => item.instance.alarm_id)).size === items.length)
  need(!row.has_more || row.next_before_alarm_id === items.at(-1)?.instance.alarm_id)
  return { coverage: coverage as unknown as AlarmCoverage, items, has_more: row.has_more as boolean, next_before_alarm_id: row.next_before_alarm_id as string | null }
}
function action(value: unknown, alarmId: string): AlarmAction {
  const row = object(value, ['action_id', 'alarm_id', 'actor', 'at', 'kind', 'note_id', 'previous_action_id', 'before', 'after'])
  need(alarmIdentifier(row.action_id) && row.alarm_id === alarmId && alarmIdentifier(row.actor) && timestamp(row.at))
  need(['ACKNOWLEDGE', 'NOTE_ADD', 'NOTE_EDIT', 'RULE_CHANGE_CLOSE'].includes(row.kind as string))
  need(nullableId(row.note_id) && nullableId(row.previous_action_id))
  need((row.before === null || alarmText(row.before)) && (row.after === null || alarmText(row.after)))
  if (row.kind === 'NOTE_ADD') need(row.note_id !== null && row.after !== null && row.before === null && row.previous_action_id === null)
  else if (row.kind === 'NOTE_EDIT') need([row.note_id, row.previous_action_id, row.before, row.after].every(item => item !== null))
  else need([row.note_id, row.previous_action_id, row.before, row.after].every(item => item === null))
  return row as unknown as AlarmAction
}
export function parseActionPage(value: unknown, alarmId: string): ActionPage {
  const row = page(value, 'next_before_action_id')
  const items = (row.items as unknown[]).map(item => action(item, alarmId))
  need(new Set(items.map(item => item.action_id)).size === items.length)
  need(!row.has_more || row.next_before_action_id === items.at(-1)?.action_id)
  return { items, has_more: row.has_more as boolean, next_before_action_id: row.next_before_action_id as string | null }
}
export function parseAlarmIntent(value: unknown, expected: AlarmScope): AlarmIntent {
  need(value !== null && typeof value === 'object' && 'kind' in value)
  const kind = value.kind
  need(['ACKNOWLEDGE', 'NOTE_ADD', 'NOTE_EDIT'].includes(kind as string))
  const keys = ['action_id', 'scope', 'rule_id', 'alarm_id', 'actor', 'kind']
  if (kind !== 'ACKNOWLEDGE') keys.push('note_id', 'text')
  if (kind === 'NOTE_EDIT') keys.push('expected_previous_action_id')
  const row = object(value, keys)
  need([row.action_id, row.rule_id, row.alarm_id, row.actor].every(alarmIdentifier))
  parseScope(row.scope, expected)
  if (kind !== 'ACKNOWLEDGE') need(alarmIdentifier(row.note_id) && alarmText(row.text))
  if (kind === 'NOTE_EDIT') need(alarmIdentifier(row.expected_previous_action_id))
  return freeze(row) as unknown as AlarmIntent
}
export function sameAlarmIntent(a: AlarmIntent, b: AlarmIntent): boolean {
  return a.action_id === b.action_id && sameAlarmScope(a.scope, b.scope) && a.rule_id === b.rule_id && a.alarm_id === b.alarm_id
    && a.actor === b.actor && a.kind === b.kind && ('note_id' in a ? 'note_id' in b && a.note_id === b.note_id && a.text === b.text : true)
    && ('expected_previous_action_id' in a ? 'expected_previous_action_id' in b && a.expected_previous_action_id === b.expected_previous_action_id : true)
}
export function parseAlarmReceipt(value: unknown, expected: AlarmIntent): AlarmReceipt {
  const row = object(value, ['intent', 'recorded_at', 'receipt', 'action'])
  const intent = parseAlarmIntent(row.intent, expected.scope)
  need(sameAlarmIntent(intent, expected) && timestamp(row.recorded_at))
  const result = object(row.receipt, ['input_id', 'disposition', 'created_alarm_id', 'changed_alarm_id', 'replayed', 'notify_current'])
  need(result.input_id === expected.action_id && alarmIdentifier(result.disposition)
    && nullableId(result.created_alarm_id) && nullableId(result.changed_alarm_id)
    && typeof result.replayed === 'boolean' && typeof result.notify_current === 'boolean')
  need([result.created_alarm_id, result.changed_alarm_id].every(id => id === null || id === expected.alarm_id))
  if (row.action !== null) {
    const actual = action(row.action, expected.alarm_id)
    need(actual.action_id === expected.action_id && actual.actor === expected.actor && actual.kind === expected.kind && actual.at === row.recorded_at)
    if ('note_id' in expected) need(actual.note_id === expected.note_id && actual.after === expected.text)
    if ('expected_previous_action_id' in expected) need(actual.previous_action_id === expected.expected_previous_action_id)
  } else need(expected.kind === 'ACKNOWLEDGE' && result.disposition === 'NO_CHANGE')
  return row as unknown as AlarmReceipt
}
export function alarmReference(intent: AlarmReference): AlarmReference {
  return freeze({ action_id: intent.action_id, scope: { ...intent.scope }, rule_id: intent.rule_id, alarm_id: intent.alarm_id })
}
export const alarmQuery = (scope: AlarmScope, ruleId?: string) => new URLSearchParams({ ...scope, ...(ruleId ? { rule_id: ruleId } : {}) })
export async function alarmRequest(path: string, signal: AbortSignal, intent?: AlarmIntent, csrf?: string): Promise<unknown> {
  const response = await fetch(path, {
    method: intent ? 'POST' : 'GET', credentials: 'same-origin', cache: 'no-store',
    headers: intent ? { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf ?? '' } : {},
    ...(intent ? { body: JSON.stringify(intent) } : {}),
    signal: AbortSignal.any([signal, AbortSignal.timeout(12_000)]),
  })
  if (!response.ok) throw new AlarmHTTPError(response.status)
  return response.json()
}
export const alarmCanAct = (session: Session) => session.roles.includes('operator') && !session.must_change_password
