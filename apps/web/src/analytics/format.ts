/** Display formatting for reporting pages. Missing values render as an explicit dash, never 0. */

export const TIME_ZONE = 'Asia/Seoul'
export const MISSING = '—'

const numberFormat = new Intl.NumberFormat('ko-KR')

export function quantity(value: string | null | undefined): string {
  if (value === null || value === undefined) return MISSING
  try { return numberFormat.format(BigInt(value)) } catch { return MISSING }
}

export function percent(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return MISSING
  return `${(value * 100).toFixed(digits)}%`
}

export function decimal(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return MISSING
  return value.toLocaleString('ko-KR', { minimumFractionDigits: digits, maximumFractionDigits: digits })
}

export function duration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return MISSING
  const total = Math.round(seconds)
  const hours = Math.floor(total / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  const rest = total % 60
  if (hours > 0) return `${hours}시간 ${minutes}분`
  if (minutes > 0) return `${minutes}분 ${rest}초`
  return `${rest}초`
}

const dateTimeFormat = new Intl.DateTimeFormat('ko-KR', { timeZone: TIME_ZONE, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false })
const timeFormat = new Intl.DateTimeFormat('ko-KR', { timeZone: TIME_ZONE, hour: '2-digit', minute: '2-digit', hour12: false })
const secondFormat = new Intl.DateTimeFormat('ko-KR', { timeZone: TIME_ZONE, hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false })
const hourFormat = new Intl.DateTimeFormat('ko-KR', { timeZone: TIME_ZONE, hour: '2-digit', hour12: false })

export function dateTime(value: string | null | undefined): string {
  if (!value) return MISSING
  const parsed = Date.parse(value)
  return Number.isFinite(parsed) ? dateTimeFormat.format(parsed) : MISSING
}

export function clock(value: string | number): string {
  const parsed = typeof value === 'number' ? value : Date.parse(value)
  return Number.isFinite(parsed) ? timeFormat.format(parsed) : MISSING
}

export function secondClock(value: string | number): string {
  const parsed = typeof value === 'number' ? value : Date.parse(value)
  return Number.isFinite(parsed) ? secondFormat.format(parsed) : MISSING
}

export function hourLabel(value: string): string {
  const parsed = Date.parse(value)
  return Number.isFinite(parsed) ? hourFormat.format(parsed).replace(/시$/, '') : MISSING
}

/** Today's date in Seoul as YYYY-MM-DD. */
export function seoulToday(now = Date.now()): string {
  return new Intl.DateTimeFormat('en-CA', { timeZone: TIME_ZONE, year: 'numeric', month: '2-digit', day: '2-digit' }).format(now)
}

export const lineStateLabels: Record<string, string> = {
  STOPPED: '정지', READY: '준비', RUNNING: '운전 중', PAUSED: '일시 정지', STOPPING: '정지 중',
  FAULT: '고장', ESTOP: '비상 정지', RECOVERY_REQUIRED: '복구 필요', UNKNOWN: '불명',
}

export const settlementLabels: Record<string, string> = {
  SETTLED: '정산 완료', PENDING: '정산 대기', MISMATCH: '수량 불일치', CONFLICT: '요약 충돌',
}

export const provisionalLabels: Record<string, string> = {
  DAY_NOT_FINISHED: '하루가 끝나지 않음', NOT_SETTLED: '실적 정산 전', EXECUTION_NOT_SETTLED: '실적 정산 전',
  TIME_UNKNOWN: '시간 품질 불명', UNKNOWN_TIME: '불명 시간 포함',
  COMMUNICATION_LOST: '통신 단절 구간 포함', RECEIVED_EVENTS: '수신 사건 기준', RETENTION_EXPIRED: '보존 기간 지난 구간 포함',
}

export const unknownReasonLabels: Record<string, string> = {
  COMMUNICATION_LOST: '통신 단절', TIME_UNKNOWN: '시간 품질 불명', NO_EVIDENCE: '근거 없음', RETENTION_EXPIRED: '보존 기간 지남', NO_DATA: '수신 없음 (설비 꺼짐)',
}

export const tagLabels: Record<string, string> = {
  'Infeed.MotorCurrent': '투입 모터 전류', 'Infeed.ActualSpeed': '투입 속도',
  'Sealer.MotorCurrent': '실링 모터 전류', 'Sealer.ActualSpeed': '실링 속도',
  'Sealer.TemperaturePV': '실링 온도', 'Sealer.TemperatureSP': '실링 설정 온도', 'Sealer.AirPressure': '실링 공기 압력',
  'Outfeed.MotorCurrent': '배출 모터 전류', 'Outfeed.ActualSpeed': '배출 속도',
}

export const analogTags = Object.keys(tagLabels)

/** Request end for a span: whole seconds up to one hour, whole minutes beyond (server `auto` rule). */
export function alignedEnd(anchor: number, span: number) {
  const step = span <= 3600e3 ? 1000 : 60e3
  return Math.floor(anchor / step) * step
}

/**
 * Window to zoom into when values cover only a small part of the requested span (e.g. a short run
 * inside a 1-hour view). Null when values already fill enough of it or there are none. The zoom is a
 * display choice only; the full span and its missing slots stay reported next to the chart.
 */
export function focusWindow(times: number[], from: number, to: number, stepMs: number): { from: number; to: number } | null {
  if (!times.length || to <= from) return null
  const first = Math.min(...times)
  const last = Math.max(...times) + stepMs
  if ((last - first) / (to - from) >= 0.5) return null
  const pad = Math.max((last - first) * 0.1, 10 * stepMs)
  return { from: Math.max(from, first - pad), to: Math.min(to, last + pad) }
}
