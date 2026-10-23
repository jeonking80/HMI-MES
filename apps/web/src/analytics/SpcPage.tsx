import { useState } from 'react'
import { parseExecutionReports, parseSpc, query } from './api'
import { TimeSeriesChart } from './charts'
import { SERIES_COLORS } from './scale'
import type { Threshold } from './charts'
import { clock, dateTime, decimal, MISSING, seoulToday, tagLabels } from './format'
import { useResource, useSources } from './hooks'
import type { Source } from './hooks'
import { Card, ChartOrTable, EmptyState, KpiTile, PageNotice, RefreshStatus, ScopePicker } from './ui'
import type { Tone } from './ui'

const SPC_TAGS = ['Sealer.TemperaturePV', 'Sealer.AirPressure'] as const
const ruleLabels: Record<string, string> = {
  WE1: '규칙 1 · 관리한계(3σ) 밖 1점',
  WE2: '규칙 2 · 연속 3점 중 2점이 2σ 밖',
  WE3: '규칙 3 · 연속 5점 중 4점이 1σ 밖',
  WE4: '규칙 4 · 중심선 한쪽에 연속 8점',
}
const capabilityTone = (value: number | null | undefined): Tone => value === null || value === undefined ? 'unknown' : value >= 1.33 ? 'good' : value >= 1 ? 'warn' : 'bad'
const capabilityText = (value: number | null | undefined) => value === null || value === undefined ? '산정 불가' : value >= 1.33 ? '충분' : value >= 1 ? '주의' : '부족'

/** Control limits only: specification limits apply to individual values, not subgroup means. */
function limitLines(limits: { ucl: number; cl: number; lcl: number }): Threshold[] {
  return [
    { value: limits.ucl, label: `UCL ${decimal(limits.ucl)}`, tone: 'limit' },
    { value: limits.cl, label: `CL ${decimal(limits.cl)}`, tone: 'center' },
    { value: limits.lcl, label: `LCL ${decimal(limits.lcl)}`, tone: 'limit' },
  ]
}

