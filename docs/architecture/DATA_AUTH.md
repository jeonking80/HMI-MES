# DB·설정·인증 설계 (DEV-04a)

## 19차 현행 기준 (2026-10-26)

19차 사용자 승인으로 수집 원본 보관은 `HMI_INGEST_RETENTION_HOURS` 기본 168시간·최소 3시간을 사용한다. 3 미만·비숫자는 시작을 거절하고, 구 설정 `HMI_INGEST_RETENTION_DAYS`는 단독·동시·빈 값 모두 거절한다. 현재 runtime 자동 정리는 schema 0023에서만 실행하며 schema 0022의 보관 종료 조회·재전송 거절은 호환 지원한다. 아래 18차/CX-40의 일 단위·영구 개별 영수증 설명은 당시 이력이며 이 현행 기준으로 대체된다. 변경 이유와 영향은 [INGEST_RETENTION.md](INGEST_RETENTION.md)를 따른다.

만료된 비참조 영수증은 실제 FK 목록을 검사한 뒤 최대 256개 정확 batch ID 청크(`ingest_retired_batch_chunks`, GIN)와 연속 capture 범위로 압축한다. 임의 batch ID 재사용을 정확히 판별하는 저장 바이트는 여전히 O(captures)다. 실제 FK 참조 영수증은 개별 보존한다. 현재 알람 판단은 매 capture마다 FK를 남기므로 그 경로에서는 사건 수에 비례하는 영수증 수를 달성할 수 없다. 사용자의 **기존 참조 보존·한계 보고** 결정에 따라 알람 판단·FK를 유지한다.

범위의 epoch별 clock endpoint와 `latest_capture`, 영속 최초 역행 registry를 함께 백업·복원한다. runtime에 임의 삭제 권한을 추가하지 않고 제한된 정리 함수가 원본 삭제·압축·누적 해시 보존을 같은 트랜잭션으로 처리한다. 0023 추가 이관 자체는 기존 자료를 삭제하지 않는다. 공개 이관·환경변수 변경·배포는 Claude 담당이며 이 작업은 공개 DB를 읽기 전용 범위로만 다룬다.

상태: **설계 산출물**. 이 문서는 마이그레이션, 인증 코드, 실행 구성 또는 DB 시험의 구현·통과를 뜻하지 않는다. 기준 정보 migration·bootstrap과 25개 실제 PostgreSQL 시험은 [DEV-04b 결과](DATABASE_BOOTSTRAP.md)에 기록한다. 나머지 업무 DB·인증·경합 검증은 후속 작업이다. 기준은 [기획서 §03~14](../HMI_MES_시스템_기획서_v1_0.md), [개발계획서 §4~6·10](../HMI_MES_개발계획서_v1_0.md), [태그 계약](../../contracts/tags/README.md), [프로토콜 계약](../../contracts/PROTOCOL.md)이다. 아래 테이블·설정 이름은 구현 설계이며 OPC 메서드·사건의 필드와 상태 이름은 DEV-02 계약에 매핑한다. 내부 배치 ACK는 [v1 전송 계약](../../contracts/TRANSPORT.md)과 검증 모델을 따른다. 계약 검증 통과는 실제 수집·커밋·전송 구현을 의미하지 않는다.

## 1. 저장소 소유권과 자료형

| 프로세스 | 소유 저장소 | 접근 경계 |
|---|---|---|
| 앱 A 운영 API | PostgreSQL 운영 DB | DB 계정으로 업무 트랜잭션 수행 |
| 앱 A 게이트웨이 | `.local/gateway/buffer.sqlite3` | API의 내부 HTTP 경로와 앱 B의 OPC UA에만 연결. 운영 DB 직접 접근 없음 |
| 앱 B 시뮬레이터·PLC·콘솔 | `.local/simulator/state.sqlite3` | 한 프로세스가 DB 소유. 앱 A DB·게이트웨이 파일 접근 없음 |
| 웹 | 저장소 없음 | 운영 API 세션만 사용. DB·OPC·서비스 자격증명 보유 금지 |

앱 B의 설비·PLC·콘솔은 내부 모듈이며 앱 A와 메모리·파일·DB를 공유하지 않는다. 두 앱의 설비 연동은 `SubmitCommand`, `GetCommandResult`, `ReadEvents`, `GetRunSummary`와 읽기 전용 태그를 통한 OPC UA만 사용한다. 콘솔 HTTP는 사람이 앱 B를 조작하는 경로이며 앱 A의 우회 연동 경로가 아니다.

