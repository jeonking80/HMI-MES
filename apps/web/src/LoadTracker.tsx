import { useEffect, useState } from 'react'
import { CommandHTTPError, commandHTTP, commandLookupPath, parseResult, sameScope, statusLabels, terminal } from './commands'
import type { CommandScope } from './commands'
import type { WorkOrderWorkflow } from './useWorkOrderWorkflow'
import { sameWorkflowValue } from './workflow'

/** Passive original LOAD tracking, independent of the existing operation tracker. */
export default function LoadTracker({ source, workflow, onSessionInvalid }: { source: CommandScope; workflow: WorkOrderWorkflow; onSessionInvalid: () => void }) {
  const [revision, setRevision] = useState(0)
  const [message, setMessage] = useState('')
  const [polling, setPolling] = useState(false)
  const receipt = workflow.attempt?.record?.receipt
  const binding = receipt && 'execution_id' in receipt ? receipt : null
  const result = workflow.attempt?.loadResult
  const scoped = binding !== null && sameScope(source, binding)
  const report = workflow.recordLoadResult
  useEffect(() => {
    if (!binding || !scoped) return
    const controller = new AbortController()
    let timer: number | undefined
    const deadline = performance.now() + 10_000
    async function poll() {
      setPolling(true)
      try {
        const raw = await commandHTTP(commandLookupPath(binding!.command_id, binding!), controller.signal)
        if (controller.signal.aborted) return
        if (!raw || typeof raw !== 'object' || !('request' in raw) || !('result' in raw) || !sameWorkflowValue(raw.request, binding!.load_request)) throw new Error('Unconfirmed original LOAD request')
        const parsed = parseResult(raw.result, binding!.command_id)
        report(parsed)
        setMessage('')
        if (terminal(parsed)) { setPolling(false); return }
      } catch (cause) {
        if (controller.signal.aborted) return
        if (cause instanceof CommandHTTPError && [401, 403].includes(cause.status)) { onSessionInvalid(); return }
        setMessage('원래 작업 불러오기 결과를 확인할 수 없습니다. 결과 불명이며 새 명령을 보내지 않습니다.')
      }
      if (performance.now() >= deadline) { setPolling(false); setMessage('자동 조회가 끝났습니다. 원래 ID로 다시 조회해 주세요. 실패나 미실행으로 단정할 수 없습니다.') }
      else timer = window.setTimeout(() => void poll(), 1000)
    }
    void poll()
    return () => { controller.abort(); window.clearTimeout(timer) }
  }, [binding, scoped, revision, onSessionInvalid, report])
  if (!binding) return null
  return <section id="load-tracker" className="command-tracker" aria-labelledby="load-command-status"><h3 id="load-command-status" data-status={result?.status ?? 'UNKNOWN'}>{result ? statusLabels[result.status] : '작업 불러오기 결과 불명'}</h3><p>원래 명령 ID: <strong id="load-command-id">{binding.command_id}</strong></p><p>실행 {binding.execution_id} · 서버의 연결 등록은 PLC 실행 완료가 아닙니다.</p>{result && <p>서버 기록: {result.status} · {result.updated_at}</p>}{message && <p className="message caution" role="status">{message}</p>}{!scoped && <p className="message caution">원래 라인·수집원을 선택하면 같은 ID를 조회할 수 있습니다.</p>}<button id="load-lookup" className="button secondary" type="button" disabled={!scoped || polling} onClick={() => setRevision(value => value + 1)}>원래 작업 불러오기 결과 조회</button><p className="form-help">거부·만료·결과 불명이어도 실행 예약을 자동 해제하거나 새 연결을 만들지 않습니다.</p></section>
}
