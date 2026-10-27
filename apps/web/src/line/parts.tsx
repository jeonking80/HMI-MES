import { tagEnums, tagMetadata } from '../tagMetadata'
import type { ScreenSample as Sample } from '../screenState'
import { useLine } from './lineState'
import type { LiveLine } from './lineState'
import { toneOf } from './logic'
import { lampOf, numberText, stateLabel } from './lineModel'
import type { Lamp, Zone, ZoneId } from './lineModel'

export function StateBadge({ state, id, large = false }: { state: string; id?: string; large?: boolean }) {
  return <span id={id} className={`lc-state${large ? ' large' : ''}`} data-tone={toneOf(state)}><i aria-hidden="true" />{stateLabel(state)}</span>
}

export function LampChip({ known, lamp }: { known: LiveLine['known']; lamp: Lamp }) {
  const value = lampOf(known, lamp)
  return <span className="lc-lamp" data-state={value.alarm ? 'alarm' : value.state} title={lamp.label}><i aria-hidden="true" />{lamp.label} {value.text}</span>
}

export function Drawing({ zone }: { zone: ZoneId }) {
  return <svg className="lc-drawing" viewBox="0 0 240 90" aria-hidden="true" focusable="false"><g fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinejoin="round">
    {zone === 'Infeed' ? <><path d="M40 10H104L90 40H54Z" /><path d="M72 40V52" /><rect x="120" y="44" width="26" height="16" rx="3" /><rect x="160" y="44" width="26" height="16" rx="3" /></>
      : zone === 'Sealer' ? <><path d="M64 8H176V56H156V32H84V56H64Z" /><path d="M104 36V46M120 36V46M136 36V46" /><rect x="102" y="50" width="36" height="8" rx="2" /></>
        : <><path d="M84 6H156V22H84Z" /><path d="M94 22L112 40M146 22L128 40" /><rect x="105" y="42" width="30" height="16" rx="3" /><path d="M180 36L190 46L210 24" /></>}
    <path d="M20 64H220V76H20Z" />
  </g></svg>
}

export function MetricBox({ known, zone, index }: { known: LiveLine['known']; zone: Zone; index: number }) {
  const metric = zone.metrics[index]
  const text = numberText(known, metric)
  const tone = metric.tag === 'Outfeed.GoodCounter' ? 'good' : metric.tag === 'Outfeed.RejectCounter' ? 'bad' : undefined
  return <div className="lc-metric" data-process-value={metric.tag}>
    <span>{metric.label}{metric.tag === 'Sealer.TemperaturePV' && numberText(known, { tag: 'Sealer.TemperatureSP', label: '', unit: '', digits: 0 }) !== null ? <small> 설정 {numberText(known, { tag: 'Sealer.TemperatureSP', label: '', unit: '', digits: 0 })}</small> : null}</span>
    <strong data-tone={text === null ? 'unknown' : tone}>{text ?? '—'}<small>{text === null ? ' 불명' : ` ${metric.unit}`}</small></strong>
  </div>
}

/** Line and source picker; hidden when there is a single source. */
export function SourcePicker() {
  const line = useLine()
  if (line.sourceError) return <span className="lc-chip" data-tone="bad">{line.sourceError} <button type="button" className="lc-link" onClick={line.retry}>다시 조회</button></span>
  if (!line.sources || line.sources.length <= 1) return null
  return <select id="source-selector" aria-label="라인 · 수집원" value={line.selected} onChange={event => line.select(event.target.value)}>
    <option value="" disabled>라인을 선택해 주세요</option>
    {line.sources.map((item, index) => <option key={`${item.line_id}/${item.source_id}/${item.stream_id}`} value={String(index)}>{item.line_name} · {item.source_id} / {item.stream_id}</option>)}
  </select>
}

