import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import process from 'node:process'
import { after, test } from 'node:test'
import { fileURLToPath, URL } from 'node:url'

const output = mkdtempSync(join(tmpdir(), 'hmi-cancellation-api-'))
try {
  execFileSync(process.execPath, [fileURLToPath(new URL('../node_modules/typescript/bin/tsc', import.meta.url)), fileURLToPath(new URL('../src/cancellationApi.ts', import.meta.url)), fileURLToPath(new URL('../src/workOrderMutationGuard.ts', import.meta.url)), '--outDir', output, '--module', 'commonjs', '--target', 'ES2022', '--lib', 'ES2022,DOM', '--strict', '--skipLibCheck'], { stdio: 'inherit' })
  writeFileSync(join(output, 'package.json'), '{"type":"commonjs"}')
} catch (error) { rmSync(output, { recursive: true, force: true }); throw error }
after(() => rmSync(output, { recursive: true, force: true }))
const require = createRequire(import.meta.url)
const { cancellationBody, cancellationCanWrite, cancellationFailure, cancellationPath, cancellationReference, cancellationRequest, parseCancellation } = require(join(output, 'cancellationApi.js'))
const { WorkOrderMutationGuard } = require(join(output, 'workOrderMutationGuard.js'))
const clone = value => globalThis.structuredClone(value)
const reference = { work_order_id: 'work-A', cancellation_id: 'cancel-A' }
const intent = { ...reference, actor: 'planner', expected_row_version: '1' }
const at = '2026-10-21T00:00:00.123456+00:00'
const record = () => ({ intent: clone(intent), receipt: { ...reference, cancelled_by: 'planner', cancelled_at: at, status: 'CANCELLED', row_version: '2' } })

