# 시뮬레이터 공정 영속성 (DEV-07a)

구현 범위는 한 SQLite 파일에 **공정 snapshot·수량·생산/상태/고장 사건·공유 순번을 원자적으로 저장**하는 내부 Python 저장 계층이다. [순수 공정 모델](../../apps/simulator/process.py), [사건·페이지 계약](../../contracts/PROTOCOL.md), [DB·인증 설계](DATA_AUTH.md)를 사용한다. PLC·명령 journal이 동일 저장 트랜잭션에 참여할 수 있도록 내부 composition API를 제공한다. 운전 허용·사용자 인증·명령 성공 판정은 이 저장 계층이 담당하지 않는다.

## 소유권과 저장 형식

`SimulatorStorage(path)`는 프로세스 한 개가 소유한다. 런타임 경로 예시는 `.local/simulator/state.sqlite3`이며 앱 A의 DB나 게이트웨이 버퍼를 읽지 않는다. POSIX 로컬 파일시스템을 대상으로 canonical resolved path의 `.lock` 파일에 비차단 배타 잠금을 잡는다. 심볼릭 링크 별칭도 같은 잠금을 사용한다. hardlink DB 파일은 명시적으로 거절한다. 네트워크 파일시스템, DB/lock 파일을 실행 중 이동·교체하는 작업, 잠금을 따르지 않는 외부 writer는 지원하지 않는다. 새 DB·lock 파일은 0600으로 생성하며, 기존 파일/상위 디렉터리 권한은 운영자가 보호해야 한다.

연결마다 `foreign_keys=ON`, `journal_mode=WAL`, `synchronous=FULL`, `busy_timeout=5000`을 적용한다. 현재 `PRAGMA user_version=3`을 사용한다. v1 파일은 명시적인 `_migrate_schema_v1()` 경로로 사건 테이블과 인덱스를 v2 형태로 재구성하고, v2→v3 경로가 보관 근거 테이블을 추가한다. v1→v2의 목표 버전은 상수 2로 고정하며 현재 목표 버전으로 건너뛰지 않는다. 기존 파일의 전체 이관 체인은 하나의 외부 `BEGIN IMMEDIATE`에 참여하므로 두 번째 단계에서 실패해도 최초 schema·모든 데이터·user_version으로 rollback된다. 기존 사건·실행 snapshot·식별자·순번·명령 journal·runtime·감사·설정 테이블을 보존한다. 알 수 없는 기존 schema나 이미 삭제된 prefix의 검증 근거가 없는 v1/v2 파일은 초기화·근거 조작 대신 거절한다. 데이터 초기화·운영 백업 명령은 제공하지 않는다.

| 저장 테이블 | 의미·제약 |
|---|---|
| simulator_identity | 하나의 source_id·stream_id·counter_epoch, active execution FK, 마지막 seq·보관 시작 seq, recovery_required. 최초 식별자는 지정하거나 UUID로 생성하고 재시작 후 유지 |
| executions | execution_id PK, `ProcessState.to_json()` snapshot, 투입·양품·불량·폐기·재공·판정 cursor·tick의 별도 투영. 이전 실행 snapshot도 보존 |
| source_events | event_id 전역 PK, `(source_id,stream_id,seq)` UNIQUE, nullable execution FK, `(execution_id,item_id)` UNIQUE, typed 사건 JSON. 생산/폐기는 실행과 양의 item_id 필수, 상태/고장은 item_id NULL이며 execution은 선택 사항. identity source/stream FK |
| event_retention | 원본 사건별 보관 기준 recorded_at. 원본 사건과 FK로 연결하며 원본 삭제 시 함께 제거 |
| retention_checkpoint | 불변 source_id·stream_id·counter_epoch와 원본이 제거된 연속 prefix의 마지막 seq. 현재 identity 및 `retained_from_seq-1`과 일치해야 함 |
| execution_checkpoints | 삭제 prefix의 실행별 GOOD/REJECT/SCRAP 합계. UInt64 고정 길이 TEXT |
| archived_event_keys | 삭제 사건의 event ID·seq·execution·item ID·종류·원본 body SHA-256. 원본 JSON은 없지만 사건·물품 중복 방지 근거는 영구 유지 |

