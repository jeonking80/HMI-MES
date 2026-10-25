/** Fixed 409 reason codes (contracts/HTTP_CONFLICT_CODES.md) and what the operator can do about each. */
const messages: Record<string, string> = {
  RESERVED_EXECUTION_NOT_SETTLED: '이전 작업의 실적 정산이 끝나지 않았습니다. 이전 작업을 종료하고 정산(SETTLED)을 확인한 뒤 다시 요청해 주세요.',
  WORK_ORDER_VERSION_CHANGED: '작업지시가 그사이 바뀌었습니다. 목록을 새로 조회한 뒤 다시 요청해 주세요.',
  WORK_ORDER_ALREADY_BOUND: '이 작업지시는 이미 실행에 연결되어 있습니다. 같은 작업지시는 한 번만 연결할 수 있습니다.',
  FRESH_SOURCE_REQUIRED: '설비의 최신 값이 아직 들어오지 않았습니다. 수집 상태가 "최근 수집 확인"이 된 뒤 다시 요청해 주세요.',
  PHYSICAL_EXECUTION_MISMATCH: '설비에 올라가 있는 작업이 요청한 작업과 다릅니다. 라인 모니터링에서 현재 실행 ID를 확인해 주세요.',
  ID_CONTENT_CONFLICT: '같은 ID에 다른 내용이 이미 등록되어 있습니다. 기존 내용을 덮어쓰지 않았습니다.',
  CONTROL_VERSION_STALE: '화면이 보던 설비 상태가 그사이 바뀌었습니다. 화면이 갱신된 뒤 다시 눌러 주세요.',
  STATE_NOT_ALLOWED: '현재 설비 상태나 모드에서는 이 요청을 할 수 없습니다. 운전 단계 안내를 확인해 주세요.',
}

export function conflictMessage(code: string | null | undefined): string | null {
  return code ? messages[code] ?? null : null
}

/** Read `reason_code` from a rejected response without trusting or showing any other body text. */
export async function reasonCodeOf(response: Response): Promise<string | null> {
  if (response.status !== 409) return null
  try {
    const body: unknown = await response.json()
    const code = typeof body === 'object' && body !== null && 'reason_code' in body ? (body as { reason_code: unknown }).reason_code : null
    return typeof code === 'string' && /^[A-Z][A-Z0-9_]{0,63}$/.test(code) ? code : null
  } catch { return null }
}