test('cancel receipt preserves exact original actor/time/version in a deeply immutable record', () => {
  const parsed = parseCancellation(record(), reference, intent)
  assert.equal(parsed.receipt.cancelled_at, at)
  assert.equal(parsed.intent.expected_row_version, '1')
  assert.throws(() => { parsed.intent.actor = 'viewer' }, TypeError)
  assert.throws(() => { parsed.receipt.status = 'DRAFT' }, TypeError)
  assert.deepEqual(cancellationBody(parsed.intent), { cancellation_id: 'cancel-A', expected_row_version: '1' })
  assert.equal(cancellationPath(reference, false), '/api/v1/work-order-actions/cancel?work_order_id=work-A')
  assert.equal(cancellationPath(reference), '/api/v1/work-order-receipts/cancellation?work_order_id=work-A&cancellation_id=cancel-A')
})
test('complete scope/actor/version/ID matching rejects substitution and unexpected fields', () => {
  for (const mutate of [r => { r.intent.work_order_id = 'other' }, r => { r.intent.cancellation_id = 'other' }, r => { r.intent.actor = 'other' }, r => { r.intent.expected_row_version = '2' }, r => { r.receipt.work_order_id = 'other' }, r => { r.receipt.cancellation_id = 'other' }, r => { r.receipt.cancelled_by = 'other' }, r => { r.receipt.status = 'RELEASED' }, r => { r.receipt.row_version = '3' }, r => { r.receipt.extra = true }, r => { r.extra = true }]) {
    const value = record(); mutate(value)
    assert.throws(() => parseCancellation(value, reference, intent))
  }
})
test('version and UTC calendar fields reject coercion, overflow, final newline and malformed dates', () => {
  for (const bad of ['0', '01', '1\n', '1\r\n', '18446744073709551616', 1]) {
    const value = record(); value.intent.expected_row_version = bad
    assert.throws(() => parseCancellation(value, reference))
  }
  for (const bad of ['2026-02-30T00:00:00Z', '2026-10-21T00:00:00Z\n', '2026-10-21T00:00:00+09:00', '0000-01-01T00:00:00Z']) {
    const value = record(); value.receipt.cancelled_at = bad
    assert.throws(() => parseCancellation(value, reference))
  }
  const highest = record()
  highest.intent.expected_row_version = '18446744073709551614'
  highest.receipt.row_version = '18446744073709551615'
  assert.equal(parseCancellation(highest, reference).receipt.row_version, '18446744073709551615')
})
test('only actual production_manager role without initial-password gate may write', () => {
  const session = { login: 'planner', must_change_password: false }
  for (const role of ['viewer', 'operator', 'equipment_admin']) assert.equal(cancellationCanWrite({ ...session, roles: [role] }), false)
  assert.equal(cancellationCanWrite({ ...session, roles: ['production_manager'] }), true)
  assert.equal(cancellationCanWrite({ ...session, roles: ['production_manager'], must_change_password: true }), false)
  assert.equal(cancellationCanWrite(null), false)
})
test('unknown manual lookup retains only exact reference and never invents original actor/version', () => {
  const ref = cancellationReference('work-A', 'cancel-A')
  const failed = cancellationFailure({ reference: ref, intent: null, receipt: null }, 404)
  assert.equal(failed.phase, 'UNKNOWN')
  assert.deepEqual(failed.reference, reference)
  assert.equal(failed.intent, null)
  assert.equal(failed.receipt, null)
  assert.throws(() => { failed.reference.cancellation_id = 'replacement' }, TypeError)
})
test('loss/404/409 preserve immutable original intent and any previously verified receipt', () => {
  const original = { reference: clone(reference), intent: clone(intent), receipt: record().receipt }
  for (const status of [0, 404, 409, 500, 503]) {
    const failed = cancellationFailure(clone(original), status)
    assert.equal(failed.phase, status === 409 ? 'REJECTED' : 'UNKNOWN')
    assert.deepEqual(failed.intent, intent)
    assert.deepEqual(failed.receipt, record().receipt)
    assert.deepEqual(failed.reference, reference)
  }
})
test('same-work-order guard prevents concurrent intents without locking unrelated work orders', () => {
  const guard = new WorkOrderMutationGuard()
  assert.equal(guard.acquire('work-A', 'workflow', 'release-A'), true)
  assert.equal(guard.acquire('work-A', 'cancellation', 'cancel-A'), false)
  assert.equal(guard.acquire('work-B', 'cancellation', 'cancel-B'), true)
  guard.release('work-A', 'cancellation', 'wrong-owner')
  assert.equal(guard.blocked('work-A'), true)
  guard.release('work-A', 'workflow', 'release-A')
  assert.equal(guard.acquire('work-A', 'cancellation', 'cancel-A'), true)
  assert.equal(guard.blocked('unrelated-active-execution'), false)
})
test('independent passive references retain both original owners until each exact record is released', () => {
  const guard = new WorkOrderMutationGuard()
  guard.retain('work-A', 'cancellation', 'cancel-A')
  guard.retain('work-A', 'workflow', 'load-A')
  guard.release('work-A', 'workflow', 'load-A')
  assert.equal(guard.blocked('work-A'), true)
  assert.equal(guard.acquire('work-A', 'workflow', 'new-load'), false)
  guard.release('work-A', 'cancellation', 'cancel-A')
  assert.equal(guard.acquire('work-A', 'workflow', 'new-load'), true)
  guard.retain('work-A', 'cancellation', 'lookup-reference')
  guard.release('work-A', 'cancellation', 'lookup-reference')
  assert.equal(guard.blocked('work-A'), true)
  assert.equal(guard.acquire('work-A', 'cancellation', 'other-cancel'), false)
})
test('POST sends only cancellation ID/version once; passive recovery uses GET and no CSRF/body', async () => {
  const previous = globalThis.fetch
  const calls = []
  globalThis.fetch = async (path, options) => {
    calls.push({ path, options })
    return new globalThis.Response(JSON.stringify(options.method === 'POST' ? record().receipt : record()), { status: 200, headers: { 'Content-Type': 'application/json' } })
  }
  try {
    const signal = new globalThis.AbortController().signal
    await cancellationRequest(reference, signal, intent, 'memory-csrf')
    const recovered = await cancellationRequest(reference, signal)
    assert.equal(recovered.intent.actor, 'planner')
    assert.equal(recovered.receipt.cancelled_by, 'planner')
    assert.equal(calls.length, 2)
    assert.equal(calls[0].options.method, 'POST')
    assert.deepEqual(JSON.parse(calls[0].options.body), { cancellation_id: 'cancel-A', expected_row_version: '1' })
    assert.equal(calls[0].options.headers['X-CSRF-Token'], 'memory-csrf')
    assert.equal(calls[1].options.method, 'GET')
    assert.equal(calls[1].options.body, undefined)
    assert.deepEqual(calls[1].options.headers, {})
    assert.equal(calls[1].options.credentials, 'same-origin')
    assert.equal(calls[1].options.cache, 'no-store')
  } finally { globalThis.fetch = previous }
})
test('POST transport loss and passive GET404 never retry, create another ID or resubmit', async () => {
  const previous = globalThis.fetch
  const calls = []
  globalThis.fetch = async (path, options) => {
    calls.push({ path, options })
    if (options.method === 'POST') throw new TypeError('controlled response loss')
    return new globalThis.Response('{}', { status: 404 })
  }
  try {
    const signal = new globalThis.AbortController().signal
    await assert.rejects(cancellationRequest(reference, signal, intent, 'memory-csrf'))
    await assert.rejects(cancellationRequest(reference, signal), error => error.status === 404)
    assert.equal(calls.length, 2)
    assert.equal(calls.filter(call => call.options.method === 'POST').length, 1)
    assert.equal(calls[0].path, cancellationPath(reference, false))
    assert.equal(calls[1].path, cancellationPath(reference))
  } finally { globalThis.fetch = previous }
})