const names: Record<string, string> = {
  State: '운전 상태', ControlMode: '제어 모드', OperationMode: '운전 모드', EStop: '비상 정지', GuardClosed: '안전문 닫힘', Permissive: '운전 허가', FaultCode: '고장 코드', ActiveExecutionId: '실행 ID', RecipeVersion: '레시피 버전', CounterEpoch: '카운터 구간', TargetQuantity: '목표 수량', WipCount: '재공 수량', GoodTotal: '누적 양품', RejectTotal: '누적 불량', ScrapTotal: '누적 폐기', Heartbeat: 'PLC 하트비트', ControlVersion: '제어 버전', EventSeq: '사건 순번', RunFeedback: '운전 피드백', MotorCurrent: '모터 전류', ActualSpeed: '실제 속도', EntrySensor: '투입 센서', BufferCount: '버퍼 수량', LinkHeartbeat: '연결 하트비트', TemperaturePV: '현재 온도', TemperatureSP: '설정 온도', AirPressure: '공기 압력', HeaterOn: '히터 켜짐', Ready: '준비 완료', ExitSensor: '배출 센서', CheckResult: '검사 결과', GoodCounter: '양품 카운터', RejectCounter: '불량 카운터',
}
const groups: Record<string, string> = { Line: '라인 공통', Infeed: '투입', Sealer: '실링', Outfeed: '배출' }
function displayValue(sample: Sample, enumName: string | null): string {
  if (sample.reading.value === null) return '값 불명'
  if (sample.reading.data_type === 'UInt64' && typeof sample.reading.value !== 'string') return '정밀도 확인 불가'
  if (enumName) {
    const raw = tagEnums[enumName]?.[String(sample.reading.value)]
    return raw ? stateLabel(raw) === raw ? ({ LOCAL: '현장', REMOTE: '원격', MANUAL: '수동', AUTO: '자동', UNDECIDED: '미판정', GOOD: '양품', REJECT: '불량' } as Record<string, string>)[raw] ?? raw : stateLabel(raw) : '알 수 없는 코드'
  }
  return typeof sample.reading.value === 'boolean' ? (sample.reading.value ? 'ON' : 'OFF') : String(sample.reading.value) || '빈 문자열'
}

/** All 48 tags with quality; folded by default. */
export function TagDetails({ live }: { live: LiveLine }) {
  return <details id="equipment-details" className="lc-card lc-tags">
    <summary><span>전체 태그 {tagMetadata.length}개 · 품질과 수집 시각</span><span className="lc-summary-hint">펼치기</span></summary>
    <div className="lc-tag-grid">{Object.entries(groups).map(([group, title]) => <section key={group} aria-labelledby={`group-${group}`}>
      <h3 id={`group-${group}`}>{title}</h3>
      <dl>{tagMetadata.filter(tag => tag.group === group).map(tag => {
        const sample = live.sample(tag.logical_name)
        const fresh = live.sampleFresh(sample)
        const quality = sample ? (sample.status_code >>> 30) === 0 ? 'Good' : (sample.status_code >>> 30) === 1 ? 'Uncertain' : 'Bad' : '미수신'
        const usable = fresh && quality === 'Good' && sample?.reading.value !== null && (sample?.reading.data_type !== 'UInt64' || typeof sample.reading.value === 'string')
        return <div key={tag.logical_name} data-tag-id={tag.logical_name} className={usable ? 'usable' : 'unusable'}>
          <dt title={tag.logical_name}>{names[tag.name] ?? tag.name}</dt>
          <dd><strong>{usable && sample ? displayValue(sample, tag.enum) : '—'}{usable && tag.unit ? <small> {tag.unit}</small> : null}</strong><span>{!sample ? '미수신' : `${({ Good: '정상', Uncertain: '불확실', Bad: '불량' } as Record<string, string>)[quality]}${fresh ? '' : ' · 갱신 안 됨'}`}</span></dd>
        </div>
      })}</dl>
    </section>)}</div>
  </details>
}
