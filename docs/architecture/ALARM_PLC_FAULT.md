# 전역 PLC fault의 논리 Line 알람 · 첫 구현 단위

`apps/api/alarm_fault.py`는 현재 pinned simulator 생산자의 원본 1.2 capture 및 검증된 source 사건으로 전역 PLC fault를 판정한다. `alarm_store.apply_fault_capture`는 이를 실제 PostgreSQL 생명주기·receipt·outbox에 원자적으로 저장한다. **혼합 consumer/runtime 연결은 아직 하지 않았다.** 기존 analog runtime에 이 기능이 자동 활성화되었다고 표시하지 않는다.

현재 `apps/simulator/tags.py`는 하나의 PLC latch를 Line/Infeed/Sealer/Outfeed의 FaultCode에 반복 투영하고 제어 사건의 equipment_id는 Line이다. 따라서 이 단위는 **asset_id="Line"인 논리 line 사건 하나**를 만든다. 같은 fault를 세 설비의 독립 고장으로 세거나 실링기 원인이라고 추정하지 않는다. 기획서 §07의 설비별 고장 원인 귀속, 고장에서 MES 정지 구간까지의 연결, 실제 T04/T05 전체 수락은 별도 미완성이다. 임의 PLC의 기존 태그·사건에도 이 reset 의미가 성립한다고 주장하지 않는다.

## 설정·scope와 schema 0012

```text
PLCFaultRuleConfig {
  kind: "PLC_FAULT",
  rule: {rule_id, version, severity}
}
```

severity와 version은 기존처럼 호출자가 명시하는 Identifier다. 새 임계값·지속시간·자동 reset 옵션은 없다. 정상적인 현재 FaultCode 비영은 즉시 발생 조건이며 생산/예열 상태와 무관하다. ESTOP가 Line.State에서 우선하더라도 남아 있는 fault latch는 발생·유지해야 한다.

`apps/api/alarm_rules.py`의 StoredRuleConfig는 기존 Temperature/Pressure와 PLC_FAULT의 union이다. 기존 analog RuleConfig·qualifier는 analog 전용으로 유지한다. scope는 analog=Sealer, PLC_FAULT=Line에 정확히 결합하며 설정 교체도 규칙 ID·종류를 바꾸지 않는다. 기존 Sealer scope의 JSON/hash, 설정 JSON과 checkpoint 형식을 바꾸지 않는다.

migration `0012_alarm_line_fault`는 0011 위에 다음을 추가한다.

- alarm_channels.asset_id는 Sealer 또는 논리 Line이다.
- `physical_asset_code`는 Line일 때 NULL, Sealer일 때 Sealer인 저장 generated column이다.
- `(line_id,physical_asset_code)` FK는 기존 물리 Sealer의 자산 결합을 유지한다. Line은 fake assets row를 만들지 않는다.
- 기존 source/stream/line FK가 논리 line의 실제 등록 결합을 계속 보장한다.
- channel INSERT는 column을 명시하여 generated column을 쓰지 않는다. 기존 지원 head의 analog INSERT도 유지한다.
- Line channel 또는 digital config 이력이 있으면 downgrade를 거절한다. 사용하지 않은 확장은 이전 제약으로 복귀할 수 있다.

## 순수 입력·판정

`normalize_fault_capture(batch,config,context,events=...,verified_through_event_seq=...)`는 FaultObservation을 만든다. context는 기존 서버 소유 AlarmProcessingContext이며 raw batch나 사용자 HTTP에서 받아 신뢰해서는 안 된다. 원본 capture identity/body hash, source/stream/line, rule identity/version, 정규화 position, LIVE/HISTORICAL 및 clock assessment를 정확히 결합한다. position을 capture_seq나 source event seq에서 자동 산출하지 않는다.

complete 48 tag 존재를 요구하고, 다음 네 tag만 Good/non-null/같은 captured_at 품질 조건에 사용한다.

