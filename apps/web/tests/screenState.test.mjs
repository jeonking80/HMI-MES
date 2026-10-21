import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import process from 'node:process'
import { after, test } from 'node:test'
import { fileURLToPath, URL } from 'node:url'

// Use the project's existing compiler and Node's built-in test runner; no loader/dependency.
const output = mkdtempSync(join(tmpdir(), 'hmi-screen-state-'))
try {
  execFileSync(process.execPath, [
    fileURLToPath(new URL('../node_modules/typescript/bin/tsc', import.meta.url)),
    fileURLToPath(new URL('../src/screenState.ts', import.meta.url)),
    fileURLToPath(new URL('../src/commands.ts', import.meta.url)),
    fileURLToPath(new URL('../src/mesApi.ts', import.meta.url)),
    fileURLToPath(new URL('../src/workflow.ts', import.meta.url)),
    '--outDir', output, '--module', 'commonjs', '--target', 'ES2022',
    '--lib', 'ES2022,DOM', '--strict', '--skipLibCheck',
  ], { stdio: 'inherit' })
  writeFileSync(join(output, 'package.json'), '{"type":"commonjs"}')
} catch (error) { rmSync(output, { recursive: true, force: true }); throw error }
after(() => rmSync(output, { recursive: true, force: true }))
const { beginScreen, acceptScreen, decodeScreenFrame, screenUInt64, screenWatchdogExpired, MAX_SCREEN_BYTES } = createRequire(import.meta.url)(join(output, 'screenState.js'))
const dictionary = JSON.parse(readFileSync(new URL('../../../contracts/tags/tags.v1.json', import.meta.url), 'utf8'))
const scope = { line_id: 'line-A', source_id: 'source-A', stream_id: 'stream-A' }
const at = '2026-10-21T00:00:00Z'
const heartbeatIds = ['Line.Heartbeat', 'Infeed.LinkHeartbeat', 'Sealer.LinkHeartbeat', 'Outfeed.LinkHeartbeat']
function frame(overrides = {}) {
  return {
    schema_version: '1.0', scope: { ...scope },
    screen: { schema_version: '1.0', kind: 'SNAPSHOT', sync_id: 'sync-A', revision: '0', emitted_at: at,
      view: { line_id: scope.line_id, state: 'RUNNING', execution_id: 'run-A', control_restricted: true,
        samples: [{ sample_id: 'sample-A', tag_id: 'Line.GoodTotal', source_timestamp: at, server_timestamp: at,
          acquired_at: at, status_code: 0, reading: { data_type: 'UInt64', value: '18446744073709551615' } }] } },
    health: { state: 'CAPTURE_FRESH', heartbeat: 'GOOD',
      communication: { state: 'GOOD', connected: true, read_age_seconds: 0,
        heartbeat_ages_seconds: Object.fromEntries(heartbeatIds.map(id => [id, 0])) },
      communication_captured_at: at, communication_stored_at: at,
      source_conflicted: false, reconciliation_required: false },
    evidence: [{ tag_id: 'Line.GoodTotal', batch_id: 'batch-A', capture_generation: '1', capture_seq: '1', captured_at: at, stored_at: at, fresh: true }],
    missing_tag_ids: ['Line.State'], ...overrides,
  }
}
const decode = value => decodeScreenFrame(JSON.stringify(value), scope)
function update(base = '0', revision = '1') {
  const value = frame()
  Object.assign(value.screen, { kind: 'UPDATE', base_revision: base, revision })
  return value
}
const rejection = mutate => {
  const value = frame()
  mutate(value)
  assert.throws(() => decode(value), { message: 'Invalid screen protocol message' })
}

test('atomic projection preserves original evidence/times and exact UInt64 and is deeply frozen', () => {
  const source = frame()
  source.screen.emitted_at = '2026-10-21T01:00:00Z'
  const decoded = decode(source)
  assert.equal(decoded.screen.view.samples[0].reading.value, '18446744073709551615')
  assert.equal(decoded.evidence[0].captured_at, at)
  assert.equal(decoded.evidence[0].stored_at, at)
  assert.equal(decoded.screen.view.samples[0].acquired_at, at)
  assert.throws(() => { decoded.evidence[0].fresh = false }, TypeError)
  assert.throws(() => { decoded.screen.view.samples[0].reading.value = '0' }, TypeError)
})

