import { useCallback, useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { AuthError, changePassword, login, logout } from './auth'
import type { Session } from './auth'
import SnapshotPanel from './SnapshotPanel'
import MesDraftPanel from './MesDraftPanel'
import WorkflowTracker from './WorkflowTracker'
import { useWorkOrderWorkflow } from './useWorkOrderWorkflow'
import type { WorkOrder } from './mesApi'
import type { TrackedCommand } from './commands'
import type { AlarmLedger } from './alarmApi'
import CancellationPanel from './CancellationPanel'
import { useCancellation } from './useCancellation'
import { WorkOrderMutationGuard } from './workOrderMutationGuard'

const roleLabels: Record<string, string> = {
  viewer: '조회 사용자', operator: '작업자', production_manager: '생산 담당자', equipment_admin: '설비 관리자',
}

export default function App() {
  // A reload intentionally drops the CSRF token and requires login.
  const [session, setSession] = useState<Session | null>(null)
  const [account, setAccount] = useState('')
  const [password, setPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [changingPassword, setChangingPassword] = useState(false)
  const [workspaceTab, setWorkspaceTab] = useState<'operations' | 'drafts'>('operations')
  const [mesOpened, setMesOpened] = useState(false)
  const [busy, setBusy] = useState(false)
  const inFlight = useRef(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const heading = useRef<HTMLHeadingElement>(null)
  const passwordRequired = session?.must_change_password === true
  const showPasswordForm = session !== null && (passwordRequired || changingPassword)
  const screen = !session ? 'login' : showPasswordForm ? 'password' : 'workspace'

  useEffect(() => { document.title = 'HMI-MES · 운영 시스템' }, [])
  useEffect(() => { heading.current?.focus() }, [screen])
  useEffect(() => {
    if (!session) return
    const timer = window.setTimeout(() => {
      setSession(null)
      setNewPassword('')
      setConfirmation('')
      setChangingPassword(false)
      setNotice('세션 유효 시간이 지났습니다. 다시 로그인해 주세요.')
    }, Math.max(0, Date.parse(session.expires_at) - Date.now()))
    return () => window.clearTimeout(timer)
  }, [session])

  const invalidateSession = useCallback(() => {
    setSession(null)
    setChangingPassword(false)
    setError('세션이 만료되었거나 조회 권한이 변경되었습니다. 다시 로그인해 주세요.')
  }, [])
  const [selectedOrder, setSelectedOrder] = useState<WorkOrder | null>(null)
  const [tracked, setTracked] = useState<TrackedCommand | null>(null)
  const [alarmLedger, setAlarmLedger] = useState<AlarmLedger | null>(null)
  const [workOrderGuard] = useState(() => new WorkOrderMutationGuard())
  const workflow = useWorkOrderWorkflow(session, invalidateSession, () => setSelectedOrder(null), workOrderGuard)
  const knownLinked = (id: string) => (workflow.attempt?.record !== null && workflow.attempt?.kind === 'bind' && workflow.attempt.record?.intent.work_order_id === id)
    || workflow.history.some(item => item.attempt.intent.work_order_id === id)
  const cancellation = useCancellation(session, invalidateSession, workOrderGuard, knownLinked, () => { setMesOpened(true); setWorkspaceTab('drafts') })

  async function run(action: 'login' | 'password' | 'logout') {
    if (inFlight.current) return
    inFlight.current = true
    setBusy(true)
    setError('')
    setNotice('')
    try {
      if (action === 'login') {
        setSession(await login(account, password))
        setChangingPassword(false)
        setWorkspaceTab('operations')
        setMesOpened(false)
      } else if (session && action === 'password') {
        setSession(await changePassword(session, newPassword))
        setChangingPassword(false)
        setNotice('비밀번호를 변경했습니다. 다른 기존 세션은 해제되었습니다.')
      } else if (session && action === 'logout') {
        await logout(session)
        setSession(null)
        setChangingPassword(false)
        setNotice('로그아웃했습니다.')
      }
    } catch (cause) {
      const failure = cause instanceof AuthError ? cause : new AuthError(0, '요청 결과를 확인할 수 없습니다.')
      if (session && (failure.status === 401 || failure.status === 403 || (action === 'password' && failure.status === 0))) {
        setSession(null)
        setChangingPassword(false)
      }
      setError(action === 'password' && failure.status === 0
        ? '비밀번호 변경 결과를 확인할 수 없습니다. 변경을 요청한 비밀번호로 다시 로그인해 확인해 주세요.'
        : action === 'logout' && failure.status === 0
          ? '로그아웃 결과를 확인할 수 없습니다. 연결을 확인한 뒤 로그아웃을 다시 시도해 주세요.'
          : failure.message)
    } finally {
      setPassword('')
      setNewPassword('')
      setConfirmation('')
      setBusy(false)
      inFlight.current = false
    }
  }

  function submitLogin(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    void run('login')
  }
  function submitPassword(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const size = new TextEncoder().encode(newPassword).length
    if (size < 12 || size > 1024 || newPassword.includes('\0')) {
      setError('새 비밀번호는 UTF-8 기준 12~1,024바이트이며 NUL 문자를 포함할 수 없습니다.')
      return
    }
    if (newPassword !== confirmation) {
      setError('새 비밀번호와 확인 입력이 일치하지 않습니다.')
      return
    }
    void run('password')
  }

  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">본문으로 이동</a>
      <header className="topbar">
        <div className="brand"><span className="brand-mark" aria-hidden="true">H</span><span>HMI<span className="brand-separator"> / </span>MES</span></div>
        <span className="app-label">앱 A · 포장라인 운영</span>
      </header>
      <main id="main-content" className={screen === 'workspace' ? 'workspace' : 'auth-layout'}>
        {screen !== 'workspace' && <aside className="welcome" aria-label="시스템 안내">
          <p className="eyebrow">PACKAGING LINE OPERATIONS</p>
          <h2>현장을 확인하고,<br />생산을 이어갑니다.</h2>
          <p>포장라인의 설비 상태와 생산 업무를<br className="wide-break" /> 한 곳에서 확인하는 운영 시스템입니다.</p>
          <div className="line-art" aria-hidden="true"><span /><i /><span /><i /><span /></div>
          <div className="welcome-foot"><span>HMI · 설비 모니터링</span><span>MES · 생산 관리</span></div>
        </aside>}
        <div className={screen === 'workspace' ? 'workspace-content' : 'auth-card'}>
          <div className="section-kicker">{screen === 'workspace' ? 'OPERATOR WORKSPACE' : 'ACCOUNT ACCESS'}</div>
          <div className="page-heading">
            <div>
              <h1 ref={heading} tabIndex={-1}>{!session ? '운영 시스템 로그인' : showPasswordForm ? '비밀번호 변경' : '운영 작업 공간'}</h1>
              <p className="intro">{!session ? '발급받은 앱 A 계정으로 로그인해 주세요.' : showPasswordForm ? (passwordRequired ? '초기 비밀번호를 변경한 뒤 업무를 시작해 주세요.' : '새 비밀번호를 설정해 주세요.') : '인증된 계정과 현재 연결 범위를 확인하세요.'}</p>
            </div>
            {session && <button id="logout" type="button" className="button secondary" disabled={busy} onClick={() => void run('logout')}>{busy ? '처리 중…' : '로그아웃'}</button>}
          </div>
          {error && <div className="message error" role="alert">{error}</div>}
          {notice && <div className="message success" role="status">{notice}</div>}
          {!session ? <form id="login-form" onSubmit={submitLogin} aria-busy={busy}>
            <label htmlFor="login">계정</label>
            <input id="login" name="username" autoComplete="username" autoCapitalize="none" spellCheck={false} required maxLength={64} value={account} disabled={busy} onChange={event => setAccount(event.target.value)} />
            <label htmlFor="login-password">비밀번호</label>
            <input id="login-password" name="password" type="password" autoComplete="current-password" required value={password} disabled={busy} onChange={event => setPassword(event.target.value)} />
            <button id="login-submit" className="button primary full-width" type="submit" disabled={busy}>{busy ? '로그인 확인 중…' : '로그인'}</button>
            <p className="form-help">계정 발급이나 비밀번호 초기화는 설비 관리자에게 문의해 주세요.</p>
          </form> : showPasswordForm ? <>
            <div className="account-summary"><span>로그인 계정</span><strong>{session.login}</strong></div>
            <form id="password-form" onSubmit={submitPassword} aria-busy={busy}>
              <label htmlFor="new-password">새 비밀번호</label>
              <input id="new-password" name="new-password" type="password" autoComplete="new-password" required aria-describedby="password-policy" value={newPassword} disabled={busy} onChange={event => setNewPassword(event.target.value)} />
              <p className="input-help" id="password-policy">현재와 다른 비밀번호 · UTF-8 기준 12~1,024바이트<br />영문·숫자는 보통 1자당 1바이트, 한글은 3바이트입니다.</p>
              <label htmlFor="confirm-password">새 비밀번호 확인</label>
              <input id="confirm-password" name="confirm-password" type="password" autoComplete="new-password" required value={confirmation} disabled={busy} onChange={event => setConfirmation(event.target.value)} />
              <button id="password-submit" className="button primary full-width" type="submit" disabled={busy}>{busy ? '변경 확인 중…' : '비밀번호 변경'}</button>
              {!passwordRequired && <button className="button secondary full-width" type="button" disabled={busy} onClick={() => { setChangingPassword(false); setNewPassword(''); setConfirmation(''); setError('') }}>취소</button>}
              <p className="form-help">변경하면 다른 기기의 기존 로그인 세션도 해제됩니다.</p>
            </form>
          </> : <>
            <section className="identity-card" aria-labelledby="current-login">
              <div className="identity-avatar" aria-hidden="true">{session.login.charAt(0).toUpperCase()}</div>
              <div className="identity-detail"><h2 id="current-login">{session.login}</h2><p>서버가 확인한 계정 · 로그인 응답 기준</p><div id="current-roles" className="role-list">{session.roles.map(role => <span className="role" key={role}>{roleLabels[role] ?? role}</span>)}</div></div>
              <button className="button secondary" type="button" disabled={busy} onClick={() => { setChangingPassword(true); setError(''); setNotice('') }}>비밀번호 변경</button>
            </section>
            <nav className="workspace-tabs" aria-label="업무 화면">
              <button id="workspace-operations" className={workspaceTab === 'operations' ? 'is-active' : ''} type="button" aria-pressed={workspaceTab === 'operations'} onClick={() => setWorkspaceTab('operations')}>설비 운영</button>
              <button id="workspace-drafts" className={workspaceTab === 'drafts' ? 'is-active' : ''} type="button" aria-pressed={workspaceTab === 'drafts'} onClick={() => { setMesOpened(true); setWorkspaceTab('drafts') }}>작업지시 · 공정 설정</button>
            </nav>
            <div hidden={workspaceTab !== 'operations'}><SnapshotPanel session={session} onSessionInvalid={invalidateSession} workflow={workflow} selectedOrder={selectedOrder} tracked={tracked} setTracked={setTracked} alarmLedger={alarmLedger} setAlarmLedger={setAlarmLedger} /></div>
            {mesOpened && <div hidden={workspaceTab !== 'drafts'}><MesDraftPanel session={session} onSessionInvalid={invalidateSession} workflow={workflow} cancellation={cancellation} onSelectOrder={order => { setSelectedOrder(order); setWorkspaceTab('operations') }} /></div>}
            <WorkflowTracker workflow={workflow} login={session.login} onSessionInvalid={invalidateSession} />
            <CancellationPanel cancellation={cancellation} login={session.login} />
          </>}
          <p id="session-reset" className="session-note">페이지를 새로고침하거나 다시 열면 재로그인이 필요합니다.</p>
        </div>
      </main>
      <footer className="footer"><span>HMI-MES · 앱 A</span><span>별도 설비 앱 B의 계정은 사용할 수 없습니다.</span></footer>
    </div>
  )
}