| 원본 tag | 용도 |
|---|---|
| Line.FaultCode | 하나의 실제 latch 값 |
| Line.State | fault 상태와 zero의 모순 확인; ESTOP가 비영 fault를 숨기지 않음 |
| Line.EventSeq | 이 capture 시점의 source 사건 상한 |
| Line.ControlVersion | reset 전이의 제어 버전 대조 |

communication은 GOOD이어야 하고 캡처 시간 품질도 명시한 API 기준에서 KNOWN이어야 한다. 원본 UTC read bracket도 보존하며 span이 3초 이상이면 UNKNOWN이다. source 사건의 occurred_at은 원본 증거로 보관하지만 gateway/API 시계의 증명으로 사용하지 않는다. 상태·값·시간·통신 불명은 활성 조건을 정상 복귀시키지 않는다.

`qualify_fault(checkpoint,observation)`는 다음을 적용한다.

- Good 비영 FaultCode와 일치하는 applied 최신 Line FAULT_CHANGED(active=true)가 있으면 ACTIVE다. ESTOP 우선 상태도 허용한다.
- 비영 code가 바뀌어도 같은 열린 전역 이상은 한 occurrence를 유지한다.
- 아직 열린 occurrence가 없고 알려진 현재 code=0이면 NORMAL이다. 처음부터 이전 reset 이력이 필요하지는 않다.
- 열린 occurrence에서 code=0만 보이면 UNKNOWN이다. 아래 reset proof가 있어야 NORMAL이다.
- 처음 발생 capture의 EventSeq를 occurrence fence로 보존한다. 이전 fence 이하의 reset을 새 고장의 정상 복귀로 재사용하지 않는다.
- checkpoint의 `highest_event_ceiling`은 Good captured EventSeq가 실제 contiguous verified frontier 이하일 때만 최댓값으로 보존한다. 시간·통신 UNKNOWN에서도 source 순서 floor 자체는 낮추지 않으며, rule version 변경·restart도 이 floor를 보존한다. historical 입력은 checkpoint 전체를 변경하지 않는다.
- source EventSeq 역행, 미검증 event 상한, 누락·Bad/Uncertain·시간·통신 불명은 UNKNOWN이다. 과거 evidence는 HISTORY_ONLY이며 현재 checkpoint와 outbox를 바꾸지 않는다.
- restart/gap의 continuity break는 UNKNOWN을 남기며 활성 latch/fence를 보존한다. 복구 뒤에도 같은 occurrence의 완전한 검증 proof 없이 닫지 않는다.

## 현재 생산자의 RESET 완료 proof

현재 simulator controller는 fault 원인을 지워도 latch를 유지한다. RESET_FAULT의 PLC guard가 원인 해소·정지·안전 조건을 확인하고 성공한 경계에서만 latch를 지우며, 같은 source transaction에 다음 사건을 순서대로 저장한다.

1. STATE_CHANGED(previous_state=FAULT,state=STOPPED,control_version=v), seq=n.
2. FAULT_CHANGED(fault_code=현재 latch code,active=false), seq=n+1.

정상 복귀는 위 두 사건의 source/stream/equipment=Line/execution/occurred_at이 정확히 일치하고, 기존 발생 fence 뒤에 있으며, captured ControlVersion이 v 이상일 때만 가능하다. 그 clear 바로 전의 fault 사건도 active=true이며 같은 code인지 확인한다. code 교체가 통신 불명 중 발생했더라도 저장된 실제 latest fault chain으로 대조하므로 초기 code와 달라졌다는 이유만으로 알람이 영원히 남지 않는다. 반대로 다른 code의 clear는 증명이 아니다.

이는 **현재 pinned simulator의 producer invariant**다. ACK는 이 source 사건을 만들지 않으며 reset·원인 해소·재가동이 아니다. 외부 PLC를 붙일 때 이 의미를 입증하거나 명시적인 reset 완료/원인 command ID가 있는 새 사건 계약을 설계해야 한다.

