/** Where the line is in the job cycle and which request comes next, from the latest PLC values. */
import type { CommandKind } from './commands'

export const STEPS = ['작업 연결', '예열 · 준비', '운전', '종료 · 정산']
const count = (value: unknown) => typeof value === 'number' ? value : typeof value === 'string' && /^\d+$/.test(value) ? Number(value) : null

/** One line of guidance per PLC state: where the line is and which request comes next. */
export function stepGuide(values: Record<string, unknown>): { step: number; tone: 'info' | 'good' | 'warn' | 'bad'; title: string; next: string; recommended: CommandKind | null; progress: number | null } {
  const state = Number(values['Line.State'])
  const good = count(values['Line.GoodTotal'])
  const target = count(values['Line.TargetQuantity'])
  const progress = good !== null && target ? good / target : null
  const amount = good !== null && target ? `양품 ${good.toLocaleString('ko-KR')} / 목표 ${target.toLocaleString('ko-KR')}개.` : ''
  const pv = Number(values['Sealer.TemperaturePV']), sp = Number(values['Sealer.TemperatureSP'])
  const heat = Number.isFinite(pv) && Number.isFinite(sp) && sp > 0 ? ` 현재 실링 온도 ${pv.toFixed(1)} / 설정 ${sp.toFixed(0)} ℃.` : ''
  switch (state) {
    case 0: return { step: 0, tone: 'info', title: '정지 상태입니다.', next: `새 작업은 작업지시 화면에서 배포한 뒤 실행 연결로 불러옵니다. 방금 작업을 불러왔다면 예열이 끝나 준비 상태가 될 때까지 기다립니다.${heat}`, recommended: null, progress: null }
    case 1: return { step: 1, tone: 'good', title: '준비 완료입니다.', next: '운전 시작 · 재개를 누르면 생산을 시작합니다.', recommended: 'START', progress }
    case 2: return { step: 2, tone: 'good', title: '운전 중입니다.', next: `${amount} 실행 연결 때 자동 종료를 켰다면 목표에 닿을 때 PLC가 스스로 끝냅니다. 직접 끝내려면 현재 작업 종료를 누릅니다.`, recommended: progress !== null && progress >= 1 ? 'END_RUN' : null, progress }
    case 3: return { step: 2, tone: 'warn', title: '일시 정지 상태입니다.', next: `${amount} 운전 시작 · 재개로 이어서 생산하거나 현재 작업 종료로 끝냅니다.`, recommended: 'START', progress }
    case 4: return { step: 3, tone: 'info', title: '종료 중입니다.', next: '설비가 재공을 정리하고 있습니다. 정지가 확인되면 실적 정산을 확인합니다.', recommended: null, progress }
    case 5: return { step: 2, tone: 'bad', title: `고장 상태입니다 (코드 ${String(values['Line.FaultCode'] ?? '?')}).`, next: '현장에서 원인을 해소한 뒤 고장 리셋을 누릅니다.', recommended: 'RESET_FAULT', progress }
    case 6: return { step: 2, tone: 'bad', title: '비상정지 상태입니다.', next: '비상정지는 원격으로 해제할 수 없습니다. 현장에서 안전을 확인하고 해제해야 합니다.', recommended: null, progress }
    case 7: return { step: 0, tone: 'warn', title: '복구 확인이 필요합니다.', next: '설비 재시작 뒤 현장의 복구 확인이 끝나야 원격 요청을 받을 수 있습니다.', recommended: null, progress: null }
    default: return { step: 0, tone: 'warn', title: '설비 상태를 확인할 수 없습니다.', next: '수집과 통신 상태를 먼저 확인합니다.', recommended: null, progress: null }
  }
}
