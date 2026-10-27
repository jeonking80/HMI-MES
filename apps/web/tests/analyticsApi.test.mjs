import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import process from 'node:process'
import { after, test } from 'node:test'
import { fileURLToPath, URL } from 'node:url'

const output = mkdtempSync(join(tmpdir(), 'hmi-analytics-api-'))
try {
  execFileSync(process.execPath, [fileURLToPath(new URL('../node_modules/typescript/bin/tsc', import.meta.url)), fileURLToPath(new URL('../src/analytics/api.ts', import.meta.url)), fileURLToPath(new URL('../src/analytics/format.ts', import.meta.url)), fileURLToPath(new URL('../src/analytics/assistant.ts', import.meta.url)), '--outDir', output, '--module', 'commonjs', '--target', 'ES2022', '--lib', 'ES2022,DOM', '--strict', '--skipLibCheck'], { stdio: 'inherit' })
  writeFileSync(join(output, 'package.json'), '{"type":"commonjs"}')
} catch (error) { rmSync(output, { recursive: true, force: true }); throw error }
after(() => rmSync(output, { recursive: true, force: true }))
const require = createRequire(import.meta.url)
const api = require(join(output, 'api.js'))
const format = require(join(output, 'format.js'))
const assistant = require(join(output, 'assistant.js'))
const fixture = name => JSON.parse(readFileSync(new URL(`./fixtures/analytics/${name}.json`, import.meta.url), 'utf8'))

test('contract fixtures parse without changing values', () => {
  const dashboard = api.parseDashboard(fixture('dashboard'))
  assert.equal(dashboard.production.good, fixture('dashboard').production.good)
  assert.equal(dashboard.oee_partial.performance, null)
  assert.equal(api.parseDashboard(fixture('dashboard-no-data')).active_execution, null)
  assert.equal(api.parseTrend(fixture('trend')).series.length, 3)
  assert.equal(api.parseExecutionReports(fixture('executions')).items.length, 4)
  assert.equal(api.parseTimeline(fixture('timeline')).segments.find(item => item.class === 'UNKNOWN').unknown_reason, 'COMMUNICATION_LOST')
  assert.equal(api.parseDailyReport(fixture('daily')).hourly.length, 24)
  assert.equal(api.parseSpc(fixture('spc')).limits.xbar.cl, 150.02)
  assert.equal(api.parseSpc(fixture('spc-insufficient')).limits, null)
})

test('server may add fields without breaking the client', () => {
  const value = fixture('dashboard')
  value.extra_field = { anything: true }
  value.production.extra = 1
  assert.doesNotThrow(() => api.parseDashboard(value))
})

test('missing trend slots stay null and are never coerced to zero', () => {
  const trend = api.parseTrend(fixture('trend'))
  const missing = trend.series[0].points.filter(point => point.quality === 'MISSING')
  assert.ok(missing.length > 0)
  assert.ok(missing.every(point => point.value === null && point.min === null && point.max === null))
  const forged = fixture('trend')
  forged.series[0].points.find(point => point.quality === 'MISSING').value = 0
  assert.throws(() => api.parseTrend(forged))
})

test('quantities must be exact decimal strings', () => {
  for (const bad of [120, '-1', '01', '1.5', '18446744073709551616', null]) {
    const value = fixture('dashboard')
    value.production.good = bad
    assert.throws(() => api.parseDashboard(value), String(bad))
  }
  const value = fixture('dashboard')
  value.production.good = '18446744073709551615'
  assert.equal(api.parseDashboard(value).production.good, '18446744073709551615')
})

test('ratios outside 0..1 and unknown freshness states are rejected', () => {
  const ratio = fixture('dashboard')
  ratio.time.running_ratio = 1.2
  assert.throws(() => api.parseDashboard(ratio))
  const fresh = fixture('dashboard')
  fresh.freshness.status = 'OK'
  assert.throws(() => api.parseDashboard(fresh))
  const performance = fixture('dashboard')
  performance.oee_partial.performance = 0.9
  assert.throws(() => api.parseDashboard(performance))
})

