import { useCallback, useEffect, useState } from 'react'
import type { Session } from './auth'

interface Control { desired: 'ON' | 'OFF'; run_until: string | null; requested_by: string | null; agent_online: boolean; reported_state: string; reported_detail: string | null }

const stateLabels: Record<string, string> = { OFF: '꺼짐', STARTING: '켜는 중', RUNNING: '동작 중', STOPPING: '끄는 중', ERROR: '오류', UNKNOWN: '설비 호스트 연결 안 됨' }
const PATH = '/api/v1/runtime-control'

function parse(value: unknown): Control {
  const raw = value as Record<string, unknown>
  if (!raw || typeof raw !== 'object' || !['ON', 'OFF'].includes(raw.desired as string) || typeof raw.agent_online !== 'boolean' || typeof raw.reported_state !== 'string') throw new Error('Unconfirmed runtime control')
  return { desired: raw.desired as Control['desired'], run_until: typeof raw.run_until === 'string' ? raw.run_until : null, requested_by: typeof raw.requested_by === 'string' ? raw.requested_by : null, agent_online: raw.agent_online, reported_state: raw.reported_state, reported_detail: typeof raw.reported_detail === 'string' ? raw.reported_detail : null }
}

/** Plant runtime power: everyone sees the state; equipment admins request a bounded start or a stop. */
export default function RuntimePowerPanel({ session, onSessionInvalid }: { session: Session; onSessionInvalid: () => void }) {
  const [control, setControl] = useState<Control | null>(null)
  const [hidden, setHidden] = useState(false)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const canChange = session.roles.includes('equipment_admin') && !session.must_change_password

  const load = useCallback(async (signal?: AbortSignal) => {
    const response = await fetch(PATH, { credentials: 'same-origin', cache: 'no-store', signal })
    if (response.status === 401 || response.status === 403) { onSessionInvalid(); return }
    if (response.status === 404) { setHidden(true); return }
    if (!response.ok) throw new Error('runtime control unavailable')
    setControl(parse(await response.json()))
  }, [onSessionInvalid])

  useEffect(() => {
    const controller = new AbortController()
    const tick = () => { load(controller.signal).catch(() => { if (!controller.signal.aborted) setMessage('설비 전원 상태를 확인하지 못했습니다.') }) }
    tick()
    const timer = window.setInterval(tick, 5000)
    return () => { controller.abort(); window.clearInterval(timer) }
  }, [load])

  async function change(action: 'start' | 'stop') {
    setBusy(true); setMessage('')
    try {
      const response = await fetch(PATH, { method: 'POST', credentials: 'same-origin', cache: 'no-store', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': session.csrf_token }, body: JSON.stringify({ action }), signal: AbortSignal.timeout(12_000) })
      if (response.status === 401 || response.status === 403) { onSessionInvalid(); return }
      if (!response.ok) throw new Error('runtime control rejected')
      setControl(parse(await response.json()))
      setMessage(action === 'start' ? '켜기 요청을 보냈습니다. 설비가 켜지기까지 보통 30초~1분 걸립니다.' : '끄기 요청을 보냈습니다.')
    } catch {
      setMessage('요청 결과를 확인하지 못했습니다. 잠시 뒤 상태를 다시 확인해 주세요.')
    } finally { setBusy(false) }
  }

  if (hidden) return null
  const reported = control?.reported_state ?? 'UNKNOWN'
  const until = control?.run_until ? new Date(control.run_until).toLocaleTimeString('ko-KR', { hour: '2-digit', minute: '2-digit' }) : null
  return <section id="runtime-power" className="runtime-power" aria-labelledby="runtime-power-heading" data-state={reported}>
    <div><p className="section-kicker">PLANT POWER</p><h2 id="runtime-power-heading">설비 전원</h2>
      <p id="runtime-power-state" role="status"><strong>{stateLabels[reported] ?? reported}</strong>{control?.desired === 'ON' && until ? ` · ${until}에 자동으로 꺼집니다` : ''}{reported === 'ERROR' && control?.reported_detail ? ` · ${control.reported_detail}` : ''}</p>
      {!canChange && <p className="form-help">설비 켜기·끄기는 설비 관리자 계정만 할 수 있습니다.</p>}
    </div>
    {canChange && <div className="runtime-power-actions">
      <button id="runtime-power-start" type="button" className="button primary" disabled={busy || control?.desired === 'ON'} onClick={() => void change('start')}>설비 켜기</button>
      <button id="runtime-power-stop" type="button" className="button secondary" disabled={busy || control?.desired !== 'ON'} onClick={() => void change('stop')}>설비 끄기</button>
    </div>}
    {message && <p className="form-help" role="status">{message}</p>}
  </section>
}
