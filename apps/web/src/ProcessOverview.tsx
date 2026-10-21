import { tagEnums } from './tagMetadata'
import './processOverview.css'

/** The caller must return null for stale, bad, missing or disconnected observations. */
interface Props { readKnownValue: (tagId: string) => string | number | boolean | null }

const equipment = [
  { id: 'Infeed', label: '투입', detail: '자재 투입 · 이송', measurements: [['BufferCount', '대기 수량', '개'], ['ActualSpeed', '이송 속도', 'm/min']] },
  { id: 'Sealer', label: '실링', detail: '온도 · 공압 제어', measurements: [['TemperaturePV', '현재 온도', '℃'], ['AirPressure', '공기 압력', 'bar']] },
  { id: 'Outfeed', label: '배출', detail: '검사 · 양품 / 불량 분류', measurements: [['GoodCounter', '양품', '개'], ['RejectCounter', '불량', '개']] },
] as const
const labels: Record<string, string> = {
  STOPPED: '정지', READY: '준비', RUNNING: '운전 중', PAUSED: '일시 정지', STOPPING: '정지 중',
  FAULT: '고장', ESTOP: '비상 정지', RECOVERY_REQUIRED: '복구 필요', UNKNOWN: '불명',
}

function MachineDrawing({ kind }: { kind: string }) {
  return <svg viewBox="0 0 240 100" aria-hidden="true" focusable="false">
    <path className="machine-belt" d="M20 66H220V80H20Z" />
    {[32, 61, 90, 119, 148, 177, 206].map(x => <circle key={x} cx={x} cy="73" r="4" />)}
    <path d="M36 81V92M204 81V92" />
    {kind === 'Infeed' ? <><path className="machine-body" d="M34 14H96L82 48H48Z" /><path d="M65 48V60" /><rect x="110" y="48" width="28" height="17" rx="3" /><rect x="157" y="48" width="28" height="17" rx="3" /></>
      : kind === 'Sealer' ? <><path className="machine-body" d="M65 17H177V65H157V39H85V65H65Z" /><path d="M105 44V55M121 44V55M137 44V55" /><rect x="103" y="58" width="36" height="7" rx="2" /></>
        : <><path className="machine-body" d="M83 13H157V31H83Z" /><path d="M94 31L112 48M146 31L128 48" /><rect x="105" y="49" width="30" height="16" rx="3" /><path d="M180 44L190 54L211 30" /></>}
  </svg>
}

export default function ProcessOverview({ readKnownValue }: Props) {
  const quantity = (tag: string) => {
    const value = readKnownValue(tag)
    return typeof value === 'number' && Number.isFinite(value) || typeof value === 'string' && /^\d+$(?![\s\S])/.test(value) ? String(value) : '—'
  }
  return <section className="process-overview" aria-labelledby="process-heading">
    <div className="process-heading"><div><h3 id="process-heading">라인 공정</h3><p>설비를 선택하면 측정값과 품질을 확인할 수 있습니다.</p></div>
      <div className="process-totals"><span>재공 <strong data-process-value="Line.WipCount">{quantity('Line.WipCount')}</strong></span><span>양품 <strong data-process-value="Line.GoodTotal">{quantity('Line.GoodTotal')}</strong></span><span>불량 <strong data-process-value="Line.RejectTotal">{quantity('Line.RejectTotal')}</strong></span></div>
    </div>
    <ol className="process-flow">{equipment.map((item, index) => {
      const rawState = readKnownValue(`${item.id}.State`)
      const state = typeof rawState === 'number' ? tagEnums.EquipmentState[String(rawState)] ?? 'UNKNOWN' : 'UNKNOWN'
      return <li key={item.id} data-equipment={item.id} data-state={state}>
        <a className="process-machine" href={`#group-${item.id}`} aria-label={`${item.label} 설비 상세 · ${labels[state]}`}>
          <div className="process-machine-heading"><span className="process-step">0{index + 1}</span><h4>{item.label}</h4><span className="process-state">{labels[state]}</span></div>
          <MachineDrawing kind={item.id} />
          <p>{item.detail}</p>
          <dl>{item.measurements.map(([tag, label, unit]) => {
            const value = readKnownValue(`${item.id}.${tag}`)
            const usable = typeof value === 'number' && Number.isFinite(value) || typeof value === 'string' && /^\d+$(?![\s\S])/.test(value)
            return <div key={tag}><dt>{label}</dt><dd data-process-value={`${item.id}.${tag}`}>{usable ? String(value) : '—'}{usable ? <small> {unit}</small> : <small> 불명</small>}</dd></div>
          })}</dl>
        </a>
      </li>
    })}</ol>
  </section>
}
