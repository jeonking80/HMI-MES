# 온도·공압 알람 순수 조건 판정 · DEV-14의 두 번째 작은 단위

`apps/api/alarm_qualification.py`는 원본 관측과 호출자가 입증한 생산 단계·시간 근거를 받아 지속시간·히스테리시스를 판정한다. 현재 시각 조회, ID 생성, DB, HTTP, UI, PLC 조작을 수행하지 않는다. [알람 생명주기](ALARM_LIFECYCLE.md)에 전달할 `QualifiedCondition`을 만드는 순수 adapter와 두 결과를 함께 반환하는 함수가 있다. 실제 수집·영속 알람·운영 화면 통합은 아직 이 단위의 범위가 아니다.

기준은 [기획서 F04·§09](../HMI_MES_시스템_기획서_v1_0.md), [개발계획서 §5.4](../HMI_MES_개발계획서_v1_0.md)다. 온도·공압의 값은 초기 시뮬레이터 검증용 예시이며 실제 설비 허용 조건으로 주장하지 않는다.

## 버전이 고정된 초기 규칙

| 설정 | 발생 | 정상 복귀 |
|---|---|---|
| `TemperatureRuleConfig` | 생산 중 `abs(PV-setpoint_c) >= 10℃`가 5초 연속 | `abs(PV-setpoint_c) <= 7℃`가 5초 연속 |
| `PressureRuleConfig` | 생산 중 압력 `< 4.0 bar`가 3초 연속 | 압력 `>= 4.5 bar`가 3초 연속 |

온도 ±10℃ 경계는 **포함**, 복귀 ±7℃ 경계도 **포함**으로 이 예시 계약에서 명시한다. 두 온도 경계 사이와 두 압력 경계 사이는 히스테리시스 영역이다. 이 영역은 대기 중인 지속시간을 끊고 마지막 확정 조건을 유지한다. 아직 확정한 조건이 없으면 UNKNOWN이다. 공압 기본값 4.0/4.5 bar는 실제 `apps/simulator/plc.py`의 `PLCConfig` 기준을 참고해 버전에 고정한 예시다. API 모듈은 시뮬레이터 코드·메모리를 읽지 않으며, PLC의 즉시 인터록과 API 알람의 3초 지속시간은 별도 판정이다.

각 config는 `AlarmRule(rule_id, version, severity)`와 임계값·발생/복귀 지속시간·`max_gap_seconds`를 함께 고정한다. 온도는 `setpoint_c`도 필수다. 수치에는 유한한 숫자만 허용하고 Boolean·숫자 문자열·NaN·Infinity를 거절한다. 지속시간은 양수, 복귀 경계는 발생 경계 안쪽이어야 한다. `max_gap_seconds`는 0 초과 3초 이하이며 기본 3초다. 더 짧은 공백 기준은 새 설정 버전에 담을 수 있다.

## 입력·상태·공개 함수

- `RuleObservation`: 호출자가 부여한 `observation_id`, 정확한 `AlarmScope`, 현재 `rule_version`, 정규화된 `position`, UTC `observed_at`, `clock_epoch`, 유한한 0 이상 `monotonic_seconds`, `time_quality`, 단계 `phase`와 그 근거 ID `phase_references`, 원본 `TagSample`인 `actual`·선택적 `setpoint`, `historical`.
- `QualificationState`: scope·현재 config, 마지막 입력, 마지막 알려진 UTC 시각, 고정 UTC/단조 anchor, 마지막 확정 조건과 대기 조건·시작 시각. 직렬화·복원할 수 있는 frozen DTO다.
- `qualify(state, observation) -> QualificationResult`: 새 상태, ACTIVE/NORMAL/UNKNOWN, 판정 사유, 품질·시간 품질과 LIVE/DUPLICATE/HISTORY_ONLY 구분을 반환한다.
- `as_qualified_condition(result, new_alarm_id=...)`: 기존 생명주기 DTO로 변환한다. 새 알람 ID는 호출자가 주며 최초 발생에 필요하다.
- `qualify_and_apply(alarm_state, qualification_state, observation, new_alarm_id=...)`: scope·현재 rule을 결합해 두 순수 변경 결과를 반환한다. 후속 영속 adapter가 둘을 같은 트랜잭션으로 확정해야 한다.
- `replace_rule_config(state, new_config)`: 동일 rule ID·종류의 새 버전을 설치하며 확정 조건과 dwell을 초기화한다. 같은 버전에 다른 설정을 넣으면 거절한다. 반드시 생명주기의 명시적 `ChangeAlarmRule`과 함께 처리해야 한다.

온도는 정확히 `Sealer.TemperaturePV`와 `Sealer.TemperatureSP`, 압력은 `Sealer.AirPressure`의 Double 관측을 받는다. `TagSample`의 원래 ID·품질·SourceTimestamp·ServerTimestamp·acquired_at을 보존한다. 실제 sample의 acquired_at이 관측의 observed_at과 다르면 UNKNOWN이며 값을 정상 판정에 사용하지 않는다. 온도 SP 관측값은 현재 config의 setpoint_c와 정확히 같아야 한다. 수치 오차를 임의의 epsilon으로 숨기지 않는다.

## 생산 단계와 품질 불명

