import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import process from 'node:process'
import { after, test } from 'node:test'
import { setImmediate } from 'node:timers'
import { fileURLToPath, URL } from 'node:url'

const { Response, AbortController, ReadableStream, TextEncoder } = globalThis
const output = mkdtempSync(join(tmpdir(), 'hmi-screen-poll-'))
try {
  execFileSync(process.execPath, [fileURLToPath(new URL('../node_modules/typescript/bin/tsc', import.meta.url)),
    fileURLToPath(new URL('../src/screenPolling.ts', import.meta.url)), '--outDir', output,
    '--module', 'commonjs', '--target', 'ES2022', '--lib', 'ES2022,DOM', '--strict', '--skipLibCheck'], { stdio: 'inherit' })
  writeFileSync(join(output, 'package.json'), '{"type":"commonjs"}')
} catch (error) { rmSync(output, { recursive: true, force: true }); throw error }
after(() => rmSync(output, { recursive: true, force: true }))
const { createScreenPoller, readScreenSnapshot, screenTransport, screenPollingURL, ScreenPollingAuthError } = createRequire(import.meta.url)(join(output, 'screenPolling.js'))
const scope = { line_id: 'line', source_id: 'source', stream_id: 'stream' }
const at = '2026-10-21T00:00:00Z'
const heartbeats = ['Line.Heartbeat', 'Infeed.LinkHeartbeat', 'Sealer.LinkHeartbeat', 'Outfeed.LinkHeartbeat']
let serial = 0
function frame() {
  return { schema_version: '1.0', scope, screen: { schema_version: '1.0', kind: 'SNAPSHOT', sync_id: `sync-${serial++}`, revision: '0', emitted_at: at,
    view: { line_id: 'line', state: 'UNKNOWN', execution_id: null, control_restricted: true,
      samples: [{ sample_id: 'original', tag_id: 'Line.GoodTotal', source_timestamp: at, server_timestamp: null, acquired_at: at, status_code: 0,
        reading: { data_type: 'UInt64', value: '18446744073709551615' } }] } },
    health: { state: 'STALE', heartbeat: 'ALARM', communication: { state: 'ALARM', connected: true, read_age_seconds: 6,
      heartbeat_ages_seconds: Object.fromEntries(heartbeats.map(id => [id, 6])) }, communication_captured_at: at, communication_stored_at: at,
      source_conflicted: false, reconciliation_required: false },
    evidence: [{ tag_id: 'Line.GoodTotal', batch_id: 'original-batch', capture_generation: '1', capture_seq: '1', captured_at: at, stored_at: at, fresh: false }], missing_tag_ids: ['Line.State'] }
}
const json = value => new Response(JSON.stringify(value), { headers: { 'content-type': 'application/json' } })
const decode = (response, previous = null, signal = new AbortController().signal) => readScreenSnapshot(response, scope, previous, signal)
const flush = () => new Promise(resolve => setImmediate(resolve))
function harness(fetch, extra = {}) {
  let clock = 0, sequence = 0
  const timers = new Map(), projections = [], phases = []
  let invalid = 0
  const controller = createScreenPoller({ scope, initialProjection: null, fetch,
    now: () => clock, setTimer: (fn, delay) => { const id = ++sequence; timers.set(id, { at: clock + delay, fn }); return id }, clearTimer: id => timers.delete(id),
    onProjection: value => projections.push(value), onPhase: (phase, message) => phases.push({ phase, message }), onSessionInvalid: () => invalid++, ...extra })
  return { controller, projections, phases, timers, invalid: () => invalid,
    async advance(ms) { const target = clock + ms; while (true) { const next = [...timers].filter(([, t]) => t.at <= target).sort((a, b) => a[1].at - b[1].at)[0]; if (!next) break; clock = next[1].at; timers.delete(next[0]); next[1].fn(); await flush() } clock = target; await flush() },
    setClock(value) { clock = value },
  }
}

test('polling requires explicit deployment setting; scope identifiers remain query values', () => {
  for (const value of [undefined, null, '', 'websocket', 'POLLING', true]) assert.equal(screenTransport(value), 'websocket')
  assert.equal(screenTransport('polling'), 'polling')
  const unusual = { line_id: '../라인', source_id: 'a/b', stream_id: '.%2F' }
  const url = new URL(screenPollingURL(unusual), 'https://hmi.test')
  assert.equal(url.pathname, '/api/v1/screen')
  assert.deepEqual(Object.fromEntries(url.searchParams), unusual)
})

test('successful HTTP snapshots preserve original stale health, sample/evidence time and UInt64', async () => {
  const original = frame()
  const value = await decode(json(original))
  assert.deepEqual(value, original)
  const next = frame()
  next.screen.emitted_at = '2026-10-21T00:00:30Z'
  const refreshed = await decode(json(next), value)
  assert.equal(refreshed.health.heartbeat, 'ALARM')
  assert.equal(refreshed.evidence[0].fresh, false)
  assert.equal(refreshed.screen.view.samples[0].acquired_at, at)
  assert.equal(refreshed.screen.view.samples[0].reading.value, '18446744073709551615')
  assert.ok(Object.isFrozen(refreshed.evidence[0]))
})