UInt64 투영·순번·물품 ID는 **20자리 0-padding TEXT + BINARY 정렬**로 저장한다. SQL CHECK로 길이·숫자·범위 `0..18446744073709551615`를 제한하며 사건 순번·물품 ID·보관 시작 순번은 1 이상이다. signed SQLite INTEGER/REAL로 cast·합산하지 않는다. Python 정수로 계산하고 범위를 검사한다. 마지막 사건 순번이 최댓값에 도달하면 추가 사건을 만드는 전이를 거절하며 wrap/stream 자동 교체를 하지 않는다.

공정 snapshot은 Python 내부 복원 형식이다. 정확한 Fraction 진행량은 정규 유리수 문자열로 보관하고 내부 JSON의 정수는 Python에서 손실 없이 복원한다. snapshot을 JavaScript 전송 DTO로 사용하지 않는다. 외부 사건·페이지는 공유 모델의 `model_dump_json()`을 사용하므로 UInt64가 정규 십진 **문자열**로 직렬화된다.

## 전이와 원자성

`register_execution(state)`는 `fresh_execution()`으로 생성할 수 있는 빈 실행만 등록한다. 현재 실행의 재공이 0이어야 하며 이전 snapshot은 지우지 않는다. 모든 snapshot을 확인하여 inactive 실행에 남은 재공도 교체를 막는다. 이는 내부 저장 조건이며 PLC의 LOAD_JOB 허용, 이전 MES 정산 완료 또는 원격 권한 판정을 대신하지 않는다.

`apply_transition(previous, result, occurred_at=...)`는 다음을 수행한다.

1. snapshot을 `ProcessState.from_json()`으로 다시 검증하고 pure `step`/명시적 `scrap` 결과와 비교한다. 같은 실행·설정, 정확한 물품 귀속, delta와 수량/재공/판정 cursor 일치를 확인한다. 이 재계산은 결과의 일관성 검사이며 PLC 운전 허용 조건은 아니다.
2. `BEGIN IMMEDIATE` 안에서 recovery gate, 모든 실행의 재공 귀속, 현재 active snapshot과 previous의 일치를 확인한다. stale/replayed 전이나 다른 실행 delta는 거절한다.
3. 각 종료 물품마다 수량 1의 `PRODUCTION_GOOD`, `PRODUCTION_REJECT`, `WIP_SCRAP` 사건을 만든다. 원래 execution_id, 물품 ID, source/stream, counter_epoch를 유지한다. 폐기는 명시적인 사유와 `EXPLICIT_WIP_SCRAP` reason_code가 필요하다. equipment_id는 source의 논리 그룹 `Outfeed`/`Line`이며 운영 DB asset 매핑은 후속 연동에서 명시해야 한다.
4. UUID 사건 ID·연속 seq, 새 snapshot·별도 수량 투영·마지막 seq를 **한 트랜잭션**으로 확정한다. 단독 호출은 커밋 뒤에 사건을 반환한다. 외부 `transaction()` 안의 반환 사건·snapshot은 최외곽 커밋 전까지 provisional이다. ID 충돌·범위 초과·검증 실패는 전체 rollback이며 새 ID로 결과 불명을 해결하는 명령 재실행 기능은 없다.

`transaction()`은 공정·제어 상태·명령 journal을 하나의 `BEGIN IMMEDIATE`에 묶는다. 내부 저장 메서드가 중첩 호출되면 savepoint를 사용하며 최외곽 문맥만 커밋한다. `connection`은 같은 연결을 사용하는 내부 컴포넌트용이다. 직접 `commit()`/`rollback()`하지 않는다. `transaction_generation`은 외부 `BEGIN` 성공마다 증가하고 중첩 savepoint에서는 유지되는 프로세스 내부 식별자다. rollback 후 새 트랜잭션도 새 값을 쓰며 영속 제어 버전이나 외부 계약 값이 아니다. transition의 after-commit hook도 최외곽 커밋까지 지연하고 rollback 시 취소한다.