- 사건·명령·실행·물품 ID는 계약에 맞는 불변 식별자다. PK와 FK의 동일 자료형을 사용하고 현재 화면에서 선택한 작업 ID를 사건 귀속에 사용하지 않는다. SQL 표의 ID는 별도 언급이 없으면 `text NOT NULL`이며 프로토콜 ID는 대소문자를 구별하는 공백 없는 1~128자 CHECK를 적용한다.
- PostgreSQL의 UInt64는 `numeric(20,0) CHECK (v >= 0 AND v <= 18446744073709551615)`로 보존한다. `seq`, 카운터, 제어·설정 버전과 UInt64형 태그에 적용한다. 순번이 1부터 시작하는 필드는 `v >= 1`로 강화한다. signed `bigint`는 최대 UInt64를 담지 못하므로 대신 쓰지 않는다. 초과·음수·소수 입력은 반올림 캐스팅 **전에** 경계 검증으로 거절한다.
- JSON/브라우저 경계에서는 UInt64를 정규 십진 문자열로 전달하고 JavaScript `Number`로 바꾸지 않는다. DB 드라이버도 문자열 또는 정확한 정수/Decimal로 변환한다. 내부 업무용 signed `bigint`를 선택하는 필드는 그 범위를 명시하며 UInt64 전체 범위를 보장한다고 표시하지 않는다.
- SQLite의 UInt64는 BINARY 정렬의 **0으로 채운 20자리 TEXT**로 저장한다. `CHECK(length(v)=20 AND v NOT GLOB '*[^0-9]*' AND v <= '18446744073709551615')`와 `typeof(v)='text'`를 적용한다. 동일 폭이므로 비교·인덱스 순서가 정수 순서와 같다. 증가·합산은 애플리케이션의 정확한 정수 연산과 범위 검사 후 같은 트랜잭션에서 저장한다. SQL의 TEXT→INTEGER/REAL 캐스팅이나 `SUM`으로 UInt64 계산을 하지 않는다. 전송 시 패딩을 제거해 정규 문자열로 변환한다.
- PostgreSQL 시각은 `timestamptz`, SQLite는 UTC 정규 ISO 8601 문자열로 보존한다. 원본·OPC 서버·수집·운영 저장 시각과 시간 품질을 구분한다. 기간은 `[start,end)`, 화면·일일 보고 경계는 `Asia/Seoul`이다. 시각 역행·2초 초과 오차는 집계 확정 보류 근거다.
- 태그 값은 OPC 자료형과 함께 저장한다. nullable 숫자 하나로 Bool·String·UInt64를 혼합하지 않는다. 원본 StatusCode, SourceTimestamp, ServerTimestamp, observed_at, stored_at, 최신성, 마지막 정상 관측을 별도 보존한다. Bad/Uncertain·누락은 정상값이나 0으로 치환하지 않는다.

## 2. PostgreSQL 초기 모델과 제약

모든 FK의 기본 삭제 정책은 `RESTRICT`다. 운영 API 계정은 마이그레이션·소유자 권한을 갖지 않는다. 원본 사건·명령·감사 테이블은 일반 업무 경로에서 UPDATE/DELETE하지 않으며 상태 투영 테이블과 추가 이력에 변경을 기록한다. 보관 작업은 별도 제한 계정과 명시적 보관 조건으로만 삭제한다.

| 테이블 묶음 | 핵심 필드·필수 제약 | 초기 인덱스·조회 |
|---|---|---|
| `sites`, `lines`, `assets` | 안정된 code UNIQUE, asset→line→site FK | `assets(line_id)` |
| `tag_definitions`, `alarm_rules` | tag logical_name UNIQUE, `(source_id,namespace_uri,node_identifier)` UNIQUE. 주소에 namespace index 저장 금지. 자료형·단위·활성 여부·수집주기·규칙 버전. 규칙은 새 버전으로 변경 | `tag_definitions(asset_id,enabled)`, `alarm_rules(asset_id,version)` UNIQUE |
| `recipe_versions`, `execution_recipe_applications` | `(recipe_id,version)` UNIQUE, 온도·속도·시험 범위. 버전 내용 불변. 실행에 사용한 버전·실제 값·적용 사건/시각·명령 FK 고정 | `(execution_id,applied_at)` |
| `users`, `roles`, `user_roles`, `auth_sessions` | 정규화 login UNIQUE, 암호 해시, active, auth_version. `(user_id,role_id)` PK. session token digest UNIQUE, 만료·취소·CSRF secret·auth_version | `auth_sessions(user_id)`, `(expires_at)` |
| `service_principals`, `service_credentials` | gateway principal→허용 source/line. token digest UNIQUE, key_id, 만료·취소·회전 시각. 평문 token 보관 금지 | `(principal_id,revoked_at)` |
| `source_streams`, `ingest_batches` | `(source_id,stream_id)` PK. `(gateway_id,batch_id)` UNIQUE, 정규 payload hash·영속 ACK 결과. 같은 ID 다른 내용은 충돌 | `ingest_batches(stored_at)` |
| `event_inbox`, `event_apply_errors` | `event_id` PK, `(source_id,stream_id,seq)` UNIQUE, 원본 내용/hash·schema_version·적용 상태. 알 수 없는 실행은 여기 보류하며 잘못된 execution FK를 만들지 않음 | `(source_id,stream_id,seq)`, 미적용 부분 인덱스 `(stored_at) WHERE applied_at IS NULL` |
| `production_events` | `event_id` PK/FK→inbox, `execution_id NOT NULL` FK, asset FK, 종류 GOOD/REJECT/WIP_SCRAP, 정수 양수 quantity, 폐기 사유 필수. source/stream/seq는 inbox의 불변 식별에 연결 | `(execution_id,occurred_at,event_id)`, `(occurred_at)` |
| `equipment_state_events` | inbox event FK, asset·이전/새 상태·원본 시각·품질. 원본 순번과 발생 실행 보존 | `(asset_id,occurred_at,event_id)` |
| `stream_progress`, `stream_gaps` | progress PK `(source_id,stream_id)`, 수신 위치·마지막 연속 검증 seq 분리. gap은 시작/끝 seq CHECK(start<=end), 미수신/재조회/복구불가와 영향 실행·보관 경계·근거 보존 | gap `(source_id,stream_id,start_seq,end_seq)`, 미해결 부분 인덱스 |
| `tag_latest`, `tag_samples` | latest PK tag_id. 표본 UNIQUE `(gateway_id,sample_id)`; sample_id는 최초 수집 때 영속 부여해 재전송 시 유지. 품질·5종 시각 및 관측 순서. 아날로그 원시는 지정 9개만 | samples `(tag_id,observed_at)`; 이력 조회는 범위·4개 태그·페이지 제한 |
| `minute_aggregates` | PK `(tag_id,bucket_start)`, min/max/avg/last, valid_count·expected_count·누락/시간 품질, 집계 버전. `0<=valid_count<=expected_count` | PK로 태그·시간 범위 조회 |
| `work_orders`, `production_executions`, `execution_totals` | work order 목표 정수 양수, line·recipe FK, 상태·row_version. execution→work_order/line FK, 설정 snapshot. totals PK execution_id, 양품·불량·폐기·투입·재공은 음수 불가 | work orders `(line_id,status,planned_at)`, executions `(work_order_id)` |
| `control_commands`, `command_dispatch`, `command_results` | command_id PK, 불변 정규 내용/hash, target·execution FK(명령별 필수성), requester·expires_at·expected_control_version. dispatch의 lease·전달 상태와 append-only 결과 분리. `(command_id,result_revision)` UNIQUE | 대기 부분 인덱스 `(expires_at,requested_at)`, `(target,status)` |
| `alarm_instances`, `alarm_actions` | instance에 규칙 버전·발생/복귀·확인 시각/사람·종료 사유. active와 acknowledged는 독립. 조치/메모 수정은 새 action으로 보존 | `(asset_id,rule_id) WHERE condition_closed_at IS NULL` UNIQUE, `(occurred_at)`, actions `(alarm_id,created_at)` |
| `connection_intervals`, `equipment_intervals`, `downtime_reason_history` | asset별 시작/끝, KNOWN/UNKNOWN·시간 품질·근거 사건. `end>start`(열린 구간 end NULL). 정지 사유 변경은 append-only | asset 행 잠금 후 중첩 검사·분할. `(asset_id,start_at)`, 열린 구간은 asset당 UNIQUE |
| `run_reconciliations`, `report_versions` | execution·stream·counter_epoch·최종 seq·PLC 최종 수량·사건 합계·재공·결과·보류 사유. 보고 버전·대상 구간·원본 근거·변경 전후 버전 보존 | `(execution_id,created_at)`, `(report_key,version)` UNIQUE |
| `audit_logs` | actor 사용자/서비스, action·target·request_id·명령 ID·시각·성공/거절·안전한 변경 근거. 비밀번호/token/cookie/개인키 제외 | `(created_at)`, `(target_type,target_id,created_at)` |

