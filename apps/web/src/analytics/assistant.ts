import { AnalyticsError } from './api'
import type { Scope } from './api'

/** One question or answer in the conversation sent to the read-only assistant (CX-08). */
export interface ChatTurn { role: 'user' | 'assistant'; content: string }

/** A read API the assistant actually called while answering; shown so the answer can be checked. */
export interface Evidence { tool: string; label: string; status: number; generated_at: string | null }

export interface ChatAnswer { answer: string; evidence: Evidence[]; model: string; generated_at: string }

export const MAX_TURNS = 12
export const MAX_CHARS = 2000

const fail = (): never => { throw new AnalyticsError(0, '도우미 응답 형식을 확인할 수 없습니다.') }

export function parseChatAnswer(value: unknown): ChatAnswer {
  if (!value || typeof value !== 'object' || Array.isArray(value)) fail()
  const root = value as Record<string, unknown>
  if (typeof root.answer !== 'string' || !root.answer.trim() || typeof root.model !== 'string' || typeof root.generated_at !== 'string' || !Array.isArray(root.evidence)) fail()
  const evidence = (root.evidence as unknown[]).slice(0, 20).map(item => {
    if (!item || typeof item !== 'object') fail()
    const entry = item as Record<string, unknown>
    if (typeof entry.tool !== 'string' || typeof entry.label !== 'string' || typeof entry.status !== 'number') fail()
    const at = entry.generated_at
    return { tool: entry.tool as string, label: entry.label as string, status: entry.status as number, generated_at: typeof at === 'string' ? at : null }
  })
  return { answer: root.answer as string, evidence, model: root.model as string, generated_at: root.generated_at as string }
}

/** Keeps the latest turns within the server bound, ending with the user's question. */
export function conversationFor(history: ChatTurn[], question: string): ChatTurn[] {
  const turns = [...history, { role: 'user' as const, content: question.slice(0, MAX_CHARS) }]
  let start = Math.max(0, turns.length - MAX_TURNS)
  // The server accepts a conversation that starts with a question.
  while (start < turns.length - 1 && turns[start].role !== 'user') start++
  return turns.slice(start)
}

const errorMessages: Record<number, string> = {
  401: '세션이 만료되었습니다.',
  403: '도우미를 사용할 권한이 없거나 보안 확인에 실패했습니다. 다시 로그인해 주세요.',
  413: '질문이 너무 깁니다.',
  422: '질문 형식을 확인해 주세요.',
  429: '잠시 질문이 많았습니다. 몇 분 뒤 다시 시도해 주세요.',
  502: 'AI 모델이 응답하지 않았습니다. 잠시 뒤 다시 시도해 주세요.',
  504: '답변 시간이 초과되었습니다. 질문 범위를 좁혀 다시 시도해 주세요.',
}

export async function askAssistant(turns: ChatTurn[], scope: Scope | null, csrf: string, signal: AbortSignal): Promise<ChatAnswer> {
  let response: Response
  try {
    response = await fetch('/api/v1/assistant/chat', {
      method: 'POST', credentials: 'same-origin', cache: 'no-store',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf, 'X-HMI-Request': '1' },
      body: JSON.stringify({ messages: turns, scope }),
      signal: AbortSignal.any([signal, AbortSignal.timeout(45_000)]),
    })
  } catch (cause) {
    if (signal.aborted) throw cause
    throw new AnalyticsError(0, '도우미 응답을 받지 못했습니다. 연결을 확인해 주세요.')
  }
  if (!response.ok) {
    void response.body?.cancel().catch(() => {})
    throw new AnalyticsError(response.status, errorMessages[response.status] ?? '도우미가 답하지 못했습니다.')
  }
  try {
    return parseChatAnswer(await response.json())
  } catch (cause) {
    if (cause instanceof AnalyticsError) throw cause
    throw new AnalyticsError(0, '도우미 응답 형식을 확인할 수 없습니다.')
  }
}
