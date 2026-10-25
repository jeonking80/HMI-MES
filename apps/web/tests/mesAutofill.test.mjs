import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import process from 'node:process'
import { after, test } from 'node:test'
import { fileURLToPath, URL } from 'node:url'

const output = mkdtempSync(join(tmpdir(), 'hmi-mes-autofill-'))
try {
  const sources = ['../src/mesAutofill.ts', '../src/commandGuide.ts'].map(path => fileURLToPath(new URL(path, import.meta.url)))
  execFileSync(process.execPath, [fileURLToPath(new URL('../node_modules/typescript/bin/tsc', import.meta.url)), ...sources, '--outDir', output, '--module', 'commonjs', '--target', 'ES2022', '--lib', 'ES2022,DOM', '--strict', '--skipLibCheck'], { stdio: 'inherit' })
  writeFileSync(join(output, 'package.json'), '{"type":"commonjs"}')
} catch (error) { rmSync(output, { recursive: true, force: true }); throw error }
after(() => rmSync(output, { recursive: true, force: true }))
const load = createRequire(import.meta.url)
const { recipeGroups, knownItems, suggestRecipe, suggestOrderId, todayKst, parameterProblem } = load(join(output, 'mesAutofill.js'))
const { stepGuide } = load(join(output, 'commandGuide.js'))

const recipe = (id, recipe_id, version, item_id, created_at, temperature_c = 150) => ({ id, recipe_id, version, item_id, created_at, created_by: 'demo', parameters: { temperature_c, speed_m_per_min: 10 } })
const order = (id, item_id, created_at) => ({ id, item_id, line_id: 'line', recipe_version_id: 'r', target_quantity: 1, planned_date: '2026-10-25', status: 'DRAFT', row_version: '1', created_by: 'demo', created_at })
const recipes = [
  recipe('RCP-SNACK-v1', 'RCP-SNACK', 'v1', 'SNACK', '2026-10-20T00:00:00Z', 140),
  recipe('RCP-SNACK-v2', 'RCP-SNACK', 'v2', 'SNACK', '2026-10-21T00:00:00Z', 155),
  recipe('legacy', 'OLD', 'first', 'BAR', '2026-10-19T00:00:00Z'),
]

test('groups pick the highest numbered version as latest', () => {
  const groups = recipeGroups(recipes)
  assert.deepEqual(groups.map(group => [group.recipe_id, group.latest.id, group.versions]), [['OLD', 'legacy', 1], ['RCP-SNACK', 'RCP-SNACK-v2', 2]])
})

test('next version skips IDs already taken and new items start at v1', () => {
  assert.deepEqual(suggestRecipe(recipes, 'RCP-SNACK', 'SNACK'), { recipe_id: 'RCP-SNACK', version: 'v3', id: 'RCP-SNACK-v3' })
  assert.deepEqual(suggestRecipe([...recipes, recipe('RCP-SNACK-v3', 'X', 'v9', 'Y', '2026-10-22T00:00:00Z')], 'RCP-SNACK', 'SNACK').id, 'RCP-SNACK-v4')
  assert.deepEqual(suggestRecipe(recipes, '', 'choco bar 100g'), { recipe_id: 'RCP-CHOCO-BAR-100G', version: 'v1', id: 'RCP-CHOCO-BAR-100G-v1' })
  assert.deepEqual(suggestRecipe(recipes, '', ''), { recipe_id: '', version: '', id: '' })
})

test('known items are unique and most recently used first', () => {
  assert.deepEqual(knownItems(recipes, [order('WO-1', 'BAR', '2026-10-24T00:00:00Z')]), ['BAR', 'SNACK'])
})

test('work order numbers continue per planned date', () => {
  const orders = [order('WO-20261025-001', 'A', 'x'), order('WO-20261025-007', 'A', 'x'), order('WO-20261024-020', 'A', 'x')]
  assert.equal(suggestOrderId(orders, '2026-10-25'), 'WO-20261025-008')
  assert.equal(suggestOrderId(orders, '2026-10-26'), 'WO-20261026-001')
  assert.equal(todayKst(Date.parse('2026-10-25T16:30:00Z')), '2026-10-26')
})

test('parameters outside the PLC ranges are explained before release', () => {
  assert.equal(parameterProblem(150, 10), '')
  assert.match(parameterProblem(49, 10), /50~200/)
  assert.match(parameterProblem(150, 61), /1~60/)
  assert.match(parameterProblem(Number.NaN, 10), /온도/)
})

test('step guide recommends the next request per PLC state', () => {
  assert.equal(stepGuide({ 'Line.State': 1 }).recommended, 'START')
  const running = stepGuide({ 'Line.State': 2, 'Line.GoodTotal': '50', 'Line.TargetQuantity': 100 })
  assert.equal(running.step, 2); assert.equal(running.progress, 0.5); assert.equal(running.recommended, null)
  assert.equal(stepGuide({ 'Line.State': 2, 'Line.GoodTotal': '100', 'Line.TargetQuantity': 100 }).recommended, 'END_RUN')
  assert.equal(stepGuide({ 'Line.State': 5, 'Line.FaultCode': 3 }).recommended, 'RESET_FAULT')
  assert.equal(stepGuide({ 'Line.State': 6 }).recommended, null)
  assert.equal(stepGuide({}).tone, 'warn')
})
