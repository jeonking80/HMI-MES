# 같은 capture 알람 입력 정규화 · DEV-14의 다음 작은 단위

`apps/api/alarm_capture.py`는 원본 `BatchBody` 1.2와 서버가 제공하는 명시적 처리 context를 `RuleObservation`으로 변환하는 순수 함수다. DB·현재 시각·수신 지연·최신 tag 조회·네트워크·PLC 제어를 사용하지 않는다. 아직 알람 consumer나 영속 저장소가 아니며 migration 0010·ingestion 자동 처리·알림·화면·DEV-14 전체 완료를 뜻하지 않는다.

```python
normalize_alarm_capture(batch, config, context) -> NormalizedAlarmCapture
```

반환값은 `observation`과 원래 `context`를 함께 보관한다. 원본 analog actual/SP, 다섯 phase sample 전체, 원본 batch SHA-256, 시간 assessment SHA-256은 관측 및 qualification digest에 남는다. 생명주기 evidence에도 phase의 값·StatusCode·원래 시각·sample ID와 두 digest를 보존한다. 온도 actual이나 SP가 없으면 `None` 그대로 두고 qualification은 UNKNOWN으로 pending dwell을 끊는다. 누락 sample을 정상 값이나 합성 sample로 채우지 않는다. 실제로 제공된 다른 tag를 actual/SP로 쓰면 기존 conflict 규칙을 유지한다.

## 신뢰 경계·동일성

`AlarmProcessingContext`는 **서버 내부에서만** 만든다. gateway JSON에서 처리 context·line·asset·규칙 버전·시간 품질·historical을 받아 신뢰해서는 안 된다. 다음 값이 필요하다.

- `CaptureIdentity`: gateway/source/stream, capture_generation/capture_seq, batch_id.
- 검증한 원본 `batch_sha256`, 등록된 line/source/stream/asset의 `AlarmScope`, 적용할 immutable `AlarmRule` 전체.
- 저장소가 원자적으로 부여한 정규화 순서 `position` 및 `classification=LIVE|HISTORICAL`.
- 선택적인 capture 전용 `ClockAssessment`.

batch source/stream과 context scope, asset `Sealer`, capture identity, 원본 body hash 및 config의 rule identity/version/severity를 정확히 확인한다. batch에 line ID가 없으므로 line 등록 결합은 신뢰한 서버 context의 책임이다. raw capture_seq를 position으로 자동 복사하거나 raw 사건 seq와 비교하지 않는다.

관측 ID는 scope, rule ID/version 및 capture identity의 canonical JSON SHA-256으로 결정한다. position·historical·시간 proof는 ID 생성에 넣지 않는다. 같은 capture/rule에 새 처리 context를 부여하여 중복 ID를 새 관측으로 위장할 수 없게 한다. 원본 body나 정규화 내용 변경은 원래 digest와 비교하여 충돌로 처리한다.

**후속 영속 consumer는 재전송을 정규화하기 전에 저장된 capture/rule 처리 결과와 context를 먼저 조회해야 한다.** 당시의 position·historical·시간 assessment를 다시 생성하면 안 된다. 같은 ID의 변경된 입력은 qualification/lifecycle replay 검증에서 충돌한다. 해당 조회·원자적 저장·재시작 대조는 이번 순수 adapter에 구현하지 않았다.

## 같은 capture의 생산 단계

phase를 알려진 값으로 분류하려면 사전의 48 tag가 전부 같은 batch에 존재해야 한다. 모든 sample의 acquired_at은 batch captured_at과 같아야 하며, 이 조건은 BatchBody 1.2 자체에서도 검증한다. phase 품질에는 다음 **다섯 tag만** 사용한다.

| tag | 사용 |
|---|---|
| Line.State | 전체 line 상태 |
| Line.OperationMode | AUTO/MANUAL |
| Line.ActiveExecutionId | 유효한 현재 execution ID 또는 빈 값 |
| Sealer.State | Sealer 상태 |
| Sealer.RunFeedback | 실제 운전 피드백 |

다섯 tag 중 누락·Bad·Uncertain·null·잘못된 acquired_at이 있거나 두 상태가 다르면 UNKNOWN이다. complete 48 존재가 부족해도 UNKNOWN이다. 다른 tag의 Bad/Uncertain은 phase를 UNKNOWN으로 만들지 않는다. analog의 품질은 이후 qualification에서 별도로 판정한다.

| 같은 capture 조합 | phase |
|---|---|
| Line/Sealer RUNNING, AUTO, 유효하고 비어 있지 않은 execution, RunFeedback=true | PRODUCTION |
| Line/Sealer PAUSED, AUTO, 유효한 execution, RunFeedback=false | PAUSED |
| 일치하는 나머지 비운전 상태, RunFeedback=false | OTHER |
| RUNNING 조건 불일치, 그 외 상태에서 RunFeedback=true, 잘못된 execution ID | UNKNOWN |

`Sealer.Ready`는 phase 근거가 아니다. 생산 중 온도 이상이 Ready를 false로 만들더라도 phase가 사라져 알람 발생 dwell을 가리지 않는다. STOPPED에 execution이 연결되어 있다는 사실만으로 실제 예열을 입증할 수 없으므로 WARMUP을 만들어내지 않는다. phase reference는 실제 phase sample ID 순서이며 phase sample이 하나도 없으면 **실제 batch_id**를 provenance로 사용한다.

## 시간과 과거 분류

