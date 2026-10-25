/** Suggested registration values so operators pick from what exists instead of typing identifiers. */
import type { RecipeVersion, WorkOrder } from './mesApi'

/** Ranges the virtual PLC accepts for LOAD_JOB; outside them the job is rejected after release. */
export const TEMPERATURE_RANGE = { min: 50, max: 200 } as const
export const SPEED_RANGE = { min: 1, max: 60 } as const
export const DEFAULT_PARAMETERS = { temperature_c: 150, speed_m_per_min: 10 } as const
export const DEFAULT_TARGET = 1000

const slug = (value: string) => value.trim().toUpperCase().replace(/[^A-Z0-9가-힣._-]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 60)

export interface RecipeGroup { recipe_id: string; item_id: string; latest: RecipeVersion; versions: number }

/** Groups keyed by recipe_id; the latest is the highest numeric vN, else the newest registration. */
export function recipeGroups(recipes: readonly RecipeVersion[]): RecipeGroup[] {
  const groups = new Map<string, RecipeGroup>()
  for (const recipe of recipes) {
    const current = groups.get(recipe.recipe_id)
    if (!current) { groups.set(recipe.recipe_id, { recipe_id: recipe.recipe_id, item_id: recipe.item_id, latest: recipe, versions: 1 }); continue }
    current.versions += 1
    const newer = versionNumber(recipe.version) > versionNumber(current.latest.version)
      || (versionNumber(recipe.version) === versionNumber(current.latest.version) && recipe.created_at > current.latest.created_at)
    if (newer) { current.latest = recipe; current.item_id = recipe.item_id }
  }
  return [...groups.values()].sort((a, b) => a.recipe_id.localeCompare(b.recipe_id))
}

export function versionNumber(version: string): number {
  const match = /^v?(\d+)$/i.exec(version.trim())
  return match ? Number(match[1]) : 0
}

/** Known items from both recipes and orders, newest use first. */
export function knownItems(recipes: readonly RecipeVersion[], orders: readonly WorkOrder[]): string[] {
  const seen = new Map<string, string>()
  for (const entry of [...recipes, ...orders]) {
    const previous = seen.get(entry.item_id)
    if (!previous || entry.created_at > previous) seen.set(entry.item_id, entry.created_at)
  }
  return [...seen.entries()].sort((a, b) => b[1].localeCompare(a[1])).map(([item]) => item)
}

/** Next unused version of a group (vN+1), or v1 for a new group. IDs already taken are skipped. */
export function suggestRecipe(recipes: readonly RecipeVersion[], group: string, item: string) {
  const recipeId = group || (item ? `RCP-${slug(item)}` : '')
  const taken = new Set(recipes.map(recipe => recipe.id))
  const used = recipes.filter(recipe => recipe.recipe_id === recipeId).map(recipe => versionNumber(recipe.version))
  let next = Math.max(0, ...used) + 1
  while (taken.has(`${recipeId}-v${next}`)) next += 1
  return recipeId ? { recipe_id: recipeId, version: `v${next}`, id: `${recipeId}-v${next}` } : { recipe_id: '', version: '', id: '' }
}

/** Today's planned date in Korea time as YYYY-MM-DD. */
export function todayKst(now = Date.now()): string {
  return new Date(now + 9 * 3_600_000).toISOString().slice(0, 10)
}

/** WO-YYYYMMDD-NNN with the next number not used for that date. */
export function suggestOrderId(orders: readonly WorkOrder[], date: string): string {
  const prefix = `WO-${date.replaceAll('-', '')}-`
  let next = 1 + Math.max(0, ...orders.filter(order => order.id.startsWith(prefix)).map(order => Number(order.id.slice(prefix.length)) || 0))
  const taken = new Set(orders.map(order => order.id))
  while (taken.has(`${prefix}${String(next).padStart(3, '0')}`)) next += 1
  return `${prefix}${String(next).padStart(3, '0')}`
}

/** Empty string when acceptable; otherwise the reason shown next to the field. */
export function parameterProblem(temperature: number, speed: number): string {
  if (!Number.isFinite(temperature) || temperature < TEMPERATURE_RANGE.min || temperature > TEMPERATURE_RANGE.max) return `설정 온도는 ${TEMPERATURE_RANGE.min}~${TEMPERATURE_RANGE.max} ℃ 안에서 입력해 주세요. 범위를 벗어나면 PLC가 작업 불러오기를 거절합니다.`
  if (!Number.isFinite(speed) || speed < SPEED_RANGE.min || speed > SPEED_RANGE.max) return `설정 속도는 ${SPEED_RANGE.min}~${SPEED_RANGE.max} m/min 안에서 입력해 주세요. 범위를 벗어나면 PLC가 작업 불러오기를 거절합니다.`
  return ''
}
