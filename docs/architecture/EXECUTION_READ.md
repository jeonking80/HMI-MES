# 원래 실행의 수동 조회

`GET /api/v1/execution-record?execution_id=...&line_id=...&source_id=...&stream_id=...`는 앱 A에 등록된 실행의 원래 관리 연결, 원래 LOAD 명령 결과, 마지막 저장 정산, 수량과 현재 source 플래그를 조회한다. 새 작업 허가·정산 실행·라인 예약 해제 API가 아니다. 취소·보고서·자동 다음 작업 실행을 추가하지 않는다.

새 고정 경로는 execution_id와 세 scope 식별자, 총 네 query를 각각 정확히 한 번씩 명시한다. 없거나 중복·추가 인자·잘못된 식별자는 422다. execution과 세 범위가 함께 일치하지 않으면 404다. 임의 문자열 work_order_id가 catalog와 같다는 이유로 관리 실행으로 해석하지 않는다.

기존 `GET /api/v1/executions/{execution_id}?line_id=...&source_id=...&stream_id=...`는 유지하며 그 경로는 세 scope query만 받는다. 새 alias는 같은 reader·권한·idle 정책을 사용한다. Identifier/DB 범위를 줄이지 않고 slash·단독 `.`/`..`·percent literal·정상 UTF8을 지원하기 위해 UI는 `URLSearchParams`로 execution ID도 query 값에 한 번 인코딩한다. 추가 unquote·문자 정규화·404 후 legacy fallback·새 ID·POST를 만들지 않는다.

## 인증과 응답

실제 로그인 세션과 최초 비밀번호 변경이 필요하다. viewer, operator, production_manager, equipment_admin 모두 기존 `read` 권한으로 조회한다. 수동 GET은 idle 만료를 연장하지 않으며 Origin/CSRF mutation 검사를 필요로 하지 않는다. 사용자 서버의 실제 Host/scheme 검사는 유지한다. 응답은 `Cache-Control: no-store`다. [API_RUN.md](API_RUN.md)의 사용자 서버에 연결되며 서비스 bearer API와 분리된다.

| 응답 필드 | 의미 |
|---|---|
| `execution` | execution_id, work_order_id, recipe_version, parameters, counter_epoch, registered_at, target_quantity |
| `scope` | 정확히 조회한 line_id, source_id, stream_id |
| `binding` | 명시적 관리 연결의 `{intent,receipt}`; 기존 binding recovery와 같은 최초 JSON |
| `load` | 그 연결의 원래 `{request,result,collision_latched}`; 다른 ID나 최신 명령을 대신 선택하지 않음 |
| `settlement` | 마지막 저장된 status, reason_code, checked_at, received_at, summary |
| `totals` | 현재 good_quantity, reject_quantity, scrap_quantity |
| `source` | 현재 gateway_id, capture_generation, conflicted, reconciliation_required, verified_through_seq, latest_ingested_seq, control_restricted |

레거시 실행은 `binding:null`, `load:null`이다. 과거 실행에 목표 계획이 없으면 `execution.target_quantity:null`이며 자동 보정하지 않는다. 저장된 정산이 없으면 다음과 같이 명시한다.

```json
{
  "status": "PENDING",
  "reason_code": "FINAL_SUMMARY_REQUIRED",
  "checked_at": null,
  "received_at": null,
  "summary": null
}
```

정산 status는 PENDING, SETTLED, MISMATCH, CONFLICT 중 하나다. 원래 LOAD 결과는 계약의 REQUESTED, DELIVERED, ACCEPTED, SUCCEEDED, FAILED, REJECTED, EXPIRED, OUTCOME_UNKNOWN을 구별한다. UInt64 수량·순서·버전은 JSON 정규 십진 문자열로 반환한다. 목표 수량은 UInt32 양의 정수 또는 null이다. 원래 binding 영수증과 LOAD JSON은 저장한 값 그대로 보존한다.

정산 summary는 항상 정확한 execution/source/stream이어야 한다. SETTLED summary는 final 및 원래 counter epoch·recipe·parameters 일치가 필요하다. 실제 MISMATCH는 잘못된 epoch나 설정의 원래 관측 증거를 그대로 보여주며 기대값으로 바꾸지 않는다. 예를 들어 execution.counter_epoch는 `epoch-A`, MISMATCH summary.counter_epoch는 `epoch-B`일 수 있다. 타입·필수 수량·관리 연결·원래 LOAD·정산 식별자가 불일치하거나 지원하지 않는 schema면 503이다. 내부 증거·SQL·driver 오류를 오류 응답에 노출하지 않는다.