`capture_time`은 clock_epoch, 단조 capture 좌표 및 UTC read bracket을 제공할 뿐 시간 품질을 보증하지 않는다. `ClockAssessment`는 동일 원본 body hash·gateway·epoch에 결합된, measurement uncertainty와 capture UTC read bracket을 포함한 보수적인 gateway/API offset interval이다.

- `CONFIGURED_API_REFERENCE`: `reference_id`가 가리키는 deployment API 시간 기준을 신뢰한다는 명시적 가정이다. 같은 host의 명시적 시간 기준도 가능하다.
- `AUTHENTICATED_PROBE`: 호출자가 인증된 probe·동일 epoch의 UTC 연속성·probe 구간 안의 capture·측정 불확실성을 검증하여 interval을 만든다.

이 adapter는 신뢰한 평가 context의 결합과 interval을 검사한다. 실제 probe 수행·검증·deployment 설정·평가 생성은 후속 작업이다. 절대 UTC 정확도나 새 NTP 전제조건을 주장하지 않는다.

전체 interval이 **[-2초,+2초] 안에 포함**되어야 KNOWN이며 정확한 양 끝은 허용한다. proof 누락, 경계에 걸친 interval, 범위 밖 interval은 UNKNOWN이다. 잘못된 body/epoch/gateway에 대한 proof는 conflict다. 원본 analog SourceTimestamp가 오래 고정되거나 도착이 늦었다는 사실에서 skew나 historical을 추정하지 않는다. observed_at은 captured_at, monotonic_seconds/epoch는 원본 capture_time을 그대로 쓴다. `utc_read_span_seconds`도 `RuleObservation.monotonic_span_seconds`로 보존하며 observation digest와 생명주기 evidence에 남긴다. historical 여부는 처리 context만 결정하며, HISTORICAL 관측은 현재 dwell을 바꾸지 않는다.

## UTC read bracket의 지속시간 불확실성

capture의 실제 UTC read 단조 좌표는 `[m-span, m]` 안에 있다. offset interval이 ±2초 안이라는 사실만으로 capture 구간의 내부 위치나 5초 dwell을 입증하지 않는다. `RuleObservation.monotonic_span_seconds`는 0 이상이고 `monotonic_seconds`를 넘을 수 없으며, 기존 직접 호출 호환을 위한 기본값은 0이다. 실제 1.2 adapter는 기본값으로 대체하지 않고 원본 span을 반드시 전달한다.

qualification은 보수적으로 다음 두 경계를 사용한다.

- 발생·복귀 dwell의 확정 하한: `현재 m - 현재 span - candidate 시작 m`. candidate 시작은 가장 늦은 가능한 시점이다. 이 하한이 설정 duration 이상일 때만 확정한다.
- 관측 간격의 가능한 상한: `현재 m - (직전 m - 직전 span)`. 이 값이 `max_gap_seconds` 이상이면 이전 dwell을 버리고 현재 capture부터 다시 시작한다.

현재 span 자체가 `max_gap_seconds` 이상이면 TIME_UNKNOWN으로 pending dwell을 끊는다. 기본 3초와 정확히 같은 span도 UNKNOWN이며 더 짧은 규칙 gap 설정도 지킨다. 이런 불확실성이 생겼다고 이미 활성인 알람을 정상 복귀시키지는 않는다.

예를 들어 후보 시작이 단조 100초이고 현재 capture 끝이 105초라도 현재 span이 1초이면 확정 가능한 연속시간은 4초뿐이다. 다음 capture 끝이 106초·span 1초이면 처음으로 5초 하한을 채운다. 정상 복귀에도 같은 계산을 적용한다.

## 지원하지 않는 capture의 처리

schema 1.0/1.1, capture_time 없는 capture, sample 없는 event/gap batch에는 명시적 `UnsupportedAlarmCapture`를 낸다. 좌표를 합성하지 않는다. **후속 consumer는 지원하지 않는 관측 구간을 무시하고 다음 capture로 넘어가면 안 된다. pending 발생·복귀 dwell을 반드시 끊는 durable gap/UNKNOWN 결정을 기록해야 한다.** 그렇지 않으면 실제로 관측·입증하지 않은 구간을 연속시간으로 더할 수 있다. 이 오류 경로를 실제 저장소에 연결하는 작업은 아직 남아 있다.

## 로컬 검증

```sh
.venv/bin/ruff check apps/api/alarm_capture.py apps/api/alarm_qualification.py tests/unit/test_alarm_capture.py
.venv/bin/ruff format --check apps/api/alarm_capture.py apps/api/alarm_qualification.py tests/unit/test_alarm_capture.py
.venv/bin/pytest -q tests/unit/test_alarm_capture.py tests/unit/test_alarm_qualification.py tests/unit/test_alarm_lifecycle.py
```

순수 시험은 complete capture·phase 조합·관련/비관련 품질·원본 근거·누락 actual/SP의 UNKNOWN 및 dwell reset·정확한 scope/rule/hash/epoch 결합·±2초 경계·proof 부재·fixed SourceTimestamp·명시적 historical·legacy 오류·정규화에서 qualification/생명주기까지의 발생/복귀·중복 및 변경 재전송 충돌·nonzero read bracket의 조기 발생/복귀 방지·3초 가능한 gap 경계와 활성 latch 보존을 검증한다. DB 처리·수집에서 알람까지의 실제 지연·10명 부하·장시간 복구·T04~T06 전체 수락은 검증 범위에 포함하지 않는다.
