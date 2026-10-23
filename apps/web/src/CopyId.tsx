import { useEffect, useState } from 'react'

/** Shortens a long identifier for display while keeping the full value available to read and copy. */
export default function CopyId({ value }: { value: string }) {
  const [copied, setCopied] = useState(false)
  useEffect(() => {
    if (!copied) return
    const timer = window.setTimeout(() => setCopied(false), 1500)
    return () => window.clearTimeout(timer)
  }, [copied])
  const short = value.length > 16 ? `${value.slice(0, 8)}…${value.slice(-4)}` : value
  async function copy() {
    try {
      await navigator.clipboard.writeText(value)
      setCopied(true)
    } catch {
      setCopied(false)
    }
  }
  return <span className="copy-id">
    <code title={value}>{short}</code>
    <button type="button" className="copy-id-button" aria-label={`${value} 복사`} onClick={() => void copy()}>{copied ? '복사됨' : '복사'}</button>
  </span>
}
