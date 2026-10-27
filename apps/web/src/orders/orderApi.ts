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
  queue_position: number | null
  execution: OrderExecution | null
}

export interface OrderChange { field: string; before: string | number | null; after: string | number | null }
export interface OrderEvent { action: string; actor: string; occurred_at: string; changes: OrderChange[] | null }

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

export interface EditBody {
  edit_id: string; expected_row_version: string; recipe_version_id: string; target_quantity: number; planned_date: string
}

export function editOrder(id: string, body: EditBody, csrf: string, signal: AbortSignal) {
  return mesRequest(`/api/v1/work-order-actions/edit?work_order_id=${encodeURIComponent(id)}`, signal, csrf, body)
}

export const fieldLabels: Record<string, string> = { recipe_version_id: '공정 설정', target_quantity: '목표', planned_date: '예정일' }

export const phaseLabels: Record<OrderPhase, string> = { DRAFT: '작성 중', WAITING: '대기', RUNNING: '진행 중', DONE: '완료', CANCELLED: '취소' }
export const eventLabels: Record<string, string> = {
  REGISTER_WORK_ORDER: '등록', EDIT_WORK_ORDER: '수정', RELEASE_WORK_ORDER: '배포', BIND_EXECUTION: '라인에 불러옴', CANCEL_WORK_ORDER: '취소',
}
export const endReasonLabels: Record<string, string> = { TARGET_REACHED: '목표 도달', OPERATOR: '작업자 종료' }
