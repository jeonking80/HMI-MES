import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import process from 'node:process'
import { after, test } from 'node:test'
import { fileURLToPath, URL } from 'node:url'

const output = mkdtempSync(join(tmpdir(), 'hmi-execution-api-'))
try {
  execFileSync(process.execPath, [fileURLToPath(new URL('../node_modules/typescript/bin/tsc', import.meta.url)), fileURLToPath(new URL('../src/executionApi.ts', import.meta.url)), '--outDir', output, '--module', 'commonjs', '--target', 'ES2022', '--lib', 'ES2022,DOM', '--strict', '--skipLibCheck'], { stdio: 'inherit' })
  writeFileSync(join(output, 'package.json'), '{"type":"commonjs"}')
} catch (error) { rmSync(output, { recursive: true, force: true }); throw error }
after(() => rmSync(output, { recursive: true, force: true }))
const { parseExecution, archiveBlocked, executionPath, freezeArchivedWorkflow } = createRequire(import.meta.url)(join(output, 'executionApi.js'))
const clone = value => globalThis.structuredClone(value)
const at = '2026-10-21T00:00:00Z'
const later = '2026-10-21T00:00:05Z'
const scope = { line_id: 'line-A', source_id: 'source-A', stream_id: 'stream-A' }
const parameters = { temperature_c: 150, speed_m_per_min: 6 }
function fixture() {
  const intent = { work_order_id: 'order-A', actor: 'operator', command_id: 'load-A', ...scope, expected_work_order_version: '2', expected_control_version: '42' }
  const request = { schema_version: '1.0', command_id: 'load-A', target_id: 'Line', execution_id: 'exec-A', command: { kind: 'LOAD_JOB', args: { work_order_id: 'order-A', recipe_version: 'recipe-v1', target_quantity: 4, parameters: clone(parameters) } }, requested_by: 'operator', requested_at: at, expires_at: later, expected_control_version: '42' }
  const receipt = { ...scope, work_order_id: 'order-A', execution_id: 'exec-A', command_id: 'load-A', work_order_row_version: '3', load_request: request }
  const record = { intent, receipt }
  const original = { kind: 'bind', intent: clone(intent), knownIntent: true, expectedOrder: { recipe_version_id: 'recipe-v1', target_quantity: 4 }, phase: 'confirmed', record: clone(record), message: 'original' }
  const view = {
    execution: { execution_id: 'exec-A', work_order_id: 'order-A', recipe_version: 'recipe-v1', parameters: clone(parameters), counter_epoch: 'epoch-A', registered_at: at, target_quantity: 4 }, scope: clone(scope), binding: clone(record),
    load: { request: clone(request), collision_latched: false, result: { schema_version: '1.0', command_id: 'load-A', status: 'SUCCEEDED', updated_at: later, delivered_at: at, completed_at: later, reason_code: null, reason: null, applied_control_version: '43', evidence: { state: 'READY', execution_id: 'exec-A', recipe_version: 'recipe-v1', last_event_seq: null } } },
    settlement: { status: 'SETTLED', reason_code: 'COUNTS_MATCH', checked_at: later, received_at: later, summary: { schema_version: '1.0', ...scope, execution_id: 'exec-A', state: 'ENDED', as_of: later, counter_epoch: 'epoch-A', recipe_version: 'recipe-v1', parameters: clone(parameters), input_quantity: '4', good_quantity: '4', reject_quantity: '0', scrap_quantity: '0', wip_quantity: 0, last_event_seq: '4', final: true, ended_at: later } }, totals: { good_quantity: '4', reject_quantity: '0', scrap_quantity: '0' },
    source: { gateway_id: 'gateway', capture_generation: '1', conflicted: false, reconciliation_required: false, verified_through_seq: '4', latest_ingested_seq: '4', control_restricted: false },
  }
  delete view.settlement.summary.line_id
  return { view, original, receipt }
}
test('exact managed execution preserves immutable original evidence and is eligible only for original actor', () => {
  const { view, original, receipt } = fixture()
  const parsed = parseExecution(view, receipt)
  assert.equal(archiveBlocked(parsed, original, 'operator'), '')
  assert.match(archiveBlocked(parsed, original, 'viewer'), /원래 요청 계정/)
  assert.equal(parsed.settlement.checked_at, later)
  assert.throws(() => { parsed.binding.intent.actor = 'other' }, TypeError)
  assert.equal(executionPath(receipt), '/api/v1/execution-record?line_id=line-A&source_id=source-A&stream_id=stream-A&execution_id=exec-A')
})
test('CX-33/34 policy flags on the original intent and LOAD args, and end_reason, are accepted', () => {
  const { view, original, receipt } = fixture()
  for (const target of [view.binding.intent, original.intent]) Object.assign(target, { auto_end_at_target: true, start_when_ready: false })
  for (const request of [view.binding.receipt.load_request, view.load.request, receipt.load_request]) Object.assign(request.command.args, { auto_end_at_target: true, start_when_ready: false })
  view.settlement.summary.end_reason = 'OPERATOR'
  assert.equal(parseExecution(view, receipt).settlement.status, 'SETTLED')
  const bad = fixture(); bad.view.binding.intent.unexpected = true
  assert.throws(() => parseExecution(bad.view, bad.receipt))
})
test('UInt64 maximum totals remain exact, balanced and unrounded', () => {
  const { view, original, receipt } = fixture()
  view.totals.good_quantity = view.settlement.summary.good_quantity = view.settlement.summary.input_quantity = '18446744073709551615'
  assert.equal(archiveBlocked(parseExecution(view, receipt), original, 'operator'), '')
  for (const bad of ['18446744073709551616', '01', '1\n', 4]) {
    const f = fixture(); f.view.totals.good_quantity = bad
    assert.throws(() => parseExecution(f.view, f.receipt))
  }
})
test('wrong scope, malformed identities, dates and original LOAD/config substitution fail closed', () => {
  for (const mutate of [v => { v.scope.stream_id = 'other' }, v => { v.execution.execution_id = 'other' }, v => { v.execution.registered_at = '2026-02-30T00:00:00Z' }, v => { v.execution.registered_at += '\n' }, v => { v.execution.parameters.temperature_c = -1 }, v => { v.execution.target_quantity = 0 }, v => { v.load.request.requested_by = 'other' }, v => { v.binding.receipt.execution_id = 'other' }, v => { v.load.result.command_id = 'other' }, v => { v.load.result.evidence.execution_id = 'other' }, v => { v.load.result.evidence.recipe_version = 'other' }, v => { v.source.control_restricted = true }, v => { v.source.verified_through_seq = '5' }, v => { v.unexpected = true }]) {
    const { view, receipt } = fixture(); mutate(view)
    assert.throws(() => parseExecution(view, receipt))
  }
})
test('original actor/expected versions/complete receipt must match the retained ledger', () => {
  for (const mutate of [o => { o.intent.actor = o.record.intent.actor = 'other' }, o => { o.record.intent.expected_control_version = '41' }, o => { o.intent.expected_control_version = '41' }, o => { o.expectedOrder.recipe_version_id = 'other' }, o => { o.expectedOrder.target_quantity = 5 }, o => { o.record.intent.expected_work_order_version = '3' }, o => { o.record.receipt.command_id = 'other' }, o => { o.record.receipt.load_request.requested_at = later }, o => { o.phase = 'uncertain' }, o => { o.knownIntent = false }]) {
    const { view, receipt, original } = fixture(); mutate(original)
    assert.notEqual(archiveBlocked(parseExecution(view, receipt), original, 'operator'), '')
  }
})
test('PENDING, legitimate MISMATCH/CONFLICT and legacy unmanaged rows stay visible without archive', () => {
  for (const status of ['PENDING', 'MISMATCH', 'CONFLICT']) {
    const { view, receipt, original } = fixture()
    view.settlement.status = status
    if (status === 'PENDING') Object.assign(view.settlement, { summary: null, checked_at: null, received_at: null })
    else view.settlement.summary.counter_epoch = 'different-observed-epoch'
    const parsed = parseExecution(view, receipt)
    assert.notEqual(archiveBlocked(parsed, original, 'operator'), '')
    if (status !== 'PENDING') assert.equal(parsed.settlement.summary.counter_epoch, 'different-observed-epoch')
  }
  const { view, receipt, original } = fixture(); view.binding = view.load = null; view.execution.target_quantity = null
  assert.notEqual(archiveBlocked(parseExecution(view, receipt), original, 'operator'), '')
})
test('no failed/expired/unknown LOAD or collision can archive even with a stored SETTLED summary', () => {
  for (const status of ['REQUESTED', 'DELIVERED', 'ACCEPTED', 'FAILED', 'REJECTED', 'EXPIRED', 'OUTCOME_UNKNOWN']) {
    const { view, receipt, original } = fixture()
    const terminal = ['FAILED', 'REJECTED', 'EXPIRED'].includes(status)
    Object.assign(view.load.result, { status, delivered_at: ['REQUESTED', 'EXPIRED'].includes(status) ? null : at, completed_at: terminal ? later : null, evidence: null, applied_control_version: null, reason_code: 'TEST_OUTCOME', reason: 'stored test outcome' })
    assert.notEqual(archiveBlocked(parseExecution(view, receipt), original, 'operator'), '')
  }
  const { view, receipt, original } = fixture(); view.load.collision_latched = true
  assert.notEqual(archiveBlocked(parseExecution(view, receipt), original, 'operator'), '')
})
test('final summary must have exact epoch/config/scope and valid end boundary and balance', () => {
  for (const mutate of [s => { s.counter_epoch = 'other' }, s => { s.recipe_version = 'other' }, s => { s.parameters.speed_m_per_min = 7 }, s => { s.execution_id = 'other' }, s => { s.source_id = 'other' }, s => { s.stream_id = 'other' }, s => { s.final = false }, s => { s.wip_quantity = 1 }, s => { s.ended_at = null }, s => { s.ended_at = '2026-10-21T00:00:06Z' }, s => { s.input_quantity = '3' }, s => { s.last_event_seq = '0' }]) {
    const { view, receipt } = fixture(); mutate(view.settlement.summary)
    assert.throws(() => parseExecution(view, receipt))
  }
})
test('late totals, source conflict/reconciliation and unverified frontier block local archive', () => {
  for (const mutate of [v => { v.totals.good_quantity = '5' }, v => { v.source.conflicted = v.source.control_restricted = true }, v => { v.source.reconciliation_required = v.source.control_restricted = true }, v => { v.source.latest_ingested_seq = '5'; v.source.control_restricted = true }, v => { v.source.latest_ingested_seq = v.source.verified_through_seq = '3' }]) {
    const { view, receipt, original } = fixture(); mutate(view)
    assert.notEqual(archiveBlocked(parseExecution(view, receipt), original, 'operator'), '')
  }
})

test('archive history deep freezes original ledger and response without discarding original metadata', () => {
  const { view, original, receipt } = fixture()
  const archived = freezeArchivedWorkflow({ attempt: original, execution: parseExecution(view, receipt) })
  assert.equal(archived.attempt.message, 'original')
  assert.throws(() => { archived.attempt.intent.actor = 'other' }, TypeError)
  assert.throws(() => { archived.attempt.expectedOrder.target_quantity = 1 }, TypeError)
  assert.throws(() => { archived.execution.totals.good_quantity = '0' }, TypeError)
})

test('execution record lookup preserves exact slash, dot, percent and UTF8 identifiers in query', () => {
  const { receipt } = fixture()
  for (const id of ['exec-A', '/', '.', '..', 'x/../y', 'a%2Fb', '실행-é-Ａ']) {
    const url = new URL(executionPath({ ...receipt, execution_id: id }), 'https://example.test')
    assert.equal(url.pathname, '/api/v1/execution-record')
    assert.equal(url.searchParams.get('execution_id'), id)
    assert.equal(url.searchParams.get('line_id'), receipt.line_id)
  }
})
