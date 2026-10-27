import { mesRequest, validId } from '../mesApi'
import type { WorkOrder } from '../mesApi'

/** DRAFT 작성 중 · WAITING 배포됨(미생산) · RUNNING 라인에 불러옴(정산 전) · DONE 정산됨 · CANCELLED */
export type OrderPhase = 'DRAFT' | 'WAITING' | 'RUNNING' | 'DONE' | 'CANCELLED'

export interface OrderExecution {
  execution_id: string; source_id: string; stream_id: string; bound_at: string; bound_by: string
  settlement_status: 'PENDING' | 'SETTLED' | 'MISMATCH' | 'CONFLICT'
  good_quantity: number | null; reject_quantity: number | null; end_reason: string | null; ended_at: string | null
}

export interface OrderSummary extends WorkOrder {
  phase: OrderPhase
  released_at: string | null; released_by: string | null
  cancelled_at: string | null; cancelled_by: string | null
  revised_to: string | null; revised_from: string | null
  queue_position: number | null
  execution: OrderExecution | null
}

export interface OrderEvent { action: string; actor: string; occurred_at: string }

const phases: OrderPhase[] = ['DRAFT', 'WAITING', 'RUNNING', 'DONE', 'CANCELLED']

function parseSummary(value: unknown): OrderSummary {
  const item = value as OrderSummary
  if (typeof value !== 'object' || value === null || !validId(item.id) || !phases.includes(item.phase)
    || typeof item.target_quantity !== 'number' || typeof item.row_version !== 'string') throw new Error('Unconfirmed work-order overview')
  return item
}

export async function readOverview(lineId: string, signal: AbortSignal): Promise<OrderSummary[]> {
  const value = await mesRequest(`/api/v1/work-order-overview?line_id=${encodeURIComponent(lineId)}`, signal) as { items?: unknown }
  if (!Array.isArray(value?.items)) throw new Error('Unconfirmed work-order overview')
  return value.items.map(parseSummary)
}

export async function readHistory(id: string, signal: AbortSignal): Promise<OrderEvent[]> {
  const value = await mesRequest(`/api/v1/work-order-history?work_order_id=${encodeURIComponent(id)}`, signal) as { events?: unknown }
  if (!Array.isArray(value?.events)) throw new Error('Unconfirmed work-order history')
  return value.events as OrderEvent[]
}

export interface RevisionBody {
  new_work_order_id: string; recipe_version_id: string; target_quantity: number; planned_date: string
  expected_row_version: string; release_id: string
}

export function revise(id: string, body: RevisionBody, csrf: string, signal: AbortSignal) {
  return mesRequest(`/api/v1/work-order-actions/revise?work_order_id=${encodeURIComponent(id)}`, signal, csrf, body)
}

/** 다음 수정본 ID: WO-1 → WO-1-R2 → WO-1-R3 (이미 있는 ID는 건너뜀). */
export function nextRevisionId(id: string, taken: ReadonlySet<string>) {
  const base = id.replace(/-R\d+$/, '')
  for (let n = 2; n < 1000; n += 1) {
    const candidate = `${base}-R${n}`
    if (!taken.has(candidate) && candidate.length <= 128) return candidate
  }
  return `${base}-R${Date.now()}`
}

export const phaseLabels: Record<OrderPhase, string> = { DRAFT: '작성 중', WAITING: '대기', RUNNING: '진행 중', DONE: '완료', CANCELLED: '취소' }
export const eventLabels: Record<string, string> = {
  REGISTER_WORK_ORDER: '등록', RELEASE_WORK_ORDER: '배포', BIND_EXECUTION: '라인에 불러옴', CANCEL_WORK_ORDER: '취소',
}
export const endReasonLabels: Record<string, string> = { TARGET_REACHED: '목표 도달', OPERATOR: '작업자 종료' }
