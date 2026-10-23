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
  execFileSync(process.execPath, [fileURLToPath(new URL('../node_modules/typescript/bin/tsc', import.meta.url)), fileURLToPath(new URL('../src/analytics/api.ts', import.meta.url)), fileURLToPath(new URL('../src/analytics/format.ts', import.meta.url)), '--outDir', output, '--module', 'commonjs', '--target', 'ES2022', '--lib', 'ES2022,DOM', '--strict', '--skipLibCheck'], { stdio: 'inherit' })
  writeFileSync(join(output, 'package.json'), '{"type":"commonjs"}')
} catch (error) { rmSync(output, { recursive: true, force: true }); throw error }
after(() => rmSync(output, { recursive: true, force: true }))
const require = createRequire(import.meta.url)
const api = require(join(output, 'api.js'))
const format = require(join(output, 'format.js'))
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