Gateway는 tag read 후 ReadEvents를 수행하므로 같은 batch.events 안에 capture보다 나중인 사건이 들어올 수 있다. proof는 반드시 **원본 Good Line.EventSeq 이하**만 사용한다. 또한 실제 DB의 contiguous verified_through_event_seq가 captured ceiling 이상이어야 한다. raw event seq와 capture seq·정규화 position을 서로 비교하지 않는다. reset pair가 여러 batch/page로 나뉘면 먼저 한 조각만 보고 닫지 않고, 두 조각이 실제 applied/verified 이력에 모인 뒤에만 사용한다.

## 실제 store 연결과 replay

`apply_fault_capture(db,batch,context,new_alarm_id=...)`는 신뢰한 내부 adapter용 공개 함수다. HTTP 입력 경로가 아니다. schema→gateway→line/source→정렬된 channel/input 잠금 순서를 사용하고 현재 source 바인딩과 reconciliation 상태를 새 입력에서 확인한다. 원본 batch가 실제 ingest_batches의 identity/hash/body와 같은지 확인한 뒤, applied event_inbox에서 captured ceiling 안의 최신 fault와 reset 앞 state/active fault를 조회한다. 호출자가 임의 proof 사건을 넣어 store를 통과시킬 수 없다.

원래 receipt의 immutable capture/context/ID가 정확하면 현재 source conflict·capture generation 변경보다 **먼저 원래 receipt를 재조회**한다. 이미 확정한 처리를 후속 불명 상태 때문에 다시 실행하지 않는다. 새 입력은 현재 binding/flag 검사에 실패하면 적용하지 않는다. 다른 원본 내용이나 재생성한 position/context로 같은 ID를 사용하면 충돌한다.

첫 처리의 정규화 관측·검증 frontier·원본 사건 proof는 receipt intent 안에 보존한다. 이후 검증 frontier가 바뀌어도 retry가 proof를 다시 만들어 기존 처리를 바꾸지 않는다. 같은 capture/rule 재전송은 outbox·instance를 추가하지 않는다. 과거 처리 context는 store 전에 재생성하지 않고 원래 durable context를 먼저 복구해야 한다.

FaultCheckpoint는 기존 qualification JSON 위치에 별도 형식으로 저장한다. 기존 analog decoder는 그대로 유지한다. digital channel에 raw QualifiedCondition이나 analog RuleObservation을 적용하여 reset 검증을 우회하는 기존 helper 경로는 거절한다. operator ACK/note·규칙 변경·CONFIG_CHANGED 종료·네 상태와 원래 action ID 복구는 기존 store를 재사용한다.

## HTTP와 남은 통합

기존 alarm GET/ACK/note 경로는 `asset_id=Line`의 정확한 scope를 지원한다. Line list coverage의 implemented_kinds는 PLC_FAULT이며 fake physical asset을 요구하지 않는다. consumer_status와 runtime_configuration_status는 UNKNOWN을 유지한다. 이 first unit의 scope read가 가능하다는 사실은 digital consumer가 현재 실행 중이라는 증거가 아니다.

다음 단위는 여러 scope/규칙을 처리하는 mixed consumer와 runtime/provisioning이다. 해당 coordinator는 source-first 순서를 유지하고 **전체 채널의 정렬된 lock union**을 먼저 획득한 뒤 각 store input을 적용해야 한다. 이번 작업에서 기존 alarm_consumer/runtime의 판정·시작 설정을 수정하지 않았다. 그다음 실제 App B fault 주입→HMI ACK→원인 지속 중 RESET 거절→원인 해소→별도 RESET 성공→정상 복귀/출력 정지·자동 시작 없음의 실제 fault pipeline 및 Line UI를 검증해야 한다.

## 로컬 검증

