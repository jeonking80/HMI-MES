import type { Session } from './auth'
import { screenUInt64 } from './screenState'
import { sameWorkflowValue, workflowId } from './workflow'
import { MesHTTPError, mesDetailPath, parseOrder } from './mesApi'
import type { WorkOrder } from './mesApi'

export interface CancellationReference { work_order_id: string; cancellation_id: string }
export interface CancellationIntent extends CancellationReference { actor: string; expected_row_version: string }
export interface CancellationReceipt extends CancellationReference { cancelled_by: string; cancelled_at: string; status: 'CANCELLED'; row_version: string }
export interface CancellationRecord { intent: CancellationIntent; receipt: CancellationReceipt }
export interface CancellationLedger {
  reference: CancellationReference; intent: CancellationIntent | null
  phase: 'SENDING' | 'UNKNOWN' | 'REJECTED' | 'CONFIRMED'; receipt: CancellationReceipt | null; message: string
  submission?: CancellationSubmission | null; observations?: readonly CancellationObservation[]; recovery_conflicted?: boolean
}
const fail = (): never => { throw new Error('Unconfirmed cancellation record') }
function object(value: unknown, keys: readonly string[]): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return fail()
  const result = value as Record<string, unknown>
  if (Object.keys(result).length !== keys.length || !keys.every(key => Object.hasOwn(result, key))) return fail()
  return result
}
const positive = (value: unknown): value is string => screenUInt64(value) && value !== '0'
function timestamp(value: unknown): value is string {
  return typeof value === 'string' && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)$(?![\s\S])/.test(value)
    && !value.startsWith('0000-') && Number.isFinite(Date.parse(value)) && new Date(value).toISOString().slice(0, 10) === value.slice(0, 10)
}
export function freezeCancellation<T>(value: T): T {
  if (value && typeof value === 'object') { Object.values(value).forEach(freezeCancellation); Object.freeze(value) }
  return value
}
export function cancellationReference(order: string, id: string): CancellationReference {
  if (!workflowId(order) || !workflowId(id)) return fail()
  return freezeCancellation({ work_order_id: order, cancellation_id: id })
}
export function cancellationPath(reference: CancellationReference, recover = true) {
  cancellationReference(reference.work_order_id, reference.cancellation_id)
  const query = new URLSearchParams({ work_order_id: reference.work_order_id, ...(recover ? { cancellation_id: reference.cancellation_id } : {}) })
  return `/api/v1/${recover ? 'work-order-receipts/cancellation' : 'work-order-actions/cancel'}?${query}`
}
export function cancellationBody(intent: CancellationIntent) {
  if (!workflowId(intent.actor) || !positive(intent.expected_row_version)) return fail()
  cancellationReference(intent.work_order_id, intent.cancellation_id)
  return freezeCancellation({ cancellation_id: intent.cancellation_id, expected_row_version: intent.expected_row_version })
}
export function parseCancellation(value: unknown, reference: CancellationReference, expected: CancellationIntent | null = null): CancellationRecord {
  const raw = object(value, ['intent', 'receipt'])
  const intent = object(raw.intent, ['work_order_id', 'actor', 'cancellation_id', 'expected_row_version'])
  const receipt = object(raw.receipt, ['cancellation_id', 'work_order_id', 'cancelled_by', 'cancelled_at', 'status', 'row_version'])
  cancellationReference(reference.work_order_id, reference.cancellation_id)
  if (intent.work_order_id !== reference.work_order_id || intent.cancellation_id !== reference.cancellation_id || !workflowId(intent.actor) || !positive(intent.expected_row_version)) return fail()
  if (receipt.work_order_id !== intent.work_order_id || receipt.cancellation_id !== intent.cancellation_id || receipt.cancelled_by !== intent.actor || !timestamp(receipt.cancelled_at) || receipt.status !== 'CANCELLED' || !positive(receipt.row_version) || BigInt(receipt.row_version) !== BigInt(intent.expected_row_version) + 1n) return fail()
  if (expected && !sameWorkflowValue(intent, expected)) return fail()
  return freezeCancellation(raw as unknown as CancellationRecord)
}
export const cancellationCanWrite = (session: Session | null) => session !== null && !session.must_change_password && session.roles.includes('production_manager')
export async function cancellationRequest(reference: CancellationReference, signal: AbortSignal, intent: CancellationIntent | null = null, csrf?: string): Promise<CancellationRecord> {
  const response = await fetch(cancellationPath(reference, intent === null), {
    method: intent ? 'POST' : 'GET', credentials: 'same-origin', cache: 'no-store',
    headers: intent ? { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf ?? '' } : {},
    ...(intent ? { body: JSON.stringify(cancellationBody(intent)) } : {}),
    signal: AbortSignal.any([signal, AbortSignal.timeout(12_000)]), redirect: 'error',
  })
  if (!response.ok) {
    if (intent && response.status === 409) {
      // A status/header alone is insufficient: a truncated body remains unconfirmed.
      const body = await response.json()
      const coherent = object(body, ['detail'])
      if (coherent.detail !== 'cancellation conflicts with original state or intent' || signal.aborted) return fail()
      throw new CancellationRejection(new Date().toISOString())
    }
    throw new MesHTTPError(response.status)
  }
  try {
    const body = await response.json()
    return parseCancellation(intent ? { intent, receipt: body } : body, reference, intent)
  } catch (cause) {
    if (!intent) throw new CancellationRecoveryConflict()
    throw cause
  }
}
export function cancellationFailure(original: Pick<CancellationLedger, 'reference' | 'intent' | 'receipt' | 'submission' | 'observations' | 'recovery_conflicted'>, status: number): CancellationLedger {
  return freezeCancellation({ ...original, phase: [400, 409, 413, 415, 422, 429].includes(status) ? 'REJECTED' : 'UNKNOWN', message: status === 409 ? '취소 요청이 거절되었습니다. 이미 연결되었거나 원래 버전·내용이 달라졌을 수 있습니다. 원래 ID만 조회하며 새 요청을 보내지 않습니다.' : status === 404 ? '원래 취소 기록을 아직 확인하지 못했습니다. 미처리로 단정하지 않으며 원래 ID를 보존합니다.' : '취소 응답을 확인할 수 없습니다. 원래 ID와 내용을 보존하며 조회만 가능합니다.' })
}