test('aborted original request never schedules a retry or changes the original actor/ID', async () => {
  const previous = globalThis.fetch
  const calls = []
  globalThis.fetch = async (path, options) => {
    calls.push({ path, options })
    return new Promise((_, reject) => { options.signal.addEventListener('abort', () => reject(new globalThis.DOMException('controlled abort', 'AbortError')), { once: true }) })
  }
  try {
    const controller = new globalThis.AbortController()
    const pending = cancellationRequest(reference, controller.signal, intent, 'memory-csrf')
    controller.abort()
    await assert.rejects(pending, error => error.name === 'AbortError')
    await new Promise(resolve => globalThis.setTimeout(resolve, 5))
    assert.equal(calls.length, 1)
    assert.deepEqual(intent, { ...reference, actor: 'planner', expected_row_version: '1' })
    assert.equal(calls[0].path, cancellationPath(reference, false))
  } finally { globalThis.fetch = previous }
})

const { CancellationRejection, CancellationRecoveryConflict, cancellationReprepareBlocked, archiveRejectedCancellation, readCancellationOrder } = require(join(output, 'cancellationApi.js'))
const owner = { login: 'planner', roles: ['production_manager'], must_change_password: false }
const observation = { method: 'POST', status: 409, outcome: 'HTTP', observed_at: at }
const rejected = () => ({ reference: clone(reference), intent: clone(intent), receipt: null, phase: 'REJECTED', message: '', submission: { actor: 'planner', generation: 3, send_count: 1, rejection: clone(observation), indeterminate: false }, observations: [clone(observation)] })
const currentOrder = () => ({ id: 'work-A', item_id: 'item-A', line_id: 'line-A', recipe_version_id: 'recipe-A', target_quantity: 7, planned_date: '2026-10-25', status: 'RELEASED', row_version: '2', created_by: 'planner', created_at: at })

