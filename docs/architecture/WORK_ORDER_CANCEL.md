# C1 미연결 작업지시 취소

이 단위는 **명시적 관리 실행 연결이 없는 DRAFT/RELEASED 작업지시**만 CANCELLED로 바꾼다. 연결된 실행의 취소·자동 END_RUN·LOAD_JOB 재제출·라인 예약 해제는 제공하지 않는다. DEV-17/18 전체 취소 완료를 의미하지 않는다.

기준은 [기획서 §10](../HMI_MES_시스템_기획서_v1_0.md#10-mes-작업지시와-생산실적)의 “미연결 작업은 즉시 취소”, “연결 요청 이후에는 결과 확인과 실행 종료 후 취소 확정”, 생산 발생 시 실적 보존 종료 요구와 [개발계획서 §5.2](../HMI_MES_개발계획서_v1_0.md#52-생산-사건과-작업-정합성)의 최종 순번·수량 대조 경계다.

## 명시적 schema와 사용자 API

`0013_work_order_cancel`은 `0012_alarm_line_fault` 다음의 고정 DDL이다. 운영자가 명시적으로 적용한다. API는 migration이나 사용자·작업·실행을 자동 생성하지 않는다.

```sh
.venv/bin/python -m alembic -c alembic.ini upgrade 0013_work_order_cancel
```

| 현재 앱 A 경로 | 정확한 query / 권한과 의미 |
|---|---|
| `POST /api/v1/work-order-actions/cancel` | `work_order_id` / production_manage로 미연결 작업지시 취소 |
| `GET /api/v1/work-order-receipts/cancellation` | `work_order_id`, `cancellation_id` / read로 원래 의도·영수증 수동 복구 |

새 경로는 표의 식별자를 각각 정확히 한 번 요구한다. 누락·중복·추가 query와 잘못된 Identifier는422다. 기존 `POST /api/v1/work-orders/{id}/cancel`, `GET /api/v1/work-orders/{id}/cancellations/{cancellation_id}`는 그대로 보존하며 이 legacy 경로의 모든 query 거부도 유지한다. slash·단독 dot segment 때문에 path로 표현되지 않는 유효 ID는 고정 query 경로를 사용한다. UI는 `URLSearchParams`로 한 번 인코딩하며 percent literal·정상 UTF8·원래 ID를 보존한다. Identifier/DB 제한·추가 decoding·404 fallback·자동 POST는 추가하지 않는다.

POST는 실제 production_manager 로그인 세션, 최초 비밀번호 변경, 정확한 Origin과 CSRF를 요구한다. operator·equipment_admin·viewer에게 작업지시 취소 권한을 자동 부여하지 않는다. `end_run` 설비 종료는 별도 권한·행위다.

```json
{"cancellation_id":"cancel-work-A","expected_row_version":"1"}
```

서버가 사용자·최초 UTC 시각·다음 버전을 결정한다. 클라이언트가 actor, 상태, 취소 시각, 실행 ID를 넣을 수 없다. 성공 응답은 다음 형태다.

```json
{
  "cancellation_id":"cancel-work-A",
  "work_order_id":"work-A",
  "cancelled_by":"production.manager",
  "cancelled_at":"2026-10-28T00:00:00+00:00",
  "status":"CANCELLED",
  "row_version":"2"
}
```

버전은 정규 십진 UInt64 문자열이며 overflow를 거부한다. 동일 cancellation_id·작업지시·실제 사용자·원래 expected version의 정확한 재요청은 현재 버전·시각을 다시 생성하기 전에 최초 영수증을 반환한다. 다른 사용자·내용·작업지시에 같은 ID를 쓰거나 stale version이면 409다. 기존 ID로 등록을 반복해도 CANCELLED를 DRAFT로 되돌리지 않는다. 과거 release 영수증도 변경하지 않는다.

잘못된 식별자·추가 필드·허용되지 않은 query·중복/누락 query·입력은422, JSON이 아니면 415, 16KiB 초과면 413이다. 없는 작업지시는 404, 지원하지 않는 schema/저장 오류는 비밀·SQL·driver 내용을 포함하지 않는 503이다. 취소·복구 경로는 위 exact 식별자 query 외의 필터나 최신 ID 추측을 제공하지 않는다.

## 원자성과 경합

`work_order_cancellations`는 cancellation_id PK, work_order_id UNIQUE FK, 실제 actor FK, 원래 expected version, 불변 original_intent/receipt, 고유 취소 감사 FK, 최초 UTC 시각을 저장한다. DB trigger가 receipt UPDATE/DELETE를 거부한다. 같은 트랜잭션에서 작업지시의 CANCELLED/version 증가, CANCEL_WORK_ORDER 감사, 원래 영수증을 commit한다. 어느 저장 단계가 실패해도 모두 rollback한다.

잠금 순서는 기존 schema/사용자/세션 다음 취소 ID → 전역 작업지시 ID → line → 작업지시 행이다. 기존 bind의 작업지시→line 순서와 같게 사용한다. 잠금 후 DRAFT/RELEASED·기대 버전·명시적 work_order_execution_links 부재를 다시 확인한다. bind가 먼저 확정되면 취소는 409, 취소가 먼저 확정되면 bind는 거부한다. 부분 실행·고아 예약을 만들지 않는다. 동일 취소 ID를 서로 다른 작업지시로 동시에 사용해도 한 의도만 확정한다.

레거시 실행의 work_order_id 문자열이 새 작업지시 ID와 같아도 관리 연결로 추정하지 않는다. 미연결 catalog 작업지시를 취소하면서 기존 레거시 실행의 ID·설정·계획·수량·사건은 바꾸지 않는다. 원래 command journal/result/canonical request·실행 링크·라인 예약을 변경하지 않는다.

## 연결 요청 이후는 이 단위에서 거부

명시적 관리 link가 있으면 다음 모든 경우 409이며 취소 intent·영수증·감사를 만들지 않는다.

| 관측/명령 상태 | C1 의미 |
|---|---|
| LOAD REQUESTED | 아직 전달되지 않은 듯 보여도 이미 연결 요청이므로 거부 |
| DELIVERED/ACCEPTED/OUTCOME_UNKNOWN | 원래 ID 결과 확인 필요. 접수·타임아웃을 취소 확정으로 바꾸지 않음 |
| SUCCEEDED/LOADED | 명시적 종료와 최종 증거·정산 절차가 필요 |
| RUNNING/PAUSED/ENDING, 생산 발생 | 실적을 보존하는 종료 절차가 필요 |
| FAILED/REJECTED/EXPIRED, ENDED | 상태 하나만으로 연결 슬롯 해제·취소 확정하지 않음 |

physical ActiveExecutionId가 비거나 저장 SETTLED가 있더라도 C1은 link를 삭제하지 않는다. 이후 bound 취소에는 dispatch/START 경합, 원래 결과 조회, 명시적 END_RUN, WIP·최종 경계·연속 cursor·대조가 필요하다. 이 미완성 절차를 현재 API가 수행하는 것처럼 표시하지 않는다. 새 LOAD/END/다른 command ID를 자동 생성하지 않는다.

## 응답 유실 복구와 downgrade

GET은 `{intent,receipt}`를 저장한 원래 JSON 그대로 반환한다. intent에는 work_order_id, actor, cancellation_id, expected_row_version이 있다. 다른 사용자도 기존 read 권한으로 원래 담당자를 확인할 수 있지만 POST의 actor 동일성 조건은 유지한다. 다른 작업지시·없는 ID는 404다. 유효한 세션과 최초 비밀번호 변경이 필요하며 passive GET은 idle 만료를 연장하지 않는다. `Cache-Control: no-store`이며 업무 쓰기·감사·운영 상태 조회·정산·PLC 호출을 하지 않는다.

사용한 취소 영수증·감사 또는 CANCELLED 작업지시가 있으면 downgrade를 거부하여 기록을 보존한다. 사용 전에는 0012로 되돌릴 수 있으며 기존 정의·실행·수량을 유지한다. 원래 작업지시/배포/연결과 수동 조회 경계는 [WORK_ORDERS.md](WORK_ORDERS.md), [EXECUTION_READ.md](EXECUTION_READ.md)를 따른다.

## 실제 앱 A UI와 확정 거절 이후 새 검토

작업지시 목록의 취소 검토·최종 확인은 실제 production_manager에게만 제공한다. DRAFT/RELEASED라는 상태만으로 미연결이라고 추정하지 않는다. 현재 확인한 관리 실행 연결은 UI에서 비활성화하고, 미확인 연결은 서버의 bound409가 최종 판단한다. CANCELLED는 조회·표시하되 배포/실행 연결 대상으로 선택하지 않는다. 취소 metadata 처리에 WS 정상이나 PLC 정지를 요구하지 않으며, 다른 LOAD/END_RUN/일반 command의 추적과 권한을 대체하지 않는다.

독립된 App 메모리 ledger는 원래 cancellation ID·actor·expected version·영수증을 화면 전환·로그아웃 후에도 유지한다. 다른 계정은 read 권한으로 원래 actor의 기록을 수동 조회할 수 있지만 재전송하거나 원래 요청을 인수하지 않는다. 응답 유실·abort·timeout·GET404는 미처리 증거가 아니며 일반 clear·자동 POST·자동 새 ID를 허용하지 않는다.

현재 명시적 새 준비는 **한 번 보낸 원래 C1 POST의 완전한409 응답을 확인한 경우**로 제한한다. C1 전용 transport가 본문 JSON을 끝까지 읽고 정확히 `{"detail":"cancellation conflicts with original state or intent"}`인 정적 envelope를 검증해 typed rejection으로 기록한다. 본문 읽기 실패·다른 응답 형식·GET409는 확정 POST 거절 근거가 아니다. 정적 detail은 route 응답의 일관성 검증에만 사용하며 stale/bound 원인이나 현재 취소 자격을 추론하지 않는다. 공통 MES transport는 바꾸지 않는다.

원래 POST409 관측과 이후 GET404·조회 실패는 append-only 관측으로 분리한다. GET404 때문에 표시 phase가 UNKNOWN이 되어도 원래 POST 거절 증거를 덮어쓰지 않는다. 기존 성공 영수증·원래 제출 불명·수동 입력으로만 복구한 reference·불일치 복구 기록은 거절 재준비 대상이 아니다.

원래 actor인 production_manager가 원래 로그인 세대에서 **거절 기록 보관 후 작업 다시 확인**을 명시적으로 선택하면 최신 작업 상세를 GET으로 읽는다. 같은 사용자·로그인 세대, 정확한 작업지시, DRAFT/RELEASED·버전을 검증한 뒤 원래 ledger·모든 관측·최신 작업 응답을 불변 이력에 보존한다. 해당 C1 owner/ID의 lease만 해제하고 별도 취소 확인을 연다. 보관·조회에서는 새 ID나 POST가 생기지 않는다. 사용자가 별도 확인을 완료한 때만 새 ID·최신 버전으로 한 번 POST하며 서버는 조건을 다시 검증한다. bound409를 보관해도 취소 가능·미연결이라는 결론이나 DB link/slot 변경은 생기지 않는다.

로그아웃·재로그인은 같은 actor여도 원래 재준비 권한을 승계하지 않는다. 같은 작업지시에 남은 workflow lease, 기존 원래 LOAD/OUTCOME_UNKNOWN, unrelated command tracker는 해제하지 않는다. release/bind·일반 command 재준비는 이 단위에 포함하지 않는다. 확인된 취소 이력과 거절 이력은 현재 페이지 메모리 범위이며 새로고침 후 영속 보존되는 감사 기록으로 표현하지 않는다. DB의 불변 영수증·감사는 앞 절의 서버 저장 경계를 따른다.

## 검증

```sh
.venv/bin/ruff check apps/api/work_order_cancel.py apps/api/work_order_cancel_http.py \
  apps/api/migrations/versions/0013_work_order_cancel.py \
  tests/integration/test_work_order_cancel.py
.venv/bin/pytest -q tests/integration/test_work_order_cancel.py \
  tests/integration/test_work_orders.py tests/integration/test_work_orders_http.py
```

실제 PostgreSQL/ASGI 시험은 DRAFT/RELEASED 취소, clock-free 동일 재요청·원래 배포 보존·취소 후 재등록, 권한/세션/Origin/CSRF, stale/overflow, 저장 단계 rollback, bind·취소 및 전역 ID 경합, 모든 bound 단계 거부와 journal/slot/수량 불변, legacy alias 독립, passive recovery 쓰기 부재, DB receipt 불변성과 사용 후 downgrade 거부를 확인한다. 실제 PLC 취소나 프로젝트 전체 취소 수락 시험의 통과를 주장하지 않는다.

### 웹·실제 브라우저 확인

웹 순수 시험 **59개**, typecheck·lint·build, `scripts/cancellation_browser_checks.py` Ruff가 통과했다. 실행 명령과 UI 범위는 [웹 README](../../apps/web/README.md)의 검증 기록을 따른다. 실제 Chromium/일회용 PostgreSQL/사용자 API 전체 검사는 DRAFT/RELEASED 취소·원래 release 영수증 보존, commit 후 응답 유실·화면 전환·로그아웃/다른 actor GET 복구, GET404 미확정·추가 POST 부재, stale/bound409 무효과, 역할별 UI/API403, additive same-work-order guard·원래 LOAD/command 추적 보존을 확인했다.

추가로 stale POST409→GET404 후 거절 근거 유지, 명시적 fresh 작업 버전2 확인/불변 이력 보관 시 POST 부재, 별도 확인에서 새 ID 1회로 CANCELLED 버전3, bound 거절 보관 이후에도 새 요청409·DB/slot 불변, 재로그인 후 준비 권한 미승계를 확인했다. 기존 auth·WS·controls·MES·two-job·alarm 회귀도 같은 실행에서 통과했다.

전체 로그: `.local/qa/c1-reprepare-browser.log`. 원래 LOAD와 취소 영수증·거절/조회 이력 화면: `.local/qa/app-a-reprepare-run-reprepare.png`. 이전 C1 로그/화면 `.local/qa/c1-browser.log`, `.local/qa/app-a-cancellation.png`는 보존했다. 전체 검사 후 실패 조회를 성공처럼 표시하지 않도록 관측 문구만 수정하고 typecheck·lint·build를 재실행해 통과했다. 실제 OPC/PLC 실행·설비 취소 검증은 별도이며 이 결과가 이를 대체하지 않는다.

### Query alias 후속 검증

현재 웹은 **61개 순수 시험·typecheck/lint/build**를 통과했다. Identifier alias·기존 작업지시/취소/workflow의 실제PG **106개**, 별도 제어 HTTP **35개**가 통과했다. 실행/알람 새 alias **34개+기존67개**는 별도 API 검증 범위다. 최신 전체 브라우저 로그는 `.local/qa/identifier-browser.log`,6-case 원래 취소ID 이력 화면은 `.local/qa/app-a-identifiers-run-identifiers.png`다.6-case 실제UI등록·GET복구·취소와 특수 원래 cancellation/release 영수증 GET, 기존 C1 확정409 재준비/unknown/no-autoPOST·LOAD/guard 보존 및 auth/WS/MES/two-job/alarm 회귀를 같은 실행에서 확인했다. 앞 절의 C1 전용 로그는 과거 단위 증거로 보존한다. 실제 OPC/PLC 취소 검증과 구별한다.
