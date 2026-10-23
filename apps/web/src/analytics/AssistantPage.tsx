import { Fragment, useEffect, useRef, useState } from 'react'
import type { FormEvent, KeyboardEvent, ReactNode } from 'react'
import type { Session } from '../auth'
import { AnalyticsError } from './api'
import { askAssistant, conversationFor, MAX_CHARS } from './assistant'
import type { ChatTurn, Evidence } from './assistant'
import { dateTime } from './format'
import { useSources } from './hooks'
import type { Source } from './hooks'
import { Badge, EmptyState, PageNotice, ScopePicker } from './ui'

interface Message extends ChatTurn { evidence?: Evidence[]; model?: string; at?: string }

const suggestions = [
  '오늘 생산 현황 요약해 줘',
  '지금 라인 상태와 통신 상태는 어때?',
  '최근 1시간 실링 온도 추세 알려줘',
  '최근 7일 작업별 수율 비교해 줘',
]

/** Renders the model's light Markdown (paragraphs, lists, tables, quotes, **bold**, `code`) without injecting HTML. */
function inline(text: string): ReactNode[] {
  return text.split(/(\*\*[^*]+\*\*|`[^`]+`)/u).map((part, index) => {
    if (part.startsWith('**') && part.endsWith('**') && part.length > 4) return <strong key={index}>{part.slice(2, -2)}</strong>
    if (part.startsWith('`') && part.endsWith('`') && part.length > 2) return <code key={index}>{part.slice(1, -1)}</code>
    return <Fragment key={index}>{part}</Fragment>
  })
}

const cells = (line: string) => line.trim().replace(/^\|/u, '').replace(/\|$/u, '').split('|').map(cell => cell.trim())
const isSeparator = (line: string) => /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/u.test(line)

function Block({ lines }: { lines: string[] }) {
  if (lines.length >= 2 && lines.every(line => line.trim().startsWith('|')) && isSeparator(lines[1])) {
    const [head, , ...rows] = lines
    return <div className="table-scroll"><table className="data-table assistant-table">
      <thead><tr>{cells(head).map((cell, index) => <th key={index} scope="col">{inline(cell)}</th>)}</tr></thead>
      <tbody>{rows.map((row, index) => <tr key={index}>{cells(row).map((cell, column) => <td key={column}>{inline(cell)}</td>)}</tr>)}</tbody>
    </table></div>
  }
  if (lines.every(line => /^\s*[-*•]\s+/u.test(line))) {
    return <ul>{lines.map((line, item) => <li key={item}>{inline(line.replace(/^\s*[-*•]\s+/u, ''))}</li>)}</ul>
  }
  if (lines.every(line => /^\s*\d+[.)]\s+/u.test(line))) {
    return <ol>{lines.map((line, item) => <li key={item}>{inline(line.replace(/^\s*\d+[.)]\s+/u, ''))}</li>)}</ol>
  }
  if (lines.every(line => line.trimStart().startsWith('>'))) {
    return <blockquote>{lines.map((line, item) => <Fragment key={item}>{item > 0 && <br />}{inline(line.replace(/^\s*>\s?/u, ''))}</Fragment>)}</blockquote>
  }
  return <p>{lines.map((line, item) => <Fragment key={item}>{item > 0 && <br />}{inline(line.replace(/^#{1,6}\s+/u, ''))}</Fragment>)}</p>
}

/** Splits on blank lines, and also where a table or list starts or ends without one. */
function blocks(text: string): string[][] {
  const result: string[][] = []
  let current: string[] = []
  let kind = ''
  for (const line of text.trim().split('\n')) {
    const next = !line.trim() ? '' : line.trim().startsWith('|') ? 'table' : /^\s*([-*•]|\d+[.)])\s+/u.test(line) ? 'list' : line.trimStart().startsWith('>') ? 'quote' : 'text'
    if (!next || (current.length && next !== kind)) { if (current.length) result.push(current); current = [] }
    if (next) { current.push(line); kind = next }
  }
  if (current.length) result.push(current)
  return result
}

function Answer({ text }: { text: string }) {
  return <>{blocks(text).map((lines, index) => <Block key={index} lines={lines} />)}</>
}

function EvidenceList({ evidence, model, at }: { evidence: Evidence[]; model?: string; at?: string }) {
  return <div className="assistant-evidence">
    <span className="assistant-evidence-title">조회 근거</span>
    {evidence.length === 0
      ? <Badge tone="warn">조회한 데이터 없음 · 답변을 그대로 믿지 마세요</Badge>
      : evidence.map((item, index) => <Badge key={index} tone={item.status === 200 ? 'info' : 'bad'} title={item.tool}>
        {item.label}{item.status === 200 ? (item.generated_at ? ` · ${dateTime(item.generated_at)}` : '') : ` · 실패(${item.status})`}
      </Badge>)}
    {model && <span className="assistant-model">{model}{at ? ` · ${dateTime(at)}` : ''}</span>}
  </div>
}

export default function AssistantPage({ session, scope, onScope, onSessionInvalid }: { session: Session; scope: Source | null; onScope: (source: Source | null) => void; onSessionInvalid: () => void }) {
  const sources = useSources(onSessionInvalid)
  const [messages, setMessages] = useState<Message[]>([])
  const [draft, setDraft] = useState('')
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const controller = useRef<AbortController | null>(null)
  const log = useRef<HTMLDivElement>(null)
  const input = useRef<HTMLTextAreaElement>(null)

  useEffect(() => () => controller.current?.abort(), [])
  useEffect(() => { log.current?.scrollTo({ top: log.current.scrollHeight }) }, [messages, pending])

  async function send(question: string) {
    const text = question.trim()
    if (!text || pending) return
    const history: ChatTurn[] = messages.map(({ role, content }) => ({ role, content }))
    const turns = conversationFor(history, text)
    setMessages(current => [...current, { role: 'user', content: text }])
    setDraft('')
    setError('')
    setPending(true)
    const request = new AbortController()
    controller.current = request
    try {
      const result = await askAssistant(turns, scope ? { line_id: scope.line_id, source_id: scope.source_id, stream_id: scope.stream_id } : null, session.csrf_token, request.signal)
      setMessages(current => [...current, { role: 'assistant', content: result.answer, evidence: result.evidence, model: result.model, at: result.generated_at }])
    } catch (cause) {
      if (request.signal.aborted) {
        setError('질문을 취소했습니다.')
      } else if (cause instanceof AnalyticsError && cause.status === 401) {
        onSessionInvalid()
      } else {
        setError(cause instanceof AnalyticsError ? cause.message : '도우미가 답하지 못했습니다.')
      }
      // A failed question goes back to the input so it can be retried as is.
      setMessages(current => current.slice(0, -1))
      setDraft(text)
    } finally {
      setPending(false)
      controller.current = null
      input.current?.focus()
    }
  }

  function submit(event: FormEvent) {
    event.preventDefault()
    void send(draft)
  }

  function keyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault()
      void send(draft)
    }
  }

  return <div className="analytics-page assistant-page" data-page="assistant">
    <div className="filter-row">
      <ScopePicker id="assistant-scope" sources={sources.data} value={scope} onChange={onScope} />
      <div className="refresh-status">
        {messages.length > 0 && <button type="button" className="button secondary compact" disabled={pending} onClick={() => { setMessages([]); setError('') }}>대화 지우기</button>}
      </div>
    </div>
    <PageNotice tone="info" title="조회 전용 AI 도우미입니다.">
      로그인한 사용자 권한으로 대시보드·트렌드·보고서·SPC를 조회해 답합니다. 운전 명령이나 등록은 할 수 없습니다. AI 답변은 틀릴 수 있으니 아래 조회 근거와 화면 데이터를 함께 확인해 주세요.
    </PageNotice>
    <section className="panel-card assistant-panel" aria-label="도우미 대화">
      <div ref={log} className="assistant-log" role="log" aria-live="polite" aria-busy={pending}>
        {messages.length === 0 && !pending && <EmptyState title="무엇이 궁금하신가요?">
          <span className="assistant-suggestions">
            {suggestions.map(item => <button key={item} type="button" className="button secondary compact" onClick={() => void send(item)}>{item}</button>)}
          </span>
        </EmptyState>}
        {messages.map((message, index) => <article key={index} className={`assistant-message ${message.role}`} aria-label={message.role === 'user' ? '질문' : '답변'}>
          <span className="assistant-role">{message.role === 'user' ? session.login : '도우미'}</span>
          <div className="assistant-bubble">{message.role === 'user' ? <p>{message.content}</p> : <Answer text={message.content} />}</div>
          {message.role === 'assistant' && <EvidenceList evidence={message.evidence ?? []} model={message.model} at={message.at} />}
        </article>)}
        {pending && <article className="assistant-message assistant" aria-label="답변 작성 중">
          <span className="assistant-role">도우미</span>
          <div className="assistant-bubble is-pending"><span className="assistant-dots" aria-hidden="true"><i /><i /><i /></span>데이터를 조회하고 답변을 작성하고 있습니다…</div>
        </article>}
      </div>
      {error && <div className="message error" role="alert">{error}</div>}
      <form className="assistant-form" onSubmit={submit}>
        <label className="sr-only" htmlFor="assistant-input">질문</label>
        <textarea
          id="assistant-input"
          ref={input}
          rows={2}
          value={draft}
          maxLength={MAX_CHARS}
          placeholder="예: 오늘 불량률이 왜 높아졌어? (Enter 전송, Shift+Enter 줄바꿈)"
          disabled={pending}
          onChange={event => setDraft(event.target.value)}
          onKeyDown={keyDown}
        />
        <div className="assistant-actions">
          <span className="assistant-count">{draft.length.toLocaleString('ko-KR')} / {MAX_CHARS.toLocaleString('ko-KR')}</span>
          {pending
            ? <button type="button" className="button secondary" onClick={() => controller.current?.abort()}>취소</button>
            : <button type="submit" className="button primary" disabled={!draft.trim()}>보내기</button>}
        </div>
      </form>
    </section>
  </div>
}