test('timeline segments must be ordered and unknown spans cannot carry a stop reason', () => {
  const overlap = fixture('timeline')
  overlap.segments[1].from = overlap.segments[0].from
  assert.throws(() => api.parseTimeline(overlap))
  const reason = fixture('timeline')
  const unknown = reason.segments.find(item => item.class === 'UNKNOWN')
  unknown.stop_reason = { code: 'EQUIPMENT', note: '', recorded_by: 'operator1', recorded_at: reason.to }
  assert.throws(() => api.parseTimeline(reason))
  const state = fixture('timeline')
  state.segments.find(item => item.class === 'UNKNOWN').state = 'RUNNING'
  assert.throws(() => api.parseTimeline(state))
  // The server keeps microseconds: a sub-millisecond span is ordered, not empty.
  const tiny = fixture('timeline')
  tiny.segments.splice(1, 0, { ...tiny.segments[4], from: '2026-10-22T01:35:00Z', to: '2026-10-22T01:35:00.000400Z' })
  tiny.segments[2].from = '2026-10-22T01:35:00.000400Z'
  assert.equal(api.parseTimeline(tiny).segments[1].to, '2026-10-22T01:35:00.000400Z')
  const reversed = fixture('timeline')
  reversed.segments[0].to = '2026-10-22T01:35:00.000400Z'
  reversed.segments[1].from = '2026-10-22T01:35:00.000300Z'
  assert.throws(() => api.parseTimeline(reversed))
})

test('SPC limits require a baseline and capability requires a specification', () => {
  const limits = fixture('spc-insufficient')
  limits.limits = fixture('spc').limits
  assert.throws(() => api.parseSpc(limits))
  const noLimits = fixture('spc')
  noLimits.limits = null
  assert.throws(() => api.parseSpc(noLimits))
  const capability = fixture('spc')
  capability.spec = null
  assert.throws(() => api.parseSpc(capability))
  const inverted = fixture('spc')
  inverted.limits.xbar = { ucl: 149, cl: 150, lcl: 151 }
  assert.throws(() => api.parseSpc(inverted))
})

test('fractional durations from the server are accepted, negative ones are not', () => {
  const value = fixture('executions')
  value.items[0].durations_seconds = { running: 812.37, stopped: 95.5, unknown: 0.13, total: 908 }
  value.items[0].stopped_by_state = { PAUSED: 60.25 }
  assert.equal(api.parseExecutionReports(value).items[0].durations_seconds.running, 812.37)
  value.items[0].durations_seconds.stopped = -1
  assert.throws(() => api.parseExecutionReports(value))
})

test('identifiers follow the shared contract: any script, no whitespace, up to 128 characters', () => {
  const value = fixture('executions')
  value.items[0].work_order_id = '작업지시-001'
  value.items[0].item_id = '.포장/500g'
  assert.equal(api.parseExecutionReports(value).items[0].work_order_id, '작업지시-001')
  value.items[0].work_order_id = 'has space'
  assert.throws(() => api.parseExecutionReports(value))
  value.items[0].work_order_id = 'x'.repeat(129)
  assert.throws(() => api.parseExecutionReports(value))
})

test('unlinked executions may have no item, and source scope must be complete when present', () => {
  const value = fixture('executions')
  value.items[0].item_id = null
  value.items[0].source_id = 'source-a'
  value.items[0].stream_id = 'stream-a'
  const item = api.parseExecutionReports(value).items[0]
  assert.equal(item.item_id, null)
  assert.deepEqual([item.source_id, item.stream_id], ['source-a', 'stream-a'])
  delete value.items[0].stream_id
  assert.throws(() => api.parseExecutionReports(value))
  const dashboard = fixture('dashboard')
  dashboard.active_execution.item_id = null
  assert.equal(api.parseDashboard(dashboard).active_execution.item_id, null)
})

test('a permanent SPC baseline limitation is kept for display', () => {
  const value = fixture('spc-insufficient')
  value.baseline_limitation = 'ORIGINAL_PREFIX_RAW_UNAVAILABLE'
  assert.equal(api.parseSpc(value).baseline_limitation, 'ORIGINAL_PREFIX_RAW_UNAVAILABLE')
  assert.equal(api.parseSpc(fixture('spc')).baseline_limitation, null)
})

