import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import process from 'node:process'
import { after, test } from 'node:test'
import { fileURLToPath, URL } from 'node:url'

const output = mkdtempSync(join(tmpdir(), 'hmi-alarm-api-'))
try {
  execFileSync(process.execPath, [
    fileURLToPath(new URL('../node_modules/typescript/bin/tsc', import.meta.url)),
    fileURLToPath(new URL('../src/alarmApi.ts', import.meta.url)),
    '--outDir', output, '--module', 'commonjs', '--target', 'ES2022',
    '--lib', 'ES2022,DOM', '--strict', '--skipLibCheck',
  ], { stdio: 'inherit' })
  writeFileSync(join(output, 'package.json'), '{"type":"commonjs"}')
} catch (error) { rmSync(output, { recursive: true, force: true }); throw error }
after(() => rmSync(output, { recursive: true, force: true }))
const {
  alarmCanAct, alarmIdentifier, alarmQuery, alarmReference, alarmRequest, alarmText, parseActionPage,
  parseAlarmIntent, parseAlarmPage, parseAlarmReceipt, parseAlarmRow, sameAlarmIntent, originalLineFaultCode, sameAlarmScope,
} = createRequire(import.meta.url)(join(output, 'alarmApi.js'))
const scope = { line_id: 'line-A', source_id: 'source-A', stream_id: 'stream-A', asset_id: 'Sealer' }
const at = '2026-10-21T00:00:00Z'
const later = '2026-10-21T00:00:03Z'
const clone = value => globalThis.structuredClone(value)
function evidence() { return { observed_at: at, references: ['capture-A'], readings: [{ name: 'TemperaturePV', value: 175.0 }], quality: 'KNOWN', time_quality: 'KNOWN' } }
function row() {
  return { instance: {
    alarm_id: 'alarm-A', scope: clone(scope), rule: { rule_id: 'temperature-A', version: 'version-A', severity: 'WARNING' },
    raised_evidence: evidence(), condition_closed_at: null, closure_reason: null, closure_event_id: null,
    return_evidence: null, acknowledged_at: null, acknowledged_by: null,
  }, status: 'ACTIVE_UNACKNOWLEDGED' }
}
function page(items = [row()]) {
  return { items, has_more: false, next_before_alarm_id: null, coverage: {
    scope: clone(scope), implemented_kinds: ['TEMPERATURE_DEVIATION', 'PRESSURE_LOW'], installed_channel_count: 1,
    installed_channels: [{ rule_id: 'temperature-A', rule_version: 'version-A', kind: 'TEMPERATURE_DEVIATION', severity: 'WARNING' }],
    has_more_installed_channels: false, consumer_status: 'UNKNOWN', runtime_configuration_status: 'UNKNOWN',
  } }
}
function intent(kind = 'ACKNOWLEDGE') {
  return { action_id: 'action-A', scope: clone(scope), rule_id: 'temperature-A', alarm_id: 'alarm-A', actor: 'operator-A', kind,
    ...(kind !== 'ACKNOWLEDGE' ? { note_id: 'note-A', text: '온도와 안전 상태 확인' } : {}),
    ...(kind === 'NOTE_EDIT' ? { expected_previous_action_id: 'previous-A' } : {}),
  }
}
function receipt(kind = 'ACKNOWLEDGE') {
  const original = intent(kind)
  return { intent: clone(original), recorded_at: later,
    receipt: { input_id: original.action_id, disposition: 'APPLIED', created_alarm_id: null, changed_alarm_id: original.alarm_id, replayed: false, notify_current: true },
    action: { action_id: original.action_id, alarm_id: original.alarm_id, actor: original.actor, at: later, kind,
      note_id: original.note_id ?? null, previous_action_id: original.expected_previous_action_id ?? null,
      before: kind === 'NOTE_EDIT' ? '이전 확인 내용' : null, after: original.text ?? null },
  }
}
const invalid = operation => assert.throws(operation, { message: 'Invalid alarm response' })

test('four alarm states preserve independent condition/ack times and CONFIG_CHANGED closure', () => {
  for (const closed of [false, true]) for (const ack of [false, true]) {
    const value = row()
    if (closed) Object.assign(value.instance, { condition_closed_at: later, closure_reason: 'NORMAL_RETURN', closure_event_id: 'close-A', return_evidence: { ...evidence(), observed_at: later } })
    if (ack) Object.assign(value.instance, { acknowledged_at: later, acknowledged_by: 'operator-A' })
    value.status = `${closed ? 'RETURNED' : 'ACTIVE'}_${ack ? 'ACKNOWLEDGED' : 'UNACKNOWLEDGED'}`
    assert.equal(parseAlarmRow(value, scope).status, value.status)
  }
  const value = row()
  Object.assign(value.instance, { condition_closed_at: later, closure_reason: 'CONFIG_CHANGED', closure_event_id: 'config-A' })
  value.status = 'RETURNED_UNACKNOWLEDGED'
  assert.equal(parseAlarmRow(value, scope).instance.closure_reason, 'CONFIG_CHANGED')
})

