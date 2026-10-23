import { useEffect, useState } from 'react'
import { tagEnums, tagMetadata } from './tagMetadata'
import CommandPanel from './CommandPanel'
import CopyId from './CopyId'
import './lineMonitoring.css'
import ProcessOverview from './ProcessOverview'
import WorkOrderBinding from './WorkOrderBinding'
import LoadTracker from './LoadTracker'
import AlarmPanel from './AlarmPanel'
import type { AlarmLedger } from './alarmApi'
import type { WorkOrderWorkflow } from './useWorkOrderWorkflow'
import type { WorkOrder } from './mesApi'
import { useScreenSubscription } from './useScreenSubscription'
import type { ScreenSample as Sample } from './screenState'
import type { Session } from './auth'
import type { TrackedCommand } from './commands'
import type { Dispatch, SetStateAction } from 'react'

const heartbeatTags = new Set(['Line.Heartbeat', 'Infeed.LinkHeartbeat', 'Sealer.LinkHeartbeat', 'Outfeed.LinkHeartbeat'])

interface Source { line_id: string; line_name: string; source_id: string; stream_id: string }
class ReadError extends Error { constructor(public status: number, message: string) { super(message) } }
async function read(path: string, signal: AbortSignal): Promise<unknown> {
  const result = await fetch(path, { credentials: 'same-origin', cache: 'no-store', signal: AbortSignal.any([signal, AbortSignal.timeout(15_000)]) })
  if (!result.ok) throw new ReadError(result.status, result.status === 503 ? '운영 데이터 서비스를 사용할 수 없습니다.' : '운영 데이터를 조회하지 못했습니다.')
  return result.json()
}
const groupNames: Record<string, string> = { Line: '라인 공통', Infeed: '투입 설비', Sealer: '실링 설비', Outfeed: '배출 설비' }
const names: Record<string, string> = {
  State: '운전 상태', ControlMode: '제어 모드', OperationMode: '운전 모드', EStop: '비상 정지', GuardClosed: '안전문 닫힘', Permissive: '운전 허가', FaultCode: '고장 코드', ActiveExecutionId: '실행 ID', RecipeVersion: '레시피 버전', CounterEpoch: '카운터 구간', TargetQuantity: '목표 수량', WipCount: '재공 수량', GoodTotal: '누적 양품', RejectTotal: '누적 불량', ScrapTotal: '누적 폐기', Heartbeat: 'PLC 하트비트', ControlVersion: '제어 버전', EventSeq: '사건 순번', RunFeedback: '운전 피드백', MotorCurrent: '모터 전류', ActualSpeed: '실제 속도', EntrySensor: '투입 센서', BufferCount: '버퍼 수량', LinkHeartbeat: '연결 하트비트', TemperaturePV: '현재 온도', TemperatureSP: '설정 온도', AirPressure: '공기 압력', HeaterOn: '히터 켜짐', Ready: '준비 완료', ExitSensor: '배출 센서', CheckResult: '검사 결과', GoodCounter: '양품 카운터', RejectCounter: '불량 카운터',
}
const stateLabels: Record<string, string> = { STOPPED: '정지', READY: '준비', RUNNING: '운전 중', PAUSED: '일시 정지', STOPPING: '정지 중', FAULT: '고장', ESTOP: '비상 정지', RECOVERY_REQUIRED: '복구 필요', UNKNOWN: '불명' }
const healthLabels: Record<string, string> = { NEVER_OBSERVED: '수집 이력 없음', STALE: '수집 지연', DEGRADED: '품질 저하 · 확인 필요', CAPTURE_FRESH: '최근 수집 확인' }
type Tone = 'good' | 'warn' | 'bad' | 'unknown' | 'neutral'
const stateTones: Record<string, Tone> = { RUNNING: 'good', READY: 'neutral', STOPPED: 'neutral', PAUSED: 'warn', STOPPING: 'warn', FAULT: 'bad', ESTOP: 'bad', RECOVERY_REQUIRED: 'bad' }
const heartbeatTones: Record<string, Tone> = { GOOD: 'good', DELAYED: 'warn', ALARM: 'bad', UNKNOWN: 'unknown' }
function timestamp(value: string) { const date = new Date(value); return Number.isFinite(date.getTime()) ? date.toLocaleString('ko-KR', { hour12: false }) : '시간 불명' }
function displayValue(sample: Sample, enumName: string | null): string {
  if (sample.reading.value === null) return '값 불명'
  if (sample.reading.data_type === 'UInt64' && typeof sample.reading.value !== 'string') return '정밀도 확인 불가'
  if (enumName) {
    const raw = tagEnums[enumName]?.[String(sample.reading.value)]
    return raw ? stateLabels[raw] ?? ({ LOCAL: '현장', REMOTE: '원격', MANUAL: '수동', AUTO: '자동', UNDECIDED: '미판정', GOOD: '양품', REJECT: '불량' } as Record<string, string>)[raw] ?? raw : '알 수 없는 코드'
  }
  return typeof sample.reading.value === 'boolean' ? (sample.reading.value ? '참 (ON)' : '거짓 (OFF)') : String(sample.reading.value) || '빈 문자열'
}

