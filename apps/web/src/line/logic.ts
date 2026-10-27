import { useEffect, useState } from 'react'
import type { WorkOrderWorkflow } from '../useWorkOrderWorkflow'
import type { LiveLine } from './lineState'
import { count, stateOf } from './lineModel'
import type { ZoneId } from './lineModel'

/** Visual tone per equipment state; UNKNOWN never looks like normal. */
export const toneOf = (state: string) => ({ RUNNING: 'run', READY: 'ready', STOPPED: 'stop', PAUSED: 'warn', STOPPING: 'warn', FAULT: 'bad', ESTOP: 'bad', RECOVERY_REQUIRED: 'bad' } as Record<string, string>)[state] ?? 'unknown'

/** Things that need attention now, read from the live tags. Unknown data is itself an issue. */
export function issuesOf(live: LiveLine): { key: string; zone: ZoneId | 'Line'; text: string }[] {
  const known = live.known
  const list: { key: string; zone: ZoneId | 'Line'; text: string }[] = []
  if (live.heartbeat !== 'GOOD') list.push({ key: 'link', zone: 'Line', text: `PLC 통신 ${live.heartbeatLabel}` })
  if (live.healthTone !== 'good') list.push({ key: 'health', zone: 'Line', text: `데이터 ${live.health}` })
  const line = stateOf(known, 'Line')
  if (line === 'ESTOP' || known('Line.EStop') === true) list.push({ key: 'estop', zone: 'Line', text: '비상정지 작동' })
  if (line === 'FAULT') list.push({ key: 'fault', zone: 'Line', text: `라인 고장 · 코드 ${String(known('Line.FaultCode') ?? '?')}` })
  if (line === 'RECOVERY_REQUIRED') list.push({ key: 'recovery', zone: 'Line', text: '복구 확인 필요' })
  for (const zone of ['Infeed', 'Sealer', 'Outfeed'] as ZoneId[]) {
    const code = known(`${zone}.FaultCode`)
    if (typeof code === 'number' && code !== 0) list.push({ key: `${zone}-fault`, zone, text: `${({ Infeed: '투입', Sealer: '실링', Outfeed: '배출' })[zone]} 고장 · 코드 ${code}` })
  }
  if (known('Line.GuardClosed') === false || known('Sealer.GuardClosed') === false) list.push({ key: 'guard', zone: 'Sealer', text: '안전문 열림' })
  return list
}

/** Progress toward the loaded job's good-quantity target. */
export function production(known: LiveLine['known']) {
  const good = count(known, 'Line.GoodTotal'), target = count(known, 'Line.TargetQuantity')
  return { good, target, reject: count(known, 'Line.RejectTotal'), wip: count(known, 'Line.WipCount'), ratio: good !== null && target ? Math.min(1, good / target) : null }
}

/** Plant runtime power as reported by the host agent (404: not managed here). */
export function usePower(): { state: string | null; until: string | null } {
  const [power, setPower] = useState<{ state: string | null; until: string | null }>({ state: null, until: null })
  useEffect(() => {
    const controller = new AbortController()
    const tick = () => fetch('/api/v1/runtime-control', { credentials: 'same-origin', cache: 'no-store', signal: controller.signal }).then(async response => {
      if (response.status === 404) { setPower({ state: 'NOT_MANAGED', until: null }); return }
      if (!response.ok) return
      const body = await response.json() as { reported_state?: unknown; run_until?: unknown }
      setPower({ state: typeof body.reported_state === 'string' ? body.reported_state : null, until: typeof body.run_until === 'string' ? body.run_until : null })
    }).catch(() => undefined)
    void tick()
    const timer = window.setInterval(() => void tick(), 9000)
    return () => { controller.abort(); window.clearInterval(timer) }
  }, [])
  return power
}
export const powerLabels: Record<string, string> = { OFF: '설비 전원 꺼짐', STARTING: '설비 켜는 중', RUNNING: '설비 전원 켜짐', STOPPING: '설비 끄는 중', ERROR: '설비 전원 오류', UNKNOWN: '설비 호스트 연결 안 됨' }

/** Blocks a new start/reset while an earlier work-order request is still unresolved. */
export function newIntentBlocked(workflow: WorkOrderWorkflow, values: Record<string, unknown>): string {
  const attempt = workflow.attempt
  return attempt && (!attempt.record || (attempt.kind === 'bind' && (attempt.loadResult?.status !== 'SUCCEEDED' || !('execution_id' in attempt.record.receipt) || attempt.record.receipt.execution_id !== values['Line.ActiveExecutionId'])))
    ? '작업지시 요청과 원래 작업 불러오기 결과를 먼저 확인해 주세요.' : ''
}

