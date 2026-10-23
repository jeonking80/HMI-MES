export interface Session {
  login: string
  roles: string[]
  must_change_password: boolean
  csrf_token: string
  expires_at: string
}

export class AuthError extends Error {
  constructor(public readonly status: number, message: string) { super(message) }
}

async function post(path: string, body?: object, csrf?: string): Promise<unknown> {
  let response: Response
  try {
    response = await fetch(path, {
      method: 'POST', credentials: 'same-origin', cache: 'no-store',
      headers: {
        'X-HMI-Request': '1',
        ...(body ? { 'Content-Type': 'application/json' } : {}),
        ...(csrf ? { 'X-CSRF-Token': csrf } : {}),
      },
      ...(body ? { body: JSON.stringify(body) } : {}),
      signal: AbortSignal.timeout(20_000),
    })
  } catch {
    throw new AuthError(0, '서버 응답을 확인할 수 없습니다. 연결을 확인해 주세요.')
  }
  if (!response.ok) {
    const messages: Record<number, string> = {
      401: '계정 또는 비밀번호를 확인해 주세요. 세션이 만료되었을 수도 있습니다.',
      403: '인증 요청이 거절되었습니다. 다시 로그인해 주세요. 계속되면 관리자에게 문의해 주세요.',
      422: '입력 조건을 확인해 주세요. 새 비밀번호는 현재 비밀번호와 달라야 합니다.',
      429: '인증 시도 횟수를 초과했습니다. 잠시 후 다시 시도해 주세요.',
      503: '인증 서비스를 사용할 수 없습니다. 잠시 후 다시 시도해 주세요.',
    }
    throw new AuthError(response.status, messages[response.status] ?? '인증 요청을 처리하지 못했습니다. 관리자에게 문의해 주세요.')
  }
  try { return await response.json() } catch {
    throw new AuthError(0, '서버 응답을 확인할 수 없습니다. 다시 로그인해 주세요.')
  }
}

function sessionFrom(value: unknown): Session {
  if (typeof value !== 'object' || value === null) throw new AuthError(0, '올바른 인증 응답을 받지 못했습니다.')
  const result = value as Partial<Session>
  if (typeof result.login !== 'string' || !Array.isArray(result.roles)
    || !result.roles.every(role => typeof role === 'string')
    || typeof result.must_change_password !== 'boolean'
    || typeof result.csrf_token !== 'string' || !result.csrf_token
    || typeof result.expires_at !== 'string' || !Number.isFinite(Date.parse(result.expires_at))) {
    throw new AuthError(0, '올바른 인증 응답을 받지 못했습니다.')
  }
  return result as Session
}

export async function login(login: string, password: string): Promise<Session> {
  const challenge = await post('/auth/challenge')
  if (typeof challenge !== 'object' || challenge === null
    || !('csrf_token' in challenge) || typeof challenge.csrf_token !== 'string' || !challenge.csrf_token) {
    throw new AuthError(0, '로그인 준비 응답을 확인할 수 없습니다.')
  }
  return sessionFrom(await post('/auth/login', { login, password }, challenge.csrf_token))
}

/** Whether this deployment offers the read-only guest session (CX-06). Any failure means no. */
export async function guestAvailable(signal: AbortSignal): Promise<boolean> {
  try {
    const response = await fetch('/auth/config', { credentials: 'same-origin', cache: 'no-store', signal })
    if (!response.ok) return false
    const value: unknown = await response.json()
    return typeof value === 'object' && value !== null && 'guest_viewer' in value && value.guest_viewer === true
  } catch {
    return false
  }
}

/** Starts a 30-minute viewer-only session without a password. */
export async function guestLogin(): Promise<Session> {
  try {
    return sessionFrom(await post('/auth/guest', {}))
  } catch (cause) {
    if (cause instanceof AuthError && cause.status === 404) throw new AuthError(404, '이 환경에서는 게스트 조회를 사용할 수 없습니다.')
    if (cause instanceof AuthError && cause.status === 429) throw new AuthError(429, '게스트 접속이 많아 잠시 후 다시 시도해 주세요.')
    throw cause
  }
}

export async function changePassword(session: Session, newPassword: string): Promise<Session> {
  return sessionFrom(await post('/auth/password', { new_password: newPassword }, session.csrf_token))
}

export async function logout(session: Session): Promise<void> {
  const result = await post('/auth/logout', undefined, session.csrf_token)
  if (typeof result !== 'object' || result === null || !('logged_out' in result) || result.logged_out !== true) {
    throw new AuthError(0, '로그아웃 결과를 확인할 수 없습니다.')
  }
}