test('scope, status, closure, known occurrence evidence, UTC timestamps cannot be fabricated', () => {
  for (const change of [
    value => { value.instance.scope.stream_id = 'foreign' }, value => { value.instance.scope.asset_id = 'Infeed' },
    value => { value.status = 'ACTIVE_ACKNOWLEDGED' }, value => { value.instance.acknowledged_at = later },
    value => { value.instance.condition_closed_at = later }, value => { value.instance.raised_evidence.quality = 'UNKNOWN' },
    value => { value.instance.raised_evidence.time_quality = 'UNKNOWN' }, value => { value.instance.raised_evidence.observed_at = at + '\n' },
    value => { value.instance.raised_evidence.observed_at = '2026-02-30T00:00:00Z' },
    value => { value.instance.raised_evidence.observed_at = '2026-10-21T00:00:00' },
    value => { value.instance.extra = 'unknown' },
  ]) { const value = row(); change(value); invalid(() => parseAlarmRow(value, scope)) }
})

test('list coverage stays unknown even when there are no rows and no installed channels', () => {
  const value = page([])
  value.coverage.installed_channel_count = 0; value.coverage.installed_channels = []
  const parsed = parseAlarmPage(value, scope)
  assert.equal(parsed.items.length, 0)
  assert.equal(parsed.coverage.consumer_status, 'UNKNOWN')
  assert.equal(parsed.coverage.runtime_configuration_status, 'UNKNOWN')
  const unknown = page(); delete unknown.coverage
  invalid(() => parseAlarmPage(unknown, scope))
})

test('list cursor and coverage consistency are scoped and bounded', () => {
  for (const change of [
    value => { value.items.push(row()) }, value => { value.has_more = true },
    value => { value.coverage.scope.source_id = 'foreign' }, value => { value.coverage.consumer_status = 'GOOD' },
    value => { value.coverage.installed_channel_count = 2 }, value => { value.coverage.installed_channels[0].kind = 'PLC_FAULT' },
    value => { value.items = Array.from({ length: 51 }, row) },
  ]) { const value = page(); change(value); invalid(() => parseAlarmPage(value, scope)) }
  const value = page(); value.has_more = true; value.next_before_alarm_id = 'alarm-A'
  assert.equal(parseAlarmPage(value, scope).next_before_alarm_id, 'alarm-A')
})

test('ACK/note add/note edit intents retain actor/scope/ID and deep freeze the request', () => {
  for (const kind of ['ACKNOWLEDGE', 'NOTE_ADD', 'NOTE_EDIT']) {
    const original = intent(kind)
    const parsed = parseAlarmIntent(original, scope)
    assert.equal(sameAlarmIntent(parsed, clone(original)), true)
    assert.throws(() => { parsed.actor = 'other' }, TypeError)
    assert.throws(() => { parsed.scope.stream_id = 'other' }, TypeError)
    if ('text' in parsed) assert.throws(() => { parsed.text = 'other' }, TypeError)
  }
  const withAt = intent(); withAt.at = at
  invalid(() => parseAlarmIntent(withAt, scope))
  const empty = intent('NOTE_ADD'); empty.text = '\u0085\n'
  invalid(() => parseAlarmIntent(empty, scope))
})

test('receipt must match every original actor, scope, rule, note parent and content field', () => {
  for (const kind of ['ACKNOWLEDGE', 'NOTE_ADD', 'NOTE_EDIT']) {
    assert.equal(parseAlarmReceipt(receipt(kind), intent(kind)).recorded_at, later)
  }
  for (const change of [
    value => { value.intent.actor = 'other' }, value => { value.intent.scope.line_id = 'other' },
    value => { value.intent.rule_id = 'other' }, value => { value.intent.alarm_id = 'other' },
    value => { value.intent.action_id = 'other' }, value => { value.intent.text = 'changed' },
    value => { value.intent.note_id = 'other' }, value => { value.intent.expected_previous_action_id = 'other' },
    value => { value.action.previous_action_id = 'other' }, value => { value.action.after = 'changed' },
    value => { value.action.actor = 'other' }, value => { value.action.at = at },
    value => { value.receipt.input_id = 'other' }, value => { value.receipt.changed_alarm_id = 'other' },
  ]) { const value = receipt('NOTE_EDIT'); change(value); invalid(() => parseAlarmReceipt(value, intent('NOTE_EDIT'))) }
})

