# D1: 단일 in-flight 필드 read 완료 지연 engine

독립 stage의 내부 알고리즘이다. field component V2, V3 schema, HTTP/CLI/UI, OPC publisher와
연결하지 않았다. repo 통합·실제 OPC/게이트웨이/브라우저·DB 실행을 수행하지 않는다.

## 범위와 입력

`apps/simulator/field_delay.py`의 `FieldDelayEngine`은 세 그룹의 순수 publication plan을 만든다.
현재 모드는 내부 NORMAL/DELAY뿐이며 사용자 요청 모델 또는 durable V3 payload가 아니다.
`DelayPolicy`는 신뢰된 committed 설정 view다. duration은 엄격한 정수 100..10,000ms,
100ms 단위다. 외부 입력이 monotonic/deadline/품질/값/generation을 지정하는 API는 없다.

- 그룹마다 in-flight read 하나, 전체 최대 세 개다. 마지막 전달 frame도 그룹별 하나다.
  engine은 폐기된 plan을 등록하거나 누적하지 않는다. publisher도 실패한 candidate를 버린다.
- `RawGroupFrame`은 완전한 dictionary 그룹의 immutable typed 필드와 실제 acquisition 시각을
  받는다. 원래 SourceTimestamp가 오래되거나 필드마다 달라도 수정하지 않는다.
- 서버 clock owner가 읽은 finite/nonnegative monotonic을 사용한다. engine은 마지막 **채택된**
  publication frontier 아래의 역행을 거절한다. prepare만 한 미래 candidate는 frontier를
  전진시키지 않는다. 채택되지 않은 clock reading 사이의 실제 시계 역행 감시는 미래 서버
  wiring의 별도 책임이다. UTC에서 monotonic이나 clock 정확도를 추정하지 않는다.

`acquisition_frame_id`는 engine 내부 원본취득 provenance다. 한 지연 frame을 반복 전달하는
동안 유지되고 새 가상 read마다 새로 생성한다. **gateway `TagSample.sample_id` 또는 capture
ID가 아니다.** 게이트웨이는 새 capture마다 원래 계약대로 별도의 고유 sample ID를 만든다.
원본이 같다는 이유로 transport sample ID를 재사용하면 안 된다.

## queue와 출력

read 시작 tick에 원본 전체 frame을 잡고 monotonic deadline을 계산한다. deadline 전에는
완료시키지 않는다. 늦은 tick에서도 완료 하나를 전달하고 그 tick의 현재 raw frame으로
다음 read 하나만 시작한다. 누락된 과거 read를 만들어 catch-up하거나 일정 latency 표본
pipeline으로 바꾸지 않는다. 각 queue의 immutable origin에는 원본값/HB/SourceTimestamp/raw
quality가 그대로 남는다. 동일 frame 재발행은 새 source observation이 아니다.

첫 완료 전에는 진입 checkpoint의 값·시각·HB를 유지하되 출력은 BadWaitingForInitialData다.
원래 품질은 `origin.fields`에 따로 보존한다. bootstrap origin의 `acquired_at=None`은 이것이
이 boot에서 새로 취득한 frame이 아니라는 뜻이다. 첫 완료 후에는 acquisition의 원래 raw
quality를 그대로 내보내고 다음 deadline까지 같은 source frame을 유지한다. publication UTC는
현재 발행 경계의 시각이며 raw SourceTimestamp와 구별한다.

`waiting_for_first_completion`은 초기 취득 완료 대기다. `read_in_flight`는 가상 read 진행이다.
이 metadata를 기존 field store의 config revision `pending`과 혼동하지 않는다. read 완료가
Good/현재성 보증이라는 label도 아니다. constant pipeline latency 검출이나 clock 검증을
이 engine의 시험 결과로 주장하지 않는다. 미래 gateway 3/5초 시험은 완료 사이 Good HB
공백에 대해 별도로 수행해야 한다.

## NORMAL provenance와 재시작