interface WorkflowProps { workflow: WorkOrderWorkflow; selectedOrder: WorkOrder | null; tracked: TrackedCommand | null; setTracked: Dispatch<SetStateAction<TrackedCommand | null>>; alarmLedger: AlarmLedger | null; setAlarmLedger: Dispatch<SetStateAction<AlarmLedger | null>> }
export default function SnapshotPanel({ session, onSessionInvalid, workflow, selectedOrder, tracked, setTracked, alarmLedger, setAlarmLedger }: { session: Session; onSessionInvalid: () => void } & WorkflowProps) {
  const [sources, setSources] = useState<Source[] | null>(null)
  const [selected, setSelected] = useState('')
  const [sourceError, setSourceError] = useState('')
  const [attempt, setAttempt] = useState(0)
  useEffect(() => {
    const controller = new AbortController()
    void read('/api/v1/sources', controller.signal).then(result => {
      if (controller.signal.aborted) return
      if (typeof result !== 'object' || result === null || !('sources' in result) || !Array.isArray(result.sources)
        || !result.sources.every(item => item && ['line_id', 'line_name', 'source_id', 'stream_id'].every(key => typeof item[key] === 'string'))) throw new Error('invalid sources')
      const list = result.sources as Source[]
      setSources(list)
      setSourceError('')
      if (list.length === 1) setSelected('0')
    }).catch(error => {
      if (controller.signal.aborted) return
      if (error instanceof ReadError && (error.status === 401 || error.status === 403)) { onSessionInvalid(); return }
      setSourceError('등록된 수집원을 조회하지 못했습니다. 연결을 확인하고 다시 시도해 주세요.')
    })
    return () => controller.abort()
  }, [attempt, onSessionInvalid])
  const source = sources && selected !== '' ? sources[Number(selected)] : undefined
  return <section className="monitor-section" aria-labelledby="monitor-heading">
    <div className="monitor-heading"><div><p className="section-kicker">DATA SOURCE</p><h2 id="monitor-heading">설비 수집 상태</h2></div><span className="read-only-tag">실제 저장 관측</span></div>
    {sourceError ? <div className="message error" role="alert">{sourceError}<button type="button" className="button secondary" onClick={() => setAttempt(value => value + 1)}>다시 조회</button></div>
      : sources === null ? <p role="status">등록된 수집원을 확인하고 있습니다.</p>
        : sources.length === 0 ? <div id="no-sources" className="connection-card"><h2>등록된 수집원이 없습니다.</h2><p>설비 관리자가 라인과 수집원을 등록한 뒤 조회할 수 있습니다. 현재 설비 상태는 확인할 수 없습니다.</p><button type="button" className="button secondary" onClick={() => setAttempt(value => value + 1)}>수집원 다시 조회</button></div>
          : <><label htmlFor="source-selector">조회할 라인 · 수집원</label><select id="source-selector" value={selected} onChange={event => setSelected(event.target.value)}><option value="" disabled>라인을 선택해 주세요</option>{sources.map((item, index) => <option key={`${item.line_id}/${item.source_id}/${item.stream_id}`} value={String(index)}>{item.line_name} · {item.source_id} / {item.stream_id}</option>)}</select>
            {source ? <LiveSnapshot key={`${source.line_id}/${source.source_id}/${source.stream_id}`} source={source} session={session} tracked={tracked} setTracked={setTracked} workflow={workflow} selectedOrder={selectedOrder} alarmLedger={alarmLedger} setAlarmLedger={setAlarmLedger} onSessionInvalid={onSessionInvalid} /> : <p>라인을 선택하면 실제 저장된 관측값을 조회합니다.</p>}</>}
    <p className="form-help">저장된 실적 정산은 원래 실행 기록에서 수동 조회합니다. 보관 후에도 다음 연결 조건은 다시 검증합니다.</p>
  </section>
}