라인당 활성 실행은 다음 DB 제약을 필수로 둔다. 상태명은 DB 내부 설계이며 계약의 업무 상태에 명시적으로 매핑한다.

```sql
CREATE UNIQUE INDEX one_active_execution_per_line
ON production_executions(line_id)
WHERE status IN ('PREPARING','RUNNING','PAUSED','ENDING','CANCEL_PENDING','RECOVERY_REQUIRED');
```

`ENDING`과 결과 확인 중 실행도 라인을 점유한다. 완료·취소로 바꾸기 전에 최종 대조가 필요하다. FK 컬럼은 별도 인덱스를 명시한다(PostgreSQL은 참조측 FK 인덱스를 자동 생성하지 않는다). 초기에는 일반 테이블과 B-tree로 시작하며 5,443,200 원시 표본 규모의 실제 조회·정리 성능을 측정한 뒤 파티션 도입을 결정한다. `event_id` 전체 고유성을 시간 파티션의 부분 UNIQUE로 약화하지 않는다.

## 3. 트랜잭션·경합과 ACK

기본 격리는 READ COMMITTED와 명시적 행 잠금이다. 잠금 순서는 `schema 공유 잠금 → 사용자/세션(사용자 요청) → 전역 command_id advisory(명령 요청) → line → stream_progress(출처/stream 정렬) → execution(ID 정렬) → command/기타`로 고정한다. migration은 같은 schema 키의 배타 잠금을 DDL보다 먼저 획득한다. 인증 트랜잭션도 테이블 접근 전에 schema 공유 잠금을 얻어 migration과의 잠금 순서 역전을 방지한다. unique 충돌·교착·직렬화 오류는 제한적으로 재시도하되 이미 만든 command_id/batch_id를 유지한다. DB 트랜잭션 안에서 OPC·외부 HTTP 응답을 기다리지 않는다.

