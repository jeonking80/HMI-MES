import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  AlarmHTTPError, alarmCanAct, alarmIdentifier, alarmQuery, alarmReference, alarmRequest, alarmStatusLabels,
  alarmText, originalLineFaultCode, parseActionPage, parseAlarmIntent, parseAlarmPage, parseAlarmReceipt, parseAlarmRow, sameAlarmScope,
} from './alarmApi'
import type { ActionPage, AlarmAction, AlarmAsset, AlarmIntent, AlarmPage, AlarmPanelProps, AlarmRow, AlarmScope } from './alarmApi'
import './alarm.css'

const displayTime = (value: string) => new Date(value).toLocaleString('ko-KR', { timeZone: 'Asia/Seoul', hour12: false })
function displayReading(value: boolean | number | string | null) {
  if (value === null) return '불명'
  if (typeof value === 'number' && Number.isInteger(value) && !Number.isSafeInteger(value)) return '숫자 정밀도 확인 불가'
  return String(value)
}
function useAlarmRead<T>(path: string | null, parse: (value: unknown) => T, onSessionInvalid: () => void) {
  const [revision, setRevision] = useState(0)
  const [result, setResult] = useState<{ key: string; data: T | null; error: string } | null>(null)
  const key = `${path}/${revision}`
  useEffect(() => {
    if (!path) return
    const controller = new AbortController()
    void alarmRequest(path, controller.signal).then(value => {
      if (!controller.signal.aborted) setResult({ key, data: parse(value), error: '' })
    }).catch(cause => {
      if (controller.signal.aborted) return
      if (cause instanceof AlarmHTTPError && (cause.status === 401 || cause.status === 403)) { onSessionInvalid(); return }
      setResult({ key, data: null, error: '알람 기록을 조회하지 못했습니다. 미발생이나 정상 상태를 뜻하지 않습니다.' })
    })
    return () => controller.abort()
  }, [path, key, parse, onSessionInvalid])
  const refresh = useCallback(() => setRevision(value => value + 1), [])
  return { data: result?.key === key ? result.data : null, error: result?.key === key ? result.error : '', refresh }
}

type AssetFilter = AlarmAsset | 'ALL'
const assetNames: Record<AlarmAsset, string> = { Sealer: '실링기', Line: '라인 PLC' }
const kindNames: Record<string, string> = { TEMPERATURE_DEVIATION: '실링 온도 이탈', PRESSURE_LOW: '실링 공압 부족', PLC_FAULT: 'PLC 고장' }
const severityNames: Record<string, string> = { CRITICAL: '위험', HIGH: '높음', WARNING: '경고', LOW: '낮음' }
const severityName = (value: string) => severityNames[value] ?? value
const PAGE = 20