test('SNAPSHOT-first then adjacent UPDATE atomically replaces view and all metadata', () => {
  const initial = acceptScreen(beginScreen(), decode(frame()))
  const next = update()
  next.screen.view.samples = []
  next.screen.view.state = 'UNKNOWN'
  next.screen.view.execution_id = null
  next.evidence = []
  next.health.state = 'NEVER_OBSERVED'
  next.health.heartbeat = 'UNKNOWN'
  next.health.communication = null
  next.health.communication_captured_at = next.health.communication_stored_at = null
  next.missing_tag_ids = ['Line.GoodTotal', 'Line.State']
  const applied = acceptScreen(initial, decode(next))
  assert.equal(applied.phase, 'synced')
  assert.equal(applied.projection.screen.revision, '1')
  assert.equal(applied.projection.screen.view.samples.length, 0)
  assert.equal(applied.projection.evidence.length, 0)
  assert.equal(applied.projection.health.state, 'NEVER_OBSERVED')
  assert.deepEqual(applied.projection.missing_tag_ids, next.missing_tag_ids)
  assert.equal(initial.projection.screen.view.samples.length, 1)
})

test('UPDATE-first, duplicate, gap, reordered and changed-sync stop application retaining observations', () => {
  const initial = acceptScreen(beginScreen(), decode(frame()))
  const first = acceptScreen(initial, decode(update()))
  for (const [previous, next] of [
    [beginScreen(), update()], [first, update()], [initial, update('1', '2')],
    [first, update('0', '1')], [initial, { ...update(), screen: { ...update().screen, sync_id: 'old-sync' } }],
  ]) {
    const result = acceptScreen(previous, decode(next))
    assert.equal(result.phase, 'resync_required')
    assert.equal(result.projection, previous.projection)
  }
  assert.equal(acceptScreen(first, decode(frame())).phase, 'resync_required')
})

test('reconnect requires a new SNAPSHOT/sync and cannot accept old-connection updates', () => {
  const previous = acceptScreen(beginScreen(), decode(frame()))
  const awaiting = beginScreen(previous.projection)
  assert.equal(acceptScreen(awaiting, decode(update())).phase, 'resync_required')
  assert.equal(acceptScreen(awaiting, decode(frame())).phase, 'resync_required')
  const newer = frame()
  newer.screen.sync_id = 'sync-B'
  const restored = acceptScreen(awaiting, decode(newer))
  assert.equal(restored.phase, 'synced')
  assert.equal(restored.projection.screen.sync_id, 'sync-B')
  assert.equal(acceptScreen(restored, decode(update())).phase, 'resync_required')
})

test('reset envelope has no projection metadata and invalidates current synchronization', () => {
  const reset = { schema_version: '1.0', scope, screen: { schema_version: '1.0', kind: 'RESYNC_REQUIRED', sync_id: 'sync-A', reason: 'BACKPRESSURE', emitted_at: at } }
  const previous = acceptScreen(beginScreen(), decode(frame()))
  const result = acceptScreen(previous, decode(reset))
  assert.equal(result.phase, 'resync_required')
  assert.equal(result.projection, previous.projection)
  assert.throws(() => decode({ ...reset, health: frame().health }))
  assert.equal(acceptScreen(result, decode(update())).phase, 'resync_required')
})

test('revision/base and capture counters retain canonical UInt64 strings including their limit', () => {
  for (const value of ['0', '9007199254740993', '18446744073709551615']) assert.equal(screenUInt64(value), true)
  for (const value of [0, 9007199254740992, '01', '-1', '1.0', '1e3', '1\n', '1\r\n', '18446744073709551616']) assert.equal(screenUInt64(value), false)
  const initial = frame()
  initial.screen.revision = '18446744073709551614'
  initial.evidence[0].capture_seq = '18446744073709551615'
  const state = acceptScreen(beginScreen(), decode(initial))
  assert.equal(acceptScreen(state, decode(update('18446744073709551614', '18446744073709551615'))).phase, 'synced')
  assert.throws(() => decode(update('18446744073709551615', '18446744073709551616')))
})