1. **실행 연결·취소·교체:** line과 work order를 `FOR UPDATE`로 잠그고 row_version·배포 상태·이전 실행 완료·미정 명령을 확인한다. execution PREPARING, 설정 snapshot, LOAD_JOB 요청, 감사 로그를 하나로 커밋한다. 전달 lease가 잡힌 LOAD_JOB을 취소할 때는 미전달로 단정하지 않는다. 원래 명령 결과 조회와 필요 END_RUN, 생산 여부 확인 후에만 취소/종료한다. 부분 UNIQUE가 동시 연결을 최종 차단한다.
2. **명령:** 사용자 인증·권한·schema 검증 후 라인 잠금으로 충돌성 명령을 직렬화한다. 프로토콜의 `canonical_command_payload()`와 같은 정규화 내용 전체 및 command_id를 저장하고 같은 트랜잭션에 감사 기록을 남긴다. 동일 ID·동일 내용은 기존 추적을 반환하고 다른 내용은 409 충돌이다. 기존 영속 ID 조회는 새 요청의 유효기간 판단보다 먼저 한다. gateway는 `FOR UPDATE SKIP LOCKED`에 해당하는 API의 원자적 lease 기능으로 명령을 가져간다. lease 만료는 실패 증거가 아니다. 재전달이 필요한 경우 같은 ID만 사용하며 전달 이력부터 확인한다.
3. **수집:** 제한된 크기 배치의 schema·서비스 source scope·hash를 검증한다. stream/실행 행을 잠그고 batch/inbox 중복 제거 → 새 유효 사건의 `production_events` INSERT → 새로 INSERT된 사건에 대해서만 execution_totals 갱신 → 적용 상태·연속 검증 seq 갱신 → 태그/상태/알람 투영·감사/배치 결과 저장을 한 PostgreSQL 트랜잭션으로 커밋한다. 동일 event_id 또는 같은 source/stream/seq에 다른 내용이 오면 덮어쓰지 않고 충돌·정산 보류를 기록한다. PLC 카운터를 사건 실적에 다시 더하지 않는다.
4. **보류·누락:** 알 수 없는 execution_id인 schema-valid 사건은 inbox에 원문과 오류를 영속 보관하고 생산 FK INSERT·실적 반영을 보류한다. 잘못된 envelope는 ACK 없이 거절한다. 내구성 있게 보관한 배치만 커밋 후 영속 수신 ACK를 반환한다. ACK는 실적 적용·정산 완료와 다르다. 정확한 ACK 필드는 [DEV-02 배치 계약](../../contracts/TRANSPORT.md)을 따른다. `recorded_gaps`는 해당 배치의 수신 이력이며 `progress.unresolved_gaps`는 현재 미해결 범위다. 이미 복구된 구간을 과거 배치 재전송 때문에 다시 미해결로 열지 않는다. ACK 응답 유실은 같은 batch_id/hash 재요청에 저장된 결과를 반환한다.
5. **커서:** 수신 위치는 높은 순번을 보관했거나 복구불가 범위를 기록한 뒤 진행할 수 있다. 마지막 연속 검증 seq는 누락·적용 보류를 넘지 않는다. 역순 사건을 보관·한 번 적용하더라도 사이가 메워질 때만 연속 커서를 전진시킨다. 재조회 시 페이지 마지막 순번만 보고 검증 완료로 처리하지 않는다. gap과 연결된 실행·보고서 보류는 새 데이터가 왔다는 이유로 해제하지 않는다.
6. **종료:** line·stream·execution 잠금 아래 신뢰 가능한 PLC 요약의 실행/stream/counter_epoch·설정 버전·마지막 seq를 확인한다. 신규 투입 중단, 재공 0, 최종 경계까지 사건 완전성, 투입=양품+불량+폐기+재공, 사건별 합계=PLC 확정 수량을 모두 확인한 뒤 reconciliation·상태 완료·감사를 함께 커밋한다. counter_epoch 변경/카운터 감소의 근거가 없거나 시간 품질·사건 누락이 있으면 종료 대기·잠정 보고를 유지한다. 수량을 직접 수정하는 관리자 API는 없다.
7. **최신 투영·시간:** 최신값은 source timestamp가 아닌 영속 수집 순서/세대로 비교해 과거 배치가 현재 화면을 되돌리지 못하게 한다. 3초 정상 수집/생존 신호 중단은 지연·제어 제한, 5초는 통신 알람·UNKNOWN이다. 알람의 복귀는 단절과 별개이며 기존 알람을 유지한다. 과거 상태 복구는 순번·시각·경계가 검증된 구간만 재계산하고 보고서 새 버전과 근거를 저장한다. 불명 시간은 전체 시간의 분모에 포함한다.

## 4. 프로세스별 SQLite 구조

두 파일 모두 연결마다 `foreign_keys=ON`, `journal_mode=WAL`, `synchronous=FULL`, 유한 `busy_timeout`을 설정하고 한 쓰기 소유자로 직렬화한다. 로컬 영속 파일시스템을 사용하며 공유 네트워크 파일시스템·두 프로세스의 동시 소유는 지원하지 않는다. WAL 사용만으로 원자성 시험을 통과했다고 보지 않는다.

### 시뮬레이터

- `schema_versions`, `sim_identity(source_id,stream_id,next_seq,counter_epoch)`, `plc_state(control_version,modes,state,active_execution_id,recipe_version)`, `runs`, `run_recipe_applications`, `items(item_id,execution_id,stage,disposition)`, `run_totals`, `source_events`, `processed_commands`, `command_result_history`, `scenario_state`, `console_users/sessions/audit`를 둔다. items.execution_id는 투입 후 변경하지 않는다. ID 고유성과 `(source_id,stream_id,seq)` 고유성을 강제하고 `(execution_id,seq)` 사건 조회 인덱스를 둔다.
- 투입·이송·배출/폐기 결정은 `BEGIN IMMEDIATE` 안에서 물품·재공 상태, 투입/양품/불량/폐기 카운터, 원본 사건, 순번 증가, 결정적 seed/판정 위치를 함께 커밋한다. 폐기는 사유가 필수다. 공정 상태가 바뀌는 명령의 성공은 그 상태·control_version·성공 결과를 같은 커밋에 넣고 커밋 뒤에만 OPC 응답/태그로 공개한다. 접수 기록과 실행 결과를 구분한다.
- processed_commands는 command_id PK·정규 내용/hash·접수·진행/결과를 재시작 후 유지한다. 같은 내용은 기존 결과, 다른 내용은 충돌이다. 명령 만료는 **전달 전**과 이미 접수된 결과 확인을 구별한다. 기본 전달 TTL 5초, START 대기 10초, END_RUN 대기 60초 초과는 결과 확인 필요이며 새 ID 자동 실행을 하지 않는다.
- source_id·stream_id·seq는 재시작 시 유지한다. counter_epoch 변경은 별도 감사 사건이며 stream 재생성이나 seq 초기화와 다르다. UInt64 소진 시 wrap하지 않고 제어를 보류하며 명시적 새 stream 절차가 필요하다. 재시작은 저장된 RUNNING을 자동 실행하지 않고 RECOVERY_REQUIRED로 진입해 실행·물품·미완료 명령을 대조한다. heartbeat/측정 변화는 control_version 증가 사유가 아니다.
- source_events는 생산·상태·고장 사건을 7일 재조회하고 보관 시작/끝 순번을 반환한다. 삭제된 범위 요청은 명시적 복구불가다. 실행 중 물품·최종 요약·명령 중복 방지 기록은 원본 사건 삭제와 함께 지우지 않는다. 오래된 command_id의 재실행을 막는 ID/hash tombstone은 명시적인 데이터 초기화 전까지 유지한다.

### 게이트웨이

