import { tagEnums } from '../tagMetadata'
import { stateLabels } from './lineState'

/** The three zones of the packaging line, in flow order, with the tags each zone shows. */
export type ZoneId = 'Infeed' | 'Sealer' | 'Outfeed'
export interface Metric { tag: string; label: string; unit: string; digits?: number }
export interface Lamp { tag: string; label: string; on: string; off: string; alarmWhen?: boolean }
export interface Zone { id: ZoneId; label: string; role: string; metrics: Metric[]; lamps: Lamp[] }

export const zones: Zone[] = [
  {
    id: 'Infeed', label: '투입', role: '자재 투입 · 이송',
    metrics: [
      { tag: 'Infeed.ActualSpeed', label: '속도', unit: 'm/min', digits: 1 },
      { tag: 'Infeed.BufferCount', label: '대기', unit: '개' },
      { tag: 'Infeed.MotorCurrent', label: '전류', unit: 'A', digits: 2 },
    ],
    lamps: [
      { tag: 'Infeed.RunFeedback', label: '구동', on: '구동', off: '정지' },
      { tag: 'Infeed.EntrySensor', label: '투입 센서', on: '감지', off: '없음' },
    ],
  },
  {
    id: 'Sealer', label: '실링', role: '가열 · 압착',
    metrics: [
      { tag: 'Sealer.TemperaturePV', label: '온도', unit: '℃', digits: 1 },
      { tag: 'Sealer.AirPressure', label: '공압', unit: 'bar', digits: 2 },
      { tag: 'Sealer.ActualSpeed', label: '속도', unit: 'm/min', digits: 1 },
    ],
    lamps: [
      { tag: 'Sealer.HeaterOn', label: '히터', on: 'ON', off: 'OFF' },
      { tag: 'Sealer.Ready', label: '준비', on: '완료', off: '대기' },
      { tag: 'Sealer.GuardClosed', label: '안전문', on: '닫힘', off: '열림', alarmWhen: false },
    ],
  },
  {
    id: 'Outfeed', label: '배출', role: '검사 · 분류',
    metrics: [
      { tag: 'Outfeed.GoodCounter', label: '양품', unit: '개' },
      { tag: 'Outfeed.RejectCounter', label: '불량', unit: '개' },
      { tag: 'Outfeed.ActualSpeed', label: '속도', unit: 'm/min', digits: 1 },
    ],
    lamps: [
      { tag: 'Outfeed.RunFeedback', label: '구동', on: '구동', off: '정지' },
      { tag: 'Outfeed.ExitSensor', label: '배출 센서', on: '감지', off: '없음' },
    ],
  },
]

export const interlocks: Lamp[] = [
  { tag: 'Line.ControlMode', label: '제어 권한', on: '원격', off: '현장' },
  { tag: 'Line.OperationMode', label: '운전 모드', on: '자동', off: '수동' },
  { tag: 'Line.Permissive', label: '운전 허가', on: '허가', off: '불가', alarmWhen: false },
  { tag: 'Line.GuardClosed', label: '안전문', on: '닫힘', off: '열림', alarmWhen: false },
  { tag: 'Line.EStop', label: '비상정지', on: '작동', off: '해제', alarmWhen: true },
]

type Known = (tagId: string) => string | number | boolean | null

/** Zone or line state name, or UNKNOWN when the value is not known. */
export function stateOf(known: Known, prefix: string): string {
  const raw = known(`${prefix}.State`)
  return typeof raw === 'number' ? tagEnums.EquipmentState[String(raw)] ?? 'UNKNOWN' : 'UNKNOWN'
}
export const stateLabel = (state: string) => stateLabels[state] ?? state

/** A number as text, or null when the value is unknown. */
export function numberText(known: Known, metric: Metric): string | null {
  const value = known(metric.tag)
  if (typeof value === 'number' && Number.isFinite(value)) return metric.digits !== undefined ? value.toFixed(metric.digits) : value.toLocaleString('ko-KR')
  if (typeof value === 'string' && /^\d+$/.test(value)) return Number(value).toLocaleString('ko-KR')
  return null
}
export function count(known: Known, tag: string): number | null {
  const value = known(tag)
  return typeof value === 'number' && Number.isFinite(value) ? value : typeof value === 'string' && /^\d+$/.test(value) ? Number(value) : null
}

/** Lamp reading: on/off/unknown, and whether it is an alarm condition. Enum modes count 1 as "on". */
export function lampOf(known: Known, lamp: Lamp): { state: 'on' | 'off' | 'unknown'; alarm: boolean; text: string } {
  const value = known(lamp.tag)
  const on = typeof value === 'boolean' ? value : typeof value === 'number' ? value === 1 : null
  if (on === null) return { state: 'unknown', alarm: false, text: '불명' }
  return { state: on ? 'on' : 'off', alarm: lamp.alarmWhen !== undefined && on === lamp.alarmWhen, text: on ? lamp.on : lamp.off }
}