test('query keeps repeated tag parameters', () => {
  assert.equal(api.query('/api/v1/trends', { tag: ['A.B', 'C.D'], line_id: 'line-1' }), '/api/v1/trends?tag=A.B&tag=C.D&line_id=line-1')
})

test('formatting shows a dash for missing values instead of zero', () => {
  assert.equal(format.percent(null), '—')
  assert.equal(format.quantity(null), '—')
  assert.equal(format.duration(undefined), '—')
  assert.equal(format.percent(0.9524), '95.2%')
  assert.equal(format.quantity('18446744073709551615'), '18,446,744,073,709,551,615')
  assert.equal(format.duration(3720), '1시간 2분')
  assert.equal(format.seoulToday(Date.parse('2026-10-22T15:00:00Z')), '2026-10-23')
})

test('trend request ends on the boundary the server buckets by', () => {
  const anchor = Date.parse('2026-10-23T03:04:05.678Z')
  // Up to one hour the server uses second buckets.
  assert.equal(new Date(format.alignedEnd(anchor, 3600e3)).toISOString(), '2026-10-23T03:04:05.000Z')
  // Beyond one hour it counts minute buckets from the floored start: 7 days must be exactly 10080.
  const week = 7 * 86400e3
  const end = format.alignedEnd(anchor, week)
  assert.equal(new Date(end).toISOString(), '2026-10-23T03:04:00.000Z')
  const start = end - week
  assert.equal((end - Math.floor(start / 60e3) * 60e3) / 60e3, 10080)
})

test('assistant answers keep their evidence and reject malformed responses', () => {
  const value = {
    answer: '오늘 양품 3개입니다(잠정).', model: 'nvidia/nemotron-3-ultra-550b-a55b', generated_at: '2026-10-23T06:00:00Z',
    evidence: [{ tool: 'get_dashboard', label: '대시보드 2026-10-23', status: 200, generated_at: '2026-10-23T06:00:00Z' }, { tool: 'get_spc', label: 'SPC', status: 404 }],
  }
  const parsed = assistant.parseChatAnswer(value)
  assert.equal(parsed.answer, value.answer)
  assert.deepEqual(parsed.evidence[1], { tool: 'get_spc', label: 'SPC', status: 404, generated_at: null })
  for (const broken of [null, [], { ...value, answer: '  ' }, { ...value, evidence: 'x' }, { ...value, evidence: [{ tool: 'x' }] }, { ...value, model: 1 }]) {
    assert.throws(() => assistant.parseChatAnswer(broken))
  }
})

test('assistant conversations stay within the server bound and start with a question', () => {
  const history = Array.from({ length: 30 }, (_, index) => ({ role: index % 2 ? 'assistant' : 'user', content: `turn ${index}` }))
  const turns = assistant.conversationFor(history, '마지막 질문')
  assert.ok(turns.length <= assistant.MAX_TURNS)
  assert.equal(turns[0].role, 'user')
  assert.deepEqual(turns.at(-1), { role: 'user', content: '마지막 질문' })
  assert.equal(assistant.conversationFor([], 'x'.repeat(5000))[0].content.length, assistant.MAX_CHARS)
})

test('trend zooms to the valued part only when it is a small share of the span', () => {
  const from = Date.parse('2026-10-23T08:00:00Z'), to = from + 3600e3
  const run = Array.from({ length: 60 }, (_, i) => to - 120e3 + i * 1000)
  const zoom = format.focusWindow(run, from, to, 1000)
  assert.ok(zoom && zoom.from >= from && zoom.to <= to && zoom.from <= run[0] && zoom.to >= run.at(-1) + 1000)
  assert.ok(zoom.to - zoom.from < 600e3)
  // Values filling most of the span, or none at all, keep the full span.
  assert.equal(format.focusWindow(Array.from({ length: 40 }, (_, i) => from + i * 60e3), from, to, 60e3), null)
  assert.equal(format.focusWindow([], from, to, 1000), null)
})
