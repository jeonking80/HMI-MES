import { useCallback, useEffect, useRef, useState } from 'react'
import { AnalyticsError, readJson } from './api'
import type { Scope } from './api'

export interface Source extends Scope { line_name: string }
/** Unambiguous even when identifiers contain '/'. */
export const scopeKey = (source: Scope) => JSON.stringify([source.line_id, source.source_id, source.stream_id])

function parseSources(value: unknown): Source[] {
  if (!value || typeof value !== 'object' || !('sources' in value) || !Array.isArray(value.sources)) throw new AnalyticsError(0, '수집원 목록 형식을 확인할 수 없습니다.')
  return value.sources.map((item: unknown) => {
    const source = item as Record<string, unknown>
    if (!source || !['line_id', 'line_name', 'source_id', 'stream_id'].every(key => typeof source[key] === 'string')) throw new AnalyticsError(0, '수집원 목록 형식을 확인할 수 없습니다.')
    return { line_id: source.line_id, line_name: source.line_name, source_id: source.source_id, stream_id: source.stream_id } as Source
  })
}

export interface Resource<T> {
  data: T | null
  error: string
  loading: boolean
  updatedAt: number | null
  refresh: () => void
}

/**
 * Fetches a read-only resource. The previous result stays on screen while a refresh is in
 * flight, and a failed refresh never clears it silently: the error is shown next to it.
 */
export function useResource<T>(path: string | null, parse: (value: unknown) => T, onSessionInvalid: () => void, refreshMs?: number): Resource<T> {
  const [attempt, setAttempt] = useState(0)
  const refresh = useCallback(() => setAttempt(value => value + 1), [])
  // Each request has a key; loading is derived from whether the latest key has settled.
  const key = path ? `${attempt}|${path}` : null
  const [state, setState] = useState<{ key: string | null; path: string | null; data: T | null; error: string; updatedAt: number | null }>({ key: null, path: null, data: null, error: '', updatedAt: null })
  // A periodic refresh never cancels a slow read in flight: it would never finish.
  const inFlight = useRef(false)
  useEffect(() => {
    if (!path || !key) return
    const controller = new AbortController()
    inFlight.current = true
    readJson(path, parse, controller.signal).then(data => {
      if (!controller.signal.aborted) setState({ key, path, data, error: '', updatedAt: Date.now() })
    }).catch((cause: unknown) => {
      if (controller.signal.aborted) return
      if (cause instanceof AnalyticsError && (cause.status === 401 || cause.status === 403)) { onSessionInvalid(); return }
      const message = cause instanceof AnalyticsError ? cause.message : '데이터를 조회하지 못했습니다.'
      setState(previous => previous.path === path ? { ...previous, key, error: message } : { key, path, data: null, error: message, updatedAt: null })
    }).finally(() => { if (!controller.signal.aborted) inFlight.current = false })
    return () => { controller.abort(); inFlight.current = false }
  }, [key, path, parse, onSessionInvalid])
  useEffect(() => {
    if (!path || !refreshMs) return
    const timer = window.setInterval(() => { if (!inFlight.current) refresh() }, refreshMs)
    return () => window.clearInterval(timer)
  }, [path, refreshMs, refresh])
  const current = state.path === path
  return { data: current ? state.data : null, error: current ? state.error : '', loading: key !== null && state.key !== key, updatedAt: current ? state.updatedAt : null, refresh }
}

export function useSources(onSessionInvalid: () => void): Resource<Source[]> {
  return useResource('/api/v1/sources', parseSources, onSessionInvalid)
}