`append_control_events((StateEventInput(...), FaultEventInput(...)))`는 검증된 typed descriptor만 받는다. descriptor는 equipment_id(기본 `Line`), 선택적 execution_id, UTC occurred_at, 공유 계약의 StatePayload/FaultPayload를 갖고 source/stream/seq/event_id 입력을 받지 않는다. 저장 계층이 UUID·공유 source/stream·연속 seq를 생성한다. execution을 지정하면 보존된 실행이어야 한다. 생산과 상태/고장 사건은 같은 전역 순번을 사용하며 페이지 순서와 복구 검사도 이를 따른다. 상태/고장 사건은 생산·불량·폐기 수량을 증가시키지 않는다. 재시작/안전 진단 이력을 남기기 위해 이 메서드는 recovery gate가 켜져 있어도 동작하지만 모든 snapshot의 내부 정합성을 확인하고 gate를 해제하지 않는다. 실제 제어 권한과 상태 전이 허용 여부는 PLC 호출자가 판정한다.

`update_process_parameters(previous, temperature_c=..., speed_m_per_min=...)`는 active snapshot의 stale 검사와 recovery gate를 적용한 뒤 ProcessConfig의 `temperature_sp_c`·`speed_m_per_min`만 바꾼 새 ProcessState를 반환한다. 실제 현재 온도, 슬롯·정확한 물품 진행량, 수량, tick, 판정 cursor, RNG, 나머지 config는 그대로 보존한다. 물리 운전 범위와 명령 권한은 PLC의 책임이다. 이 메서드도 외부 트랜잭션에 참여한다. executions는 현재 snapshot을 저장하며 설정의 불변 버전 이력은 상위 controller가 관리한다.

저장된 snapshot·사건·순번을 복원하다 발견한 형식 오류는 사용자 입력 오류와 구분하여 `StorageCorruption`으로 보고한다. 내부 컴포넌트는 자신의 journal 등에서 손상을 발견하면 `mark_corrupt()`를 호출한다. 활성 트랜잭션은 손상 표시로 커밋이 금지되고 전체 rollback 뒤 recovery gate가 latch된다. 호출자가 손상 예외를 잡아도 companion write만 커밋할 수 없다. 트랜잭션 밖의 `mark_corrupt()`는 gate를 즉시 영속화한다.

원본 사건 조회는 생산 수량을 다시 증가시키지 않는다. 동일 transition을 다시 쓰려 하면 previous snapshot이 맞지 않아 실패하고, 반복 `ReadEvents`는 같은 사건 ID/수량을 반환한다. 생산 카운터는 사건 외의 누적값을 더해 중복 집계하지 않는다.

## 재시작과 조회 경계

새 파일과 재오픈 모두 `recovery_required=True`로 시작한다. 라이브러리는 thread·타이머·자동 tick/START를 만들지 않는다. `load_execution()`은 진단용 snapshot 조회로 사용할 수 있지만 운전 허용 근거가 아니다.

**운영 replay인 `read_events()`도 recovery gate를 통과해야 한다.** 검사가 끝나기 전이나 재검사에 실패한 파일은 성공 `EventPage`를 내보내지 않는다. 페이지에 복구 표식이 없으므로 미검증 데이터를 typed 페이지로 노출하지 않는다.

내부 `acknowledge_recovery()`는 먼저 gate를 latch하고 다음을 확인한 뒤에만 해제한다.

