# 알람 순수 생명주기 · DEV-14의 첫 작은 단위

`apps/api/alarm_lifecycle.py`는 이미 판정된 조건을 받아 불변 상태와 변경 결과를
반환한다. DB·HTTP·UI·규칙 계산·현재 시각 조회·ID 생성·PLC 조작을 수행하지 않는다.
이 단위의 시험 통과는 DEV-14 또는 T04~T06 전체 완료가 아니다.

기준은 [기획서 F04·§09](../HMI_MES_시스템_기획서_v1_0.md),
[개발계획서 §5.4·DEV-14·T04~T06](../HMI_MES_개발계획서_v1_0.md),
[상태 전이 계약 §6](../../contracts/STATE_MACHINES.md#6-알람의-네-상태),
[DB·권한 설계](DATA_AUTH.md)다.

## 공개 DTO와 호출

모든 DTO는 `ContractModel`의 frozen·extra-forbid 정책을 사용한다. 중첩 기록은
불변 DTO와 tuple이고 측정값은 스칼라다. UInt64 측정값은 호출자가 원래 십진 문자열로
전달할 수 있다. 생명주기는 값을 숫자로 변환하지 않는다.

- `AlarmScope`: `line_id`, `source_id`, `stream_id`, `asset_id`.
- `AlarmRule`: `rule_id`, 불변 `version`, 호출자가 명시한 `severity`.
  임계값·지속시간 등 규칙 계산 설정은 이 모델의 범위 밖이다.
- `AlarmEvidence`: UTC `observed_at`, 필수 원본 `references`, 측정값 tuple,
  정규화된 판정 근거의 `quality`·`time_quality` (`KNOWN`/`UNKNOWN`).
  여기의 quality는 원본 OPC StatusCode를 Good으로 바꾸는 필드가 아니다.
- `QualifiedCondition`: `ACTIVE`/`NORMAL`/`UNKNOWN`, `event_id`, scope,
  `rule_version`, `position`, evidence, 선택적 `new_alarm_id`와 `historical`.
- `AcknowledgeAlarm`, `AddAlarmNote`, `EditAlarmNote`: 정확한 `alarm_id`,
  `event_id`, scope, actor, UTC `at`. 메모 수정에는 `note_id`와
  `expected_previous_action_id`를 명시한다.
- `ChangeAlarmRule`: 현재 `expected_version`, 새 rule, event ID·scope·actor·UTC 시각.
- `AlarmState`: 한 scope/rule의 규칙 버전 이력, 모든 인스턴스, action·입력 receipt,
  현재 판정의 position·시각 기준점.
- `apply_alarm(state, event) -> AlarmTransition`: 새 state, 생성·변경된 alarm ID,
  새 현재 발생에만 참인 `notify_current`를 반환한다.

```python
from datetime import UTC, datetime
from apps.api.alarm_lifecycle import (
    AlarmEvidence,
    AlarmRule,
    AlarmScope,
    AlarmState,
    QualifiedCondition,
    apply_alarm,
)

scope = AlarmScope(
    line_id="line-1",
    source_id="source-1",
    stream_id="stream-1",
    asset_id="Sealer",
)
state = AlarmState(
    scope=scope,
    rules=(AlarmRule(rule_id="plc-fault", version="v1", severity="WARNING"),),
)
# 명시적 호출 형식을 보이는 예시이며 실제 설비 관측을 생성하는 코드가 아니다.
result = apply_alarm(
    state,
    QualifiedCondition(
        event_id="qualified-fault-1",
        scope=scope,
        rule_version="v1",
        position=1,
        condition="ACTIVE",
        new_alarm_id="alarm-1",
        evidence=AlarmEvidence(
            observed_at=datetime(2026, 10, 21, tzinfo=UTC),
            references=("fault-event-1",),
        ),
    ),
)
assert result.state.active.status == "ACTIVE_UNACKNOWLEDGED"
```

호출자는 검증된 DTO를 넘긴다. 인증·역할 검사는 수행하지 않는다. 후속 API는
기존 `alarm_action` 권한과 세션·CSRF·Origin을 확인해야 하며, 규칙 변경 권한은
별도로 확인해야 한다. actor 문자열만으로 권한을 부여해서는 안 된다.

## 네 상태·재발·확인·조치

`AlarmInstance.status`는 저장된 조건 종료와 확인 기록에서 계산되는 읽기 전용
property다. 별도의 상태 문자열을 동시에 수정하지 않는다. JSON 직렬화에는
기초 필드가 포함되고 복원 후 property가 같은 상태를 계산한다.

| 조건 종료 | 확인 | 상태 |
|---|---|---|
| 없음 | 없음 | `ACTIVE_UNACKNOWLEDGED` |
| 없음 | 있음 | `ACTIVE_ACKNOWLEDGED` |
| 있음 | 없음 | `RETURNED_UNACKNOWLEDGED` |
| 있음 | 있음 | `RETURNED_ACKNOWLEDGED` |

동일 이상의 반복 ACTIVE는 새 인스턴스를 만들지 않는다. 조건이 닫힌 뒤 재발하면
호출자가 새 alarm ID를 제공해야 한다. 이전 복귀 미확인 인스턴스는 계속 남으며,
이전 인스턴스 확인은 새 활성 인스턴스를 확인하지 않는다. 이 scope/rule에서
물리 조건이 열린 인스턴스는 최대 하나다.

확인은 첫 사용자·시각을 보존한다. 같은 입력 ID·같은 내용의 재요청은 receipt도
추가하지 않는다. 다른 입력 ID의 반복 확인도 기존 확인과 action을 바꾸지 않으며
`NO_CHANGE` receipt를 남긴다. 같은 입력 ID의 다른 내용은 `AlarmConflict`다.
확인이나 메모는 물리 조건·리셋·재가동을 바꾸지 않는다.

메모는 추가와 수정을 별도 action으로 보존한다. 수정은 최신 메모 action ID가
일치해야 하며 이전 action ID, 수정 전후 내용, 사용자·시각을 새 action에 남긴다.
복귀 후에도 해당 인스턴스에 메모를 기록할 수 있다. 메모는 확인을 대신하지 않는다.

## 규칙·품질·시각·지연 이력

NORMAL 입력은 알려진 품질·시각 근거가 있을 때만 조건을 정상 복귀시킨다.
UNKNOWN 조건 또는 근거·시각 UNKNOWN은 기존 인스턴스와 마지막 알려진 시각을
보존하고 `UNKNOWN_EVIDENCE` receipt를 남긴다. 판정 순서는 진행하되 수집 불명
구간을 정상 복귀나 지속시간 충족으로 만들지 않는다. 새 position의 시각이 마지막
알려진 시각보다 역행하는 경우도 같은 방식으로 보류한다.

`position`은 후속 호출자가 영속적으로 부여하는 **정규화된 판정 순서**다.
원본 event seq와 capture seq를 서로 비교하는 값이 아니며 UTC 시각 순서도 아니다.
현재 position보다 작은 입력이나 명시적 historical 입력은 `HISTORY_ONLY` receipt만
추가한다. 현재 상태·현재 기준점·알림을 되돌리지 않는다. 같은 position의 다른
현재 입력은 충돌이다. 이 receipt는 지연 수신 근거 보관이며, 과거 알람 기간 전체를
재구성했다고 주장하지 않는다. 재구성·순번 연속성 검증은 후속 작업이다.

규칙 변경은 새 버전을 추가한다. 기존 버전 재사용과 기존 rule ID 변경은 거절한다.
열린 인스턴스가 있으면 `CONFIG_CHANGED`로 조건을 닫고 변경 사용자·시각·event를
기록한다. 정상 복귀 근거를 만들지 않으며 기존 규칙 버전·심각도·확인 기록을
보존한다. 네 상태의 RETURNED는 조건이 닫혔다는 축이므로 후속 화면은 반드시
`closure_reason`을 함께 표시해 설정 변경을 물리적 정상 복귀로 표현하지 않아야 한다.
새 규칙은 변경 시각 이후의 새 정상 판정으로 별도 인스턴스를 만든다.

## 후속 연결과 한계

입력 ACTIVE/NORMAL은 호출자가 이미 임계값·지속시간·생산 상태·품질·원본 순서를
검증한 결과여야 한다. 온도 ±10℃/5초·복귀 ±7℃/5초, 공압 하한·복귀 하한/3초,
통신 3초 지연·5초 알람, 원래 명령 ID의 결과 대조는 아직 계산하지 않는다.
시계 허용 오차 2초 판정, 정상 수집·생존 신호 age, 프로세스 재시작 시 지속시간
연속성도 호출자·후속 규칙 판정기의 책임이다. 오래된 SourceTimestamp만으로 정상
불변 값을 통신 장애로 판정해서는 안 된다.

현재 ledger는 순수 전이와 직렬화·복원의 의미를 검증하는 DTO다. 서비스 메모리에서
무기한 보관하는 캐시나 운영 DB 저장 방식이 아니다. 후속 저장 계층은 입력 중복 제거,
인스턴스·action·판정 기준점·receipt를 한 트랜잭션으로 저장하고 동시 확인·메모 수정·
발생을 직렬화해야 한다. 실제 DB 원자성·재시작·권한·화면·알림 전달은 이 단위에서
검증하지 않았다. 명령별 알람 키와 규칙 연결도 후속 범위다.

검증:

```sh
.venv/bin/ruff check apps/api/alarm_lifecycle.py tests/unit/test_alarm_lifecycle.py
.venv/bin/pytest -q tests/unit/test_alarm_lifecycle.py
```

시험은 확인 먼저/복귀 먼저, 지속 중 중복 방지, 이전 복귀 미확인 보존과 재발,
정확한 인스턴스·source 범위, 확인 재요청, 메모 전후 이력과 수정 경합,
UNKNOWN·역행 시각·지연 이력, 규칙 변경 종료 원인·버전 보존,
깊은 불변성과 원래 정수 문자열 보존·JSON 복원을 다룬다.