export default function AlarmPanel({ session, source, onSessionInvalid, ledger, setLedger }: AlarmPanelProps) {
  const [asset, setAsset] = useState<AssetFilter>('ALL')
  const [view, setView] = useState<'attention' | 'all'>('attention')
  const [severity, setSeverity] = useState('')
  const [shownCount, setShownCount] = useState(PAGE)
  const [selected, setSelected] = useState<AlarmRow | null>(null)
  const [historyCursor, setHistoryCursor] = useState<string | null>(null)
  const [note, setNote] = useState('')
  const [editing, setEditing] = useState<AlarmAction | null>(null)
  const [validation, setValidation] = useState('')
  const [busy, setBusy] = useState(false)
  const [lookupId, setLookupId] = useState('')
  const [lookupMessage, setLookupMessage] = useState('')
  const active = useRef(false)
  const controllers = useRef(new Set<AbortController>())
  useEffect(() => {
    const current = controllers.current
    return () => { current.forEach(controller => controller.abort()); current.clear() }
  }, [])
  const sealerScope = useMemo<AlarmScope>(() => ({ line_id: source.line_id, source_id: source.source_id, stream_id: source.stream_id, asset_id: 'Sealer' }), [source.line_id, source.source_id, source.stream_id])
  const lineScope = useMemo<AlarmScope>(() => ({ ...sealerScope, asset_id: 'Line' }), [sealerScope])
  const listPath = (target: AlarmScope) => {
    const query = alarmQuery(target)
    query.set('view', view); query.set('limit', String(Math.min(50, shownCount)))
    if (severity) query.set('severity', severity)
    return `/api/v1/alarms?${query}`
  }
  const parseSealer = useCallback((value: unknown) => parseAlarmPage(value, sealerScope), [sealerScope])
  const parseLine = useCallback((value: unknown) => parseAlarmPage(value, lineScope), [lineScope])
  const sealerList = useAlarmRead<AlarmPage>(asset !== 'Line' ? listPath(sealerScope) : null, parseSealer, onSessionInvalid)
  const lineList = useAlarmRead<AlarmPage>(asset !== 'Sealer' ? listPath(lineScope) : null, parseLine, onSessionInvalid)
  const lists = asset === 'ALL' ? [sealerList, lineList] : asset === 'Sealer' ? [sealerList] : [lineList]
  const listError = lists.find(item => item.error)?.error ?? ''
  const loaded = lists.every(item => item.data)
  const pages = lists.map(item => item.data).filter((item): item is AlarmPage => item !== null)
  const rows = pages.flatMap(item => item.items).sort((a, b) => b.instance.raised_evidence.observed_at.localeCompare(a.instance.raised_evidence.observed_at))
  const shown = rows.slice(0, shownCount)
  const more = rows.length > shownCount || pages.some(item => item.has_more)
  const ruleKinds = new Map(pages.flatMap(item => item.coverage.installed_channels.map(channel => [channel.rule_id, channel.kind] as const)))
  const severities = [...new Set(['CRITICAL', 'HIGH', 'WARNING', 'LOW', ...pages.flatMap(item => item.coverage.installed_channels.map(channel => channel.severity))])]
  const refreshSealer = sealerList.refresh, refreshLine = lineList.refresh
  const refreshLists = useCallback(() => { refreshSealer(); refreshLine() }, [refreshSealer, refreshLine])
  // New alarms show up without a button: a light reread every 15 s while the page is visible.
  useEffect(() => {
    const timer = window.setInterval(() => { if (document.visibilityState === 'visible') refreshLists() }, 15_000)
    return () => window.clearInterval(timer)
  }, [refreshLists])
  const scope = useMemo<AlarmScope>(() => selected && selected.instance.scope.line_id === sealerScope.line_id && selected.instance.scope.source_id === sealerScope.source_id && selected.instance.scope.stream_id === sealerScope.stream_id
    ? selected.instance.scope : asset === 'Line' ? lineScope : sealerScope, [selected, asset, sealerScope, lineScope])
  const currentSelected = selected && sameAlarmScope(selected.instance.scope, scope) ? selected : null
  const alarmId = currentSelected?.instance.alarm_id
  const ruleId = currentSelected?.instance.rule.rule_id
  const targetQuery = alarmQuery(scope, ruleId)
  if (alarmId) targetQuery.set('alarm_id', alarmId)
  const detailPath = alarmId ? `/api/v1/alarm-instance?${targetQuery}` : null
  const parseDetail = useCallback((value: unknown) => {
    const row = parseAlarmRow(value, scope)
    if (row.instance.alarm_id !== alarmId || row.instance.rule.rule_id !== ruleId) throw new Error('Unconfirmed exact instance')
    return row
  }, [scope, alarmId, ruleId])
  const detail = useAlarmRead<AlarmRow>(detailPath, parseDetail, onSessionInvalid)
  targetQuery.set('limit', '20')
  if (historyCursor) targetQuery.set('before_action_id', historyCursor)
  const parseHistory = useCallback((value: unknown) => parseActionPage(value, alarmId ?? ''), [alarmId])
  const history = useAlarmRead<ActionPage>(alarmId ? `/api/v1/alarm-actions?${targetQuery}` : null, parseHistory, onSessionInvalid)
  const canAct = alarmCanAct(session)
  const refresh = () => { refreshLists(); detail.refresh(); history.refresh() }
  const ownsLedgerScope = ledger !== null && sameAlarmScope(ledger.reference.scope, scope)

  async function submit(kind: AlarmIntent['kind']) {
    if (active.current || ledger || !canAct || !detail.data || !currentSelected) return
    if (kind !== 'ACKNOWLEDGE' && !alarmText(note)) { setValidation('조치 내용을 공백 외 문자 1~1,000자로 입력해 주세요.'); return }
    if (kind === 'NOTE_EDIT' && !editing) return
    const base = {
      action_id: crypto.randomUUID(), scope: { ...scope }, rule_id: detail.data.instance.rule.rule_id,
      alarm_id: detail.data.instance.alarm_id, actor: session.login,
    }
    const intent: AlarmIntent = kind === 'ACKNOWLEDGE' ? { ...base, kind }
      : kind === 'NOTE_ADD' ? { ...base, kind, note_id: crypto.randomUUID(), text: note }
        : { ...base, kind, note_id: editing!.note_id!, text: note, expected_previous_action_id: editing!.action_id }
    parseAlarmIntent(intent, scope)
    active.current = true; setBusy(true); setValidation('')
    setLedger({ reference: alarmReference(intent), intent, phase: 'UNKNOWN', receipt: null, message: '원래 조치 ID의 저장 결과를 확인하고 있습니다. 새 ID로 다시 보내지 않습니다.' })
    const controller = new AbortController(); controllers.current.add(controller)
    try {
      const value = await alarmRequest('/api/v1/alarm-actions', controller.signal, intent, session.csrf_token)
      if (controller.signal.aborted) return
      const receipt = parseAlarmReceipt(value, intent)
      setLedger(previous => previous?.reference.action_id === intent.action_id ? { ...previous, phase: 'CONFIRMED', receipt, message: '원래 조치의 서버 기록을 확인했습니다.' } : previous)
      setNote(''); setEditing(null); setHistoryCursor(null); refresh()
    } catch (cause) {
      if (controller.signal.aborted) return
      if (cause instanceof AlarmHTTPError && (cause.status === 401 || cause.status === 403)) { onSessionInvalid(); return }
      const rejected = cause instanceof AlarmHTTPError && [400, 404, 409, 413, 415, 422, 429].includes(cause.status)
      setLedger(previous => previous?.reference.action_id === intent.action_id ? {
        ...previous, phase: rejected ? 'REJECTED' : 'UNKNOWN',
        message: rejected ? 'API가 이 조치 요청을 거절했습니다. 원래 ID와 입력은 보관합니다. 충돌 시 이력을 새로 확인해 주세요.'
          : '저장 응답 불명입니다. 원래 ID만 조회하며 자동 재전송하지 않습니다.',
      } : previous)
    } finally {
      controllers.current.delete(controller); active.current = false
      if (!controller.signal.aborted) setBusy(false)
    }
  }

  async function recover() {
    if (active.current || (!ledger && (!alarmId || !ruleId || !alarmIdentifier(lookupId)))) return
    active.current = true; setBusy(true); setLookupMessage('')
    const original = ledger?.intent
    const reference = ledger?.reference ?? alarmReference({ action_id: lookupId, alarm_id: alarmId!, rule_id: ruleId!, scope })
    const id = reference.action_id
    const targetAlarm = reference.alarm_id
    const targetRule = reference.rule_id
    if (!ledger) setLedger({ reference, intent: null, phase: 'UNKNOWN', receipt: null, message: '원래 ID의 입력과 저장 결과를 조회하고 있습니다. 새 조치를 보내지 않습니다.' })
    const originalScope = reference.scope
    const query = alarmQuery(originalScope, targetRule)
    query.set('alarm_id', targetAlarm); query.set('action_id', id)
    const controller = new AbortController(); controllers.current.add(controller)
    try {
      const value = await alarmRequest(`/api/v1/alarm-action-receipts?${query}`, controller.signal)
      if (controller.signal.aborted) return
      if (typeof value !== 'object' || value === null || !('intent' in value)) throw new Error('Unknown original intent')
      const intent = original ?? parseAlarmIntent(value.intent, originalScope)
      if (intent.action_id !== id || intent.alarm_id !== targetAlarm || intent.rule_id !== targetRule) throw new Error('Wrong original action')
      const receipt = parseAlarmReceipt(value, intent)
      setLedger(previous => !previous || previous.reference.action_id === id ? { reference: alarmReference(intent), intent, phase: 'CONFIRMED', receipt, message: '원래 조치 ID와 불변 입력을 서버 기록으로 확인했습니다.' } : previous)
      refresh()
    } catch (cause) {
      if (controller.signal.aborted) return
      if (cause instanceof AlarmHTTPError && (cause.status === 401 || cause.status === 403)) { onSessionInvalid(); return }
      const message = cause instanceof AlarmHTTPError && cause.status === 404
        ? '현재 원래 ID의 기록을 찾지 못했습니다. 저장되지 않았다고 단정할 수 없습니다. ID와 입력을 유지하고 다시 조회해 주세요.'
        : '원래 조치 결과를 확인하지 못했습니다. ID와 입력을 유지합니다.'
      setLookupMessage(message)
      setLedger(previous => previous?.reference.action_id === id ? { ...previous, message } : previous)
    } finally {
      controllers.current.delete(controller); active.current = false
      if (!controller.signal.aborted) setBusy(false)
    }
  }
  function choose(row: AlarmRow) { setSelected(row); setHistoryCursor(null); setEditing(null); setNote(''); setValidation(''); setLookupId(''); setLookupMessage('') }
  function resetSelection() { setShownCount(PAGE); setSelected(null); setHistoryCursor(null); setEditing(null); setNote(''); setValidation(''); setLookupId(''); setLookupMessage('') }
  return <section className="alarm-center" aria-labelledby="alarm-center-heading">
    <div className="monitor-heading"><div><p className="section-kicker">ALARM CENTER</p><h2 id="alarm-center-heading">알람</h2></div></div>
    <p className="form-help">실링기 온도 · 공압 이탈과 라인 PLC 고장 기록입니다. 15초마다 자동으로 새로 읽습니다. 알람 확인과 조치 메모는 설비 고장을 해제하거나 리셋하지 않습니다.</p>
    <div className="alarm-filters">
      <label>대상<select aria-label="알람 대상" value={asset} disabled={busy} onChange={event => { setAsset(event.target.value as AssetFilter); resetSelection() }}><option value="ALL">전체</option><option value="Sealer">실링기 · 온도/공압</option><option value="Line">라인 PLC · 고장</option></select></label>
      <label>범위<select aria-label="조회 범위" value={view} onChange={event => { setView(event.target.value as 'attention' | 'all'); resetSelection() }}><option value="attention">조치 필요 (발생 중 · 미확인)</option><option value="all">전체 이력</option></select></label>
      <label>심각도<select aria-label="심각도" value={severity} onChange={event => { setSeverity(event.target.value); resetSelection() }}><option value="">전체</option>{severities.map(item => <option key={item} value={item}>{severityName(item)}</option>)}</select></label>
    </div>
    {listError ? <p className="message error" role="alert">{listError}</p> : !loaded ? <p role="status">알람 기록을 조회하고 있습니다.</p> : <>
      {shown.length === 0 ? <p id="alarm-empty">이 조건의 알람 기록이 없습니다.</p> : <ul className="alarm-list">{shown.map(row => <li key={row.instance.alarm_id} data-alarm-id={row.instance.alarm_id} data-alarm-status={row.status}>
        <button type="button" className="alarm-select" onClick={() => choose(row)} aria-pressed={alarmId === row.instance.alarm_id}>
          <span className={`alarm-status alarm-status-${row.status}`}>{alarmStatusLabels[row.status]}</span><strong>{assetNames[row.instance.scope.asset_id]} · {kindNames[ruleKinds.get(row.instance.rule.rule_id) ?? ''] ?? row.instance.rule.rule_id}</strong><span>심각도 {severityName(row.instance.rule.severity)}</span><span>발생 {displayTime(row.instance.raised_evidence.observed_at)}</span>{row.instance.scope.asset_id === 'Line' && <span>고장 코드 {originalLineFaultCode(row.instance) ?? '확인 불가'}</span>}
        </button>
      </li>)}</ul>}
      {more && <div className="alarm-pages"><button type="button" className="button secondary" disabled={shownCount >= 50} onClick={() => setShownCount(count => Math.min(50, count + PAGE))}>더 보기</button></div>}
    </>}
    {currentSelected && <section className="alarm-detail" aria-labelledby="alarm-detail-heading"><h3 id="alarm-detail-heading">선택 사건의 정확한 이력</h3>
      <p className="alarm-id">{currentSelected.instance.alarm_id} · {currentSelected.instance.rule.rule_id}</p>
      {detail.error ? <p className="message error" role="alert">{detail.error}</p> : !detail.data ? <p role="status">선택 사건을 다시 확인하고 있습니다.</p> : <>
        {scope.asset_id === 'Line' && <p className="alarm-fault-code">원래 발생 고장 코드: {originalLineFaultCode(detail.data.instance) ?? '확인 불가'} · 현재 PLC 값이 아닙니다.</p>}<dl className="alarm-facts"><div><dt>상태</dt><dd>{alarmStatusLabels[detail.data.status]}</dd></div><div><dt>발생</dt><dd>{displayTime(detail.data.instance.raised_evidence.observed_at)}</dd></div><div><dt>조건 종료</dt><dd>{detail.data.instance.condition_closed_at ? `${displayTime(detail.data.instance.condition_closed_at)} · ${detail.data.instance.closure_reason === 'NORMAL_RETURN' ? '정상 복귀' : '규칙 변경'}` : '복귀 기록 없음 · 활성 알람 유지'}</dd></div><div><dt>사용자 확인</dt><dd>{detail.data.instance.acknowledged_at ? `${detail.data.instance.acknowledged_by} · ${displayTime(detail.data.instance.acknowledged_at)}` : '확인 기록 없음'}</dd></div></dl>
        <details><summary>발생 판정 근거</summary><p>품질 {detail.data.instance.raised_evidence.quality} · 시각 품질 {detail.data.instance.raised_evidence.time_quality}</p><ul>{detail.data.instance.raised_evidence.readings.map((reading, index) => <li key={`${reading.name}/${index}`}>{reading.name}: {displayReading(reading.value)}</li>)}</ul><p className="alarm-id">근거 ID: {detail.data.instance.raised_evidence.references.join(', ')}</p></details>
        {!canAct ? <p className="message caution">현재 역할에는 알람 확인·조치 기록 권한이 없습니다. 기록 조회는 가능합니다.</p> : <button type="button" id="alarm-acknowledge" className="button primary" disabled={busy || !!ledger || detail.data.instance.acknowledged_at !== null} onClick={() => void submit('ACKNOWLEDGE')}>이 발생 사건 확인</button>}
      </>}
      <h4>확인 · 조치 변경 이력</h4>
      {history.error ? <p className="message error" role="alert">{history.error}</p> : !history.data ? <p role="status">변경 이력을 조회하고 있습니다.</p> : <>
        {history.data.items.length === 0 ? <p>조회된 조치 기록 없음.</p> : <ol className="alarm-history">{history.data.items.map(item => <li key={item.action_id}><strong>{item.kind === 'ACKNOWLEDGE' ? '사용자 확인' : item.kind === 'RULE_CHANGE_CLOSE' ? '규칙 변경으로 종료' : item.kind === 'NOTE_ADD' ? '조치 메모 추가' : '조치 메모 수정'}</strong><span>{item.actor} · {displayTime(item.at)}</span>{item.before !== null && <p>수정 전: {item.before}</p>}{item.after !== null && <p>조치 내용: {item.after}</p>}<small className="alarm-id">조치 ID {item.action_id}{item.note_id ? ` · 메모 ID ${item.note_id}` : ''}</small>{canAct && item.note_id && <button type="button" className="button secondary" disabled={busy || !!ledger} onClick={() => { setEditing(item); setNote(item.after ?? ''); setValidation('') }}>이 기록을 기준으로 메모 수정</button>}</li>)}</ol>}
        <div className="alarm-pages"><button type="button" className="button secondary" disabled={!historyCursor} onClick={() => setHistoryCursor(null)}>최근 변경</button><button type="button" className="button secondary" disabled={!history.data.has_more} onClick={() => setHistoryCursor(history.data!.next_before_action_id)}>이전 변경 더 조회</button></div>
      </>}
      {canAct && <form className="alarm-note" onSubmit={event => { event.preventDefault(); void submit(editing ? 'NOTE_EDIT' : 'NOTE_ADD') }}><label htmlFor="alarm-note">{editing ? `메모 수정 · 기준 조치 ${editing.action_id}` : '새 조치 메모'}</label><textarea id="alarm-note" value={note} onChange={event => setNote(event.target.value)} maxLength={1000} disabled={busy || !!ledger} /><p className="form-help">수정은 원래 메모와 이전 조치 ID를 고정해 새 변경 이력으로 남깁니다. 다른 사용자의 동시 수정은 서버가 다시 확인합니다.</p>{validation && <p className="message error" role="alert">{validation}</p>}<div><button type="submit" className="button primary" disabled={busy || !!ledger || !detail.data}>{editing ? '메모 수정 기록 요청' : '조치 메모 추가 요청'}</button>{editing && <button type="button" className="button secondary" disabled={busy || !!ledger} onClick={() => { setEditing(null); setNote('') }}>수정 취소</button>}</div></form>}
      {!ledger && <form className="alarm-recovery" onSubmit={event => { event.preventDefault(); void recover() }}><label htmlFor="alarm-original-action">이 사건의 원래 조치 ID 조회</label><input id="alarm-original-action" value={lookupId} onChange={event => setLookupId(event.target.value)} maxLength={128} /><button type="submit" className="button secondary" disabled={busy || !alarmIdentifier(lookupId)}>원래 ID 기록만 조회</button></form>}
    </section>}
    {ledger && <section className="alarm-ledger" aria-labelledby="alarm-ledger-heading"><h3 id="alarm-ledger-heading">원래 조치 요청 보관</h3><p className="form-help">원래 조치 ID를 기록해 주세요. 새로고침 후에는 해당 사건에서 같은 ID의 기록을 조회할 수 있습니다. 결과 불명일 때 새 요청을 보내지 않습니다.</p><p id="alarm-action-phase" data-phase={ledger.phase}>{ledger.phase === 'CONFIRMED' ? '저장 기록 확인됨' : ledger.phase === 'REJECTED' ? 'API 요청 거절' : '저장 결과 확인 필요'}</p><dl className="alarm-facts"><div><dt>원래 조치 ID</dt><dd id="alarm-original-id">{ledger.reference.action_id}</dd></div><div><dt>사건 · 규칙</dt><dd>{ledger.reference.alarm_id} · {ledger.reference.rule_id}</dd></div><div><dt>원래 요청자</dt><dd>{ledger.intent?.actor ?? '원래 입력 조회 필요 · 요청자 불명'}</dd></div><div><dt>원래 범위</dt><dd>{ledger.reference.scope.line_id} · {ledger.reference.scope.source_id} / {ledger.reference.scope.stream_id} · {ledger.reference.scope.asset_id}</dd></div><div><dt>요청 종류</dt><dd>{ledger.intent?.kind ?? '원래 입력 조회 필요'}</dd></div>{ledger.intent && 'text' in ledger.intent && <div><dt>고정 조치 내용</dt><dd>{ledger.intent.text}</dd></div>}</dl><p className="message caution">{ledger.message}</p>{ledger.receipt && <p>원래 기록 시각 {displayTime(ledger.receipt.recorded_at)} · 처리 {ledger.receipt.receipt.disposition}{ledger.receipt.action === null ? ' · 새 변경 없는 확인 기록' : ''}</p>}{!ownsLedgerScope && <p className="message caution">현재 목록과 다른 원래 범위의 기록입니다. 아래 조회는 표시한 원래 범위와 ID만 사용하며 새 조치를 보내지 않습니다.</p>}<button type="button" id="alarm-recover" className="button secondary" disabled={busy} onClick={() => void recover()}>원래 ID 기록만 다시 조회</button>{ledger.phase !== 'UNKNOWN' && <button type="button" className="button secondary" disabled={busy} onClick={() => setLedger(null)}>기록 확인 · 다음 조치 준비</button>}</section>}
    {lookupMessage && <p className="message caution" role="status">{lookupMessage}</p>}
  </section>
}
