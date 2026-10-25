import { reasonCodeOf } from './conflictReasons'
import { screenUInt64 } from './screenState'

export type CommandKind = 'START' | 'PAUSE' | 'END_RUN' | 'RESET_FAULT' | 'SET_PARAMETERS'
export interface LoadArgs { work_order_id: string; recipe_version: string; target_quantity: number; parameters: { temperature_c: number; speed_m_per_min: number } }
export interface CommandScope { line_id: string; source_id: string; stream_id: string }
export interface CommandRequest {
  schema_version: '1.0'; command_id: string; target_id: 'Line'; execution_id: string | null
  command: { kind: Exclude<CommandKind, 'SET_PARAMETERS'>; args: Record<string, never> } | { kind: 'SET_PARAMETERS'; args: { recipe_version: string; parameters: { temperature_c: number; speed_m_per_min: number } } } | { kind: 'LOAD_JOB'; args: LoadArgs }
  requested_by: string; requested_at: string; expires_at: string; expected_control_version: string
}
export type CommandStatus = 'REQUESTED' | 'DELIVERED' | 'ACCEPTED' | 'SUCCEEDED' | 'FAILED' | 'REJECTED' | 'EXPIRED' | 'OUTCOME_UNKNOWN'
export interface CommandResult {
  command_id: string; status: CommandStatus; updated_at: string
  delivered_at?: string | null; completed_at?: string | null; reason_code?: string | null; reason?: string | null
  applied_control_version?: string | null
  evidence?: { state: string; execution_id: string | null; recipe_version?: string | null; last_event_seq?: string | null } | null
}
export interface TrackedCommand {
  commandId: string; scope: CommandScope; request: CommandRequest | null; counterEpoch: string | null
  result: CommandResult | null; phase: 'SUBMITTING' | 'TRACKING' | 'UNCERTAIN' | 'REJECTED'; message: string
}
export const commandLabels: Record<CommandKind | 'LOAD_JOB', string> = { START: '운전 시작 · 재개', PAUSE: '일시 정지', END_RUN: '현재 작업 종료', RESET_FAULT: '고장 리셋', SET_PARAMETERS: '운전 속도 변경', LOAD_JOB: '작업 불러오기' }
export const statusLabels: Record<CommandStatus, string> = {
  REQUESTED: '요청 저장 · 전달 대기', DELIVERED: '전달 기록 · 접수 확인 대기', ACCEPTED: 'PLC 접수 · 실행 완료 대기',
  SUCCEEDED: '실행 완료 기록', FAILED: '실행 실패', REJECTED: '명령 거절', EXPIRED: '미전달 만료', OUTCOME_UNKNOWN: '실행 결과 불명',
}
export const terminal = (result: CommandResult | null) => result !== null && ['SUCCEEDED', 'FAILED', 'REJECTED', 'EXPIRED'].includes(result.status)
export const sameScope = (left: CommandScope, right: CommandScope) => left.line_id === right.line_id && left.source_id === right.source_id && left.stream_id === right.stream_id
export const uint64String = screenUInt64
export class CommandHTTPError extends Error { constructor(public readonly status: number, public readonly reasonCode: string | null = null) { super('Command request was not confirmed') } }
export async function commandHTTP(path: string, signal: AbortSignal, body?: object, csrf?: string): Promise<unknown> {
  const response = await fetch(path, {
    method: body ? 'POST' : 'GET', credentials: 'same-origin', cache: 'no-store',
    headers: body ? { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf ?? '' } : {},
    ...(body ? { body: JSON.stringify(body) } : {}),
    signal: AbortSignal.any([signal, AbortSignal.timeout(8000)]),
  })
  if (!response.ok) throw new CommandHTTPError(response.status, await reasonCodeOf(response))
  return response.json()
}
export function parseResult(value: unknown, commandId: string): CommandResult {
  if (typeof value !== 'object' || value === null) throw new Error('Unconfirmed result')
  const result = value as CommandResult
  if (result.command_id !== commandId || !Object.hasOwn(statusLabels, result.status) || !Number.isFinite(Date.parse(result.updated_at))) throw new Error('Unconfirmed result')
  if (terminal(result) !== (typeof result.completed_at === 'string' && Number.isFinite(Date.parse(result.completed_at)))) throw new Error('Unconfirmed completion')
  if (['DELIVERED', 'ACCEPTED', 'SUCCEEDED', 'FAILED', 'OUTCOME_UNKNOWN'].includes(result.status)
    && (typeof result.delivered_at !== 'string' || !Number.isFinite(Date.parse(result.delivered_at)))) throw new Error('Unconfirmed delivery')
  if (result.status === 'SUCCEEDED' && (!result.evidence || typeof result.evidence.state !== 'string'
    || !uint64String(result.applied_control_version))) throw new Error('Unconfirmed completion evidence')
  return result
}

export function newCommandRequest(kind: CommandKind, executionId: string, controlVersion: string, actor: string, args: Record<string, unknown> = {}): CommandRequest {
  const at = Date.now()
  return {
    schema_version: '1.0', command_id: crypto.randomUUID(), target_id: 'Line', execution_id: executionId,
    command: { kind, args } as CommandRequest['command'], requested_by: actor,
    requested_at: new Date(at).toISOString(), expires_at: new Date(at + 5000).toISOString(), expected_control_version: controlVersion,
  }
}

export function commandLookupPath(commandId: string, source: CommandScope) {
  return `/api/v1/command-record?${new URLSearchParams({ command_id: commandId, line_id: source.line_id, source_id: source.source_id, stream_id: source.stream_id })}`
}
