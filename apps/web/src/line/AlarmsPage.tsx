import type { Dispatch, SetStateAction } from 'react'
import AlarmPanel from '../AlarmPanel'
import type { AlarmLedger } from '../alarmApi'
import type { Session } from '../auth'
import { useLine } from './lineState'
import { SourcePicker } from './parts'
import './line.css'

/** The alarm centre for the selected line, on its own page. */
export default function AlarmsPage({ session, onSessionInvalid, ledger, setLedger }: { session: Session; onSessionInvalid: () => void; ledger: AlarmLedger | null; setLedger: Dispatch<SetStateAction<AlarmLedger | null>> }) {
  const line = useLine()
  return <div className="lc-page">
    <div className="lc-chips"><SourcePicker /></div>
    {line.source ? <AlarmPanel session={session} source={line.source} onSessionInvalid={onSessionInvalid} ledger={ledger} setLedger={setLedger} /> : <div className="lc-card lc-empty">라인을 선택해 주세요.</div>}
  </div>
}