test('no-change repeated ACK can have no new action and retains original recorded time', () => {
  const value = receipt(); value.action = null
  value.receipt.disposition = 'NO_CHANGE'; value.receipt.changed_alarm_id = null
  const parsed = parseAlarmReceipt(value, intent())
  assert.equal(parsed.action, null); assert.equal(parsed.recorded_at, later)
  const missingNote = receipt('NOTE_ADD'); missingNote.action = null
  invalid(() => parseAlarmReceipt(missingNote, intent('NOTE_ADD')))
})

test('exact-instance action history preserves note revisions, identities and before/after', () => {
  const edit = receipt('NOTE_EDIT').action
  const parsed = parseActionPage({ items: [edit], has_more: true, next_before_action_id: edit.action_id }, 'alarm-A')
  assert.equal(parsed.items[0].before, '이전 확인 내용')
  assert.equal(parsed.items[0].previous_action_id, 'previous-A')
  invalid(() => parseActionPage({ items: [edit], has_more: false, next_before_action_id: null }, 'other'))
  const bad = clone(edit); bad.before = null
  invalid(() => parseActionPage({ items: [bad], has_more: false, next_before_action_id: null }, 'alarm-A'))
})

test('only operator role acts; equipment admin/production manager have no inherited grant', () => {
  for (const roles of [['viewer'], ['equipment_admin'], ['production_manager'], []]) assert.equal(alarmCanAct({ roles, must_change_password: false }), false)
  assert.equal(alarmCanAct({ roles: ['operator'], must_change_password: false }), true)
  assert.equal(alarmCanAct({ roles: ['operator'], must_change_password: true }), false)
})

test('identifier/text validation matches backend whitespace and Unicode lengths', () => {
  for (const value of ['id\n', 'id\u0085', 'id\u001c', 'id\ud800', '', 'a'.repeat(129)]) assert.equal(alarmIdentifier(value), false)
  assert.equal(alarmIdentifier('😀'.repeat(128)), true)
  assert.equal(alarmText('처리\n완료'), true)
  assert.equal(alarmText('\u001c\u0085'), false)
  assert.equal(alarmText('a'.repeat(1001)), false)
})

test('HTTP submits once and passive 404 recovery makes no POST, ID replacement or actor change', async () => {
  const calls = [], original = parseAlarmIntent(intent('NOTE_ADD'), scope)
  const oldFetch = globalThis.fetch
  globalThis.fetch = async (path, options) => {
    calls.push({ path, options })
    if (options.method === 'POST') throw new TypeError('lost response')
    return new globalThis.Response('{}', { status: 404 })
  }
  try {
    const controller = new globalThis.AbortController()
    await assert.rejects(alarmRequest('/api/v1/alarms/alarm-A/actions', controller.signal, original, 'csrf-secret'))
    const query = alarmQuery(original.scope, original.rule_id)
    for (let i = 0; i < 2; i++) await assert.rejects(alarmRequest(`/api/v1/alarms/${original.alarm_id}/action-receipts/${original.action_id}?${query}`, controller.signal), error => error.status === 404)
    assert.equal(calls.filter(item => item.options.method === 'POST').length, 1)
    assert.equal(calls.filter(item => item.options.method === 'GET').length, 2)
    assert.deepEqual(JSON.parse(calls[0].options.body), original)
    assert.equal(calls[0].options.credentials, 'same-origin')
    assert.equal(calls[0].options.headers['X-CSRF-Token'], 'csrf-secret')
    assert.ok(calls.slice(1).every(item => !item.options.body && !item.options.headers['X-CSRF-Token']))
    assert.equal(original.actor, 'operator-A'); assert.equal(original.action_id, 'action-A')
  } finally { globalThis.fetch = oldFetch }
})


test('manual recovery reference preserves exact scope/ID without inventing actor or intent', () => {
  const original = intent('NOTE_EDIT')
  const reference = alarmReference(original)
  assert.deepEqual(reference, { action_id: original.action_id, scope: original.scope, rule_id: original.rule_id, alarm_id: original.alarm_id })
  assert.equal('actor' in reference, false)
  assert.equal('text' in reference, false)
  assert.throws(() => { reference.action_id = 'new' }, TypeError)
  assert.throws(() => { reference.scope.source_id = 'other' }, TypeError)
  const query = alarmQuery(reference.scope, reference.rule_id)
  assert.equal(query.get('asset_id'), 'Sealer')
  assert.equal(query.get('rule_id'), original.rule_id)
  assert.equal(query.has('actor'), false)
})