- snapshot 내부 수량 보존·UInt64·물품 원래 실행, 별도 투영과 snapshot 일치
- active 실행 존재, inactive 실행에 재공 없음
- 모든 종류의 사건 row와 typed JSON의 ID/source/stream/seq/execution 일치 및 지정된 execution 존재
- 생산/폐기 사건의 epoch·수량 1·양의 물품 ID, 물품 범위·재공과 disposition 충돌 없음; 상태/고장 사건의 item_id NULL
- archive의 1..checkpoint 연속성, checkpoint와 보관 시작 경계 일치, archive·보관 사건 간 event ID·seq·물품 중복 없음
- archive에서 독립적으로 계산한 실행별 GOOD/REJECT/SCRAP 합계와 execution checkpoint 일치
- 보관 범위의 사건 순번 연속성과 마지막 seq, checkpoint 합계와 보관 사건 합계를 더한 수량이 snapshot 수량과 일치
- 모든 보관 사건에 정확히 하나의 유효한 보관 기준 시각이 있고 FK가 유효함

`acknowledge_recovery()`도 외부 트랜잭션 안에서 호출할 수 있어 controller 복구 증거와 gate 해제를 함께 확정한다. 성공 후 외부 rollback이면 이전 gate로 돌아가며 손상을 발견하면 전체 rollback 뒤 gate를 유지한다. 해제는 **저장 파일의 내부 정합성 확인**만 뜻한다. PLC 상태·명령 복구는 [제어기](SIMULATOR_CONTROLLER.md), 조작자 인증·명시적 확인은 [콘솔](SIMULATOR_CONSOLE.md)이 추가로 검사한다. 운영 MES 대조는 별도 연동 범위다. 이 함수를 사용자 API로 공개하지 않는다.

검사 뒤 `read_events(ReadEventsRequest(...))`는 한 read transaction의 보관 경계와 최대 1000개 연속 사건으로 `EventPage`를 만든다. 조회 시에도 row/JSON·물품 귀속을 검사하고 불일치가 있으면 replay를 중단하고 recovery를 latch한다. source/stream 불일치는 `STREAM_MISMATCH`, head 뒤 cursor는 `CURSOR_AHEAD`, 보관 시작 이전 cursor는 정확한 inclusive 누락 범위를 담은 `RETENTION_GAP`이다. 빈 새 stream 경계는 1..0이다.

## 7일 연속 prefix 보관 정리

내부 `prune_events(controller_guard=..., limit=1000)`는 7일 이상 보관한 가장 오래된 연속 prefix만 정리한다. limit은 1..1000의 정수다. controller guard는 필수 callable이며 같은 저장 트랜잭션 안에서 최신 영속 제어 상태·실행 최종 요약·미해결 고장·ACCEPTED 명령을 확인하고 정리가 막히면 예외를 발생시켜야 한다. 정상 반환은 None이다. 이전에 캐시한 허용 Boolean으로 대체하지 않는다. controller 근거가 없거나 미완료·미해결 상태이면 전체 정리를 보류하는 것이 상위 계층의 계약이다. 이 메서드는 신뢰된 내부 Python composition API이며 HTTP·CLI로 직접 공개하지 않는다.

메서드는 같은 트랜잭션에서 recovery gate와 가벼운 시계·시작 후 보호 기간을 먼저 확인한다. 아직 삭제를 허용하지 않는 시점에는 전체 이력을 훑지 않는다. 삭제 가능 시점이면 전체 저장 정합성을 다시 검사하고 controller guard를 실행한 후에만 원본 사건의 key·물품 disposition을 archive에 옮기고 실행별 checkpoint를 증가시킨다. 원본 JSON과 해당 보관 시각 삭제, 글로벌 checkpoint·`retained_from_seq` 갱신까지 한 트랜잭션이며 외부 composition이 있으면 그 커밋까지 provisional이다. 첫 비대상 사건에서 멈추며 중간 사건을 건너뛰어 삭제하지 않는다. `last_seq`·snapshot·카운터·최종 요약·제어 snapshot·명령·감사·설정 이력은 바꾸지 않는다. gateway 수집 ACK 유무는 삭제 기준이 아니다.