발생 판정은 PRODUCTION에서만 시작한다. WARMUP·PAUSED·OTHER에서 이상값을 보았다고 생산 중 알람을 새로 만들지 않는다. 이미 활성인 조건은 단계 변경만으로 닫지 않는다. 같은 설정 버전의 새 정상 관측이 충분히 이어지면 PAUSED·WARMUP·OTHER에서도 정상 복귀할 수 있다. 단계 자체가 UNKNOWN이면 발생·복귀 dwell을 모두 끊고 활성 조건을 유지한다.

원본 OPC StatusCode가 Bad 또는 Uncertain이거나 값이 null이면 UNKNOWN이다. `DWELL_PENDING`도 아직 조건을 확정하지 않았다는 뜻의 UNKNOWN이지만 이때 관측 품질 자체는 KNOWN일 수 있다. UNKNOWN은 기존 확정 조건을 지우지 않고 대기 중인 연속시간만 끊는다. 재연결·품질 회복 후에는 새 관측부터 발생/복귀 지속시간을 다시 채운다.

샘플 SP가 변경되면 `SETPOINT_VERSION_MISMATCH`로 보류하고 dwell을 초기화한다. 설정 변경 자체를 NORMAL로 만들지 않는다. 호출자는 새로운 config/version과 명시적 `ChangeAlarmRule`로 이전 알람을 `CONFIG_CHANGED` 사유로 종료한 뒤 새 버전의 지속시간을 시작해야 한다. 생명주기의 규칙 버전 이력과 과거 config 전체의 보관은 후속 영속 adapter 책임이다.

## 시간·순서·재전송

지속시간은 명시한 epoch의 단조 경과시간 차이로 계산한다. epoch 변경이나 직전 관측과 3초 이상 공백이 있으면 과거 dwell을 버리고 현재 새 관측부터 시작한다. 관측하지 못한 공백을 지속시간에 더하지 않는다. 단조 시각 역행·동일 시각, 마지막 알려진 UTC보다 역행한 시각, 호출자가 UNKNOWN으로 표시한 시간 근거는 판정을 보류한다. 고정 anchor 이후 UTC 경과와 단조 경과가 2초를 **넘게** 다르면 시간 품질 UNKNOWN으로 처리하고 dwell을 끊는다. 정확히 2초는 허용한다. 고정된 acquired UTC도 여러 관측에 걸친 누적 오차가 2초를 넘으면 판정을 보류한다.

SourceTimestamp가 오래됐거나 일정하다는 사실만으로 시간 오류나 단절을 만들지 않는다. 이것은 정상적으로 변하지 않는 설비 값에서도 가능하다. 절대 시계 오차·생산 단계·현재 관측의 유효성은 호출자가 별도로 입증해야 한다. 네트워크 전송 지연을 시계 오차로 간주하거나 서버 수신 시각만으로 과거 재전송의 생산 지속시간을 만드는 adapter는 이 계약을 충족하지 않는다. 현재 수집 경로에서 검증된 단조 timeline·phase·절대 시간 품질을 이 입력으로 만드는 통합은 아직 구현하지 않았다.

position은 raw 사건 seq와 capture seq를 섞어 비교한 값이 아니라 호출자가 영속적으로 부여한 판정 순서다. 같은 마지막 입력은 결과를 중복 적용하지 않는다. 같은 ID나 position의 다른 현재 입력은 충돌이며, 과거 position 또는 historical 입력은 현재 dwell을 바꾸지 않는다. 과거 입력은 HISTORY_ONLY로 표시하며 과거의 온도 지속시간을 재구성했다고 주장하지 않는다. 그 작업에는 별도 과거 timeline이 필요하다.

생명주기 adapter는 정규화한 입력 전체의 SHA-256을 판정 근거에 포함한다. 기존 생명주기 receipt가 있는 과거 입력도 같은 내용이면 당시 확정 receipt를 재사용해 현재 상태·dwell을 유지하며, 달라진 내용은 거절한다. 원본 sample ID와 단계 근거 ID, 측정값·품질·시각·clock epoch·단조 시각·판정 사유도 evidence에 남긴다. 입력 보관·원본 태그의 scope 결합·권한·transaction·재시작 대조는 후속 실제 adapter의 책임이다.

## 검증 범위

```sh
.venv/bin/ruff check apps/api/alarm_qualification.py tests/unit/test_alarm_qualification.py
.venv/bin/ruff format --check apps/api/alarm_qualification.py tests/unit/test_alarm_qualification.py
.venv/bin/pytest -q tests/unit/test_alarm_qualification.py tests/unit/test_alarm_lifecycle.py
```

2026-10-21: 규칙 판정·기존 생명주기 시험 52개 통과. 온도 양쪽 경계·압력 경계와 deadband, 예열/일시정지, 발생·복귀 중 품질 불명, 지속시간 공백·epoch 변경·UTC 역행·2초 경계·고정 SourceTimestamp, 버전·설정 변경, 동일/과거 입력의 멱등성과 충돌, 비유한 수치·설정 경계, 원본 근거 보존과 JSON 복원을 검증한다. 이 결과는 순수 qualification 단위의 검증이며 DEV-14 전체·알람 DB·권한·운영 화면·T04~T06 전체 통과를 뜻하지 않는다.
