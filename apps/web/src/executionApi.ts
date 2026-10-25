import type { CommandResult, CommandScope, LoadArgs } from './commands'
import { parseResult } from './commands'
import { screenUInt64 } from './screenState'
import { parseWorkflow, sameWorkflowValue, workflowId } from './workflow'
import type { BindReceipt, WorkflowAttempt, WorkflowRecord } from './workflow'

interface Configuration { recipe_version: string; parameters: LoadArgs['parameters']; counter_epoch: string }
interface Totals { good_quantity: string; reject_quantity: string; scrap_quantity: string }
export interface RunSummary extends Configuration, Totals {
  schema_version: '1.0'; source_id: string; stream_id: string; execution_id: string
  state: 'LOADED' | 'RUNNING' | 'PAUSED' | 'ENDING' | 'ENDED'; as_of: string
  input_quantity: string; wip_quantity: number; last_event_seq: string; final: boolean; ended_at: string | null
}
export interface ExecutionView {
  execution: Configuration & { execution_id: string; work_order_id: string; registered_at: string; target_quantity: number | null }
  scope: CommandScope; binding: WorkflowRecord | null
  load: { request: BindReceipt['load_request']; result: CommandResult; collision_latched: boolean } | null
  settlement: { status: 'PENDING' | 'SETTLED' | 'MISMATCH' | 'CONFLICT'; reason_code: string; checked_at: string | null; received_at: string | null; summary: RunSummary | null }
  totals: Totals
  source: { gateway_id: string; capture_generation: string; conflicted: boolean; reconciliation_required: boolean; verified_through_seq: string; latest_ingested_seq: string; control_restricted: boolean }
}
export interface ArchivedWorkflow { attempt: WorkflowAttempt; execution: ExecutionView }
export class ExecutionArchiveError extends Error {}
export function freezeArchivedWorkflow(entry: ArchivedWorkflow): ArchivedWorkflow {
  function freeze(value: unknown) { if (value && typeof value === 'object') { Object.values(value).forEach(freeze); Object.freeze(value) } }
  freeze(entry)
  return entry
}
const fail = (): never => { throw new Error('Unconfirmed execution record') }
function need(value: unknown): asserts value { if (!value) fail() }
function object(value: unknown, keys: readonly string[], optional: readonly string[] = []): Record<string, unknown> {
  need(value && typeof value === 'object' && !Array.isArray(value))
  const result = value as Record<string, unknown>
  need(keys.every(key => Object.hasOwn(result, key)) && Object.keys(result).every(key => keys.includes(key) || optional.includes(key)))
  return result
}
const time = (value: unknown): value is string => typeof value === 'string'
  && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)$(?![\s\S])/.test(value)
  && !value.startsWith('0000-') && Number.isFinite(Date.parse(value)) && new Date(value).toISOString().slice(0, 10) === value.slice(0, 10)