archive 삽입은 현재 원본 row의 ID·순번·execution·item·종류·본문 hash와 일치해야 한다. archive는 갱신·삭제할 수 없으며 새 원본 삽입은 archive의 event ID·순번·물품과 충돌하면 SQL trigger가 거절한다. 재시작 복구는 SQL 제약과 별개로 archive 합계·prefix·live key와의 중복을 다시 대조한다. archive에 남은 body hash는 원래 JSON을 복원하는 자료가 아니므로 삭제된 원본은 계속 복구 불가다.

checkpoint의 source·stream·epoch는 변경할 수 없다. 원본을 모두 정리한 후에도 이 불변 binding을 현재 영속 identity 및 소유자의 identity와 대조하여 과거 archive를 다른 stream으로 재귀속하지 못하게 한다. 새 빈 DB의 identity 최초 생성 때만 binding을 만들고, 기존 파일에 binding이 없으면 현재 identity를 복사해 복구 근거를 만들지 않고 거절한다.

보관 경계 이전 요청은 기존 계약대로 정확한 inclusive `RETENTION_GAP`을 반환한다. 전부 정리한 일반 stream은 `retained_from_seq=last_seq+1`인 빈 범위를 표현한다. `last_seq=UInt64_MAX`이면 다음 양의 UInt64 경계를 표현할 수 없으므로 마지막 사건은 남긴다. 순번을 wrap하거나 stream을 자동 교체하지 않는다.

### 시계와 보수적 삭제 보호

생성자의 `utc_clock`·`monotonic_clock`을 받아 프로세스 소유 시작 시 고정 UTC·단조 anchor를 잡는다. recorded_at과 정리 기준 시각은 이 고정 UTC에 단조 경과시간을 더해 계산한다. 원본 `occurred_at`이 오래되거나 미래여도 보관 기간을 단축하지 않는다. v1/v2 원본은 이관 시각부터 새 7일을 보호한다.

recorded_at은 삽입 후 UPDATE를 허용하지 않는다. 현재 사건을 유효한 과거 시각으로 잘못 갱신하여 조기 삭제하는 경로도 SQL trigger로 차단한다. 손상 시험에서 이 제약을 의도적으로 해제하는 것은 운영 경로가 아니며, 임의 외부 DB 편집에 대한 보안 보장을 뜻하지 않는다.

각 새 소유자는 실제 단조 경과시간으로 7일을 채우기 전 `STARTUP_HOLD`로 정리를 거절한다. downtime을 신뢰할 수 있는 경과시간으로 간주하지 않으며 재시작하면 이 보호 기간이 다시 시작된다. UTC와 고정 anchor 기반 시각의 차이가 2초를 넘거나 단조 시계가 역행·유효하지 않으면 그 소유자의 정리를 `CLOCK_FROZEN`으로 latch한다. 시계를 다시 맞추더라도 해당 소유자가 자동으로 삭제를 재개하지 않는다. 수집·사건 생성은 계속하며 보관 정리만 중단한다. 재시작 후에는 다시 새 7일 보호를 적용한다. 이 정책은 잘못된 UTC 전진으로 최근 사건을 즉시 오래된 것으로 취급하지 않는 대신, 빈번한 재시작·시계 오차·미해결 상태에서 원본을 7일보다 오래 보관할 수 있다.

자동 스케줄러·CLI·삭제 정책 우회·VACUUM·archive 압축은 이번 범위에 없다. 영구 key 근거와 과거 snapshot·최종 요약·명령 이력은 계속 증가하므로 디스크 상한을 보장하지 않는다. 현재 복구는 archive와 남은 사건 전체를 검산하므로 장시간 규모에서의 용량·시작 성능 측정은 별도 검증이다.

## 로컬 검증과 남은 범위