function LiveSnapshot({ source, session, tracked, setTracked, workflow, selectedOrder, alarmLedger, setAlarmLedger, onSessionInvalid }: {
  source: Source; session: Session; tracked: TrackedCommand | null
  setTracked: Dispatch<SetStateAction<TrackedCommand | null>>; onSessionInvalid: () => void
} & Pick<WorkflowProps, 'workflow' | 'selectedOrder' | 'alarmLedger' | 'setAlarmLedger'>) {
  const stream = useScreenSubscription(source, session.csrf_token, onSessionInvalid)
  const snapshot = stream.projection
  const error = stream.message
  const now = stream.now
  const freshTime = (value: string) => { const age = now - Date.parse(value); return age >= 0 && age < 3000 }
  const current = stream.phase === 'synced' && snapshot !== null && freshTime(snapshot.screen.emitted_at)
  const sampleFresh = (sample: Sample | undefined) => {
    const evidence = snapshot?.evidence.find(item => item.tag_id === sample?.tag_id)
    return current && !!sample && evidence?.fresh === true && freshTime(evidence.captured_at) && freshTime(evidence.stored_at) && freshTime(sample.acquired_at)
  }
  const derivedUsable = (tagId: string) => {
    const sample = snapshot?.screen.view.samples.find(item => item.tag_id === tagId)
    return sampleFresh(sample) && !!sample && (sample.status_code >>> 30) === 0 && sample.reading.value !== null
  }
  const locallyStale = snapshot?.screen.view.samples.some(sample => !sampleFresh(sample))
  // Age from both capture and server receipt so a future gateway clock cannot
  // restore GOOD when wall time catches up. Missing receipt evidence stays unknown.
  const communication = snapshot?.health.communication
  const communicationCapturedAt = snapshot?.health.communication_captured_at
  const communicationStoredAt = snapshot?.health.communication_stored_at
  let heartbeat: 'UNKNOWN' | 'GOOD' | 'DELAYED' | 'ALARM' = 'UNKNOWN'
  if (stream.phase === 'synced' && communication && communicationCapturedAt && communicationStoredAt && snapshot) {
    const captureElapsed = (now - Date.parse(communicationCapturedAt)) / 1000
    const receiptElapsed = (now - Date.parse(communicationStoredAt)) / 1000
    const elapsed = Math.max(captureElapsed, receiptElapsed)
    const ages = [communication.read_age_seconds, ...Object.values(communication.heartbeat_ages_seconds)]
    const validAges = ages.every(age => age === null || (typeof age === 'number' && Number.isFinite(age) && age >= 0))
    if (Number.isFinite(captureElapsed) && captureElapsed >= 0 && Number.isFinite(receiptElapsed) && receiptElapsed >= 0
      && now >= Date.parse(snapshot.screen.emitted_at) && validAges) {
      const oldest = Math.max(elapsed, ...ages.filter((age): age is number => age !== null).map(age => age + elapsed))
      if (oldest >= 5 || snapshot.health.heartbeat === 'ALARM') heartbeat = 'ALARM'
      else if (oldest >= 3 || snapshot.health.heartbeat === 'DELAYED') heartbeat = 'DELAYED'
      else if (current && snapshot.health.heartbeat === 'GOOD' && communication.state === 'GOOD'
        && communication.connected && ages.length === 5 && ages.every(age => age !== null)) heartbeat = 'GOOD'
    }
  }
  const controlValues = Object.fromEntries(snapshot?.screen.view.samples.map(sample => [sample.tag_id, sample.reading.value]) ?? [])
  const allControlSamples = snapshot?.screen.view.samples.length === tagMetadata.length
    && new Set(snapshot.screen.view.samples.map(sample => sample.tag_id)).size === tagMetadata.length
    && tagMetadata.every(tag => {
      const sample = snapshot.screen.view.samples.find(item => item.tag_id === tag.logical_name)
      return sampleFresh(sample) && !!sample && (sample.status_code >>> 30) === 0 && sample.reading.value !== null
        && sample.source_timestamp !== null && Number.isFinite(Date.parse(sample.source_timestamp))
        && Date.parse(sample.source_timestamp) - Date.parse(sample.acquired_at) <= 2000
        && (!heartbeatTags.has(sample.tag_id)
          || Math.abs(Date.parse(sample.acquired_at) - Date.parse(sample.source_timestamp)) <= 2000)
        && (sample.reading.data_type !== 'UInt64' || typeof sample.reading.value === 'string')
    })
  const blockedReason = !current || !allControlSamples ? '전체 48개 태그의 최신 정상 품질과 원본 시각을 확인해야 합니다.'
    : snapshot.health.state !== 'CAPTURE_FRESH' || heartbeat !== 'GOOD' ? '최근 수집과 PLC 하트비트 정상을 모두 확인해야 합니다.'
      : snapshot.health.source_conflicted || snapshot.health.reconciliation_required || snapshot.missing_tag_ids.length > 0 ? '수집원 충돌·대조 필요·미수신 태그를 먼저 확인해 주세요.' : ''
  const heartbeatLabel = { UNKNOWN: '확인 불가', GOOD: '정상', DELAYED: '지연', ALARM: '통신 경보' }[heartbeat]
  const health = !snapshot ? '수집 상태 확인 중' : current ? (locallyStale && snapshot.health.state === 'CAPTURE_FRESH' ? '수집 지연' : healthLabels[snapshot.health.state] ?? '수집 상태 불명') : '조회 지연 · 상태 불명'
  const lineValue = snapshot && derivedUsable('Line.State') ? snapshot.screen.view.state : null
  const lineTone: Tone = lineValue ? stateTones[lineValue] ?? 'unknown' : 'unknown'
  const healthTone: Tone = !snapshot || !current ? 'unknown' : locallyStale || snapshot.health.state === 'STALE' || snapshot.health.state === 'DEGRADED' ? 'warn' : snapshot.health.state === 'CAPTURE_FRESH' ? 'good' : 'unknown'
  const executionText = snapshot && derivedUsable('Line.ActiveExecutionId') ? snapshot.screen.view.execution_id ?? '불명' : '불명'
  const executionId = executionText === '불명' ? null : executionText
  return <div id="snapshot-panel">
    <div className="status-bar">
      <p id="screen-connection" className="connection-pill" data-state={stream.phase} data-sync={snapshot?.screen.sync_id ?? ''} data-revision={snapshot?.screen.revision ?? ''}>
      {stream.phase === 'synced' ? (stream.transport === 'polling' ? '화면 갱신 연결됨' : '실시간 화면 연결됨') : stream.phase === 'stopped' ? '화면 연결 확인 필요' : '화면 연결 중 · 현재 상태 확인 불가'}
      </p>
      {stream.phase === 'stopped' && <button id="screen-reconnect" className="button secondary" type="button" onClick={stream.retry}>화면 다시 연결</button>}
    </div>
    {error && <div className="message error" role="alert">{error}</div>}
    <div className="snapshot-summary">
      <div data-tone={healthTone}><span>수집 상태</span><strong id="snapshot-health">{health}</strong></div>
      <div data-tone={lineTone}><span>라인 상태</span><strong id="line-state">{snapshot && derivedUsable('Line.State') ? stateLabels[snapshot.screen.view.state] ?? snapshot.screen.view.state : '불명'}</strong></div>
      <div data-tone="neutral"><span>실행 ID</span><strong className="execution-chip">{executionId ? <CopyId value={executionId} /> : '불명'}</strong></div>
      <div data-tone={heartbeatTones[heartbeat]}><span>PLC 하트비트</span><strong id="heartbeat-state" data-state={heartbeat}>{heartbeatLabel}</strong></div>
    </div>
    <p className="control-note" id="control-restricted">제어 요청은 서버와 PLC가 권한·실행 조건을 다시 확인한 뒤 실행됩니다. 화면의 정상 표시만으로 실행되지 않습니다.</p>
    {snapshot && <>
      {(snapshot.health.source_conflicted || snapshot.health.reconciliation_required) && <div className="message error">{snapshot.health.source_conflicted ? '수집원 충돌 · ' : ''}{snapshot.health.reconciliation_required ? '데이터 대조 필요' : ''}</div>}
      <p className="snapshot-time">서버 응답 시각 {timestamp(snapshot.screen.emitted_at)} · 실시간 화면 갱신 · 미수신 태그 {snapshot.missing_tag_ids.length}개</p>
    </>}
    <nav className="section-jump" aria-label="이 화면의 구역">
      <a href="#process-heading">공정 현황</a><a href="#command-heading">운전 제어</a><a href="#equipment-heading">태그 상세</a><a href="#alarm-center-heading">알람</a>
    </nav>
    <div className="line-layout">
      <div className="line-process">
      <ProcessOverview readKnownValue={tagId => {
        if (!current || heartbeat !== 'GOOD' || snapshot.health.source_conflicted || snapshot.health.reconciliation_required || !derivedUsable(tagId)) return null
        return snapshot.screen.view.samples.find(item => item.tag_id === tagId)?.reading.value ?? null
      }} />
      </div>
      <div className="line-side">
      <CommandPanel source={source} session={session} blockedReason={blockedReason} newIntentBlocked={workflow.attempt && (!workflow.attempt.record || (workflow.attempt.kind === 'bind' && (workflow.attempt.loadResult?.status !== 'SUCCEEDED' || !('execution_id' in workflow.attempt.record.receipt) || workflow.attempt.record.receipt.execution_id !== controlValues['Line.ActiveExecutionId']))) ? '작업지시 요청과 원래 작업 불러오기 결과를 먼저 확인해 주세요.' : ''} values={controlValues} tracked={tracked} setTracked={setTracked} onSessionInvalid={onSessionInvalid} />
      <WorkOrderBinding order={selectedOrder} source={source} session={session} blockedReason={blockedReason} values={controlValues} workflow={workflow} tracked={tracked} />
      <LoadTracker source={source} workflow={workflow} onSessionInvalid={onSessionInvalid} />
      </div>
    <section id="equipment-details" className="equipment-details" aria-labelledby="equipment-heading">
      <div className="equipment-details-heading"><h2 id="equipment-heading">태그 상세</h2><p>48개 태그의 현재 값과 데이터 품질입니다. 오래되거나 품질이 나쁜 값은 숫자 대신 —로 표시합니다.</p></div>
    <div className="equipment-grid">{Object.entries(groupNames).map(([group, title]) => <section key={group} className="equipment-card" aria-labelledby={`group-${group}`}>
      <h3 id={`group-${group}`}>{title}<span>{group}</span></h3>
      <dl>{tagMetadata.filter(tag => tag.group === group).map(tag => {
        const sample = snapshot?.screen.view.samples.find(item => item.tag_id === tag.logical_name)
        const fresh = sampleFresh(sample)
        const quality = sample ? (sample.status_code >>> 30) === 0 ? 'Good' : (sample.status_code >>> 30) === 1 ? 'Uncertain' : 'Bad' : '미수신'
        const usable = fresh && quality === 'Good' && sample?.reading.value !== null
          && (sample?.reading.data_type !== 'UInt64' || typeof sample.reading.value === 'string')
        const label = !sample ? '미수신 · 불명' : `${quality}${!fresh ? ' · 오래된 관측 / 불명' : ''}`
        return <div className={`tag-row ${usable ? 'usable' : 'unusable'}`} key={tag.logical_name} data-tag-id={tag.logical_name}>
          <dt title={tag.logical_name}>{names[tag.name] ?? tag.name}</dt><dd><strong>{usable && sample ? displayValue(sample, tag.enum) : '—'}{usable && tag.unit ? <small> {tag.unit}</small> : null}</strong><span className="quality">{label}</span>{sample && !usable && <span className="last-observation">마지막 관측: {displayValue(sample, tag.enum)}</span>}</dd>
        </div>
      })}</dl>
    </section>)}</div>
    </section>
    </div>
    <AlarmPanel session={session} source={source} onSessionInvalid={onSessionInvalid} ledger={alarmLedger} setLedger={setAlarmLedger} />
  </div>
}
