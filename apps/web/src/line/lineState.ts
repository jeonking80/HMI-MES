import { createContext, useContext } from 'react'
import type { useScreenSubscription } from '../useScreenSubscription'
import type { ScreenSample as Sample } from '../screenState'

/** One live line stream shared by the monitoring and control pages. */
export interface Source { line_id: string; line_name: string; source_id: string; stream_id: string }
export type Tone = 'good' | 'warn' | 'bad' | 'unknown' | 'neutral'
export type Heartbeat = 'UNKNOWN' | 'GOOD' | 'DELAYED' | 'ALARM'

export const stateLabels: Record<string, string> = { STOPPED: '정지', READY: '준비', RUNNING: '운전 중', PAUSED: '일시 정지', STOPPING: '정지 중', FAULT: '고장', ESTOP: '비상 정지', RECOVERY_REQUIRED: '복구 필요', UNKNOWN: '불명' }
export const stateTones: Record<string, Tone> = { RUNNING: 'good', READY: 'neutral', STOPPED: 'neutral', PAUSED: 'warn', STOPPING: 'warn', FAULT: 'bad', ESTOP: 'bad', RECOVERY_REQUIRED: 'bad' }
export const healthLabels: Record<string, string> = { NEVER_OBSERVED: '수집 이력 없음', STALE: '수집 지연', DEGRADED: '품질 저하', CAPTURE_FRESH: '정상 수집' }
export const heartbeatLabels: Record<Heartbeat, string> = { UNKNOWN: '확인 불가', GOOD: '정상', DELAYED: '지연', ALARM: '통신 경보' }
export const heartbeatTags = new Set(['Line.Heartbeat', 'Infeed.LinkHeartbeat', 'Sealer.LinkHeartbeat', 'Outfeed.LinkHeartbeat'])

export interface LiveLine {
  stream: ReturnType<typeof useScreenSubscription>
  snapshot: ReturnType<typeof useScreenSubscription>['projection']
  current: boolean
  heartbeat: Heartbeat
  heartbeatLabel: string
  health: string
  healthTone: Tone
  lineState: string | null
  lineTone: Tone
  executionId: string | null
  controlValues: Record<string, unknown>
  blockedReason: string
  sample: (tagId: string) => Sample | undefined
  sampleFresh: (sample: Sample | undefined) => boolean
  /** The value only when it is fresh, Good and the PLC link is healthy; otherwise null (unknown). */
  known: (tagId: string) => string | number | boolean | null
}

export interface LineData {
  sources: Source[] | null
  sourceError: string
  selected: string
  select: (value: string) => void
  retry: () => void
  source: Source | undefined
  live: LiveLine | null
}

export const LineContext = createContext<LineData | null>(null)

export function useLine(): LineData {
  const value = useContext(LineContext)
  if (!value) throw new Error('useLine outside LineDataProvider')
  return value
}


