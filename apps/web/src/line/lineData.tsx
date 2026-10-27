import { useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import { tagMetadata } from '../tagMetadata'
import { useScreenSubscription } from '../useScreenSubscription'
import type { ScreenSample as Sample } from '../screenState'
import type { Session } from '../auth'
import { LineContext, healthLabels, heartbeatLabels, heartbeatTags, stateTones } from './lineState'
import type { Heartbeat, LineData, LiveLine, Source, Tone } from './lineState'

class ReadError extends Error { constructor(public status: number) { super('read failed') } }

export function LineDataProvider({ session, onSessionInvalid, children }: { session: Session; onSessionInvalid: () => void; children: ReactNode }) {
  const [sources, setSources] = useState<Source[] | null>(null)
  const [selected, setSelected] = useState('')
  const [sourceError, setSourceError] = useState('')
  const [attempt, setAttempt] = useState(0)
  useEffect(() => {
    const controller = new AbortController()
    void fetch('/api/v1/sources', { credentials: 'same-origin', cache: 'no-store', signal: AbortSignal.any([controller.signal, AbortSignal.timeout(15_000)]) }).then(async response => {
      if (!response.ok) throw new ReadError(response.status)
      const result: unknown = await response.json()
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
      setSourceError('수집원을 조회하지 못했습니다.')
    })
    return () => controller.abort()
  }, [attempt, onSessionInvalid])
  const source = sources && selected !== '' ? sources[Number(selected)] : undefined
  const base = { sources, sourceError, selected, select: setSelected, retry: () => setAttempt(value => value + 1), source }
  if (!source) return <LineContext.Provider value={{ ...base, live: null }}>{children}</LineContext.Provider>
  return <LiveProvider key={`${source.line_id}/${source.source_id}/${source.stream_id}`} base={base} source={source} session={session} onSessionInvalid={onSessionInvalid}>{children}</LiveProvider>
}

function LiveProvider({ base, source, session, onSessionInvalid, children }: { base: Omit<LineData, 'live'>; source: Source; session: Session; onSessionInvalid: () => void; children: ReactNode }) {
  const stream = useScreenSubscription(source, session.csrf_token, onSessionInvalid)
  const snapshot = stream.projection
  // Ages run on the server clock: the emission time plus local time since arrival.
  const sinceArrival = Math.max(0, stream.now - stream.receivedAt)
  const now = snapshot ? Date.parse(snapshot.screen.emitted_at) + sinceArrival : stream.now
  const freshTime = (value: string) => { const age = now - Date.parse(value); return age >= 0 && age < 3000 }
  const current = stream.phase === 'synced' && snapshot !== null && sinceArrival < 3000
  const sample = (tagId: string) => snapshot?.screen.view.samples.find(item => item.tag_id === tagId)
  const sampleFresh = (item: Sample | undefined) => {
    const evidence = snapshot?.evidence.find(entry => entry.tag_id === item?.tag_id)
    return current && !!item && evidence?.fresh === true && freshTime(evidence.captured_at) && freshTime(evidence.stored_at) && freshTime(item.acquired_at)
  }
  const derivedUsable = (tagId: string) => {
    const item = sample(tagId)
    return sampleFresh(item) && !!item && (item.status_code >>> 30) === 0 && item.reading.value !== null
  }
  const locallyStale = snapshot?.screen.view.samples.some(item => !sampleFresh(item))
  // Age from both capture and server receipt so a future gateway clock cannot restore GOOD.
  const communication = snapshot?.health.communication
  const communicationCapturedAt = snapshot?.health.communication_captured_at
  const communicationStoredAt = snapshot?.health.communication_stored_at
  let heartbeat: Heartbeat = 'UNKNOWN'
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
  const controlValues = Object.fromEntries(snapshot?.screen.view.samples.map(item => [item.tag_id, item.reading.value]) ?? [])
  const allControlSamples = snapshot?.screen.view.samples.length === tagMetadata.length
    && new Set(snapshot.screen.view.samples.map(item => item.tag_id)).size === tagMetadata.length
    && tagMetadata.every(tag => {
      const item = snapshot.screen.view.samples.find(entry => entry.tag_id === tag.logical_name)
      return sampleFresh(item) && !!item && (item.status_code >>> 30) === 0 && item.reading.value !== null
        && item.source_timestamp !== null && Number.isFinite(Date.parse(item.source_timestamp))
        && Date.parse(item.source_timestamp) - Date.parse(item.acquired_at) <= 2000
        && (!heartbeatTags.has(item.tag_id)
          || Math.abs(Date.parse(item.acquired_at) - Date.parse(item.source_timestamp)) <= 2000)
        && (item.reading.data_type !== 'UInt64' || typeof item.reading.value === 'string')
    })
  const blockedReason = !current || !allControlSamples ? '전체 48개 태그의 최신 정상 품질과 원본 시각을 확인해야 합니다.'
    : snapshot.health.state !== 'CAPTURE_FRESH' || heartbeat !== 'GOOD' ? '최근 수집과 PLC 하트비트 정상을 모두 확인해야 합니다.'
      : snapshot.health.source_conflicted || snapshot.health.reconciliation_required || snapshot.missing_tag_ids.length > 0 ? '수집원 충돌·대조 필요·미수신 태그를 먼저 확인해 주세요.' : ''
  const health = !snapshot ? '확인 중' : current ? (locallyStale && snapshot.health.state === 'CAPTURE_FRESH' ? '수집 지연' : healthLabels[snapshot.health.state] ?? '불명') : '조회 지연'
  const lineState = snapshot && derivedUsable('Line.State') ? snapshot.screen.view.state : null
  const healthTone: Tone = !snapshot || !current ? 'unknown' : locallyStale || snapshot.health.state === 'STALE' || snapshot.health.state === 'DEGRADED' ? 'warn' : snapshot.health.state === 'CAPTURE_FRESH' ? 'good' : 'unknown'
  const executionText = snapshot && derivedUsable('Line.ActiveExecutionId') ? snapshot.screen.view.execution_id : null
  const known = (tagId: string) => {
    if (!current || heartbeat !== 'GOOD' || !snapshot || snapshot.health.source_conflicted || snapshot.health.reconciliation_required || !derivedUsable(tagId)) return null
    return sample(tagId)?.reading.value ?? null
  }
  const live: LiveLine = {
    stream, snapshot, current, heartbeat, heartbeatLabel: heartbeatLabels[heartbeat], health, healthTone,
    lineState, lineTone: lineState ? stateTones[lineState] ?? 'unknown' : 'unknown',
    executionId: executionText ?? null, controlValues, blockedReason, sample, sampleFresh, known,
  }
  return <LineContext.Provider value={{ ...base, live }}>{children}</LineContext.Provider>
}