test('all 48 data types/enums agree with the actual dictionary, not coerced JS readings', () => {
  const value = frame()
  const defaults = { Boolean: false, Int32: 0, UInt32: 0, UInt64: '0', Double: 0, String: '' }
  value.screen.view.samples = dictionary.tags.map(tag => ({ ...value.screen.view.samples[0],
    sample_id: tag.logical_name, tag_id: tag.logical_name,
    reading: { data_type: tag.data_type, value: defaults[tag.data_type] } }))
  value.evidence = dictionary.tags.map(tag => ({ ...value.evidence[0], tag_id: tag.logical_name }))
  value.missing_tag_ids = []
  assert.equal(decode(value).screen.view.samples.length, 48)
  rejection(value => { value.screen.view.samples[0].reading.value = Number('18446744073709551615') })
  rejection(value => { value.screen.view.samples[0].reading = { data_type: 'String', value: '1' } })
  rejection(value => { value.screen.view.samples[0].reading = { data_type: 'UInt64', value: '01' } })
  const invalidEnum = JSON.parse(JSON.stringify(value))
  invalidEnum.screen.view.samples.find(item => item.tag_id === 'Line.State').reading.value = 8
  assert.throws(() => decode(invalidEnum))
})

test('partial metadata, foreign scope, unknown tags and duplicate/intersecting tags are rejected', () => {
  for (const key of ['health', 'evidence', 'missing_tag_ids']) rejection(value => { delete value[key] })
  rejection(value => { value.scope.source_id = 'foreign' })
  rejection(value => { value.scope.stream_id = 'foreign' })
  rejection(value => { value.screen.view.line_id = 'foreign' })
  rejection(value => { value.screen.view.samples[0].tag_id = 'Unknown.Tag' })
  rejection(value => { value.screen.view.samples.push(value.screen.view.samples[0]) })
  rejection(value => { value.evidence.push(value.evidence[0]) })
  rejection(value => { value.evidence = [] })
  rejection(value => { value.evidence[0].tag_id = 'Line.State' })
  rejection(value => { value.evidence[0].fresh = 1 })
  rejection(value => { value.missing_tag_ids = ['Line.GoodTotal'] })
  rejection(value => { value.missing_tag_ids = ['Line.State', 'Line.State'] })
  rejection(value => { value.missing_tag_ids = ['Unknown.Tag'] })
})

test('invalid/null-good/UTC dates and contradictory health cannot become current data', () => {
  rejection(value => { value.screen.view.samples[0].reading.value = null })
  rejection(value => { value.screen.view.samples[0].acquired_at = '2026-02-30T00:00:00Z' })
  rejection(value => { value.evidence[0].captured_at = at + '\n' })
  rejection(value => { value.screen.sync_id = 'sync-A\u0085' })
  rejection(value => { value.screen.sync_id = 'sync-A\ud800' })
  rejection(value => { value.evidence[0].captured_at = '2026-10-21T00:00:00' })
  rejection(value => { value.health.communication.connected = 'true' })
  rejection(value => { value.health.communication.heartbeat_ages_seconds['Line.Heartbeat'] = -1 })
  rejection(value => { value.health.communication.read_age_seconds = 5 })
  rejection(value => { value.health.communication.heartbeat_ages_seconds = { 'Line.Heartbeat': 0 } })
  rejection(value => { value.screen.view.state = 'UNKNOWN'; value.screen.view.control_restricted = false })
  const rawBad = frame()
  rawBad.screen.view.samples[0].status_code = 0x80000000
  rawBad.screen.view.samples[0].reading.value = null
  assert.equal(decode(rawBad).screen.view.samples[0].reading.value, null)
})