- `schema_versions`, `gateway_identity`, `outbound_batches(batch_id,hash,body,created_at,bytes,status)`, `capture_progress`, `source_read_progress`, `missing_ranges`, `command_delivery_journal`을 둔다. batch_id PK, sample/event ID와 원본 stream/seq를 payload에 유지한다. 미전송 배치 `(status,created_at)` 인덱스를 둔다.
- OPC 읽기 결과는 먼저 SQLite에 배치·관측 순서·읽기 위치를 한 트랜잭션으로 저장한다. 커밋 전에는 API로 보내지 않는다. API 영속 ACK의 ID/hash를 검증하고 ACK 표시와 정리 가능 위치를 함께 저장한다. API가 이미 커밋하고 gateway가 중단되면 같은 배치를 재전송한다. API의 연속 검증 커서와 로컬 수신/읽기 위치는 별개다.
- 한도는 미전송 데이터 기준 **30분 또는 100MB(100,000,000 bytes) 중 먼저 도달**이다. payload/행 관리 크기를 계측하고 WAL·파일 실제 디스크 사용량도 별도 감시한다. 초과 시 오래된 미전송 배치의 시간·순번·태그 범위를 missing_ranges에 기록하고 삭제·경보 상태를 같은 트랜잭션으로 확정한다. 누락 메타데이터를 배치와 함께 밀어내지 않는다. ACK된 배치는 빨리 정리하고 WAL 체크포인트/공간 회수도 운영 작업으로 둔다.
- 생산·상태 사건은 OPC 원본 7일 이력으로 재조회한다. OPC 단절 중 아날로그 표본을 생성하지 않으며 복구 불가능 아날로그 구간은 그대로 누락이다. 원본 경계 밖 사건은 영구 누락·영향 실행 정산 보류로 남기고 최신 수집은 계속한다. 게이트웨이 명령 저널은 같은 ID의 전달·결과 조회를 복구할 근거이며 API/PLC 판정을 대신하지 않는다.

## 5. 사용자·API 권한표

역할은 `viewer`(조회), `operator`(작업자), `production`(생산 담당), `equipment_admin`(설비 관리자), `simulator_operator`(별도 앱)이다. 앱 A의 네 역할은 공통 운영 조회를 허용하되 역할 상속은 없다. 여러 역할을 명시적으로 부여할 수 있다. 로그인하지 않은 요청은 401, 로그인했지만 권한 부족은 403이며 모든 제어·관리 검사는 API에서 한다.

| API/행위 | viewer | operator | production | equipment_admin | gateway 서비스 |
|---|---|---|---|---|---|
| snapshot·WS·태그·알람·트렌드·명령 이력 조회 | 허용 | 허용 | 허용 | 허용 | 거절 |
| 확정 보고서·동일 기준 CSV | 허용 | 허용 | 허용 | 허용 | 거절 |
| 잠정 보고·실적 대조 상세 | 거절 | 거절 | 허용 | 거절 | 거절 |
| START·PAUSE·RESET_FAULT | 거절 | 허용 | 거절 | 거절 | 전달만 |
| 배포 작업 선택·실행 연결(LOAD_JOB) | 거절 | 허용 | 허용 | 거절 | 전달만 |
| END_RUN 요청 | 거절 | 허용 | 허용 | 거절 | 전달만 |
| 알람 확인·조치, 정지 사유 기록 | 거절 | 허용 | 거절 | 거절 | 거절 |
| 작업지시 작성·배포·취소·종료 대조 | 거절 | 거절 | 허용 | 거절 | 거절 |
| 공정 설정 버전 등록·적용 요청(SET_PARAMETERS) | 거절 | 거절 | 허용 | 거절 | 전달만 |
| 태그·알람 규칙·연동 설정·사용자 역할 변경 | 거절 | 거절 | 거절 | 허용 | 거절 |
| `/internal/ingest`, 명령 lease·결과 전달 | 거절 | 거절 | 거절 | 거절 | source/line 범위만 |
| 생산 원본 수량 수정·강제 정산·원본 삭제 | 거절 | 거절 | 거절 | 거절 | 거절 |

`/commands`는 URL만으로 허용하지 않고 command 종류·대상·실행·원래 업무 권한을 함께 검사한다. LOAD_JOB은 배포된 작업·서버 생성 실행·고정 설정으로 구성하며 operator에게 임의 설정 작성 권한을 주지 않는다. API의 END_RUN 요청 허용은 PLC 성공이나 MES 정산 완료 권한을 의미하지 않는다. 역할 보유만으로 freshness·미정 명령·모드·안전 조건을 우회하지 못한다.

simulator_operator는 앱 B의 별도 사용자·세션으로 콘솔 현장/원격·센서·고장·통신·복원 시험을 조작한다. 앱 A 계정/쿠키·gateway token을 인정하지 않는다. 콘솔은 시험 환경임을 표시하고 감사·CSRF·세션 만료를 적용한다. LOCAL·비상정지·가드의 최종 판정은 PLC에 남는다. 공개 `/health/live`는 최소 생존 정보만, `/health/ready`는 배포망에서 제한하고 상세 DB/OPC/버퍼/누락 정보는 인증한 운영 조회로 제공한다. OPC 단절을 API의 과거 조회 불가로 취급하지 않는다.

## 6. 브라우저·서비스·OPC 인증

### 브라우저 세션

