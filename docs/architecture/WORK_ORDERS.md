# DEV-17a/17b1 작업지시·공정 설정과 실행 연결

앱 A PostgreSQL에 불변 공정 설정 버전과 **작성 중(DRAFT)** 작업지시를 등록하고 조회한다. DEV-17b1은 명시적 배포(RELEASED)와 원자적 실행·LOAD_JOB 등록을 추가한다. C1은 [미연결 작업지시 취소](WORK_ORDER_CANCEL.md)만 추가하며 연결 후 취소·수정·삭제는 제공하지 않는다. DEV-17 전체와 M4 완료를 의미하지 않는다. 기준은 [기획서 §10](../HMI_MES_시스템_기획서_v1_0.md#10-mes-작업지시와-생산실적), [개발계획서 §6~7](../HMI_MES_개발계획서_v1_0.md#6-데이터와-api-구현-계획)이다.

## 저장과 마이그레이션

`0008_work_orders`는 `0007_execution_plan` 다음의 고정 DDL이다. 명시적으로 migration을 적용한 후 기존 [사용자 서버](API_RUN.md)의 `serve-user`를 실행한다. API 시작은 migration이나 업무 데이터를 자동 생성하지 않는다.

```sh
.venv/bin/python -m alembic -c alembic.ini upgrade 0009_work_order_binding
```

| 테이블 | 보존 내용과 제약 |
|---|---|
| `recipe_versions` | wire 설정 식별자 `id` PK, `recipe_id`·`version` 고유 쌍, `item_id`, 온도·속도, 생성자·UTC 시각. 유한한 온도 ≥0, 속도 >0 |
| `work_orders` | `id` PK, 품목·라인·설정 버전, 목표 양품 수량 1..4294967295, 예정일, DRAFT, row_version, 생성자·UTC 시각. 라인 FK 및 설정 버전/품목 복합 FK |
| `mes_management_audit` | 실제 사용자, 행위, 설정 또는 작업지시 FK 하나, UTC 시각, 등록 정의 snapshot. 새 등록과 같은 트랜잭션에서 추가 |

`item_id`는 계약 식별자다. 별도 품목 마스터 편집 기능을 추가하지 않는다. `recipe_id`와 `version`은 호출자가 명시하며 버전을 자동 증가시키지 않는다. `id`가 기존 `recipe_version` wire 필드에 사용할 식별자다. 수치는 기존 `Parameters` 계약을 사용하며 실제 설비의 최종 허용 범위·실행 판정은 PLC가 담당한다.

예정일은 `YYYY-MM-DD` 달력 날짜만 받는다. timestamp·숫자·존재하지 않는 날짜를 자동 변환하지 않는다. 목표 수량은 엄격한 정수이며 bool·문자열·소수는 거부한다. `status`, `row_version`, `created_by`, `created_at`은 서버 응답 필드이며 등록 요청에 넣을 수 없다. row_version은 JSON에서 정규 십진 문자열로 반환한다.

이 migration은 기존 실행·목표 수량 계획·생산 사건·totals를 변경하지 않는다. 과거 `production_executions.work_order_id`나 `recipe_version` 문자열이 새 정의와 같아도 자동 연결·보정하지 않는다. 기존 최소 실행 등록 CLI는 유지한다. 관리 실행은 0009의 명시적 연결 관계와 원자적 명령 등록을 사용한다.

`0007_execution_plan`로 downgrade하면 감사·작업지시·설정 버전 순으로 이 세 테이블만 삭제한다. 기존 실행·계획·수량은 보존한다. 이 작업은 등록 정의와 감사 데이터를 삭제하므로 폐기 가능한 시험 DB 또는 검토한 운영 백업·변경 절차에서만 실행한다.

## 사용자 API

`create_user_app()`이 `mount_work_order_routes(app,auth,session_cookie=...)`를 연결한다. 서비스 bearer 또는 앱 B 계정으로 이 API를 사용하지 않는다. 실제 로그인 세션과 최초 비밀번호 변경이 필요하며 mutation에는 정확한 Origin과 CSRF 토큰이 필요하다.

| 현재 앱 A 경로 | 정확한 query / 동작 |
|---|---|
| `POST /api/v1/recipe-versions` | query 없이 불변 설정 버전 등록·동일 정의 재확인 |
| `GET /api/v1/recipe-versions` | 기존 목록 필터로 조회 |
| `GET /api/v1/recipe-version-detail` | `recipe_version_id` / 설정 상세 |
| `POST /api/v1/work-orders` | query 없이 DRAFT 작업지시 등록·동일 정의 재확인 |
| `GET /api/v1/work-orders` | 기존 목록 필터로 DRAFT/RELEASED/CANCELLED 조회 |
| `GET /api/v1/work-order-detail` | `work_order_id` / 작업지시 상세 |
| `POST /api/v1/work-order-actions/release` | `work_order_id` / 원래 DRAFT 버전 배포와 불변 영수증 |
| `POST /api/v1/work-order-actions/bind` | `work_order_id` / RELEASED 버전 실행·목표 계획·LOAD_JOB·예약 원자 등록 |
| `GET /api/v1/work-order-receipts/release` | `work_order_id`, `release_id` / 원래 배포 의도·영수증 |
| `GET /api/v1/work-order-receipts/binding` | `work_order_id`, `command_id` / 원래 실행 연결 의도·영수증 |

고정 query-identity 경로는 표의 식별자를 각각 정확히 한 번 요구한다. 누락·중복·추가 query와 잘못된 Identifier는422다. mutation JSON DTO·Origin/CSRF·역할·트랜잭션·원래 영수증은 변경하지 않는다. detail 조회의 기존 인증/idle 정책과 receipt 조회의 passive idle 미갱신 정책도 유지한다.

기존 `GET /api/v1/recipe-versions/{id}`, `GET /api/v1/work-orders/{id}`, `POST /api/v1/work-orders/{id}/release`, `POST .../{id}/bind`, `GET .../{id}/releases/{release_id}`, `GET .../{id}/bindings/{command_id}`는 호환 경로로 보존한다. 다만 단일 path segment는 slash ID를 표현하지 못하고 단독 `.`/`..`는 URL 정규화로 다른 경로가 될 수 있다. 유효한 Identifier 전체에 대한 앱 A 호출은 위 고정 query 경로를 일관되게 사용한다. 기존 Identifier/DB 범위를 줄이거나 기존 데이터를 이관하지 않는다.

UI는 `URLSearchParams`로 query 값을 한 번 인코딩하여 slash·dot·percent literal·정상 UTF8을 원문 그대로 전달한다. 추가 unquote·문자 정규화·404 후 경로 추측/fallback은 하지 않는다. 원래 ID 조회의 경로 변경이 새 ID나 자동 POST를 만들지 않는다. C1 취소 경로는 [WORK_ORDER_CANCEL.md](WORK_ORDER_CANCEL.md), LOAD 원래 명령 조회는 [CONTROL_HTTP.md](CONTROL_HTTP.md)를 따른다.

설정 등록 JSON 예시:

```json
{
  "id": "packing-v1",
  "recipe_id": "packing",
  "version": "v1",
  "item_id": "item-A",
  "parameters": {"temperature_c": 150.0, "speed_m_per_min": 6.0}
}
```

작업지시 등록 JSON 예시:

```json
{
  "id": "work-A",
  "item_id": "item-A",
  "line_id": "packaging-line-1",
  "recipe_version_id": "packing-v1",
  "target_quantity": 100,
  "planned_date": "2026-10-23"
}
```

작업지시의 품목과 선택한 설정 버전의 품목이 일치해야 한다. 같은 ID·같은 정의는 원래 생성자·시각을 보존한 200 응답을 반환하고 감사 행을 다시 추가하지 않는다. 같은 ID의 다른 내용 또는 같은 recipe/version 쌍을 다른 ID로 등록하면 409다. 알 수 없는 라인·설정·상세 ID는 404, 잘못된 입력·추가 필드·조회 조건은 422다. JSON이 아닌 등록은 415, 16KiB 초과 본문은 413이다. schema/DB 장애는 비밀과 driver 오류를 포함하지 않는 503이다.

목록은 `limit=1..100`(기본 50), `after_id`, `item_id`를 받으며 작업지시에는 `line_id` 필터도 제공한다. 응답은 `{items:[...],next_cursor:...}`다. 다음 조회는 next_cursor를 after_id로 사용한다. ID의 C collation 순서로 조회하고 마지막 페이지의 cursor는 null이다. 중복 query 인자나 지원하지 않는 필터는 거부한다.

| 역할 | 조회 | 설정 등록 | 작업지시 등록 |
|---|---|---|---|
| viewer | 허용 | 거부 | 거부 |
| operator | 허용 | 거부 | 거부 |
| production_manager | 허용 | 허용 | 허용 |
| equipment_admin | 허용 | 거부 | 거부 |

설정은 `parameters_manage`, 작업지시는 `production_manage` 권한을 사용한다. 설비 관리자에게 생산 권한을 자동 상속하지 않는다. PUT/PATCH/DELETE는 제공하지 않는다. C1 cancel은 production_manage로 미연결 DRAFT/RELEASED만 허용하며 bound 작업은 모든 단계에서 409다. 배포는 production_manage, 실행 연결은 load_job 권한을 별도로 확인한다. DRAFT 등록 성공은 실행 연결·운전 준비·실적·정산 완료의 증거가 아니다.

## 트랜잭션과 후속 연결 경계

모든 `work_orders.py` 도우미는 호출자의 **이미 시작된 PostgreSQL Connection 트랜잭션**을 사용한다. 내부 begin/commit/rollback·외부 I/O를 수행하지 않는다. HTTP는 `UserAuth.authorized_transaction()`의 schema·사용자·세션 잠금과 권한 검사를 유지한 상태에서 정의와 감사를 함께 기록한다.

설정 등록은 ID와 recipe/version 쌍을 구별하는 정규 JSON 키 두 개를 정렬하여 transaction advisory lock을 획득한다. 작업지시는 **전역 작업지시 ID advisory lock을 먼저**, 신규 등록 시 line 행 잠금을 다음에 획득한다. 같은 ID를 서로 다른 라인으로 동시에 등록해도 한 정의만 확정한다. 설정은 불변이므로 작업지시 등록이 설정을 갱신하거나 반대 순서로 row lock을 잡지 않는다. 한 HTTP 트랜잭션은 등록 한 건을 처리한다.

실행 연결은 아래 잠금·정산 절차에 따라 실행·고정 설정·LOAD_JOB·감사를 같은 연결에서 commit한다. 최소 실행 등록 함수의 caller-Connection 도우미를 사용하며 내부 트랜잭션을 중첩하지 않는다. LOAD 전달 이후 취소를 미전달로 간주하거나 새 command ID로 반복하는 경로도 이 단위에 추가하지 않는다.

## 검증

```sh
.venv/bin/ruff check apps/api/work_orders.py apps/api/work_orders_http.py \
  apps/api/migrations/versions/0008_work_orders.py \
  tests/integration/test_work_orders.py tests/integration/test_work_orders_http.py
.venv/bin/pytest -q tests/integration/test_work_orders.py tests/integration/test_work_orders_http.py
```

실제 PostgreSQL 시험은 schema/FK/수치 범위·같은 ID/버전 경합·서로 다른 라인 경합·등록/감사의 원자적 rollback·호출자 commit 소유권·기존 실행 보존을 확인한다. 실제 UserAuth와 ASGI 사용자 앱 시험은 역할표·CSRF/Origin·초기 비밀번호 제한·DRAFT 응답·멱등 등록·오류 정보 보호와 미구현 업무 경로 부재를 확인한다. 등록 자체는 설비를 호출하지 않는다. 실행 연결의 실제 전달·정산 검증은 별도 gateway pipeline 시험에서 수행한다.


## DEV-17b1 배포와 원자적 실행 연결

`0009_work_order_binding`은 0008 다음의 고정 DDL이다. `work_order_releases`는 원래 배포 의도·사용자·감사 ID·영수증을 저장한다. `work_order_execution_links`는 작업지시와 실행을 일대일로 연결하고 원래 bind 의도·LOAD 요청·영수증을 보존한다. `line_execution_reservations`는 물리 라인의 실행 한 개를 예약한다. 복합 FK는 명령의 LOAD_JOB 종류·실행·라인·source/stream, 실행의 작업지시, 예약의 관리 연결을 함께 고정한다. 기존 문자열 ID가 같다는 이유로 레거시 실행을 연결하거나 보정하지 않는다.

배포 요청 예시:

```json
{"release_id":"release-work-A","expected_row_version":"1"}
```

`production_manager`가 배포할 수 있다. DRAFT의 원래 row_version을 확인하고 RELEASED/version 2, 감사, 불변 영수증을 같은 트랜잭션에 기록한다. 동일 release_id·작업지시·사용자·원래 버전 재요청은 bind 이후에도 최초 영수증을 반환한다. ID의 다른 의도나 다른 사용자는 409이며 새 감사를 추가하지 않는다.

실행 연결 요청 예시:

```json
{
  "command_id":"load-work-A",
  "line_id":"packaging-line-1",
  "source_id":"configured-source",
  "stream_id":"configured-stream",
  "expected_work_order_version":"2",
  "expected_control_version":"42"
}
```

`operator` 또는 `production_manager`의 load_job 권한이 필요하다. 서버가 실행 UUID, 실제 사용자, 최초 UTC 요청 시각과 5초 TTL, 계약 설정·목표 수량·fresh CounterEpoch를 결정한다. 클라이언트가 이 필드들을 지정할 수 없다. 응답에는 execution_id, command_id, 범위, work_order_row_version과 원래 load_request가 포함된다. 같은 command_id·사용자·작업지시·범위·두 expected version을 재요청하면 현 상태·TTL·시각 생성 전에 원래 영수증을 반환한다. 새로운 실행·명령·감사를 만들지 않는다. 다른 의도나 기존 레거시 command_id의 관리 연결 전환은 409다.

새 연결은 fresh GOOD 원격 제어 증거, STOPPED 상태, WIP 0, 기대 제어 버전·epoch, 기존 실행의 최종 정산을 확인한다. 실행·execution_plans·수량 0·LOAD_JOB journal·명시적 연결·예약·작업지시 버전·감사는 한 PostgreSQL 트랜잭션이다. 검증 또는 저장 어느 단계가 실패해도 전부 rollback한다. 접수 영수증은 PLC 성공을 뜻하지 않는다. Gateway가 별도 service HTTP와 OPC UA로 원래 명령을 전달하고 결과를 조회한다.

잠금 순서는 인증/schema 다음 전역 command/release ID, 전역 작업지시 ID, line, source, 작업지시 행, 실행이다. 일반 명령은 line 뒤에 작업지시 advisory를 획득하지 않는다. line 잠금 안에서 다른 실행의 신규 LOAD/START를 예약으로 차단하므로 레거시 API도 우회하지 못한다. 명시적 관리 연결 없이 catalog 작업지시를 LOAD하는 경로를 거부한다. 기존 canonical 명령의 정확한 재조회는 새 정책 전에 처리하며 나중에 catalog에 같은 문자열이 등록되어도 과거 실행을 관리 실행으로 추정하지 않는다. PAUSE/END 안전 동작은 유지한다.

예약 교체는 **예약에 기록된 정확한 execution/source/stream**의 SETTLED 증거가 있어야 한다. physical active ID가 비었다는 이유로 예약을 해제하지 않는다. 결과 불명·거부·만료된 LOAD도 자동 해제하지 않는다. C1 미연결 취소가 추가되었지만 연결 후 취소는 후속 단위이며 현재 거부한다. Simulator는 앱 A DB에 접근하지 않는다.

사용한 배포·연결·예약·감사 또는 RELEASED 작업지시가 있으면 0009 downgrade를 거부하고 기록을 보존한다. 사용 전 빈 schema에서만 0008로 되돌릴 수 있다. 다음 명령은 caller-Connection 원자성·경합·정확한 재요청·복합 FK·권한·물리 증거·실제 OPC/HTTP 전달을 검증한다.

```sh
.venv/bin/pytest -q tests/integration/test_work_order_workflow.py \
  tests/integration/test_gateway_pipeline.py
```

## 응답 유실 후 원래 ID로 수동 조회

배포·연결 POST의 응답을 잃으면 원래 작업지시와 release_id 또는 command_id를 위 GET 경로로 조회한다. 성공 응답은 `{intent,receipt}`이며 두 값은 저장한 최초 JSON이다. 현재 작업지시 버전이나 최신 명령으로 바꾸지 않는다. release의 intent는 `work_order_id,actor,release_id,expected_row_version`을, binding의 intent는 `work_order_id,actor`와 원래 BindIntent 필드들을 포함한다. receipt는 해당 POST의 최초 영수증 그대로다. 버전은 정규 십진 문자열이며 원래 UTC 시각 표기도 보존한다. binding의 `load_request`는 최초 실행 UUID·요청 시각·TTL·제어 버전·설정·수량을 유지한다.

GET은 모든 기존 역할의 `read` 권한, 유효한 로그인 세션과 최초 비밀번호 변경을 요구한다. Origin/CSRF는 mutation에만 필요하다. 조회가 idle 만료를 연장하지 않으며 응답은 `Cache-Control: no-store`다. 다른 담당자도 원래 actor를 확인할 수 있으나 GET이 원래 POST를 다른 actor로 재요청할 권한을 부여하지 않는다. 기존 POST의 동일 의도·사용자 비교는 유지한다.

두 원래 ID를 함께 확인하며 다른 작업지시·없는 ID는404다. 새 query 경로는 정확한 두 식별자를 한 번씩 요구하고 잘못된 식별자·중복·추가·누락 인자는422다. 기존 path 복구 경로는 모든 query 인자를 계속 거부한다. 저장 의도와 영수증이 불일치하거나 지원하지 않는 schema는 503이며 내부 증거·driver 오류를 응답에 노출하지 않는다. receipt가 없는 404는 미전달·실패·취소의 증거가 아니다. 자동으로 새 ID나 POST를 생성하지 않는다. 클라이언트는 원래 actor·작업지시·범위·기대 버전을 대조한 뒤 binding receipt의 원래 command_id와 scope로 기존 명령 상태를 조회할 수 있다.

이 조회는 운영 상태·freshness·TTL과 무관하게 저장된 증거만 읽는다. preflight·정산·감사·행 갱신·라인/실행 잠금·게이트웨이 호출·PLC 재제출을 수행하지 않는다. 실제 PostgreSQL/ASGI 시험은 만료된 원래 명령과 LOCAL 전환 후 복구, 역할표·세션 제한·경로 범위·잘못된 저장 증거, 업무 및 idle 쓰기 부재를 검증한다.

## Identifier 경계 검증 기록

실제 PostgreSQL/ASGI의 새6-case(ASCII, slash, `.`, `..`, percent literal, UTF8) 및 exact-query/auth/idle 시험과 기존 작업지시·취소·workflow 회귀는 **106개 통과**했다. 제어 HTTP 기존 회귀 **35개 통과**는 별도 실행이다. 실행/알람의 새 alias **34개와 기존 회귀67개**는 별도 범위이며 이106개에 포함해 주장하지 않는다.

```sh
.venv/bin/pytest -q tests/integration/test_identifier_http.py \
  tests/integration/test_work_order_cancel.py tests/integration/test_work_orders_http.py \
  tests/integration/test_work_order_workflow.py
.venv/bin/pytest -q tests/integration/test_control_http.py
```

현재 웹 **61개 순수 시험·typecheck/lint/build** 및 실제 전체 Chromium/일회용 PostgreSQL/API 검사도 통과했다. 실제 브라우저의6-case는 UI recipe/작업지시 commit 응답 유실 후 원래 GET 복구, 특수 작업지시의 UI 취소, 별도 실제 API로 저장한 특수 original cancellation/release ID의 수동 GET 복구와 추가 POST 부재를 확인한다. 원래 LOAD/command 특수 ID는 영구PG6-case와 순수 URL 시험으로, 실제 브라우저에서는 기존 원래 LOAD/command 추적 회귀로 검증했다. 로그 `.local/qa/identifier-browser.log`, 화면 `.local/qa/app-a-identifiers-run-identifiers.png`다. 실제 OPC/PLC 운전 시험을 대체하지 않는다.

## CX-32: 서버 ID 발급과 원래 등록 결과 조회

변경 이유: 목록을 읽어 화면에서 다음 번호를 계산하면 동시 등록이 충돌한다. 서버가
자동 ID·원래 등록 응답·감사를 같은 트랜잭션에 저장하도록 추가했다. 기존 수동 ID와
불변 정의·응답 형식은 유지한다. 품목 마스터는 별도 CX-37 범위다.

`POST /api/v1/recipe-versions`에서 id와 version을 모두 생략/null/빈 문자열로 보내면
recipe_id 그룹의 다음 숫자 vN과 `{recipe_id}-vN`을 발급한다. 둘 중 하나만 생략하면422다.
작업지시 id를 생략/null/빈 문자열로 보내면 KST 예정일 planned_date의
`WO-YYYYMMDD-NNN`을 발급한다. NNN은 최소3자리이며999 뒤에는1000이다.
서버 UTC 현재일로 예정일을 바꾸지 않는다. 공백 문자열은 유효한 ID로 변환하지 않는다.
자동 발급에는 request_id가 필수다. 기존 수동 등록에는 선택이고, 넣으면 같은 복구 계약을 쓴다.

예시:

```json
{"request_id":"recipe-registration-1","recipe_id":"packing","item_id":"item-A",
 "parameters":{"temperature_c":150.0,"speed_m_per_min":6.0}}
```

응답은 원래 RecipeVersion/WorkOrder 객체이고 발급된 id·version이 들어 있다.
`GET /api/v1/registration-receipts/recipe?request_id=...` 또는
`GET /api/v1/registration-receipts/work-order?request_id=...`로 원래 등록 응답을 조회한다.
정확히 한 query만 허용하며 로그인 read 권한과 Cache-Control:no-store를 적용한다.
같은 원래 계정의 새 세션에서도 조회할 수 있지만 다른 계정에는404다. POST의 같은 키에
다른 본문/계정은409 ID_CONTENT_CONFLICT이며 원래 응답을 노출하지 않는다.

정규화된 원래 본문은 request_id를 제외하고 보관한다. 같은 키·본문은 최초 응답을 그대로
반환하며 작업지시가 나중에 RELEASED/CANCELLED로 바뀌어도 최초 DRAFT 등록 응답을 반환한다.
결과 불명 시 새 키를 만들어 다시 등록하지 말고 원래 키만 조회/재확인한다.

schema0019_registration_ids는 namespace별 숫자 counter와 불변 registration_receipts를
추가한다. request lock→종류별 registration lock→기존 불변 writer 순서이며 직접 Python
수동 writer도 같은 종류 잠금을 쓴다. recipe와 work order는 서로 독립이다. counter는 기존
수동 vN/날짜 ID와 이미 쓰인 ID를 반영하며 점유 ID는 건너뛴다. 따라서 번호 공백은 가능하다.
원본 등록·counter 증가·audit·receipt가 함께 커밋/롤백된다. DB에 잠금을 우회한 임의 SQL을
동시에 넣는 경로의 자동 재시도까지 제공하지 않는다.

ID 길이를 자르지 않으며 발급 결과가 Identifier 범위를 벗어나거나 지원 숫자 범위를
소진하면 고정422 입력 오류로 거절한다. counter는 양의 정확한 정수이며 최대 signed bigint다.
기존 수동 등록은 지원되는 이전 schema에서도 동작하지만 자동/키가 있는 등록은0019가 필요하다.
head19에서도 기존 clock-evidence18의 검증과 분석 정책을 유지한다.

운영 이관은 Claude가 수행한다. 준비 파일은 `deploy/neon-http/upgrade-0018-0019.json`,
`deploy/neon-http/grants-0019.json`, `deploy/neon-registration-grants.sql`이다.
renderer의 registration-upgrade/registration-grants로 재현한다. 이전17/18 batch는 유지한다.
기존18 clock grants가 선행하며 app에는 counter SELECT/INSERT/UPDATE, receipt SELECT/INSERT만
준다. receipt UPDATE/DELETE/TRUNCATE는 금지한다. app에 ownership/DDL 권한을 주지 않는다.
되돌리기는 counter·receipt가 모두 비어 있을 때만 downgrade가 가능하다. 사용한 번호와
원래 결과를 지우는 downgrade는 차단하므로 실제 사용 이후 rollback은 사전 백업/별도 검토가 필요하다.
이번 변경으로 공개 DB에 자동 migration을 실행하거나 UI를 바꾸지 않는다.

## CX-37 — 등록된 품목 참조 (schema21)

CX-32 당시 후속 범위였던 품목 마스터를 추가했다. schema21의 새로운
recipe/work-order 등록은 실제 item_master에 있고 enabled=true인 품목만
참조한다. 수동/자동 ID 발급에 같은 검사를 적용하며, unknown item을
자동 생성하지 않는다. 기존 ID/등록 영수증 재조회는 먼저 처리하고,
구schema 동작·원래 실행/정산/bind 조건은 유지한다.

변경 이유는 자유 문자열 품목을 등록된 업무 정의로 제한하기 위해서다.
기존 품목은 0021에서 합집합으로 보존 등록하고 FK를 추가한다. name=id와
unit=EA는 이관 기본값이며 실제 품목 정보를 복원했다는 뜻이 아니다.
목록/등록 API·불변 정의·최소 grants·롤백·UI seed 영향은
[ITEM_MASTER.md](ITEM_MASTER.md)를 따른다. 공개 이관은 Claude가 수행한다.