test('stream byte bound accepts exact131072 and cancels oversized actual content without content-length', async () => {
  const raw = JSON.stringify(frame()), encoded = new TextEncoder().encode(raw)
  const exact = raw + ' '.repeat(131072 - encoded.length)
  await decode(new Response(exact, { headers: { 'content-type': 'application/json' } }))
  let cancelled = false
  const body = new ReadableStream({ start(c) { c.enqueue(new Uint8Array(131073)); }, cancel() { cancelled = true } })
  await assert.rejects(decode(new Response(body, { headers: { 'content-type': 'application/json' } })))
  assert.equal(cancelled, true)
  await assert.rejects(decode(new Response(raw, { headers: { 'content-type': 'application/json', 'content-length': '131073' } })))
})

test('fatal UTF8 decoding accepts split multibyte scalar and rejects damaged bytes', async () => {
  const value = frame(); value.screen.sync_id = '한글'
  const bytes = new TextEncoder().encode(JSON.stringify(value))
  const chunks = new ReadableStream({ start(c) { for (const byte of bytes) c.enqueue(Uint8Array.of(byte)); c.close() } })
  assert.deepEqual(await decode(new Response(chunks, { headers: { 'content-type': 'application/json' } })), value)
  await assert.rejects(decode(new Response(Uint8Array.of(0xc3), { headers: { 'content-type': 'application/json' } })))
})

test('each poll requires new sync identity, exact scope and full revision-zero SNAPSHOT', async () => {
  const previous = await decode(json(frame()))
  await assert.rejects(decode(json(previous), previous))
  for (const mutate of [v => { v.scope = { ...scope, source_id: 'foreign' } }, v => { v.screen.revision = '1' },
    v => { Object.assign(v.screen, { kind: 'UPDATE', base_revision: '0', revision: '1' }) }, v => { v.health.extra = true }]) {
    const value = frame(); mutate(value); await assert.rejects(decode(json(value), previous))
  }
  await assert.rejects(decode(json({ schema_version: '1.0', scope, screen: { schema_version: '1.0', kind: 'RESYNC_REQUIRED', sync_id: 'reset', reason: 'RECONNECT', emitted_at: at } })))
  await assert.rejects(decode(new Response('{}', { status: 503, headers: { 'content-type': 'application/json' } })))
})

test('one request in flight; next GET is one second after completion with explicit passive options', async () => {
  const calls = []; let resolve
  const h = harness((url, options) => { calls.push({ url, options }); return new Promise(done => { resolve = done }) })
  h.controller.start(); h.controller.start()
  await h.advance(1000)
  assert.equal(calls.length, 1)
  assert.equal(calls[0].options.credentials, 'same-origin'); assert.equal(calls[0].options.cache, 'no-store')
  assert.equal(calls[0].options.method, 'GET'); assert.equal(calls[0].options.redirect, 'error')
  assert.equal(Object.hasOwn(calls[0].options.headers, 'X-CSRF-Token'), false)
  resolve(json(frame())); await flush()
  assert.equal(h.projections.length, 1)
  await h.advance(999); assert.equal(calls.length, 1)
  await h.advance(1); assert.equal(calls.length, 2)
  h.controller.stop(); assert.equal(calls[1].options.signal.aborted, true)
})

test('timeout aborts and bounded exponential retries stop; no successful quality fabricated', async () => {
  const signals = []
  const h = harness((url, options) => { signals.push(options.signal); return new Promise(() => {}) })
  h.controller.start()
  for (const delay of [1000, 2000, 4000, 8000, 15000]) {
    await h.advance(2500); assert.equal(signals.at(-1).aborted, true)
    const count = signals.length
    await h.advance(delay - 1); assert.equal(signals.length, count)
    await h.advance(1); assert.equal(signals.length, count + 1)
  }
  await h.advance(2500)
  assert.equal(h.phases.at(-1).phase, 'stopped'); assert.equal(h.projections.length, 0)
  await h.advance(60000); assert.equal(signals.length, 6)
  h.controller.stop()
})

test('retired scope/disposed request can never publish late response after abort', async () => {
  let resolve, signal
  const h = harness((url, options) => { signal = options.signal; return new Promise(done => { resolve = done }) })
  h.controller.start(); h.controller.stop()
  resolve(json(frame())); await flush()
  assert.equal(signal.aborted, true); assert.equal(h.projections.length, 0); assert.equal(h.timers.size, 0)
})

test('offline aborts request; late completion ignored; watchdog still bounds clock regression', async () => {
  let resolve, calls = 0
  const h = harness(() => { calls++; return new Promise(done => { resolve = done }) })
  h.controller.start(); h.controller.offline(); resolve(json(frame())); await flush()
  assert.equal(h.projections.length, 0)
  await h.advance(1000); assert.equal(calls, 2)
  h.setClock(-1); h.controller.watchdog()
  assert.equal(h.phases.at(-1).phase, 'reconnecting')
  h.controller.stop()
  const offline = harness(() => { throw new Error('Must not fetch offline') }, { online: () => false })
  offline.controller.start(); assert.equal(offline.phases.at(-1).phase, 'reconnecting'); offline.controller.stop()
})

test('401/403 invalidate session exactly once, remain terminal, and never retry', async () => {
  for (const status of [401, 403]) {
    let calls = 0
    const h = harness(async () => { calls++; return new Response('', { status }) })
    h.controller.start(); await flush()
    assert.equal(h.invalid(), 1); assert.equal(h.phases.at(-1).phase, 'auth_required')
    await h.advance(60000); h.controller.offline(); h.controller.watchdog()
    assert.equal(calls, 1); assert.equal(h.invalid(), 1); h.controller.stop()
    await assert.rejects(decode(new Response('', { status })), ScreenPollingAuthError)
  }
})