export default function SpcPage({ scope, onScope, onSessionInvalid }: { scope: Source | null; onScope: (source: Source | null) => void; onSessionInvalid: () => void }) {
  const sources = useSources(onSessionInvalid)
  const [tag, setTag] = useState<(typeof SPC_TAGS)[number]>('Sealer.TemperaturePV')
  const [executionId, setExecutionId] = useState('')
  const today = seoulToday()
  const weekStart = new Date(`${today}T00:00:00+09:00`)
  weekStart.setUTCDate(weekStart.getUTCDate() - 6)
  const weekEnd = new Date(`${today}T00:00:00+09:00`)
  weekEnd.setUTCDate(weekEnd.getUTCDate() + 1)
  const executions = useResource(scope ? query('/api/v1/reports/executions', {
    line_id: scope.line_id, from: weekStart.toISOString().replace('.000Z', 'Z'), to: weekEnd.toISOString().replace('.000Z', 'Z'), limit: '50',
  }) : null, parseExecutionReports, onSessionInvalid)
  const runs = executions.data?.items ?? []
  const chosen = executionId || runs[0]?.execution_id || ''
  const spc = useResource(scope && chosen ? query('/api/v1/spc/xbar-r', { line_id: scope.line_id, source_id: scope.source_id, stream_id: scope.stream_id, tag, execution_id: chosen }) : null, parseSpc, onSessionInvalid)
  const data = spc.data
  const violations = data?.subgroups.filter(item => item.violations.length > 0) ?? []

  return <div className="analytics-page" data-page="spc">
    <div className="filter-row">
      <ScopePicker id="spc-scope" sources={sources.data} value={scope} onChange={value => { onScope(value); setExecutionId('') }} />
      <div className="filter-field">
        <span className="filter-label">관리 항목</span>
        <div className="segmented" role="group" aria-label="관리 항목">
          {SPC_TAGS.map(item => <button key={item} type="button" aria-pressed={tag === item} onClick={() => setTag(item)}>{tagLabels[item]}</button>)}
        </div>
      </div>
      <div className="filter-field">
        <label className="filter-label" htmlFor="spc-execution">실행 (최근 7일)</label>
        <select id="spc-execution" value={chosen} onChange={event => setExecutionId(event.target.value)} disabled={!runs.length}>
          {!runs.length && <option value="">실행 없음</option>}
          {runs.map(run => <option key={run.execution_id} value={run.execution_id}>{run.work_order_id} · {dateTime(run.started_at ?? run.registered_at)}</option>)}
        </select>
      </div>
      <RefreshStatus loading={spc.loading} updatedAt={spc.updatedAt} onRefresh={spc.refresh} />
    </div>
    {(executions.error || spc.error) && <PageNotice tone="bad" title="SPC 데이터를 조회하지 못했습니다.">{executions.error || spc.error}</PageNotice>}
    {!scope && <EmptyState title="라인을 선택해 주세요." />}
    {scope && executions.data && !runs.length && <EmptyState title="최근 7일 안에 실행이 없습니다.">관리도는 실행 하나의 실링 데이터를 기준으로 그립니다.</EmptyState>}
    {data && <div className={spc.loading ? 'is-refreshing' : ''}>
      <PageNotice tone="info" title="통계적 공정관리(SPC) · 머신러닝이 아닌 통계 규칙 기반입니다.">
        부분군 {data.subgroup_size}개 표본(1분마다 12초 간격) · 기준선은 실행 초기 {data.baseline?.subgroups ?? 0}개 부분군 · {data.rules_version}
      </PageNotice>
      {data.status === 'INSUFFICIENT_DATA' && <PageNotice tone="unknown" title="관리한계를 계산할 데이터가 부족합니다.">기준선 부분군이 20개 이상 쌓이면 관리한계를 표시합니다. 현재 완전한 부분군 {data.subgroups.length}개.</PageNotice>}
      <div className="kpi-grid">
        <KpiTile label="Cp (공정 산포)" value={decimal(data.capability?.cp)} tone={capabilityTone(data.capability?.cp)}>{capabilityText(data.capability?.cp)}{data.capability ? ` · 부분군 ${data.capability.subgroups}개` : ''}</KpiTile>
        <KpiTile label="Cpk (치우침 포함)" value={decimal(data.capability?.cpk)} tone={capabilityTone(data.capability?.cpk)}>{capabilityText(data.capability?.cpk)} · 1.33 이상 권장</KpiTile>
        <KpiTile label="이상 신호" value={String(violations.length)} unit="건" tone={violations.length ? 'bad' : data.status === 'OK' ? 'good' : 'unknown'}>규칙 위반 부분군 수</KpiTile>
        <KpiTile label="제외된 부분군" value={String(data.excluded.count)} unit="개" tone={data.excluded.count ? 'unknown' : 'neutral'}>
          {Object.entries(data.excluded.reasons).filter(([, amount]) => amount > 0).map(([reason, amount]) => `${reason === 'MISSING' ? '누락' : reason === 'BAD' ? '품질 나쁨' : reason === 'TIME_UNKNOWN' ? '시간 불명' : reason} ${amount}`).join(' · ') || '없음'}
        </KpiTile>
      </div>
      {data.trend_warning && <div className={`trend-warning ${data.trend_warning.projected_cross ? 'warn' : 'neutral'}`}>
        <strong>{data.trend_warning.projected_cross ? `추세가 유지되면 ${clock(data.trend_warning.projected_cross.at)}경 ${data.trend_warning.projected_cross.limit}에 도달할 수 있습니다.` : '규격 한계 도달 추정 없음'}</strong>
        <span>최근 15분 선형 회귀 · 기울기 {data.trend_warning.slope_per_minute === null ? MISSING : `${decimal(data.trend_warning.slope_per_minute, 3)} ${data.unit}/분`} · 결정계수 {decimal(data.trend_warning.r2)}. 추세 기반 추정이며 발생을 보장하지 않습니다.</span>
      </div>}
      <Card title={`X̄ 관리도 · ${tagLabels[data.tag_id] ?? data.tag_id}`} kicker="AVERAGE">
        <ChartOrTable label="X̄ 관리도" chart={<TimeSeriesChart title="부분군 평균" unit={data.unit} showPoints
          from={Date.parse(data.subgroups[0]?.t ?? data.generated_at)} to={Date.parse(data.subgroups[data.subgroups.length - 1]?.t ?? data.generated_at) + 60e3}
          series={[{ id: 'mean', label: '부분군 평균', color: SERIES_COLORS[0], points: data.subgroups.map(item => ({ t: Date.parse(item.t), value: item.mean, note: item.violations.length ? item.violations.map(rule => ruleLabels[rule] ?? rule).join(', ') : undefined })) }]}
          thresholds={data.limits ? limitLines(data.limits.xbar) : []}
          markers={violations.map(item => ({ t: Date.parse(item.t), value: item.mean, label: item.violations.map(rule => ruleLabels[rule] ?? rule).join(', ') }))} />}
          table={<table className="data-table"><thead><tr><th scope="col">시각</th><th scope="col">평균</th><th scope="col">범위</th><th scope="col">표본</th><th scope="col">위반 규칙</th></tr></thead>
            <tbody>{data.subgroups.map(item => <tr key={item.t} className={item.violations.length ? 'is-alert' : ''}><th scope="row">{clock(item.t)}</th><td>{decimal(item.mean)}</td><td>{decimal(item.range)}</td><td>{item.n}</td><td>{item.violations.map(rule => ruleLabels[rule] ?? rule).join(', ') || '—'}</td></tr>)}</tbody></table>} />
      </Card>
      <Card title="R 관리도 · 부분군 범위" kicker="RANGE">
        <TimeSeriesChart title="부분군 범위" unit={data.unit} height={180} showPoints
          from={Date.parse(data.subgroups[0]?.t ?? data.generated_at)} to={Date.parse(data.subgroups[data.subgroups.length - 1]?.t ?? data.generated_at) + 60e3}
          series={[{ id: 'range', label: '부분군 범위', color: SERIES_COLORS[3], points: data.subgroups.map(item => ({ t: Date.parse(item.t), value: item.range })) }]}
          thresholds={data.limits ? limitLines(data.limits.range) : []} />
      </Card>
      {violations.length > 0 && <Card title="이상 신호 목록" kicker="SIGNALS">
        <ul className="signal-list">{violations.map(item => <li key={item.t}><b>{clock(item.t)}</b><span>평균 {decimal(item.mean)} {data.unit}</span><span>{item.violations.map(rule => ruleLabels[rule] ?? rule).join(' · ')}</span></li>)}</ul>
      </Card>}
      <p className="data-footnote">규격 {data.spec ? `${decimal(data.spec.lsl, 1)} ~ ${decimal(data.spec.usl, 1)} ${data.unit} (근거: ${data.spec.source}) · 개별값 기준이므로 X̄ 관리도에는 그리지 않고 공정능력 계산에만 사용합니다` : '없음 — 공정능력을 산정하지 않습니다'} · 기준 시각 {dateTime(data.generated_at)}</p>
    </div>}
  </div>
}