현재 NORMAL의 마지막 실제 pointer publication을 engine이 채택한 후에만 DELAY에 진입한다.
다음 revision, 정확한 그룹/현재 NORMAL checkpoint 동등성이 필요하다. prepare 후보만 만든
NORMAL frame은 증거가 아니다. 현재 raw NORMAL 품질이 Bad/Uncertain이어도 checkpoint를
보존하며 Good으로 바꾸지 않는다. DELAY duration 직접 변경은 거절하고 NORMAL의 실제
발행을 다시 거친다. 동일 policy는 no-op 설정 view다.

NORMAL 해제 plan은 현재 raw acquisition/품질을 즉시 사용하며 이전 queue를 flush한다.
그 plan을 실제 채택한 이후 오래된 DELAY plan은 pointer callback 전에 거절한다.

새 engine은 신뢰된 persisted DELAY policy로 시작할 수 있다. 기존 queue/deadline/마지막
전달 frame은 복구하지 않는다. bootstrap은 초기 Waiting 출력 전용이며 이 boot의 실제
NORMAL provenance로 등록하지 않는다. 새 read를 시작해 새 deadline 후에만 완료시킨다.
이 D1에는 SQLite persistence 또는 PLC recovery 변경이 없으며 V3 integration이 기존
RECOVERY_REQUIRED/자동 재가동 금지 조건을 지켜야 한다.

## 원자 publication seam

```python
plan = engine.prepare(
    policies=committed_view,
    raw_frames=acquisitions,
    monotonic_now=server_mono,
    published_at=server_utc,
)
# 여기서 모든 48개 UA DataValue를 완전히 인코딩한다. Line/비지연 그룹은 현재 값이다.
# future publisher의 한 소유 event-loop에서 prepare → encode → publish 사이 await 금지.
engine.publish(plan, lambda: assign_prebuilt_snapshot_pointer(new_snapshot))
```

`publish`는 owner/base-version과 callback의 동기 조건을 먼저 검증하고 다음 참조/version을
계산한다. 그 뒤 **신뢰된 callback의 단일 pointer 교체**와 no-fail 참조 채택을 연속 수행한다.
교체 뒤에는 await/DB/인코딩/추가 validation/callback이 없다. 재진입도 거절한다.

callback은 이미 만든 전체 map의 pointer assignment만 해야 한다. coroutine 또는 늦은
인코딩/DB/검증을 넣으면 안 된다. 외부 pointer를 바꾼 뒤 예외를 던지는 잘못된 callback은
engine이 되돌릴 수 없다. 따라서 실패 가능한 작업은 모두 callback 이전에 끝낸다.
D1은 실제 UA pointer 교체 여부를 증명할 수 없는 신뢰 경계를 숨기지 않는다.

prepare 실패나 pointer 교체 전 callback 실패는 engine 상태를 바꾸지 않는다. 동일 frontier의
candidate 둘은 경쟁할 수 있지만 하나를 채택한 뒤 다른 것은 stale이다. 중복 채택과 다른
engine/restart의 plan도 callback 전에 거절한다. prepare가 불변이므로 더 최신 미채택 plan이
과거 candidate를 자동 폐기하지는 않는다. future caller는 외부 config 변경 또는 await가
있었던 candidate를 버리고 새 committed config로 다시 준비해야 한다. D1은 DB를 조회하지
않아 전달받지 못한 외부 revision 변경을 알아낼 수 없다.

## 순수 검증

```sh
.venv/bin/pytest -q tests/unit/test_field_delay.py
.venv/bin/ruff check apps/simulator/field_delay.py tests/unit/test_field_delay.py
.venv/bin/ruff format --check apps/simulator/field_delay.py tests/unit/test_field_delay.py
```

검증 범위는 deadline 직전/정각/late tick·세 그룹 bound/독립성·catch-up 금지·품질/값/원래
시각/UInt64MAX·실제 NORMAL 채택 전 진입 금지·restart Waiting·NORMAL flush·stale/duplicate/
foreign plan·prepare/callback 실패·clock 거절·duration/type 검증이다. 48개 fake map의 단일
pointer callback은 순수 seam 시험이며 실제 UA transport의 원자성 증거로 표시하지 않는다.
V3 이관·권한/콘솔·실제 TLS/게이트웨이·브라우저·운영 recovery는 아직 다음 checkpoint다.
