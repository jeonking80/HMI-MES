/** HTTP screen transport; capture/heartbeat quality always comes from the envelope. */
import { acceptScreen, beginScreen, decodeScreenFrame, MAX_SCREEN_BYTES, screenWatchdogExpired } from './screenState'
import type { ScreenProjection, ScreenScope } from './screenState'

export type ScreenPhase = 'connecting' | 'awaiting_snapshot' | 'synced' | 'reconnecting' | 'stopped' | 'auth_required'
export const SCREEN_POLL_INTERVAL = 1000
export const SCREEN_POLL_TIMEOUT = 2500
const backoff = [1000, 2000, 4000, 8000, 15000]
export const screenTransport = (setting: unknown) => setting === 'polling' ? 'polling' : 'websocket'
export class ScreenPollingAuthError extends Error {}
const failed = () => new Error('Screen polling response unavailable')
export function screenPollingURL(scope: ScreenScope): string {
  return `/api/v1/screen?${new URLSearchParams({ line_id: scope.line_id, source_id: scope.source_id, stream_id: scope.stream_id })}`
}
function sameScope(projection: ScreenProjection, scope: ScreenScope) {
  return projection.scope.line_id === scope.line_id && projection.scope.source_id === scope.source_id && projection.scope.stream_id === scope.stream_id
}

/** Count bytes before decoding; never buffer an unbounded response.text()/arrayBuffer(). */
export async function readScreenSnapshot(response: Response, scope: ScreenScope, previous: ScreenProjection | null, signal: AbortSignal): Promise<ScreenProjection> {
  if (response.status === 401 || response.status === 403) { void response.body?.cancel().catch(() => {}); throw new ScreenPollingAuthError() }
  if (response.status !== 200 || response.headers.get('content-type')?.split(';', 1)[0].trim().toLowerCase() !== 'application/json' || !response.body) {
    await response.body?.cancel(); throw failed()
  }
  const length = response.headers.get('content-length')
  if (length !== null && (!/^[0-9]+$/.test(length) || Number(length) > MAX_SCREEN_BYTES)) {
    await response.body.cancel(); throw failed()
  }
  const reader = response.body.getReader()
  const decoder = new TextDecoder('utf-8', { fatal: true })
  let bytes = 0, text = '', complete = false
  try {
    while (true) {
      if (signal.aborted) throw failed()
      const { done, value } = await reader.read()
      if (signal.aborted) throw failed()
      if (done) break
      bytes += value.byteLength
      if (bytes > MAX_SCREEN_BYTES) throw failed()
      text += decoder.decode(value, { stream: true })
    }
    text += decoder.decode()
    const frame = decodeScreenFrame(text, scope)
    // Each GET is an independent full snapshot, never an incremental WS UPDATE.
    if (frame.screen.kind !== 'SNAPSHOT' || frame.screen.revision !== '0') throw failed()
    const next = acceptScreen(beginScreen(previous), frame)
    if (next.phase !== 'synced' || !next.projection) throw failed()
    complete = true
    return next.projection
  } finally {
    if (!complete) await reader.cancel().catch(() => {})
    reader.releaseLock()
  }
}

interface PollOptions {
  scope: ScreenScope
  initialProjection: ScreenProjection | null
  onProjection: (projection: ScreenProjection) => void
  onPhase: (phase: ScreenPhase, message: string) => void
  onSessionInvalid: () => void
  fetch?: typeof fetch
  online?: () => boolean
  now?: () => number
  setTimer?: (callback: () => void, delay: number) => number
  clearTimer?: (timer: number) => void
}

/** One active request; discarded generations can never publish a late response. */
export function createScreenPoller(options: PollOptions) {
  const scope = { ...options.scope }
  const request = options.fetch ?? globalThis.fetch.bind(globalThis)
  const online = options.online ?? (() => true)
  const now = options.now ?? (() => performance.now())
  const setTimer = options.setTimer ?? ((callback, delay) => globalThis.setTimeout(callback, delay))
  const clearTimer = options.clearTimer ?? (timer => globalThis.clearTimeout(timer))
  let previous = options.initialProjection && sameScope(options.initialProjection, scope) ? options.initialProjection : null
  let alive = true, started = false, generation = 0, failures = 0, terminal = false
  let active: AbortController | null = null
  let nextTimer: number | undefined, timeout: number | undefined
  let lastArrival = now()
  const publish = (phase: ScreenPhase, message = '') => { if (alive) options.onPhase(phase, message) }
  function retire() {
    generation += 1
    if (timeout !== undefined) clearTimer(timeout)
    timeout = undefined
    active?.abort()
    active = null
  }
  function fail(auth = false) {
    if (!alive || terminal) return
    retire()
    if (nextTimer !== undefined) clearTimer(nextTimer)
    nextTimer = undefined
    if (auth) {
      terminal = true
      publish('auth_required', '화면 연결 권한이나 세션을 확인할 수 없습니다. 다시 로그인해 주세요.')
      options.onSessionInvalid()
    } else if (failures >= backoff.length) {
      terminal = true
      publish('stopped', '화면 갱신을 복구하지 못했습니다. 연결 상태를 확인한 뒤 다시 연결해 주세요.')
    } else {
      publish('reconnecting', '화면 갱신을 확인할 수 없어 다시 연결하고 있습니다.')
      nextTimer = setTimer(() => { nextTimer = undefined; void poll() }, backoff[failures++])
    }
  }
  async function poll() {
    if (!alive || terminal || active) return
    if (!online()) { fail(); return }
    if (failures > 0 || !previous) lastArrival = now()
    const controller = new AbortController()
    active = controller
    const currentGeneration = ++generation
    const current = () => alive && !terminal && generation === currentGeneration && active === controller
    if (!previous) publish('awaiting_snapshot')
    timeout = setTimer(() => { if (current()) fail() }, SCREEN_POLL_TIMEOUT)
    try {
      const response = await request(screenPollingURL(scope), {
        method: 'GET', credentials: 'same-origin', cache: 'no-store', redirect: 'error',
        headers: { Accept: 'application/json' }, signal: controller.signal,
      })
      if (!current()) { await response.body?.cancel(); return }
      const projection = await readScreenSnapshot(response, scope, previous, controller.signal)
      if (!current()) return
      if (timeout !== undefined) clearTimer(timeout)
      timeout = undefined
      active = null
      previous = projection
      failures = 0
      lastArrival = now()
      options.onProjection(projection)
      publish('synced')
      nextTimer = setTimer(() => { nextTimer = undefined; void poll() }, SCREEN_POLL_INTERVAL)
    } catch (error) { if (current()) fail(error instanceof ScreenPollingAuthError) }
  }
  return {
    start() { if (!started && alive) { started = true; void poll() } },
    offline() { if (started && alive && !terminal) fail() },
    watchdog() { if (active && screenWatchdogExpired(lastArrival, now())) fail() },
    stop() { alive = false; terminal = true; retire(); if (nextTimer !== undefined) clearTimer(nextTimer) },
  }
}
