/**
 * Reporting pages call the CX-01~CX-05 server APIs. The server says whether they are available
 * (`/auth/config.analytics`, CX-10); until a server reports it, the build flag
 * VITE_ANALYTICS_PAGES=enabled decides.
 */
const analyticsBuildFlag = import.meta.env.VITE_ANALYTICS_PAGES === 'enabled'

export type WorkspacePage = 'dashboard' | 'operations' | 'trends' | 'drafts' | 'reports' | 'spc' | 'assistant'

export interface NavigationFeatures { analytics: boolean | null; assistant: boolean }

export interface PageDefinition {
  page: WorkspacePage
  /** Stable DOM id; browser checks select existing pages by these ids. */
  id: string
  label: string
  title: string
  kicker: string
  description: string
  icon: 'dashboard' | 'line' | 'trend' | 'orders' | 'report' | 'spc' | 'assistant'
  /** Pages whose server API is not available stay visible but disabled. */
  available: boolean
}

export interface NavigationGroup { label: string; pages: PageDefinition[] }

export function buildNavigation(features: NavigationFeatures): NavigationGroup[] {
  const analyticsEnabled = features.analytics ?? analyticsBuildFlag
  return [
    {
      label: '모니터링',
      pages: [
        { page: 'dashboard', id: 'workspace-dashboard', label: '대시보드', title: '생산 대시보드', kicker: 'OVERVIEW', description: '오늘의 생산·가동·알람 현황을 한눈에 확인합니다.', icon: 'dashboard', available: analyticsEnabled },
        { page: 'operations', id: 'workspace-operations', label: '라인 모니터링', title: '라인 모니터링', kicker: 'LINE MONITORING', description: '설비 상태와 측정값을 확인하고 운전을 요청합니다. 실행 여부는 PLC가 최종 판정합니다.', icon: 'line', available: true },
        { page: 'trends', id: 'workspace-trends', label: '트렌드', title: '설비 트렌드', kicker: 'TREND', description: '아날로그 측정값의 기간 추세와 데이터 누락 구간을 확인합니다.', icon: 'trend', available: analyticsEnabled },
        { page: 'assistant', id: 'workspace-assistant', label: '운영 도우미', title: '운영 도우미', kicker: 'ASSISTANT', description: '라인 데이터를 질문으로 조회합니다. 조회만 할 수 있고 운전·등록은 하지 않습니다.', icon: 'assistant', available: features.assistant },
      ],
    },
    {
      label: '생산 관리',
      pages: [
        { page: 'drafts', id: 'workspace-drafts', label: '작업지시 · 공정 설정', title: '작업지시 · 공정 설정', kicker: 'PRODUCTION PLANNING', description: '공정 설정 버전과 작업지시를 등록하고 라인에 배포합니다.', icon: 'orders', available: true },
        { page: 'reports', id: 'workspace-reports', label: '실적 · 보고서', title: '실적 · 보고서', kicker: 'REPORTING', description: '작업별 실적, 가동·정지·불명 시간과 정지 사유를 확인합니다.', icon: 'report', available: analyticsEnabled },
      ],
    },
    {
      label: '품질',
      pages: [
        { page: 'spc', id: 'workspace-spc', label: 'SPC 관리도', title: 'SPC 관리도', kicker: 'QUALITY', description: '실링 온도·압력의 관리 상태와 공정능력을 통계적으로 확인합니다.', icon: 'spc', available: analyticsEnabled },
      ],
    },
  ]
}

/** Static page metadata; availability is decided per deployment by buildNavigation. */
export const pages: PageDefinition[] = buildNavigation({ analytics: true, assistant: true }).flatMap(group => group.pages)

export function pageDefinition(page: WorkspacePage): PageDefinition {
  const found = pages.find(item => item.page === page)
  if (!found) throw new Error('unknown workspace page')
  return found
}