const nullableTime = (value: unknown) => value === null || time(value)
const quantity = (value: unknown, minimum = 0) => typeof value === 'number' && Number.isInteger(value) && value >= minimum && value <= 4294967295
function parameters(value: unknown) {
  const result = object(value, ['temperature_c', 'speed_m_per_min'])
  need(typeof result.temperature_c === 'number' && Number.isFinite(result.temperature_c) && result.temperature_c >= 0)
  need(typeof result.speed_m_per_min === 'number' && Number.isFinite(result.speed_m_per_min) && result.speed_m_per_min > 0)
}
function totals(value: unknown) {
  const result = object(value, ['good_quantity', 'reject_quantity', 'scrap_quantity'])
  need(Object.values(result).every(screenUInt64))
}
function result(value: unknown, commandId: string): CommandResult {
  const r = object(value, ['schema_version', 'command_id', 'status', 'updated_at', 'delivered_at', 'completed_at', 'reason_code', 'reason', 'applied_control_version', 'evidence'])
  need(r.schema_version === '1.0' && time(r.updated_at) && nullableTime(r.delivered_at) && nullableTime(r.completed_at))
  need(r.reason_code === null || workflowId(r.reason_code))
  need(r.reason === null || (typeof r.reason === 'string' && [...r.reason].length >= 1 && [...r.reason].length <= 1000 && /\S/u.test(r.reason)))
  if (r.status === 'SUCCEEDED') {
    need(screenUInt64(r.applied_control_version))
    const evidence = object(r.evidence, ['state', 'execution_id', 'recipe_version', 'last_event_seq'])
    need(['STOPPED', 'READY', 'RUNNING', 'PAUSED', 'STOPPING', 'FAULT', 'ESTOP', 'RECOVERY_REQUIRED'].includes(evidence.state as string))
    need(evidence.execution_id === null || workflowId(evidence.execution_id))
    need(evidence.recipe_version === null || workflowId(evidence.recipe_version))
    need(evidence.last_event_seq === null || screenUInt64(evidence.last_event_seq))
  } else need(r.applied_control_version === null && r.evidence === null)
  const parsed = parseResult(r, commandId)
  if (['FAILED', 'REJECTED', 'EXPIRED', 'OUTCOME_UNKNOWN'].includes(parsed.status)) need(r.reason_code !== null && r.reason !== null)
  if (['REQUESTED', 'EXPIRED'].includes(parsed.status)) need(r.delivered_at === null)
  if (typeof r.delivered_at === 'string') need(Date.parse(r.delivered_at) <= Date.parse(r.updated_at))
  if (typeof r.completed_at === 'string') need(Date.parse(r.completed_at) <= Date.parse(r.updated_at) && (r.delivered_at === null || Date.parse(r.completed_at) >= Date.parse(r.delivered_at as string)))
  return parsed
}
export function executionPath(receipt: BindReceipt) {
  const query = new URLSearchParams({ line_id: receipt.line_id, source_id: receipt.source_id, stream_id: receipt.stream_id, execution_id: receipt.execution_id })
  return `/api/v1/execution-record?${query}`
}
export function parseExecution(value: unknown, expected: BindReceipt): ExecutionView {
  const raw = object(value, ['execution', 'scope', 'binding', 'load', 'settlement', 'totals', 'source'])
  const scope = object(raw.scope, ['line_id', 'source_id', 'stream_id'])
  need(['line_id', 'source_id', 'stream_id'].every(key => workflowId(scope[key]) && scope[key] === expected[key as keyof CommandScope]))
  const execution = object(raw.execution, ['execution_id', 'work_order_id', 'recipe_version', 'parameters', 'counter_epoch', 'registered_at', 'target_quantity'])
  need([execution.execution_id, execution.work_order_id, execution.recipe_version, execution.counter_epoch].every(workflowId))
  need(execution.execution_id === expected.execution_id && time(execution.registered_at) && (execution.target_quantity === null || quantity(execution.target_quantity, 1)))
  parameters(execution.parameters)
  if (raw.binding === null) need(raw.load === null)
  else {
    need(raw.load !== null)
    const binding = object(raw.binding, ['intent', 'receipt'])
    const original = object(binding.intent, ['work_order_id', 'actor', 'command_id', 'line_id', 'source_id', 'stream_id', 'expected_work_order_version', 'expected_control_version'])
    need(workflowId(original.command_id))
    const parsed = parseWorkflow('bind', raw.binding, execution.work_order_id as string, original.command_id)
    const receipt = parsed.receipt as BindReceipt
    const load = object(raw.load, ['request', 'result', 'collision_latched'])
    need(typeof load.collision_latched === 'boolean' && sameWorkflowValue(load.request, receipt.load_request))
    need(receipt.execution_id === execution.execution_id && ['line_id', 'source_id', 'stream_id'].every(key => scope[key] === receipt[key as keyof CommandScope]))
    const args = receipt.load_request.command.args as LoadArgs
    need(args.recipe_version === execution.recipe_version && args.target_quantity === execution.target_quantity && sameWorkflowValue(args.parameters, execution.parameters))
    const outcome = result(load.result, receipt.command_id)
    if (outcome.status === 'SUCCEEDED') need(outcome.evidence?.execution_id === execution.execution_id && outcome.evidence?.recipe_version === execution.recipe_version)
  }
  totals(raw.totals)
  const source = object(raw.source, ['gateway_id', 'capture_generation', 'conflicted', 'reconciliation_required', 'verified_through_seq', 'latest_ingested_seq', 'control_restricted'])
  need(workflowId(source.gateway_id) && [source.capture_generation, source.verified_through_seq, source.latest_ingested_seq].every(screenUInt64))
  need([source.conflicted, source.reconciliation_required, source.control_restricted].every(v => typeof v === 'boolean'))
  need(BigInt(source.verified_through_seq as string) <= BigInt(source.latest_ingested_seq as string))
  need(source.control_restricted === (source.conflicted || source.reconciliation_required || BigInt(source.verified_through_seq as string) < BigInt(source.latest_ingested_seq as string)))
  const settlement = object(raw.settlement, ['status', 'reason_code', 'checked_at', 'received_at', 'summary'])
  need(['PENDING', 'SETTLED', 'MISMATCH', 'CONFLICT'].includes(settlement.status as string))
  need(typeof settlement.reason_code === 'string' && [...settlement.reason_code].length >= 1 && [...settlement.reason_code].length <= 1000 && /\S/u.test(settlement.reason_code))
  need(nullableTime(settlement.checked_at) && nullableTime(settlement.received_at))
  if (settlement.summary === null) need(settlement.status === 'PENDING' && settlement.checked_at === null && settlement.received_at === null)
  else {
    need(time(settlement.checked_at) && time(settlement.received_at))
    const summary = object(settlement.summary, ['schema_version', 'source_id', 'stream_id', 'execution_id', 'state', 'as_of', 'counter_epoch', 'recipe_version', 'parameters', 'input_quantity', 'good_quantity', 'reject_quantity', 'scrap_quantity', 'wip_quantity', 'last_event_seq', 'final', 'ended_at'], ['end_reason', 'baseline_parameters', 'parameter_changes'])
    // CX-33/35: why the run ended, and the LOAD baseline when speed was adjusted during the run.
    need(!Object.hasOwn(summary, 'end_reason') || ['TARGET_REACHED', 'OPERATOR'].includes(summary.end_reason as string))
    need(!Object.hasOwn(summary, 'parameter_changes') || (Array.isArray(summary.parameter_changes) && Object.hasOwn(summary, 'baseline_parameters')))
    if (Object.hasOwn(summary, 'baseline_parameters')) parameters(summary.baseline_parameters)
    need(summary.schema_version === '1.0' && summary.execution_id === execution.execution_id && summary.source_id === scope.source_id && summary.stream_id === scope.stream_id)
    need(workflowId(summary.counter_epoch) && workflowId(summary.recipe_version) && time(summary.as_of) && nullableTime(summary.ended_at))
    parameters(summary.parameters)
    need(['input_quantity', 'good_quantity', 'reject_quantity', 'scrap_quantity', 'last_event_seq'].every(key => screenUInt64(summary[key])))
    need(quantity(summary.wip_quantity) && typeof summary.final === 'boolean' && ['LOADED', 'RUNNING', 'PAUSED', 'ENDING', 'ENDED'].includes(summary.state as string))
    const completed = BigInt(summary.good_quantity as string) + BigInt(summary.reject_quantity as string) + BigInt(summary.scrap_quantity as string)
    need(BigInt(summary.input_quantity as string) === completed + BigInt(summary.wip_quantity as number))
    need(completed === 0n || summary.last_event_seq !== '0')
    need(summary.final === (summary.state === 'ENDED'))
    need(summary.final ? summary.wip_quantity === 0 && time(summary.ended_at) && Date.parse(summary.ended_at) <= Date.parse(summary.as_of) : summary.ended_at === null)
    // The server already verified any speed-change evidence; the LOAD baseline must still be the original parameters.
    if (settlement.status === 'SETTLED') need(summary.final && summary.counter_epoch === execution.counter_epoch && summary.recipe_version === execution.recipe_version && sameWorkflowValue(Object.hasOwn(summary, 'baseline_parameters') ? summary.baseline_parameters : summary.parameters, execution.parameters))
  }
  function freeze(v: unknown) { if (v && typeof v === 'object') { Object.values(v).forEach(freeze); Object.freeze(v) } }
  freeze(raw)
  return raw as unknown as ExecutionView
}
/** Local history eligibility only; never next-bind permission or slot release. */
export function archiveBlocked(view: ExecutionView, original: WorkflowAttempt, login: string): string {
  if (!original.knownIntent || original.phase !== 'confirmed' || original.kind !== 'bind' || !original.record || !('execution_id' in original.record.receipt)) return '확인된 원래 실행 연결 기록이 필요합니다.'
  if (login !== original.intent.actor) return '원래 요청 계정으로 로그인해야 보관할 수 있습니다.'
  if (!sameWorkflowValue(original.intent, original.record.intent) || !view.binding || !sameWorkflowValue(view.binding, original.record)) return '원래 사용자·작업지시·명령·실행·수집원 기록이 일치하지 않습니다.'
  if (original.expectedOrder && (original.expectedOrder.recipe_version_id !== view.execution.recipe_version || original.expectedOrder.target_quantity !== view.execution.target_quantity)) return '원래 선택한 작업지시의 설정과 목표 수량이 일치하지 않습니다.'
  if (!view.load || view.load.result.status !== 'SUCCEEDED' || view.load.collision_latched || !sameWorkflowValue(view.load.request, original.record.receipt.load_request)) return '원래 작업 불러오기의 확정 성공과 충돌 없음이 필요합니다.'
  const summary = view.settlement.summary
  if (view.settlement.status !== 'SETTLED' || !summary?.final || !view.settlement.checked_at || !view.settlement.received_at) return '정확한 최종 저장 정산 일치 기록이 필요합니다.'
  if (summary.counter_epoch !== view.execution.counter_epoch || summary.recipe_version !== view.execution.recipe_version || !sameWorkflowValue(summary.parameters, view.execution.parameters)) return '원래 카운터 구간과 공정 설정이 일치하지 않습니다.'
  if (['good_quantity', 'reject_quantity', 'scrap_quantity'].some(key => summary[key as keyof Totals] !== view.totals[key as keyof Totals])) return '저장 정산 이후 현재 집계 수량이 달라졌습니다. 다시 대조해야 합니다.'
  if (view.source.conflicted || view.source.reconciliation_required || view.source.control_restricted || view.source.verified_through_seq !== view.source.latest_ingested_seq || BigInt(summary.last_event_seq) > BigInt(view.source.verified_through_seq)) return '현재 수집원 제한과 사건 대조 구간을 확인해야 합니다.'
  return ''
}