/** Only a fully read, route-coherent POST rejection is evidence for this bounded path. */
export class CancellationRecoveryConflict extends Error {}
export class CancellationRejection extends MesHTTPError {
  constructor(public readonly observedAt: string) { super(409) }
}
export interface CancellationObservation { method: 'POST' | 'GET'; status: number | null; outcome: 'HTTP' | 'UNCONFIRMED'; observed_at: string }
export interface CancellationSubmission { actor: string; generation: number; send_count: 1; rejection: CancellationObservation | null; indeterminate: boolean }
export interface RejectedCancellationArchive { ledger: CancellationLedger; current_order: WorkOrder; archived_at: string }
export function cancellationReprepareBlocked(ledger: CancellationLedger | null, session: Session | null, generation: number): string {
  if (!ledger?.intent || !ledger.submission?.rejection || ledger.submission.send_count !== 1 || ledger.submission.indeterminate || ledger.receipt || ledger.recovery_conflicted || ledger.submission.rejection.method !== 'POST' || ledger.submission.rejection.status !== 409 || ledger.submission.rejection.outcome !== 'HTTP' || !timestamp(ledger.submission.rejection.observed_at)) return '원래 취소 요청의 거절 응답을 확인한 경우에만 다시 검토할 수 있습니다.'
  if (!cancellationCanWrite(session) || session?.login !== ledger.intent.actor || ledger.submission.actor !== ledger.intent.actor || ledger.submission.generation !== generation) return '원래 요청을 보낸 로그인에서만 새 취소를 검토할 수 있습니다.'
  return ''
}
export function archiveRejectedCancellation(ledger: CancellationLedger, order: WorkOrder, session: Session | null, generation: number, at: string): RejectedCancellationArchive {
  if (cancellationReprepareBlocked(ledger, session, generation) || order.id !== ledger.reference.work_order_id || !['DRAFT', 'RELEASED'].includes(order.status) || !timestamp(at)) return fail()
  return freezeCancellation({ ledger, current_order: order, archived_at: at })
}

export async function readCancellationOrder(reference: CancellationReference, signal: AbortSignal): Promise<WorkOrder> {
  const response = await fetch(mesDetailPath('order', reference.work_order_id), { credentials: 'same-origin', cache: 'no-store', signal: AbortSignal.any([signal, AbortSignal.timeout(12_000)]), redirect: 'error' })
  if (!response.ok) throw new MesHTTPError(response.status)
  const order = parseOrder(await response.json())
  if (order.id !== reference.work_order_id) return fail()
  return freezeCancellation(order)
}