- 암호는 Argon2id 등 검증된 암호 해시 라이브러리를 후속 의존성 검토에서 고정해 저장한다. 평문/복호화 암호, 저장소 내 기본 계정·기본 암호를 두지 않는다. 로그인 시 계정/IP별 속도 제한·공통 오류 응답을 적용한다.
- 로그인 성공 시 충분한 난수의 opaque session token을 발급하고 서버에는 digest만 보관한다. 인증 세션에 사용자·auth_version·CSRF secret·생성/마지막 사용/idle·절대 만료를 둔다. 초기 정책은 idle 30분, absolute 8시간이다. 로그인/권한 변경 시 token 재발급, 로그아웃·계정 비활성·암호/역할 변경 시 기존 세션 취소와 열린 WS 종료를 적용한다. 클라이언트 localStorage에 인증 token을 넣지 않는다.
- 운영 API는 `HttpOnly; Secure; SameSite=Lax; Path=/`와 host-only cookie를 사용한다. 모든 상태 변경(로그인·로그아웃 포함)은 허용 Origin 검사와 세션에 결합된 CSRF token 검증을 통과한다. GET은 상태를 바꾸지 않는다. 로그인 전에는 단기 pre-auth CSRF 세션을 쓴다. WS는 handshake의 세션·Origin과 주기적 만료/취소를 검사하고 query string에 token을 넣지 않는다.
- 개발은 web 5173의 같은 출처 proxy로 `/api`·`/ws`를 API 8000에 연결하는 설계다. `.env.example`의 HTTP loopback은 개발 전용이며 이 모드에서만 Secure cookie를 끌 수 있다. 배포는 HTTPS와 Secure=true가 필수다. 다른 출처를 허용하면 정확한 allowlist와 credential 설정을 사용하며 wildcard를 쓰지 않는다. 앱 B 콘솔 8001은 별도 cookie 이름·세션 DB·CSRF를 사용한다.

### 게이트웨이 서비스

게이트웨이는 HTTP 서버 포트를 열지 않고 운영 API로 outbound 요청한다. API와 gateway는 런타임 secret 파일로 전달한 256비트 이상 난수 bearer token을 사용한다. API는 key_id별 digest를 비교하고 활성·만료·source/line scope를 검사한다. browser session·사용자 역할로 `/internal/*`에 접근할 수 없고 gateway도 사용자 명령을 생성하거나 업무 관리 API를 호출할 수 없다. gateway가 보고한 요청자 문자열을 원래 사용자 인증으로 신뢰하지 않고 서버의 기존 command_id와 대조한다. loopback 개발 외 통신에는 서버 인증 TLS를 적용한다. token은 헤더로만 보내며 URL·본문·로그·오류에 출력하지 않는다. 회전은 새 key 추가→gateway 전환 확인→이전 key 폐기이며 만료/회수한 token은 즉시 거절한다.

### OPC UA 인증서와 허용 메서드

- 앱 B 서버와 gateway 클라이언트의 인증서/개인키를 분리한다. `Basic256Sha256`의 `SignAndEncrypt`를 최초 제어 경로부터 사용하고 `None`·익명 제어·미신뢰 인증서 자동 승인·검증 비활성 fallback을 허용하지 않는다. 실제 라이브러리 보안 정책·identity·메서드 접근 제어는 DEV-03 PoC에서 읽기/허용/거절을 검증해야 한다.
- endpoint host와 서버 ApplicationUri, 클라이언트 ApplicationUri, 유효기간·용도·trust anchor/지정 leaf·필요한 폐기 상태를 확인한다. 앱 B는 승인한 gateway certificate identity만 읽기 및 네 메서드에 매핑한다. 태그 쓰기와 임의 메서드는 거절한다. TLS 사용자 서비스 token과 OPC application certificate는 별개 경계다.
- DEV-03의 보안 채널 PoC가 OPC UA `Anonymous` 사용자 token으로 연결하고 모든 명령을 `POC_ONLY` 거절하더라도 이는 인증서 통신 검증에 한정된다. 실제 제어는 검증된 gateway 인증서 identity를 명시적인 service principal/메서드 권한에 매핑하고 원래 API 사용자 권한·감사를 연결해야 한다. `Anonymous` token 자체에 제어 권한을 주거나 PoC 성공을 사용자 인증·PLC 권한 구현 완료로 표시하지 않는다.
- `.local/pki/gateway`와 `.local/pki/simulator`의 own/trusted/rejected 경로는 각각 해당 프로세스만 소유한다. trusted에는 승인한 상대 인증서/CA 공개 자료만 배포하고 rejected의 인증서를 자동 이동하지 않는다. leaf pinning 또는 CA 체계를 운영 설정에 명시한다. 개인키는 소유자 읽기만 허용(파일 0600·디렉터리 0700), 컨테이너에서는 읽기 전용 secret mount로 주입한다.
- 인증서 생성·상대 지문 별도 확인·trust 배포→검증→연결 순서다. 잘못된 host/ApplicationUri·만료·미신뢰·폐기 인증서를 거절하고 감사한다. 회전은 사전 승인한 새 인증서의 제한된 overlap 이후 이전 trust를 제거한다. 브라우저에 OPC 개인키나 gateway token을 배포하지 않는다.

## 7. 설정·마이그레이션·bootstrap

[`.env.example`](../../.env.example)은 향후 설정 파서의 **설계 예시**이며 현재 앱 실행 지원을 의미하지 않는다. 모든 상대 경로는 저장소 루트 기준으로 해석한다. 런타임 DB·WAL·로그·인증서·백업·로컬 비밀은 `.local/` 아래 별도 디렉터리와 프로세스별 권한으로 관리한다. 실제 배포는 별도 볼륨/secret mount의 절대 경로로 바꿀 수 있다. 현재 `.gitignore`는 `.local/`, 실제 `.env`, SQLite·WAL·로그·의존성 산출물을 제외한다. 후속 배포 경로와 비밀 mount도 같은 제외 원칙으로 검증한다.

