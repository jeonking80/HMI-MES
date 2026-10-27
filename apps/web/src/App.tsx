import { useCallback, useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { AuthError, changePassword, deploymentFeatures, guestLogin, login, logout, noFeatures } from './auth'
import type { Session } from './auth'
import { LineDataProvider } from './line/lineData'
import MonitorPage from './line/MonitorPage'
import ControlPage from './line/ControlPage'
import AlarmsPage from './line/AlarmsPage'
import type { ZoneId } from './line/lineModel'
import MesDraftPanel from './MesDraftPanel'
import type { DraftsTab } from './MesDraftPanel'
import WorkflowTracker from './WorkflowTracker'
import { useWorkOrderWorkflow } from './useWorkOrderWorkflow'
import type { WorkOrder } from './mesApi'
import type { TrackedCommand } from './commands'
import type { AlarmLedger } from './alarmApi'
import CancellationPanel from './CancellationPanel'
import { useCancellation } from './useCancellation'
import { WorkOrderMutationGuard } from './workOrderMutationGuard'
import Sidebar from './Sidebar'
import { buildNavigation, pageDefinition } from './navigation'
import type { WorkspacePage } from './navigation'
import DashboardPage from './analytics/DashboardPage'
import TrendPage from './analytics/TrendPage'
import ReportsPage from './analytics/ReportsPage'
import SpcPage from './analytics/SpcPage'
import AssistantPage from './analytics/AssistantPage'
import type { Source } from './analytics/hooks'

/** A page and the card to bring into view after moving there. */
interface GuideTarget { page: WorkspacePage; anchor: string }

export default function App() {
  // A reload intentionally drops the CSRF token and requires login.
  const [session, setSession] = useState<Session | null>(null)
  const [account, setAccount] = useState('')
  const [password, setPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [changingPassword, setChangingPassword] = useState(false)
  const [workspaceTab, setWorkspaceTab] = useState<WorkspacePage>('operations')
  const [mesOpened, setMesOpened] = useState(false)
  const [analyticsScope, setAnalyticsScope] = useState<Source | null>(null)
  const [busy, setBusy] = useState(false)
  const [features, setFeatures] = useState(noFeatures)
  const [guestMode, setGuestMode] = useState(false)
  const inFlight = useRef(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const heading = useRef<HTMLHeadingElement>(null)
  const [focusTarget, setFocusTarget] = useState<{ anchor: string; at: number } | null>(null)
  const passwordRequired = session?.must_change_password === true
  const showPasswordForm = session !== null && (passwordRequired || changingPassword)
  const screen = !session ? 'login' : showPasswordForm ? 'password' : 'workspace'
  const signedIn = session !== null

  useEffect(() => { document.title = 'HMI-MES · 운영 시스템' }, [])
  useEffect(() => {
    // Re-read on sign-in and sign-out: a deployment can change between visits.
    const controller = new AbortController()
    void deploymentFeatures(controller.signal).then(value => { if (!controller.signal.aborted) setFeatures(value) })
    return () => controller.abort()
  }, [signedIn])
  useEffect(() => { heading.current?.focus() }, [screen, workspaceTab])
  // After a guided move, bring the target card into view and mark it, so the change is visible.
  // Cards that load their data render a moment later; keep looking for a few seconds.
  useEffect(() => {
    if (!focusTarget) return
    let tries = 0
    const timer = window.setInterval(() => {
      const element = document.getElementById(focusTarget.anchor)
      const card = element?.closest('section') ?? element
      if (card && card.offsetParent !== null) {
        window.clearInterval(timer)
        card.scrollIntoView({ behavior: 'smooth', block: 'start' })
        card.classList.remove('guide-focus')
        void card.offsetWidth
        card.classList.add('guide-focus')
        window.setTimeout(() => card.classList.remove('guide-focus'), 2600)
      } else if (++tries > 40) window.clearInterval(timer)
    }, 100)
    return () => window.clearInterval(timer)
  }, [focusTarget])
  // Success notices are transient; errors stay until the next action.
  useEffect(() => {
    if (!notice) return
    const timer = window.setTimeout(() => setNotice(''), 6000)
    return () => window.clearTimeout(timer)
  }, [notice])
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
  const [controlZone, setControlZone] = useState<ZoneId | 'Line'>('Line')
  const [draftsTab, setDraftsTab] = useState<DraftsTab>('recipes')
  const [tracked, setTracked] = useState<TrackedCommand | null>(null)
  const [alarmLedger, setAlarmLedger] = useState<AlarmLedger | null>(null)
  const [workOrderGuard] = useState(() => new WorkOrderMutationGuard())
  const workflow = useWorkOrderWorkflow(session, invalidateSession, () => setSelectedOrder(null), workOrderGuard, id => setNotice(`작업지시 ${id}를 배포했습니다. 운전 제어에서 불러올 수 있습니다.`))
  const knownLinked = (id: string) => (workflow.attempt?.record !== null && workflow.attempt?.kind === 'bind' && workflow.attempt.record?.intent.work_order_id === id)
    || workflow.history.some(item => item.attempt.intent.work_order_id === id)
  const cancellation = useCancellation(session, invalidateSession, workOrderGuard, knownLinked, () => { setMesOpened(true); setDraftsTab('orders'); setWorkspaceTab('orders') }, id => setNotice(`작업지시 ${id}를 취소했습니다. 취소 기록은 작업지시의 취소 탭에 남습니다.`))

  async function run(action: 'login' | 'guest' | 'password' | 'logout') {
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
        setGuestMode(false)
      } else if (action === 'guest') {
        setSession(await guestLogin())
        setChangingPassword(false)
        setWorkspaceTab('operations')
        setMesOpened(false)
        setGuestMode(true)
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

  function navigate(page: WorkspacePage) {
    if (page === 'drafts' || page === 'orders') setMesOpened(true)
    if (page === 'drafts' && draftsTab !== 'items') setDraftsTab('recipes')
    if (page === 'orders' && (draftsTab === 'recipes' || draftsTab === 'items')) setDraftsTab('orders')
    setWorkspaceTab(page)
  }
  function go(target: GuideTarget) {
    navigate(target.page)
    if (target.anchor === 'mes-orders') setDraftsTab('orders')
    setFocusTarget({ anchor: target.anchor, at: Date.now() })
  }

  if (session && screen === 'workspace') {
    const current = pageDefinition(workspaceTab)
    const navigation = buildNavigation(features)
    return (
      <div className="app-shell workspace-shell">
        <a className="skip-link" href="#main-content">본문으로 이동</a>
        <Sidebar
          session={session}
          navigation={navigation}
          page={workspaceTab}
          busy={busy}
          onNavigate={navigate}
          onChangePassword={guestMode ? null : () => { setChangingPassword(true); setError(''); setNotice('') }}
          onLogout={() => void run('logout')}
        />
        <main id="main-content" className="workspace">
          <header className="page-header">
            <p className="section-kicker">{current.kicker}</p>
            <h1 ref={heading} tabIndex={-1}>{current.title}</h1>
            <p className="intro">{current.description}</p>
          </header>
          {guestMode && <div className="message guest-banner" role="status">게스트 조회 모드입니다. 화면을 둘러볼 수 있지만 운전·등록 요청은 서버가 거절합니다. 세션은 30분 뒤 종료됩니다.</div>}
          {error && <div className="message error" role="alert">{error}</div>}
          {notice && <div className="message success" role="status">{notice}</div>}
          <LineDataProvider session={session} onSessionInvalid={invalidateSession}>
            {/* One line page at a time: each owns its command tracker, so two never poll the same command. */}
            {workspaceTab === 'operations' && <MonitorPage session={session} onSessionInvalid={invalidateSession} workflow={workflow} tracked={tracked} setTracked={setTracked} onOpenControl={zone => { setControlZone(zone); navigate('control') }} onOpenAlarms={() => navigate('alarms')} />}
            {workspaceTab === 'control' && <ControlPage session={session} onSessionInvalid={invalidateSession} workflow={workflow} tracked={tracked} setTracked={setTracked} zone={controlZone} onZone={setControlZone} selectedOrder={selectedOrder} onSelectOrder={setSelectedOrder} onOpenMonitor={() => navigate('operations')} onOpenOrders={() => { setDraftsTab('orders'); navigate('orders') }} onOpenReports={() => navigate('reports')} />}
            {workspaceTab === 'alarms' && <AlarmsPage session={session} onSessionInvalid={invalidateSession} ledger={alarmLedger} setLedger={setAlarmLedger} />}
          </LineDataProvider>
          {mesOpened && <div hidden={workspaceTab !== 'drafts' && workspaceTab !== 'orders'}><MesDraftPanel onOpenControl={() => navigate('control')} onOpenReports={() => navigate('reports')} session={session} onSessionInvalid={invalidateSession} workflow={workflow} cancellation={cancellation} tab={draftsTab} onTab={tab => { setDraftsTab(tab); setWorkspaceTab(tab === 'recipes' || tab === 'items' ? 'drafts' : 'orders') }} onSelectOrder={order => { setSelectedOrder(order); setNotice(`작업지시 ${order.id}를 골랐습니다. ② 작업 준비에서 "불러오기"를 누르세요.`); go({ page: 'control', anchor: 'work-order-binding' }) }} /></div>}
          {workspaceTab === 'dashboard' && <DashboardPage scope={analyticsScope} onScope={setAnalyticsScope} onSessionInvalid={invalidateSession} />}
          {workspaceTab === 'trends' && <TrendPage scope={analyticsScope} onScope={setAnalyticsScope} onSessionInvalid={invalidateSession} />}
          {workspaceTab === 'reports' && <ReportsPage session={session} scope={analyticsScope} onScope={setAnalyticsScope} onSessionInvalid={invalidateSession} />}
          {/* Kept mounted (hidden) so the conversation survives switching pages. */}
          {features.assistant && <div hidden={workspaceTab !== 'assistant'}><AssistantPage session={session} scope={analyticsScope} onScope={setAnalyticsScope} onSessionInvalid={invalidateSession} /></div>}
          {workspaceTab === 'spc' && <SpcPage scope={analyticsScope} onScope={setAnalyticsScope} onSessionInvalid={invalidateSession} />}
          {/* Work-order request ledgers belong to the operations and work-order pages; hidden (not unmounted) elsewhere to keep their state. */}
          {/* Folded unless a request is open, so an unresolved original ID is never hidden. */}
          {((workflow.attempt && workflow.attempt.phase !== 'confirmed') || cancellation.ledger) && <details className="lc-card lc-ledgers" hidden={workspaceTab !== 'control' && workspaceTab !== 'orders'} open>
            <summary>확인이 필요한 요청<span>응답을 확인하지 못한 요청입니다. 원래 요청 기록을 조회해 결과를 확인하세요.</span></summary>
            {workflow.attempt && workflow.attempt.phase !== 'confirmed' && <WorkflowTracker workflow={workflow} login={session.login} onSessionInvalid={invalidateSession} />}
            {cancellation.ledger && <CancellationPanel cancellation={cancellation} login={session.login} />}
          </details>}
          <p id="session-reset" className="session-note">페이지를 새로고침하거나 다시 열면 재로그인이 필요합니다.</p>
        </main>
      </div>
    )
  }

  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">본문으로 이동</a>
      <header className="topbar">
        <div className="brand"><span className="brand-mark" aria-hidden="true">H</span><span>HMI<span className="brand-separator"> / </span>MES</span></div>
        <span className="app-label">앱 A · 포장라인 운영</span>
      </header>
      <main id="main-content" className="auth-layout">
        <aside className="welcome" aria-label="시스템 안내">
          <p className="eyebrow">PACKAGING LINE OPERATIONS</p>
          <h2>현장을 확인하고,<br />생산을 이어갑니다.</h2>
          <p>포장라인의 설비 상태와 생산 업무를<br className="wide-break" /> 한 곳에서 확인하는 운영 시스템입니다.</p>
          <div className="line-art" aria-hidden="true"><span /><i /><span /><i /><span /></div>
          <div className="welcome-foot"><span>HMI · 설비 모니터링</span><span>MES · 생산 관리</span></div>
        </aside>
        <div className="auth-card">
          <div className="section-kicker">ACCOUNT ACCESS</div>
          <div className="page-heading">
            <div>
              <h1 ref={heading} tabIndex={-1}>{!session ? '운영 시스템 로그인' : '비밀번호 변경'}</h1>
              <p className="intro">{!session ? '발급받은 앱 A 계정으로 로그인해 주세요.' : passwordRequired ? '초기 비밀번호를 변경한 뒤 업무를 시작해 주세요.' : '새 비밀번호를 설정해 주세요.'}</p>
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
            {features.guest && <div className="guest-entry">
              <span>또는</span>
              <button id="guest-login" className="button secondary full-width" type="button" disabled={busy} onClick={() => void run('guest')}>로그인 없이 둘러보기 (조회 전용)</button>
              <p className="form-help">운전·등록 같은 변경은 할 수 없으며 30분 뒤 자동으로 종료됩니다.</p>
            </div>}
          </form> : <>
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
          </>}
          <p id="session-reset" className="session-note">페이지를 새로고침하거나 다시 열면 재로그인이 필요합니다.</p>
        </div>
      </main>
      <footer className="footer"><span>HMI-MES · 앱 A</span><span>별도 설비 앱 B의 계정은 사용할 수 없습니다.</span></footer>
    </div>
  )
}
