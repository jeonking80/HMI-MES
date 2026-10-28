import { useCallback, useEffect, useRef, useState } from 'react'
import { acceptScreen, beginScreen, decodeScreenFrame, screenWatchdogExpired } from './screenState'
import type { ScreenProjection, ScreenScope } from './screenState'
import { createScreenPoller, screenTransport } from './screenPolling'

type Phase = 'connecting' | 'awaiting_snapshot' | 'synced' | 'reconnecting' | 'stopped' | 'auth_required'
/** receivedAt is the local wall time the current projection arrived; ages are measured on the server clock from it. */
interface Subscription { projection: ScreenProjection | null; phase: Phase; message: string; now: number; receivedAt: number }
const delays = [1000, 2000, 4000, 8000, 15000]
const transport = screenTransport(import.meta.env.VITE_SCREEN_TRANSPORT)

/** One bounded connection lifecycle. Credentials are sent only in the first WS frame. */
export function useScreenSubscription(scope: ScreenScope, csrf: string, onSessionInvalid: () => void) {
  const [subscription, setSubscription] = useState<Subscription>(() => ({ projection: null, phase: 'connecting', message: '', now: Date.now(), receivedAt: Date.now() }))
  const projection = useRef<ScreenProjection | null>(null)
  const receivedAt = useRef(0)
  const [revision, setRevision] = useState(0)
  useEffect(() => {
    let alive = true
    const expectedScope = { line_id: scope.line_id, source_id: scope.source_id, stream_id: scope.stream_id }
    if (projection.current && (projection.current.scope.line_id !== scope.line_id || projection.current.scope.source_id !== scope.source_id || projection.current.scope.stream_id !== scope.stream_id)) projection.current = null
    if (transport === 'polling') {
      const poller = createScreenPoller({
        scope: expectedScope, initialProjection: projection.current,
        online: () => navigator.onLine,
        visible: () => document.visibilityState === 'visible',
        onProjection: value => { if (alive) { projection.current = value; receivedAt.current = Date.now() } },
        onPhase: (phase, message) => { if (alive) setSubscription({ projection: projection.current, phase, message, now: Date.now(), receivedAt: receivedAt.current }) },
        onSessionInvalid,
      })
      poller.start()
      const watchdog = window.setInterval(() => poller.watchdog(), 250)
      const aging = window.setInterval(() => { if (alive) setSubscription(previous => ({ ...previous, now: Date.now() })) }, 500)
      const offline = () => poller.offline()
      const shown = () => { if (document.visibilityState === 'visible') poller.resume() }
      window.addEventListener('offline', offline)
      document.addEventListener('visibilitychange', shown)
      return () => { alive = false; poller.stop(); window.removeEventListener('offline', offline); document.removeEventListener('visibilitychange', shown); window.clearInterval(watchdog); window.clearInterval(aging) }
    }
    let generation = 0
    let reconnects = 0
    let socket: WebSocket | null = null
    let retryTimer: number | undefined
    let lastArrival = performance.now()
    let protocol = beginScreen(projection.current)
    const expected = { line_id: scope.line_id, source_id: scope.source_id, stream_id: scope.stream_id }
    function publish(phase: Phase, message = '') {
      if (alive) setSubscription({ projection: projection.current, phase, message, now: Date.now(), receivedAt: receivedAt.current })
    }
    function retire() {
      generation += 1
      if (socket) {
        socket.onopen = socket.onmessage = socket.onerror = socket.onclose = null
        socket.close()
        socket = null
      }
    }
    function fail(message: string, auth = false) {
      if (!alive) return
      retire()
      protocol = { phase: 'resync_required', projection: projection.current }
      if (auth) {
        publish('auth_required', '화면 연결 권한이나 세션을 확인할 수 없습니다. 다시 로그인해 주세요.')
        onSessionInvalid()
        return
      }
      if (reconnects >= delays.length) { publish('stopped', '화면 연결을 복구하지 못했습니다. 연결 상태를 확인한 뒤 다시 연결해 주세요.'); return }
      publish('reconnecting', message)
      const delay = delays[reconnects++]
      retryTimer = window.setTimeout(() => { publish('connecting'); connect() }, delay)
    }
    function connect() {
      if (!alive) return
      const currentGeneration = ++generation
      protocol = beginScreen(projection.current)
      lastArrival = performance.now()
      const url = new URL('/ws/v1/screen', window.location.href)
      url.protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
      try { socket = new WebSocket(url) } catch {
        queueMicrotask(() => { if (alive && generation === currentGeneration) fail('화면 연결을 시작하지 못했습니다. 다시 연결하고 있습니다.') })
        return
      }
      const currentSocket = socket
      const current = () => alive && generation === currentGeneration && socket === currentSocket
      currentSocket.onopen = () => {
        if (!current()) return
        publish('awaiting_snapshot')
        try { currentSocket.send(JSON.stringify({ ...expected, csrf_token: csrf })) }
        catch { fail('화면 연결 요청을 확인할 수 없어 다시 연결하고 있습니다.') }
      }
      currentSocket.onmessage = event => {
        if (!current()) return
        try {
          if (typeof event.data !== 'string') throw new Error('Unrecognized screen frame')
          const next = acceptScreen(protocol, decodeScreenFrame(event.data, expected))
          if (next.phase !== 'synced') { fail('화면 정보를 다시 확인하기 위해 연결하고 있습니다.'); return }
          protocol = next
          projection.current = next.projection
          receivedAt.current = Date.now()
          lastArrival = performance.now()
          publish('synced')
        } catch { fail('화면 정보를 확인할 수 없어 다시 연결하고 있습니다.') }
      }
      currentSocket.onerror = () => {
        // Wait for close to retain the policy code; the watchdog bounds a missing close.
        if (current()) { protocol = { phase: 'resync_required', projection: projection.current }; publish('reconnecting', '화면 연결 결과를 확인하고 있습니다.') }
      }
      currentSocket.onclose = event => {
        if (current()) fail('화면 연결이 끊겨 다시 연결하고 있습니다.', event.code === 1008)
      }
    }
    connect()
    const watchdog = window.setInterval(() => {
      if (socket && screenWatchdogExpired(lastArrival, performance.now())) fail('화면 갱신을 확인할 수 없어 다시 연결하고 있습니다.')
    }, 250)
    const aging = window.setInterval(() => { if (alive) setSubscription(previous => ({ ...previous, now: Date.now() })) }, 500)
    return () => { alive = false; retire(); window.clearTimeout(retryTimer); window.clearInterval(watchdog); window.clearInterval(aging) }
  }, [scope.line_id, scope.source_id, scope.stream_id, csrf, revision, onSessionInvalid])
  const retry = useCallback(() => {
    setSubscription(previous => ({ ...previous, phase: 'connecting', message: '' }))
    setRevision(value => value + 1)
  }, [])
  const visible = subscription.projection
  const matches = !visible || (visible.scope.line_id === scope.line_id && visible.scope.source_id === scope.source_id && visible.scope.stream_id === scope.stream_id)
  return { ...subscription, projection: matches ? visible : null, phase: matches ? subscription.phase : 'connecting' as Phase, retry, transport }

}