test('future/stale evidence stays raw rather than being rewritten to fresh emitted time', () => {
  const old = frame()
  old.screen.emitted_at = '2026-10-21T00:00:10Z'
  old.evidence[0].captured_at = '2026-10-21T00:00:20Z'
  old.evidence[0].stored_at = at
  old.evidence[0].fresh = false
  const decoded = decode(old)
  assert.equal(decoded.evidence[0].captured_at, '2026-10-21T00:00:20Z')
  assert.equal(decoded.evidence[0].stored_at, at)
  assert.equal(decoded.evidence[0].fresh, false)
})

test('watchdog is monotonic, finite and fails closed at the 3-second deadline', () => {
  assert.equal(screenWatchdogExpired(1000, 3999), false)
  assert.equal(screenWatchdogExpired(1000, 4000), true)
  assert.equal(screenWatchdogExpired(1000, 999), true)
  assert.equal(screenWatchdogExpired(NaN, 1000), true)
})

test('oversized/binary/malformed/versioned/unknown-field frames are rejected before application', () => {
  assert.throws(() => decodeScreenFrame('x'.repeat(MAX_SCREEN_BYTES + 1), scope))
  assert.throws(() => decodeScreenFrame('{}', scope))
  assert.throws(() => decodeScreenFrame(new Uint8Array(), scope))
  rejection(value => { value.schema_version = '2.0' })
  rejection(value => { value.health.extra = true })
  rejection(value => { value.screen.extra = true })
})


test('MES publication states and command counters use the same exact UInt64 contract', () => {
  const require = createRequire(import.meta.url)
  const { parseOrder } = require(join(output, 'mesApi.js'))
  const { uint64String } = require(join(output, 'commands.js'))
  const order = { id: 'order', item_id: 'item', line_id: 'line', recipe_version_id: 'recipe',
    target_quantity: 1, planned_date: '2026-10-21', status: 'DRAFT', row_version: '1',
    created_by: 'manager', created_at: at }
  assert.equal(parseOrder(order).status, 'DRAFT')
  assert.equal(parseOrder({ ...order, status: 'RELEASED', row_version: '3' }).status, 'RELEASED')
  assert.equal(parseOrder({ ...order, status: 'CANCELLED', row_version: '4' }).status, 'CANCELLED')
  assert.throws(() => parseOrder({ ...order, status: 'RUNNING' }))
  assert.throws(() => parseOrder({ ...order, row_version: '0' }))
  for (const value of ['1\n', '1\r\n', '01', '18446744073709551616', 1]) {
    assert.equal(uint64String(value), false)
    assert.throws(() => parseOrder({ ...order, row_version: value }))
  }
  assert.equal(uint64String('18446744073709551615'), true)
})

