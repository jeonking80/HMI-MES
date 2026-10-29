import { reasonCodeOf } from './conflictReasons'
import { screenUInt64 } from './screenState'

export type RegistrationKind = 'recipe' | 'order'
export interface RecipeDefinition {
  id: string; recipe_id: string; version: string; item_id: string
  parameters: { temperature_c: number; speed_m_per_min: number; air_pressure_bar?: number }
}
/** One-line process settings, e.g. "150 ℃ · 10 m/min · 5.5 bar"; pressure only when the version sets it. */
export const parameterText = (parameters: { temperature_c: number; speed_m_per_min: number; air_pressure_bar?: number | null }, separator = ' · ') =>
  [`${parameters.temperature_c} ℃`, `${parameters.speed_m_per_min} m/min`, ...(parameters.air_pressure_bar == null ? [] : [`${parameters.air_pressure_bar} bar`])].join(separator)
export interface RecipeVersion extends RecipeDefinition { created_by: string; created_at: string }
export interface OrderDefinition {
  id: string; item_id: string; line_id: string; recipe_version_id: string
  target_quantity: number; planned_date: string
}
export interface WorkOrder extends OrderDefinition {
  status: 'DRAFT' | 'RELEASED' | 'CANCELLED'; row_version: string; created_by: string; created_at: string
}
export type Definition = RecipeDefinition | OrderDefinition
export type Registration = RecipeVersion | WorkOrder
export interface Page<T> { items: T[]; next_cursor: string | null }
export const mesPaths = { recipe: '/api/v1/recipe-versions', order: '/api/v1/work-orders' }
export const validId = (value: unknown): value is string => typeof value === 'string' && value.length >= 1 && value.length <= 128 && !/\s/.test(value)
export const validDate = (value: unknown): value is string => {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value) || value.startsWith('0000-')) return false
  const date = new Date(`${value}T00:00:00Z`)
  return Number.isFinite(date.getTime()) && date.toISOString().slice(0, 10) === value
}
export class MesHTTPError extends Error { constructor(public readonly status: number, public readonly reasonCode: string | null = null) { super('MES request was not confirmed') } }
export async function mesRequest(path: string, signal: AbortSignal, csrf?: string, definition?: object): Promise<unknown> {
  const response = await fetch(path, {
    method: definition ? 'POST' : 'GET', credentials: 'same-origin', cache: 'no-store',
    headers: definition ? { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf ?? '' } : {},
    ...(definition ? { body: JSON.stringify(definition) } : {}),
    signal: AbortSignal.any([signal, AbortSignal.timeout(12_000)]),
  })
  if (!response.ok) throw new MesHTTPError(response.status, await reasonCodeOf(response))
  return response.json()
}
function record(value: unknown): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) throw new Error('Unconfirmed MES response')
  return value as Record<string, unknown>
}
function created(value: Record<string, unknown>) {
  if (!validId(value.created_by) || typeof value.created_at !== 'string' || !Number.isFinite(Date.parse(value.created_at))) throw new Error('Unconfirmed MES registration')
}
export function parseRecipe(value: unknown): RecipeVersion {
  const result = record(value)
  const parameters = record(result.parameters)
  if (![result.id, result.recipe_id, result.version, result.item_id].every(validId)
    || typeof parameters.temperature_c !== 'number' || !Number.isFinite(parameters.temperature_c) || parameters.temperature_c < 0
    || typeof parameters.speed_m_per_min !== 'number' || !Number.isFinite(parameters.speed_m_per_min) || parameters.speed_m_per_min <= 0
    || (parameters.air_pressure_bar !== undefined && (typeof parameters.air_pressure_bar !== 'number' || !Number.isFinite(parameters.air_pressure_bar) || parameters.air_pressure_bar <= 0))) throw new Error('Unconfirmed recipe')
  created(result)
  return result as unknown as RecipeVersion
}
export function parseOrder(value: unknown): WorkOrder {
  const result = record(value)
  if (![result.id, result.item_id, result.line_id, result.recipe_version_id].every(validId)
    || typeof result.target_quantity !== 'number' || !Number.isInteger(result.target_quantity) || result.target_quantity < 1 || result.target_quantity > 4294967295
    || !validDate(result.planned_date) || !['DRAFT', 'RELEASED', 'CANCELLED'].includes(result.status as string)
    || !screenUInt64(result.row_version) || BigInt(result.row_version) === 0n) throw new Error('Unconfirmed work order')
  created(result)
  return result as unknown as WorkOrder
}
function parsePage<T>(value: unknown, parse: (item: unknown) => T): Page<T> {
  const result = record(value)
  if (!Array.isArray(result.items) || result.items.length > 100 || (result.next_cursor !== null && !validId(result.next_cursor))) throw new Error('Unconfirmed MES page')
  return { items: result.items.map(parse), next_cursor: result.next_cursor as string | null }
}
export const recipePage = (value: unknown) => parsePage(value, parseRecipe)
export const orderPage = (value: unknown) => parsePage(value, parseOrder)
export const registrationFrom = (kind: RegistrationKind, value: unknown): Registration => kind === 'recipe' ? parseRecipe(value) : parseOrder(value)
export function sameDefinition(kind: RegistrationKind, stored: Registration, expected: Definition): boolean {
  if (kind === 'recipe') {
    const actual = stored as RecipeVersion
    const wanted = expected as RecipeDefinition
    return actual.id === wanted.id && actual.recipe_id === wanted.recipe_id && actual.version === wanted.version && actual.item_id === wanted.item_id
      && actual.parameters.temperature_c === wanted.parameters.temperature_c && actual.parameters.speed_m_per_min === wanted.parameters.speed_m_per_min
      && actual.parameters.air_pressure_bar === wanted.parameters.air_pressure_bar
  }
  const actual = stored as WorkOrder
  const wanted = expected as OrderDefinition
  return actual.id === wanted.id && actual.item_id === wanted.item_id && actual.line_id === wanted.line_id
    && actual.recipe_version_id === wanted.recipe_version_id && actual.target_quantity === wanted.target_quantity && actual.planned_date === wanted.planned_date
}

export function mesDetailPath(kind: RegistrationKind, id: string) {
  return `/api/v1/${kind === 'recipe' ? 'recipe-version-detail' : 'work-order-detail'}?${new URLSearchParams({ [kind === 'recipe' ? 'recipe_version_id' : 'work_order_id']: id })}`
}

/** CX-37 item master. Registrations may reference only enabled, registered items. */
export interface ItemMaster { id: string; name: string; unit: string; enabled: boolean }
export const itemsPath = '/api/v1/items'
export function itemPage(value: unknown): Page<ItemMaster> {
  const raw = record(value)
  if (!Array.isArray(raw.items) || !(raw.next_cursor === null || validId(raw.next_cursor))) throw new Error('Unconfirmed item page')
  return {
    items: raw.items.map(entry => {
      const item = record(entry)
      if (!validId(item.id) || typeof item.name !== 'string' || typeof item.unit !== 'string' || typeof item.enabled !== 'boolean') throw new Error('Unconfirmed item')
      return { id: item.id, name: item.name, unit: item.unit, enabled: item.enabled }
    }),
    next_cursor: raw.next_cursor,
  }
}