- 포트 기준은 API 8000, OPC 4840, simulator console 8001, web 개발 5173, PostgreSQL 5432다. gateway listener는 없다. loopback URL은 호스트에서 각각 실행할 때의 예시다. 향후 Compose 내부에서는 서비스 DNS로 API/DB/OPC 주소를 명시적으로 덮어쓰고 PostgreSQL을 외부 공개하지 않는다.
- `*_FILE`은 파일 **경로**이며 내용을 읽어 인증에 사용한다. 예시의 빈 credential 파일 경로는 해당 프로세스가 시작되기 전에 반드시 지정해야 한다. 비밀번호·token·개인키 본문을 `.env`나 문서에 채워 배포하지 않는다. 브라우저 빌드에 주입하는 설정에는 API 경로 등 공개 값만 허용한다. 설정 누락·모순·잘못된 타입·미존재 secret/trust는 시작 실패로 처리하며 데모 자격증명으로 fallback하지 않는다.
- PostgreSQL migration은 순번·checksum 기록, 단일 migration lock, schema·FK·CHECK·UNIQUE·index·최소 DB grants를 포함한다. 앱의 지원 schema 범위가 맞지 않으면 readiness 실패이며 시작 시 자동 파괴/초기화하지 않는다. 작업용 DB 소유 계정과 API DML 계정을 분리한다. SQLite도 각 파일의 schema version으로 독립 migration하며 변경 전에 검증 가능한 백업을 만든다. 적용 실패 시 transaction rollback 또는 명시적 복원 절차를 사용한다.
- bootstrap은 한 site·line·세 asset·48개 태그를 안정된 code/계약 ID와 내용 hash로 idempotent 등록한다. 같은 ID의 다른 계약 내용은 조용히 덮어쓰지 않고 migration/계약 변경을 요구한다. 시험용 recipe·alarm 설정은 `test`로 명시하고 온도/속도·공압 하한/복귀·예열 조건의 확정값이 없으면 제어 가능 상태가 되지 않는다. 태그 수를 늘려 누락된 사건/요약을 대신하지 않는다.
- 최초 앱 A 설비 관리자·앱 B 조작자는 서로 다른 일회성 bootstrap 작업으로 만든다. 로그인 이름과 필수 암호 secret 파일은 operator가 지정하며 일반 서버 시작은 계정을 만들지 않는다. bootstrap 후 secret mount를 제거하고 초기 암호 변경을 요구한다. 역할·권한·초기 설정과 계정 생성 감사는 남기되 secret은 남기지 않는다. 기존 계정의 암호를 반복 bootstrap으로 재설정하지 않는다. 계정/데이터 초기화는 일반 시작·중지와 별도 작업이다.

## 8. 보관·백업·복원 일관성

| 대상 | 보관·삭제 조건 |
|---|---|
| 9개 아날로그 1초 원시 표본 | 7일. 품질·원본 시각 포함, 집계 생성/검증 여부 확인 |
| 1분 집계 | 90일. min/max/avg/last·valid/expected 수·누락·버전 포함 |
| 운영 생산·알람·명령·감사 | 180일. 미완료 작업·미해결 명령/알람/누락·미확정 정산과 참조 근거는 보호 |
| 서버 수집 원본·해당 clock proof/consumed probe | 현행 schema23은 기본168시간·최소3시간. 검증된 정산 완료 사건 배치 또는 안전한 관측 전용 배치만 제한 함수로 정리; 현행19차 정책과 INGEST_RETENTION 참조 |
| simulator 원본 사건 | 7일. 현재 보관 시작/끝과 삭제된 순번 명시. 수집 ACK를 기다리는 무기한 보관으로 정책을 바꾸지 않음 |
| 현재 상태·설정/적용 이력 | 활성 동안 유지. 사용 중 버전 삭제 금지 |

보관 정리는 작은 배치로 수행하고 line/실행 상태를 잠금 아래 다시 검사한다. unresolved FK·커서·불명 구간 근거를 cascade 삭제하지 않는다. batch ACK, event ID/hash·source/stream/seq 중복 방지 tombstone, command ID/hash는 payload 정리와 분리한다. 최소한 가능한 재전송·복원 보관창 전체를 덮고, 명령 tombstone은 명시적 안전한 초기화 전까지 유지해 오래된 ID가 새 명령으로 재실행되지 않게 한다. 생산 사건·명령의 중복 증거는 계속 보호한다. 서버 수집 원본이18차 정책으로 종료된 경우에는 작은 원래 식별 기록으로 재사용을 판별하되 ACK를 재생성하지 않고 RETENTION_EXPIRED로 거절한다.

매일 PostgreSQL과 설정, 앱 B 영속 상태, gateway buffer를 **같은 backup_set_id**로 백업한다. 초기 구현은 세 저장소의 분산 live snapshot을 가정하지 않고 다음 정지 지점 절차를 사용한다.

1. 신규 사용자 명령·bootstrap·보관 작업을 막고, PLC를 명시적으로 안전한 정지/일시정지 상태로 전환한다. 진행 중 명령이 있으면 같은 ID로 대조하거나 결과 불명으로 남긴다. gateway 수집/전달을 멈추고 각 DB writer가 진행 중 트랜잭션을 완료한 barrier를 확인한다. 제어·생산이 계속되는 상태에서 파일 복사로 일관된 백업을 주장하지 않는다.
2. 모든 writer가 정지한 동안 PostgreSQL의 transaction-consistent dump와 설정/계약 버전을 보관한다. 각 SQLite는 backup API(또는 완전 정지·검증된 checkpoint 이후 파일 세트 복사)로 복사하고 integrity/FK 검사한다. 실행 중 main 파일만 복사해 WAL 커밋을 잃지 않게 한다.
3. manifest에 schema·앱/계약 버전·checksum, backup 시각, source/stream/counter_epoch, 실행/최종 경계, DB 연속 커서, gateway 미전송 범위, 미정 명령·열린 알람을 기록한다. secret은 데이터 archive에 무방비로 섞지 않고 별도 암호화 secret 백업/재주입 정책을 사용한다. 백업 성공·검사 후 수집을 재개해 기존 ID로 중복 대조한다. PLC 자동 START는 하지 않는다.
4. 별도 `.local/restore/<backup_set_id>/`와 DB/볼륨으로 전체 세트를 복원한다. 서로 다른 날의 DB·버퍼·simulator를 혼합하지 않는다. 처음에는 외부 제어를 막고 session/token을 취소·회전한다. simulator는 RECOVERY_REQUIRED, API는 제어 제한으로 시작한다. stream/커서/누락·실행·물품·양품/불량/폐기·명령·알람을 manifest와 검산한 뒤 명시적으로 재개한다. 부분 복원이나 7일 밖 누락은 보류 사유를 남기고 정산 완료로 표시하지 않는다.

