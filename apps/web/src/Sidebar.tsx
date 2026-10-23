import type { ReactNode } from 'react'
import type { Session } from './auth'
import { navigation } from './navigation'
import type { PageDefinition, WorkspacePage } from './navigation'

const roleLabels: Record<string, string> = {
  viewer: '조회 사용자', operator: '작업자', production_manager: '생산 담당자', equipment_admin: '설비 관리자',
}

const iconPaths: Record<PageDefinition['icon'], ReactNode> = {
  dashboard: <><rect x="3" y="3" width="7" height="9" rx="1.5" /><rect x="14" y="3" width="7" height="5" rx="1.5" /><rect x="14" y="12" width="7" height="9" rx="1.5" /><rect x="3" y="16" width="7" height="5" rx="1.5" /></>,
  line: <><rect x="2.5" y="9" width="5" height="6" rx="1" /><rect x="9.5" y="9" width="5" height="6" rx="1" /><rect x="16.5" y="9" width="5" height="6" rx="1" /><path d="M2 19h20" /></>,
  trend: <><path d="M3 20h18" /><path d="M4 15l5-5 4 3 7-8" /></>,
  orders: <><rect x="5" y="3" width="14" height="18" rx="2" /><path d="M9 8h6M9 12h6M9 16h4" /></>,
  report: <><path d="M4 20V10M10 20V4M16 20v-7M22 20H2" /></>,
  spc: <><path d="M3 6h18M3 18h18" strokeDasharray="2 2" /><path d="M3 12l4-2 3 3 4-4 3 2 4-1" /></>,
}

function NavIcon({ icon }: { icon: PageDefinition['icon'] }) {
  return <svg className="nav-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">{iconPaths[icon]}</svg>
}

interface SidebarProps {
  session: Session
  page: WorkspacePage
  busy: boolean
  onNavigate: (page: WorkspacePage) => void
  /** Null hides the action, e.g. for the guest session that cannot change a password. */
  onChangePassword: (() => void) | null
  onLogout: () => void
}

export default function Sidebar({ session, page, busy, onNavigate, onChangePassword, onLogout }: SidebarProps) {
  return <aside className="sidebar" aria-label="업무 메뉴">
    <div className="sidebar-brand">
      <span className="brand-mark" aria-hidden="true">H</span>
      <span className="brand-text"><strong>HMI<span className="brand-separator"> / </span>MES</strong><small>포장라인 1 · 앱 A</small></span>
    </div>
    <nav className="sidebar-nav" aria-label="업무 화면">
      {navigation.map(group => <div className="nav-group" key={group.label}>
        <p className="nav-group-label">{group.label}</p>
        <ul>
          {group.pages.map(item => <li key={item.page}>
            <button
              id={item.id}
              type="button"
              className={`nav-item${page === item.page ? ' is-active' : ''}`}
              aria-pressed={page === item.page}
              aria-current={page === item.page ? 'page' : undefined}
              disabled={!item.available}
              title={item.available ? undefined : '서버 API 연동 후 제공됩니다.'}
              onClick={() => onNavigate(item.page)}
            >
              <NavIcon icon={item.icon} />
              <span className="nav-label">{item.label}</span>
              {!item.available && <span className="nav-badge">예정</span>}
            </button>
          </li>)}
        </ul>
      </div>)}
    </nav>
    <section className="sidebar-user" aria-labelledby="current-login">
      <div className="sidebar-user-head">
        <div className="identity-avatar" aria-hidden="true">{session.login.charAt(0).toUpperCase()}</div>
        <div className="identity-detail">
          <h2 id="current-login">{session.login}</h2>
          <div id="current-roles" className="role-list">{session.roles.map(role => <span className="role" key={role}>{roleLabels[role] ?? role}</span>)}</div>
        </div>
      </div>
      <div className="sidebar-user-actions">
        {onChangePassword && <button className="button ghost" type="button" disabled={busy} onClick={onChangePassword}>비밀번호 변경</button>}
        <button id="logout" type="button" className="button ghost" disabled={busy} onClick={onLogout}>{busy ? '처리 중…' : '로그아웃'}</button>
      </div>
    </section>
  </aside>
}