const lineScope = { ...scope, asset_id: 'Line' }
function lineRow() {
  const value = row()
  value.instance.scope = clone(lineScope)
  value.instance.rule.rule_id = 'line-fault'
  value.instance.raised_evidence.readings = [
    { name: 'Line.FaultCode', value: 123 },
    { name: 'Line.EventSeq', value: '18446744073709551615' },
    { name: 'source_event', value: '{"kind":"FAULT_CHANGED"}' },
  ]
  return value
}
function linePage() {
  const value = page([lineRow()])
  value.coverage.scope = clone(lineScope)
  value.coverage.implemented_kinds = ['PLC_FAULT']
  value.coverage.installed_channels = [{ rule_id: 'line-fault', rule_version: 'version-A', kind: 'PLC_FAULT', severity: 'WARNING' }]
  return value
}

test('logical Line PLC_FAULT coverage uses exact scope and preserves original evidence DTO', () => {
  const value = parseAlarmPage(linePage(), lineScope)
  assert.deepEqual(value.coverage.implemented_kinds, ['PLC_FAULT'])
  assert.equal(value.coverage.consumer_status, 'UNKNOWN')
  assert.equal(value.coverage.runtime_configuration_status, 'UNKNOWN')
  assert.equal(originalLineFaultCode(value.items[0].instance), 123)
  assert.equal(value.items[0].instance.raised_evidence.readings[1].value, '18446744073709551615')
  const empty = linePage(); empty.items = []; empty.coverage.installed_channel_count = 0; empty.coverage.installed_channels = []
  assert.equal(parseAlarmPage(empty, lineScope).items.length, 0)
  assert.equal(parseAlarmPage(empty, lineScope).coverage.consumer_status, 'UNKNOWN')
})

test('Line cannot accept Sealer coverage or foreign source/current-status claims', () => {
  invalid(() => parseAlarmPage(linePage(), scope))
  invalid(() => parseAlarmPage(page(), lineScope))
  for (const change of [
    value => { value.coverage.implemented_kinds = ['PRESSURE_LOW'] },
    value => { value.coverage.implemented_kinds = ['PLC_FAULT', 'PLC_FAULT'] },
    value => { value.coverage.installed_channels[0].kind = 'TEMPERATURE_DEVIATION' },
    value => { value.items[0].instance.scope.source_id = 'other-source' },
    value => { value.items[0].instance.scope.asset_id = 'Sealer' },
    value => { value.coverage.runtime_configuration_status = 'ACTIVE' },
  ]) { const value = linePage(); change(value); invalid(() => parseAlarmPage(value, lineScope)) }
  const unsupported = { ...lineScope, asset_id: 'Infeed' }
  const value = lineRow(); value.instance.scope = unsupported
  invalid(() => parseAlarmRow(value, unsupported))
})

test('original Line fault code is an unambiguous UInt32 occurrence value, never inferred current state', () => {
  for (const code of [1, 123, 4294967295]) {
    const value = lineRow().instance; value.raised_evidence.readings = [{ name: 'Line.FaultCode', value: code }]
    assert.equal(originalLineFaultCode(value), code)
  }
  for (const code of [null, false, '123', 0, -1, 1.5, 4294967296, Number.NaN]) {
    const value = lineRow().instance; value.raised_evidence.readings = [{ name: 'Line.FaultCode', value: code }]
    assert.equal(originalLineFaultCode(value), null)
  }
  assert.equal(originalLineFaultCode(row().instance), null)
  const duplicate = lineRow().instance
  duplicate.raised_evidence.readings.push({ name: 'Line.FaultCode', value: 123 })
  assert.equal(originalLineFaultCode(duplicate), null)
})

test('Line ACK and note ledger keeps original scope/actor/ID across a different selected asset', () => {
  for (const kind of ['ACKNOWLEDGE', 'NOTE_ADD', 'NOTE_EDIT']) {
    const original = intent(kind); original.scope = clone(lineScope); original.rule_id = 'line-fault'
    const frozen = parseAlarmIntent(original, lineScope)
    const saved = receipt(kind); saved.intent = clone(original)
    assert.equal(parseAlarmReceipt(saved, frozen).intent.scope.asset_id, 'Line')
    const reference = alarmReference(frozen)
    assert.equal(sameAlarmScope(reference.scope, scope), false)
    const query = alarmQuery(reference.scope, reference.rule_id)
    assert.equal(query.get('asset_id'), 'Line')
    assert.equal(query.get('rule_id'), 'line-fault')
    assert.equal(reference.action_id, original.action_id)
    assert.equal(Object.isFrozen(reference.scope), true)
    const changedScope = clone(original); changedScope.scope = clone(scope)
    assert.equal(sameAlarmIntent(frozen, changedScope), false)
    saved.intent = changedScope
    invalid(() => parseAlarmReceipt(saved, frozen))
  }
})