## 9. 후속 로컬 검증과 완료 범위

이 설계의 검토는 원문 불변 조건·보관값·권한 경계와의 대조다. 기준 정보 migration·bootstrap 검증은 [실행 결과](DATABASE_BOOTSTRAP.md)를 따른다. 아래 업무 모델·권한·운영 복구의 남은 검증은 **미실행**이며 부분 검증 결과를 전체 수락 시험으로 확대하지 않는다. 실제 PostgreSQL을 사용하며 SQLite 대체로 통과 처리하지 않는다. CI 구축은 범위에 없다.

- 먼저 고정 PostgreSQL 버전, 실행 가능한 로컬 서버 또는 Docker와 이미지 접근, `psql`/검증 드라이버, 독립 시험 DB·DML/migration 계정, secret 파일, migration과 fixture가 필요하다. 현재 로컬 검증은 고정 이미지의 임시 PostgreSQL과 psycopg 연결을 사용한다. 운영 DML/migration 계정 분리는 후속 구현이다. 컨테이너 실행 시 클라우드 proxy/CA 정책을 따른다. 준비 후 비밀 없는 연결 선택과 `psql --version`, `psql -X -v ON_ERROR_STOP=1 -c 'SELECT version();'`로 실제 서버/계정 확인을 남긴다(암호는 보호된 pgpass/secret 입력 사용).
- 새 DB migration→bootstrap 2회에서 1/1/3/48 고정 수와 미변경 계약, 기존 DB upgrade·실패 rollback, 잘못된 schema readiness 거절을 검증한다. UInt64 0·2^63·최댓값·초과·소수·음수·JSON 정확도를 양 저장소에서 검증한다.
- 독립 PostgreSQL 연결 두 개로 LOAD_JOB 경합·취소/전달 lease 경합·중복 배치·같은 ID 다른 내용·event seq 충돌·A 지연 사건·unknown execution quarantine·역순/영구 누락과 연속 커서 차이를 시험한다. commit 직전/직후 API·gateway·simulator 중단을 주입하고 수량·물품·사건·커서 원자성과 ACK 재전송을 확인한다(T03·T07~T10).
- viewer 직접 API 제어 403, 역할별 명령·관리 거절, CSRF/Origin/만료/회수 세션, WS 권한 회수, 잘못된 source의 서비스 token, 미신뢰/만료/잘못된 URI OPC 인증서와 쓰기/임의 메서드 거절을 실제 경로에서 시험한다(T02·T12). 첫 제어 기능의 완료 조건에 포함한다.
- 30분/100MB 경계·원본 7일 경계·미해결 보호 삭제·별도 경로 전체 backup set 복원과 수량/시간 식을 검산한다. 7일 규모 태그 4개·24시간 조회 p95 3초, 30분 버퍼 5분 복구, 10명·8시간 성능은 M5의 실제 측정으로 남긴다. 설계 검토와 모의 fixture 성공으로 대체하지 않는다.

이 문서가 정한 추가 선택은 UInt64 DB 표현, 세션/서비스 인증 분리, 보호용 tombstone, 정지 지점 backup set 방식이다. 기획서의 안전·정합성·보관·수락 목표를 완화하지 않으며 구현 시 계약 변경이 필요하면 원인·영향을 별도 기록한다.

## 18차 승인 변경 이력 — 서버 원본 보관 종료

변경 이유: 무료 DB에서 수집 원본·시간 proof를 무기한 저장할 수 없어 사용자가7일 보관 후 정리를 승인했다. HMI_INGEST_RETENTION_DAYS는 기본7, 최소1인 정수 일수다. 최소값도 gateway 원장2시간보다 길다. 공개 트렌드 원시1시간/분14일 프로필과 별도 정책이다.

해당 사건의 실행이 저장된 SETTLED이고 사건 원문/APPLIED·최종 수량/경계/설정 이력·verified 커서·관련 gap/격리/미결 명령을 재검증한 배치만 지운다. 사건 없는 관측 배치는 보수적으로 source 전체 보호 조건을 확인한다. null execution 상태 사건을 관측 전용으로 속이지 않는다. PENDING/MISMATCH/CONFLICT·원인 미해결 자료는 기간만으로 지우지 않는다.

삭제는 수집 커밋 후 별도 짧은 트랜잭션의 제한된 소유자 함수로만 수행한다. runtime에 raw/clock/compact 증거의 임의 DELETE/UPDATE 권한을 주지 않는다. 공개 스키마 이관은 자료를 지우지 않으며, 자동 정리는 병합·이관·grants·배포 이후에만 동작한다.

원본 body/ACK·전체 시간 proof가 사라진 오래된 실행은 원본으로 다시 검증할 수 없다. event_inbox/production_events·실적·요약/정산·업무/알람/감사·분 집계는 이 정리로 삭제하지 않는다. 알람 FK는 원본 대신 영구한 작은 ingest_receipts의 동일 식별/hash에 연결한다. capture sequence와 임의 UUID의 정확한 재사용 판별을 위해 그 작은 식별 기록은 남으며 선형 증가한다. 연속 보관 종료 범위는 합치지만 전체 metadata가 고정 크기라는 뜻은 아니다.

백업/복원에는 ingest_receipts·ingest_retention_ranges·scheduler와 clock 순서 registry도 포함해야 한다. 전체 pg_dump는 이 새 테이블을 포함한다. 원본이 이미 정리된 경우 이 증거를 빼고 복원하거나 보관 종료 기록을 정상 ACK로 복원하지 않는다. 상세 규칙·롤백·API 의미는 [INGEST_RETENTION.md](INGEST_RETENTION.md)를 따른다.