```sh
.venv/bin/ruff check apps/simulator/storage.py tests/integration/test_simulator_storage.py tests/integration/test_simulator_state_events.py tests/integration/test_simulator_transactions.py tests/integration/test_simulator_retention.py
.venv/bin/ruff format --check apps/simulator/storage.py tests/integration/test_simulator_storage.py tests/integration/test_simulator_state_events.py tests/integration/test_simulator_transactions.py tests/integration/test_simulator_retention.py
.venv/bin/python -m pytest -q tests/integration/test_simulator_storage.py tests/integration/test_simulator_state_events.py tests/integration/test_simulator_transactions.py tests/integration/test_simulator_retention.py
```

2026-10-20: storage·state events·transaction targeted suite **67개 통과**. v1→v2 전 데이터 보존 및 table 교체 후 예외의 전체 migration rollback, 생산/상태/고장 혼합 페이지·재오픈 복구, 설정 변경 시 모든 WIP/진행량 보존, 공정·설정·제어 사건·명령 journal의 외부 commit/rollback, 손상 예외를 잡은 경우의 전체 rollback과 gate latch를 검증했다. 새 임시 SQLite 파일에서 정상 양품/불량/폐기, A→B 귀속, 페이지 반복, ID 충돌·stale/불일치 전이 rollback, 재시작 순번/identity·recovery gate, UInt64 저장/직렬화·최대 순번 후 overflow 거절을 검증했다. 최댓값의 표현 검사는 인공 고순번·카운터 fixture를 사용하며 2^64개 물품을 생산했다는 뜻이 아니다. 사건 근거 없는 카운터 fixture는 recovery가 거절한다.

별도 Python 프로세스 두 개를 커밋 직전/직후 `os._exit()`로 실제 종료했다. 재오픈 시 직전에는 이전 물품·수량·snapshot·순번·사건 0개, 직후에는 새 물품·수량·snapshot·순번·사건 1개 전체가 유지됨을 확인했고 SQLite integrity/FK 검사도 수행했다. 예외만 발생시키는 시험으로 대체하지 않았다. 이는 **DEV-07a 저장 경계의 부분 crash 증거**이며 PLC/명령/알람/API/게이트웨이 전체 T09, 전원 장애 또는 운영 백업 복원 시험의 통과가 아니다.

독립 검토에서 발견한 replay 검증 경계, active pointer 변경으로 inactive 재공을 남기는 경로, 별칭 경로 잠금 우회를 수정하고 회귀 시험으로 고정했다. schema-valid GOOD→REJECT 변조도 재오픈 후 성공 replay 전에 전체 수량 검사가 차단하며, 심볼릭 링크 중복 소유와 hardlink 파일은 거절한다.

2026-10-21: storage·state events·transactions·retention 네 모듈 **97개 통과**, 그중 retention 전용 시험 29개다. 7일 보관 시험은 단조·UTC 시계를 함께 주입해 경계·새 소유자 보호·UTC 앞뒤 이동·단조 역행을 검증한다. 실제 7일 wall-clock 운전 시험을 대신하지 않는다. 정리 전후 원본·archive·checkpoint·경계의 실제 자식 프로세스 종료, 외부 트랜잭션 rollback, 원본 발생 시각과 보관 시각 구분, 제한된 prefix 정리와 최신 사건 보존, archive 후 물품·ID 중복 거절, 근거 변조 시 recovery 차단, v2 및 전체 v1→v3 migration 실패 rollback도 확인한다. 고순번 표현 fixture는 여전히 인공 근거이며 삭제 prefix의 archive가 없으면 재오픈 복구가 이를 거절한다.

저장 계층 밖의 PLC 제어/명령/인증은 해당 컴포넌트 검증으로 별도 판단한다. 남은 저장·운영 범위는 counter_epoch 변경/stream 교체의 승인 절차, 실제 7일 장시간 규모·성능, 운영 백업/복원, 프로세스 전체 crash·T09/T10 및 OPC/게이트웨이/MES 통합이다. 저장 계층의 통과를 DEV-07 전체 또는 M1 완료로 표시하지 않는다.