```sh
.venv/bin/ruff check apps/api/alarm_rules.py apps/api/alarm_fault.py apps/api/alarm_store.py apps/api/migrations/versions/0012_alarm_line_fault.py tests/unit/test_alarm_fault.py tests/integration/test_alarm_line_scope.py
.venv/bin/pytest -q tests/unit/test_alarm_fault.py tests/integration/test_alarm_line_scope.py
```

순수 시험과 실제 일회용 PostgreSQL 시험은 ESTOP priority와 실제 simulator 투영, source ceiling, 정확한 reset proof·wrong code·split page·gap·시계/통신 불명, code 교체·과거 replay, source conflict/generation 변경 뒤 원래 receipt 복구, 한 전역 occurrence·재발·outbox 멱등, Line FK/downgrade, actor ACK/note 및 HTTP target 격리를 검증한다. 별도 high review는 migration의 실제 late-DDL 실패 rollback과 Sealer 기존 행/hash/FK 보존을 대조한다. 이는 설비별 고장 귀속·MES 정지 구간·T04/T05 전체·프로젝트 완료를 뜻하지 않는다.

## 서버 소유 강제 UNKNOWN seam

`apply_fault_capture(..., forced_unknown_reason=None)` 및 순수 normalizer는 선택적인
`FaultUnknownReason`을 받는다. 허용 값은 `CAPTURE_SEQUENCE_GAP`,
`CAPTURE_FRESHNESS_UNKNOWN`, `CONFIG_INTERVAL_UNKNOWN`이다. coordinator만 원래 durable
순서·freshness·설치 이력 판정으로 선택한다. HTTP/gateway/config 사용자 입력이 아니며,
raw batch·StatusCode·communication·clock assessment를 꾸미지 않는다.

현재 capture의 강제 UNKNOWN은 실제 시간 유효성을 별도로 보존하며 condition/quality를
UNKNOWN으로 제한한다. 검증된 Good 원본 EventSeq의 durable 최댓값은 올릴 수 있으나,
latch·occurrence fence·last fault code·last known time은 변경하지 않는다. 통신/시계가
본래 UNKNOWN인 경우에도 검증된 source 순서 사실은 보존한다. HISTORICAL은 강제 사유와
무관하게 checkpoint 전체를 그대로 유지한다. 미검증/Bad EventSeq는 floor를 올리지 않는다.

`CONFIG_INTERVAL_UNKNOWN`은 잠근 현재 설치 PLC_FAULT 규칙만 processing owner로 허용한다.
이 context.rule은 해당 capture를 지배한 버전의 증명이 아니다. coordinator의 capture decision은
`QUARANTINED` / `NO_UNAMBIGUOUS_INSTALLED_CONFIG`로 유지한다. 정확한 현재 owner를 결정하지
못하면 기존 거절 경로를 사용한다. 이 seam은 source binding/reconciliation 등 새 쓰기 검사를
완화하지 않는다.

강제 사유는 원래 receipt의 immutable input·정규화 observation·evidence에 보존한다. None이면
stable input에 새 key를 넣지 않아 기존 receipt 재조회를 유지한다. 옛 observation은 기본값
None으로 해석한다. 같은 ID의 사유 변경은 충돌이며, retry는 원래 context/사유를 먼저 복구한다.
확정 receipt의 정확한 재조회는 이후 source/config 변경보다 먼저 수행한다. shared processing
context와 analog consumer/runtime은 이 변경으로 수정하지 않는다.


역사 fault receipt를 저장할 때는 channel checkpoint UPDATE 자체를 생략한다. 따라서 새 optional
필드가 없던 pre-seam qualification 원문에도 null key를 추가하지 않으며, 이미 저장된 non-null
사유를 지우지 않는다. 명시적 HISTORICAL과 낮은 position으로 판정한 역사 입력 모두 동일하게
원래 qualification JSON·channel position·last observed time을 보존한다.
