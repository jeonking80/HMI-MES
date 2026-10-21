import type { CommandRequest, CommandResult, CommandScope } from './commands'
import { screenUInt64 } from './screenState'

export type WorkflowKind = 'release' | 'bind'
export interface ReleaseBody { release_id: string; expected_row_version: string }
export interface BindBody extends CommandScope { command_id: string; expected_work_order_version: string; expected_control_version: string }
export type WorkflowIntent = { work_order_id: string; actor: string } & (ReleaseBody | BindBody)
export interface ReleaseReceipt { release_id: string; work_order_id: string; released_by: string; released_at: string; status: 'RELEASED'; row_version: string }
export interface BindReceipt extends CommandScope { work_order_id: string; execution_id: string; command_id: string; work_order_row_version: string; load_request: CommandRequest }
export interface WorkflowRecord { intent: WorkflowIntent; receipt: ReleaseReceipt | BindReceipt }
export interface WorkflowAttempt { kind: WorkflowKind; intent: WorkflowIntent; knownIntent: boolean; expectedOrder: { recipe_version_id: string; target_quantity: number } | null; phase: 'sending' | 'uncertain' | 'confirmed' | 'conflict'; record: WorkflowRecord | null; message: string; loadResult?: CommandResult }
export function sameWorkflowValue(a: unknown, b: unknown): boolean {
  if (a === b) return true
  if (!a || !b || typeof a !== 'object' || typeof b !== 'object' || Array.isArray(a) !== Array.isArray(b)) return false
  const left = a as Record<string, unknown>, right = b as Record<string, unknown>
  const keys = Object.keys(left)
  return keys.length === Object.keys(right).length && keys.every(key => Object.hasOwn(right, key) && sameWorkflowValue(left[key], right[key]))
}
const fail = () => { throw new Error('Unconfirmed workflow record') }
const whitespace = new Set([9, 10, 11, 12, 13, 32, 133, 160, 5760, ...Array.from({ length: 11 }, (_, i) => 8192 + i), 8232, 8233, 8239, 8287, 12288])
export const workflowId = (v: unknown): v is string => typeof v === 'string' && [...v].length >= 1 && [...v].length <= 128 && ![...v].some(c => whitespace.has(c.codePointAt(0)!)) && ![...v].some(c => { const n = c.codePointAt(0)!; return n >= 0xd800 && n <= 0xdfff })
const positive = (v: unknown) => screenUInt64(v) && v !== '0'
const time = (v: unknown): v is string => typeof v === 'string' && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)$(?![\s\S])/.test(v) && !v.startsWith('0000-') && Number.isFinite(Date.parse(v)) && new Date(v).toISOString().slice(0, 10) === v.slice(0, 10)
function obj(v: unknown, keys: string[]): Record<string, unknown> {
  if (!v || typeof v !== 'object' || Array.isArray(v)) return fail()
  const r = v as Record<string, unknown>
  if (Object.keys(r).length !== keys.length || !keys.every(k => Object.hasOwn(r, k))) return fail()
  return r
}
export function workflowBody(kind: WorkflowKind, intent: WorkflowIntent): ReleaseBody | BindBody {
  if (kind === 'release' && 'release_id' in intent) return { release_id: intent.release_id, expected_row_version: intent.expected_row_version }
  if (kind === 'bind' && 'command_id' in intent) return { command_id: intent.command_id, line_id: intent.line_id, source_id: intent.source_id, stream_id: intent.stream_id, expected_work_order_version: intent.expected_work_order_version, expected_control_version: intent.expected_control_version }
  return fail()
}
export function workflowPath(kind: WorkflowKind, order: string, id?: string) {
  const query = new URLSearchParams({ work_order_id: order, ...(id === undefined ? {} : kind === 'release' ? { release_id: id } : { command_id: id }) })
  return id === undefined ? `/api/v1/work-order-actions/${kind}?${query}` : `/api/v1/work-order-receipts/${kind === 'release' ? 'release' : 'binding'}?${query}`
}
export function workflowIdentity(intent: WorkflowIntent) { return 'release_id' in intent ? intent.release_id : intent.command_id }
export function sameIntent(left: WorkflowIntent, right: WorkflowIntent) { const keys = Object.keys(left); return keys.length === Object.keys(right).length && keys.every(k => left[k as keyof WorkflowIntent] === right[k as keyof WorkflowIntent]) }
export function parseWorkflow(kind: WorkflowKind, value: unknown, order: string, id: string, expected?: WorkflowIntent, expectedOrder?: { recipe_version_id: string; target_quantity: number } | null): WorkflowRecord {
  const raw = obj(value, ['intent', 'receipt'])
  const intent = obj(raw.intent, kind === 'release' ? ['work_order_id', 'actor', 'release_id', 'expected_row_version'] : ['work_order_id', 'actor', 'command_id', 'line_id', 'source_id', 'stream_id', 'expected_work_order_version', 'expected_control_version'])
  if (intent.work_order_id !== order || !workflowId(intent.actor) || !workflowId(order) || !workflowId(id)) return fail()
  if (kind === 'release') {
    if (intent.release_id !== id || !positive(intent.expected_row_version)) return fail()
    const r = obj(raw.receipt, ['release_id', 'work_order_id', 'released_by', 'released_at', 'status', 'row_version'])
    if (r.release_id !== id || r.work_order_id !== order || r.released_by !== intent.actor || !time(r.released_at) || r.status !== 'RELEASED' || !positive(r.row_version) || BigInt(r.row_version as string) !== BigInt(intent.expected_row_version as string) + 1n) return fail()
  } else {
    if (intent.command_id !== id || ![intent.line_id, intent.source_id, intent.stream_id].every(workflowId) || !positive(intent.expected_work_order_version) || !screenUInt64(intent.expected_control_version)) return fail()
    const r = obj(raw.receipt, ['work_order_id', 'execution_id', 'command_id', 'line_id', 'source_id', 'stream_id', 'work_order_row_version', 'load_request'])
    if (r.work_order_id !== order || r.command_id !== id || !workflowId(r.execution_id) || !positive(r.work_order_row_version) || BigInt(r.work_order_row_version as string) !== BigInt(intent.expected_work_order_version as string) + 1n || !['line_id', 'source_id', 'stream_id'].every(k => r[k] === intent[k])) return fail()
    const request = obj(r.load_request, ['schema_version', 'command_id', 'target_id', 'execution_id', 'command', 'requested_by', 'requested_at', 'expires_at', 'expected_control_version'])
    if (request.schema_version !== '1.0' || request.command_id !== id || request.target_id !== 'Line' || request.execution_id !== r.execution_id || request.requested_by !== intent.actor || request.expected_control_version !== intent.expected_control_version || !time(request.requested_at) || !time(request.expires_at) || Date.parse(request.expires_at) - Date.parse(request.requested_at) !== 5000) return fail()
    const command = obj(request.command, ['kind', 'args'])
    const args = obj(command.args, ['work_order_id', 'recipe_version', 'target_quantity', 'parameters'])
    const parameters = obj(args.parameters, ['temperature_c', 'speed_m_per_min'])
    if (expectedOrder && (args.recipe_version !== expectedOrder.recipe_version_id || args.target_quantity !== expectedOrder.target_quantity)) return fail()
    if (command.kind !== 'LOAD_JOB' || args.work_order_id !== order || !workflowId(args.recipe_version) || typeof args.target_quantity !== 'number' || !Number.isInteger(args.target_quantity) || args.target_quantity < 1 || args.target_quantity > 4294967295 || typeof parameters.temperature_c !== 'number' || !Number.isFinite(parameters.temperature_c) || parameters.temperature_c < 0 || typeof parameters.speed_m_per_min !== 'number' || !Number.isFinite(parameters.speed_m_per_min) || parameters.speed_m_per_min <= 0) return fail()
  }
  const result = raw as unknown as WorkflowRecord
  if (expected && !sameIntent(expected, result.intent)) return fail()
  // Keep the stored original immutable; later list updates cannot rewrite intent.
  function freeze(v: unknown) { if (v && typeof v === 'object') { Object.values(v).forEach(freeze); Object.freeze(v) } }
  freeze(result)
  return result
}