test('only the original fully observed single POST409 permits same-session same-actor preparation', () => {
  assert.equal(cancellationReprepareBlocked(rejected(), owner, 3), '')
  for (const mutate of [r => { r.submission = null }, r => { r.intent = null }, r => { r.submission.rejection = null }, r => { r.submission.indeterminate = true }, r => { r.submission.send_count = 2 }, r => { r.submission.rejection.method = 'GET' }, r => { r.submission.rejection.status = 404 }, r => { r.submission.rejection.outcome = 'UNCONFIRMED' }, r => { r.submission.rejection.observed_at += '\n' }, r => { r.submission.actor = 'other' }, r => { r.receipt = record().receipt }, r => { r.recovery_conflicted = true }]) {
    const value = rejected(); mutate(value)
    assert.notEqual(cancellationReprepareBlocked(value, owner, 3), '')
  }
  assert.notEqual(cancellationReprepareBlocked(rejected(), { ...owner, login: 'other' }, 3), '')
  assert.notEqual(cancellationReprepareBlocked(rejected(), owner, 4), '')
  assert.notEqual(cancellationReprepareBlocked(rejected(), { ...owner, roles: ['viewer'] }, 3), '')
})
test('passive GET404 changes display phase without erasing the independent original rejection', () => {
  const original = rejected()
  const later = cancellationFailure({ ...original, observations: [...original.observations, { method: 'GET', status: 404, outcome: 'HTTP', observed_at: '2026-10-21T00:01:00Z' }] }, 404)
  assert.equal(later.phase, 'UNKNOWN')
  assert.deepEqual(later.submission.rejection, observation)
  assert.equal(later.observations.length, 2)
  assert.equal(cancellationReprepareBlocked(later, owner, 3), '')
  assert.throws(() => { later.submission.rejection.status = 200 }, TypeError)
  assert.throws(() => { later.observations.push(observation) }, TypeError)
})
test('loss or manual reference followed by GET404 never becomes a confirmed original rejection', () => {
  for (const submission of [null, { actor: 'planner', generation: 3, send_count: 1, rejection: null, indeterminate: true }]) {
    const value = cancellationFailure({ reference, intent, receipt: null, submission, observations: [{ method: 'GET', status: 404, outcome: 'HTTP', observed_at: at }] }, 404)
    assert.notEqual(cancellationReprepareBlocked(value, owner, 3), '')
  }
})
test('explicit rejection archive retains the complete original record and fresh work-order evidence', () => {
  const original = rejected()
  const archived = archiveRejectedCancellation(original, currentOrder(), owner, 3, at)
  assert.deepEqual(archived.ledger, original)
  assert.equal(archived.current_order.row_version, '2')
  assert.throws(() => { archived.ledger.intent.cancellation_id = 'new' }, TypeError)
  assert.throws(() => { archived.current_order.status = 'DRAFT' }, TypeError)
  for (const bad of [{ ...currentOrder(), id: 'other' }, { ...currentOrder(), status: 'CANCELLED' }]) assert.throws(() => archiveRejectedCancellation(rejected(), bad, owner, 3, at))
})
test('rejection archive releases only its exact C1 guard, preserving workflow and other command ownership', () => {
  const guard = new WorkOrderMutationGuard()
  guard.retain('work-A', 'workflow', 'load-A')
  guard.retain('work-A', 'cancellation', 'cancel-A')
  archiveRejectedCancellation(rejected(), currentOrder(), owner, 3, at)
  guard.release('work-A', 'cancellation', 'cancel-A')
  assert.equal(guard.blocked('work-A'), true)
  assert.equal(guard.acquire('work-A', 'cancellation', 'new'), false)
  assert.equal(guard.acquire('work-B', 'cancellation', 'new-B'), true)
})
test('C1 transport accepts only the complete route-coherent POST409 body as typed rejection', async () => {
  const previous = globalThis.fetch
  try {
    for (const body of ['{"detail":"cancellation conflicts with original state or intent"}', '{', '{}', '{"detail":"current evidence does not permit this command"}', '{"detail":"cancellation conflicts with original state or intent","extra":true}']) {
      globalThis.fetch = async () => new globalThis.Response(body, { status: 409 })
      const valid = body === '{"detail":"cancellation conflicts with original state or intent"}'
      await assert.rejects(cancellationRequest(reference, new globalThis.AbortController().signal, intent, 'memory'), error => valid ? error instanceof CancellationRejection : !(error instanceof CancellationRejection))
    }
    globalThis.fetch = async () => new globalThis.Response('{"detail":"cancellation conflicts with original state or intent"}', { status: 409 })
    await assert.rejects(cancellationRequest(reference, new globalThis.AbortController().signal), error => error.status === 409 && !(error instanceof CancellationRejection))
    globalThis.fetch = async () => ({ ok: false, status: 409, json: async () => { throw new TypeError('incomplete body') } })
    await assert.rejects(cancellationRequest(reference, new globalThis.AbortController().signal, intent, 'memory'), error => !(error instanceof CancellationRejection))
  } finally { globalThis.fetch = previous }
})
test('abort while reading a409 body cannot produce typed rejection or retry', async () => {
  const previous = globalThis.fetch
  const controller = new globalThis.AbortController()
  let calls = 0
  globalThis.fetch = async () => { calls++; return { ok: false, status: 409, json: async () => { controller.abort(); return { detail: 'cancellation conflicts with original state or intent' } } } }
  try {
    await assert.rejects(cancellationRequest(reference, controller.signal, intent, 'memory'), error => !(error instanceof CancellationRejection))
    assert.equal(calls, 1)
  } finally { globalThis.fetch = previous }
})
test('incoherent successful recovery cannot be bypassed through old rejection preparation', async () => {
  const previous = globalThis.fetch
  globalThis.fetch = async () => new globalThis.Response(JSON.stringify({ intent: { ...intent, actor: 'other' }, receipt: record().receipt }), { status: 200 })
  try { await assert.rejects(cancellationRequest(reference, new globalThis.AbortController().signal), error => error instanceof CancellationRecoveryConflict) }
  finally { globalThis.fetch = previous }
})
test('explicit fresh detail read performs one GET only and validates the exact work-order identity', async () => {
  const previous = globalThis.fetch
  const calls = []
  globalThis.fetch = async (path, options) => { calls.push({ path, options }); return new globalThis.Response(JSON.stringify(currentOrder()), { status: 200 }) }
  try {
    const order = await readCancellationOrder(reference, new globalThis.AbortController().signal)
    assert.equal(order.row_version, '2')
    assert.equal(calls.length, 1)
    assert.equal(calls[0].path, '/api/v1/work-order-detail?work_order_id=work-A')
    assert.equal(calls[0].options.body, undefined)
    assert.equal(calls[0].options.credentials, 'same-origin')
    assert.equal(calls[0].options.cache, 'no-store')
    assert.throws(() => { order.row_version = '1' }, TypeError)
    await assert.rejects(readCancellationOrder({ ...reference, work_order_id: 'other' }, new globalThis.AbortController().signal))
  } finally { globalThis.fetch = previous }
})

