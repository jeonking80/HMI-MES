/**
 * Reporting pages call the CX-01~CX-05 server APIs. The server says whether they are available
 * (`/auth/config.analytics`, CX-10); until a server reports it, the build flag
 * VITE_ANALYTICS_PAGES=enabled decides.
 */
const analyticsBuildFlag = import.meta.env.VITE_ANALYTICS_PAGES === 'enabled'

export type WorkspacePage = 'dashboard' | 'operations' | 'control' | 'alarms' | 'trends' | 'orders' | 'drafts' | 'reports' | 'spc' | 'assistant'

export interface NavigationFeatures { analytics: boolean | null; assistant: boolean }

export interface PageDefinition {
  page: WorkspacePage
  /** Stable DOM id; browser checks select existing pages by these ids. */
  id: string
  label: string
  title: string
  kicker: string
  description: string
  icon: 'dashboard' | 'line' | 'trend' | 'orders' | 'recipe' | 'report' | 'spc' | 'assistant' | 'control' | 'alarm'
  /** Pages whose server API is not available stay visible but disabled. */
  available: boolean
}

export interface NavigationGroup { label: string; pages: PageDefinition[] }

export function buildNavigation(features: NavigationFeatures): NavigationGroup[] {
  const analyticsEnabled = features.analytics ?? analyticsBuildFlag
  return [
    {
      label: '감시',
      pages: [
        { page: 'dashboard', id: 'workspace-dashboard', label: '대시보드', title: '생산 대시보드', kicker: 'OVERVIEW', description: '오늘의 생산·가동·알람 현황을 한눈에 확인합니다.', icon: 'dashboard', available: analyticsEnabled },
        { page: 'operations', id: 'workspace-operations', label: '라인 모니터링', title: '라인 모니터링', kicker: 'LINE MONITORING', description: '라인 공정의 상태와 측정값을 실시간으로 봅니다. 설비를 누르면 그 설비의 상세 값이 열립니다.', icon: 'line', available: true },
        { page: 'alarms', id: 'workspace-alarms', label: '알람', title: '알람', kicker: 'ALARMS', description: '설비별 알람과 확인·조치 기록입니다.', icon: 'alarm', available: true },
        { page: 'trends', id: 'workspace-trends', label: '트렌드', title: '설비 트렌드', kicker: 'TREND', description: '아날로그 측정값의 기간 추세와 데이터 누락 구간을 확인합니다.', icon: 'trend', available: analyticsEnabled },
        { page: 'assistant', id: 'workspace-assistant', label: '운영 도우미', title: '운영 도우미', kicker: 'ASSISTANT', description: '라인 데이터를 질문으로 조회합니다. 조회만 할 수 있고 운전·등록은 하지 않습니다.', icon: 'assistant', available: features.assistant },
      ],
    },
    {
      label: '제어',
      pages: [
        { page: 'control', id: 'workspace-control', label: '운전 제어', title: '운전 제어', kicker: 'OPERATION', description: '설비 전원, 작업 불러오기, 운전 명령과 그 결과입니다. 실행 여부는 PLC가 최종 판정합니다.', icon: 'control', available: true },
        { page: 'orders', id: 'workspace-orders', label: '작업지시', title: '작업지시', kicker: 'WORK ORDERS', description: '작업지시를 한 건씩 등록·수정·취소하고, 어디서 어떻게 쓰였는지 기록을 봅니다.', icon: 'orders', available: true },
        { page: 'drafts', id: 'workspace-drafts', label: '공정 설정 · 품목', title: '공정 설정 · 품목', kicker: 'RECIPES & ITEMS', description: '생산할 품목을 등록하고, 품목별 생산 조건(실링 온도 · 운전 속도 · 실링 공압)을 버전으로 관리합니다.', icon: 'recipe', available: true },
      ],
    },
    {
      label: '분석',
      pages: [
        { page: 'reports', id: 'workspace-reports', label: '실적 · 보고서', title: '실적 · 보고서', kicker: 'REPORTING', description: '작업별 실적, 가동·정지·불명 시간과 정지 사유를 확인합니다.', icon: 'report', available: analyticsEnabled },
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