const { parseWorkflow, workflowBody, workflowPath, sameIntent, sameWorkflowValue } = createRequire(import.meta.url)(join(output, 'workflow.js'))
function releaseRecord() {
  return { intent: { work_order_id: 'order-A', actor: 'planner', release_id: 'release-A', expected_row_version: '1' }, receipt: { work_order_id: 'order-A', released_by: 'planner', release_id: 'release-A', released_at: at, status: 'RELEASED', row_version: '2' } }
}
function bindingRecord() {
  return { intent: { work_order_id: 'order-A', actor: 'operator', command_id: 'load-A', ...scope, expected_work_order_version: '2', expected_control_version: '18446744073709551615' }, receipt: { work_order_id: 'order-A', execution_id: 'execution-A', command_id: 'load-A', ...scope, work_order_row_version: '3', load_request: { schema_version: '1.0', command_id: 'load-A', target_id: 'Line', execution_id: 'execution-A', command: { kind: 'LOAD_JOB', args: { work_order_id: 'order-A', recipe_version: 'recipe-v1', target_quantity: 4294967295, parameters: { temperature_c: 150, speed_m_per_min: 6 } } }, requested_by: 'operator', requested_at: at, expires_at: '2026-10-21T00:00:05Z', expected_control_version: '18446744073709551615' } } }
}
test('workflow records preserve immutable original intent and exact released timestamp/version', () => {
  const record = releaseRecord()
  const parsed = parseWorkflow('release', record, 'order-A', 'release-A', record.intent)
  assert.equal(parsed.receipt.released_at, at)
  assert.equal(parsed.receipt.row_version, '2')
  assert.throws(() => { parsed.intent.actor = 'other' }, TypeError)
  assert.deepEqual(workflowBody('release', parsed.intent), { release_id: 'release-A', expected_row_version: '1' })
  assert.equal(workflowPath('release', 'order-A', 'release-A'), '/api/v1/work-order-receipts/release?work_order_id=order-A&release_id=release-A')
})
test('binding receipt retains actual server-generated LOAD request and exact scope/versions', () => {
  const record = bindingRecord()
  const parsed = parseWorkflow('bind', record, 'order-A', 'load-A', record.intent)
  assert.equal(parsed.receipt.load_request.expected_control_version, '18446744073709551615')
  assert.equal(parsed.receipt.execution_id, 'execution-A')
  assert.equal(workflowPath('bind', 'order-A', 'load-A'), '/api/v1/work-order-receipts/binding?work_order_id=order-A&command_id=load-A')
  assert.equal(Object.hasOwn(workflowBody('bind', parsed.intent), 'actor'), false)
})
test('workflow recovery rejects wrong path/actor/original intent, versions and misleading receipts', () => {
  assert.throws(() => parseWorkflow('release', releaseRecord(), 'other-order', 'release-A'))
  assert.throws(() => parseWorkflow('release', releaseRecord(), 'order-A', 'other-id'))
  assert.throws(() => parseWorkflow('release', releaseRecord(), 'order-A', 'release-A', { ...releaseRecord().intent, actor: 'other' }))
  for (const mutate of [r => { r.receipt.row_version = '3' }, r => { r.intent.expected_row_version = '1\n' }, r => { r.receipt.status = 'SUCCEEDED' }, r => { r.receipt.released_by = 'other' }, r => { r.receipt.released_at += '\n' }, r => { r.receipt.unexpected = true }]) {
    const r = releaseRecord(); mutate(r)
    assert.throws(() => parseWorkflow('release', r, 'order-A', 'release-A'))
  }
  assert.equal(sameIntent(bindingRecord().intent, { ...bindingRecord().intent, expected_control_version: '42' }), false)
})
test('binding recovery rejects LOAD substitution, stale copied versions and invalid server evidence', () => {
  for (const mutate of [r => { r.receipt.source_id = 'other' }, r => { r.receipt.execution_id = 'other' }, r => { r.receipt.work_order_row_version = '2' }, r => { r.receipt.load_request.command.kind = 'START' }, r => { r.receipt.load_request.requested_by = 'other' }, r => { r.receipt.load_request.expected_control_version = Number('18446744073709551615') }, r => { r.receipt.load_request.command.args.work_order_id = 'other' }, r => { r.receipt.load_request.command.args.target_quantity = 0 }, r => { r.receipt.load_request.command.args.parameters.speed_m_per_min = 0 }, r => { r.receipt.load_request.expires_at = at }]) {
    const r = bindingRecord(); mutate(r)
    assert.throws(() => parseWorkflow('bind', r, 'order-A', 'load-A'))
  }
})

test('workflow lookup compares complete original requests independent of object key order', () => {
  const r = bindingRecord()
  assert.equal(sameWorkflowValue(r.receipt.load_request, globalThis.structuredClone(r.receipt.load_request)), true)
  assert.equal(sameWorkflowValue({ a: 1, b: { c: 2 } }, { b: { c: 2 }, a: 1 }), true)
  assert.equal(sameWorkflowValue(r.receipt.load_request, { ...r.receipt.load_request, requested_at: '2026-10-21T00:00:01Z' }), false)
  assert.throws(() => parseWorkflow('bind', r, 'order-A', 'load-A', r.intent, { recipe_version_id: 'substituted-recipe', target_quantity: 4294967295 }))
  const release = releaseRecord(); release.receipt.released_at = '2026-02-30T00:00:00Z'
  assert.throws(() => parseWorkflow('release', release, 'order-A', 'release-A'))
})