test('all fixed identity query builders preserve slash, dot, percent and UTF8 without path normalization', () => {
  const { workflowPath } = require(join(output, 'workflow.js'))
  const { mesDetailPath } = require(join(output, 'mesApi.js'))
  const { commandLookupPath } = require(join(output, 'commands.js'))
  for (const id of ['order-A', 'order/A', '.', '..', 'order%2FA', '작업-é-Ａ']) {
    const urls = [
      [mesDetailPath('order', id), '/api/v1/work-order-detail', { work_order_id: id }],
      [mesDetailPath('recipe', id), '/api/v1/recipe-version-detail', { recipe_version_id: id }],
      [workflowPath('release', id), '/api/v1/work-order-actions/release', { work_order_id: id }],
      [workflowPath('bind', id), '/api/v1/work-order-actions/bind', { work_order_id: id }],
      [workflowPath('release', id, id), '/api/v1/work-order-receipts/release', { work_order_id: id, release_id: id }],
      [workflowPath('bind', id, id), '/api/v1/work-order-receipts/binding', { work_order_id: id, command_id: id }],
      [cancellationPath({ work_order_id: id, cancellation_id: id }, false), '/api/v1/work-order-actions/cancel', { work_order_id: id }],
      [cancellationPath({ work_order_id: id, cancellation_id: id }), '/api/v1/work-order-receipts/cancellation', { work_order_id: id, cancellation_id: id }],
      [commandLookupPath(id, { line_id: 'line/A', source_id: 'source/Ａ', stream_id: 'stream%2FA' }), '/api/v1/command-record', { command_id: id, line_id: 'line/A', source_id: 'source/Ａ', stream_id: 'stream%2FA' }],
    ]
    for (const [url, path, values] of urls) {
      const actual = new URL(url, 'https://example.test')
      assert.equal(actual.pathname, path)
      assert.deepEqual(Object.fromEntries(actual.searchParams), values)
      assert.equal(actual.hash, '')
    }
  }
})