## 읽기 일관성과 경계

인증/schema 잠금 후 업무 자료는 **하나의 PostgreSQL SELECT 문장**으로 읽는다. 실행·source/line, 목표 계획·수량, 선택적 명시 연결·원래 LOAD journal·저장 정산, stream progress를 함께 join하여 한 문장의 MVCC snapshot을 사용한다. 여러 서비스 호출의 서로 다른 시점 자료를 합치지 않는다. source 필드는 그 조회 시점의 등록 및 제한 플래그이며 fresh PLC 상태나 원래 gateway 신원이라는 뜻이 아니다.

caller-owned Connection을 사용하고 내부 트랜잭션·FOR UPDATE 업무 잠금·라인/실행 advisory·idle 갱신·감사·정산·외부 I/O·PLC 제출을 하지 않는다. 기존 인증의 사용자·세션 잠금과 schema 공유 잠금만 유지한다. GET은 `checked_at`이나 정산 status를 새로 만들지 않는다. 저장 SETTLED 이후 새 사건·source 불명·다른 명령이 생겼다면 마지막 저장 판정과 현재 수량/플래그가 달라질 수 있다. 이를 새 정산이나 다음 작업 허가로 해석하면 안 된다.

후속 UI의 보관 동작은 사용자가 명시적으로 선택해야 한다. 로컬 원본 ledger와 actor·작업지시·command/execution ID·scope·기대 버전이 일치하는 관리 연결, 원래 LOAD의 SUCCEEDED 및 collision_latched false, 정확한 final summary의 저장 SETTLED가 필요하다. 실패·만료·결과 불명·없는 연결·PENDING/MISMATCH/CONFLICT를 원본 폐기 근거로 삼지 않는다. 보관은 원래 기록을 이력에 남기고 다음 준비만 가능하게 하며 DB 예약을 해제하지 않는다. 이 API는 `archive_allowed`나 `ready_for_next`를 반환하지 않는다.

다음 bind POST는 기존 fresh 제어 preflight와 **예약에 기록된 정확한 이전 실행**의 정산 검사를 다시 수행한다. 수동 GET의 과거 SETTLED만으로 이를 건너뛰지 않는다. 관리 연결과 원래 ID 복구 절차는 [WORK_ORDERS.md](WORK_ORDERS.md)를 따른다.

## 검증

```sh
.venv/bin/ruff check apps/api/execution_read.py apps/api/execution_http.py \
  tests/integration/test_execution_read.py
.venv/bin/pytest -q tests/integration/test_execution_read.py
```

실제 PostgreSQL/ASGI 시험은 등록·배포·연결 후 PENDING, 실제 command journal 결과·생산 사건 적용·최종 summary 정산 후 SETTLED, MISMATCH 원래 증거, 레거시 null, 역할·세션·정확한 범위, UInt64 최대값, 잘못된 저장 증거의 거부, 업무·idle 쓰기 부재와 한 문장 snapshot의 동시 commit 일관성을 확인한다. 이 endpoint 단위만으로 다음 작업의 실제 PLC 전달이나 전체 MES 완료를 주장하지 않는다.

## Alias·UI 검증 기록

실행/알람 새 fixed-query alias 시험 **34개(실행13+알람21)**와 기존 회귀 **67개**는 별도 실제 PostgreSQL/ASGI 실행에서 통과했다. 작업지시/취소/workflow **106개**와 제어 HTTP **35개**는 다른 범위다. 실행 query의 식별자 보존·정확한 scope·중복/추가 query422·역할·passive idle·무쓰기를 확인하며 legacy 경로도 보존한다.

```sh
.venv/bin/pytest -q tests/integration/test_execution_identifier_aliases.py \
  tests/integration/test_alarm_identifier_aliases.py
```

현재 웹 **61개 순수 시험·typecheck/lint/build**와 실제 전체 Chromium/일회용PG/API 검사도 통과했다. 실제 브라우저의 실행 수동 조회·보관·동일세션 다음 작업은 새 alias를 사용하며, 기존 auth/WS/command/MES/alarm/C1 및 MES 특수ID6-case도 같은 실행에서 확인했다. 실행 ID의 slash·dot·percent·UTF8 보존 자체는 실행 alias의 영구PG 시험과 순수 URL 시험 범위다. 로그는 `.local/qa/identifier-browser.log`다. 실제 OPC/PLC 전달·실행 성공과 전체 수락 완료를 주장하지 않는다.
