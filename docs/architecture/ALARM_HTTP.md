# 인증 알람 조회·조치 HTTP 단위

`apps/api/alarm_http.py`의 `mount_alarm_routes(app, auth, *, session_cookie="hmi_session")`를 App A user HTTP factory에 연결한다. PostgreSQL `alarm_store`를 실제 조회·변경하는 알람 센터 API다. 순수 qualification 입력이나 설정 JSON을 외부에서 적용하는 경로는 제공하지 않는다. 규칙 설치·변경 API는 별도 단위다. PLC 고장 qualification과 capture consumer는 별도 단위다. 통신·명령 결과 알람, 현재 consumer의 생존 증명, 화면·지연·장시간 수락을 이 API만으로 완료했다고 주장하지 않는다.

## 경로·권한

| 현재 앱 A 경로 | 대상 식별자 / 동작 |
|---|---|
| `GET /api/v1/alarms` | 기존 exact scope와 필터 / 여러 규칙 알람 page |
| `GET /api/v1/alarm-instance` | scope, `rule_id`, `alarm_id` / 정확한 occurrence와 네 상태 |
| `GET /api/v1/alarm-actions` | scope, `rule_id`, `alarm_id`, 선택적 `limit`, `before_action_id` / 조치 audit page |
| `GET /api/v1/alarm-action-receipts` | scope, `rule_id`, `alarm_id`, `action_id` / 원래 intent·서버 시각·receipt·선택적 action 복구 |
| `POST /api/v1/alarm-actions` | query 없이 기존 immutable intent JSON / ACK 또는 메모 추가·수정 |

scope는 `line_id`, `source_id`, `stream_id`, `asset_id`다. GET의 모든 필수 식별자는 query에 각각 정확히 한 번 요구하며 누락·중복·추가·잘못된 Identifier는422다. page별 허용 필터만 추가할 수 있다. 기존 `GET /api/v1/alarms/{alarm_id}`, `GET .../{alarm_id}/actions`, `GET .../{alarm_id}/action-receipts/{action_id}`, `POST .../{alarm_id}/actions`도 보존하며 같은 reader/store/권한/트랜잭션을 사용한다.

UI는 새 고정 alias와 `URLSearchParams`로 slash·단독 dot segment·percent literal·정상 UTF8 ID를 query 값으로 한 번 인코딩한다. 원래 Identifier/DB를 축소하지 않으며 추가 decoding·Unicode 정규화·404 후 경로 추측/fallback·새 ID·자동 POST를 하지 않는다. POST는 기존 JSON의 scope·rule_id·alarm_id를 대상으로 하며 모든 query를 거부한다.

GET은 변경된 초기 암호와 현재 read 역할을 요구한다. viewer/operator/production_manager/equipment_admin 모두 기존 read 정책으로 조회할 수 있다. 새 사용자별 line ACL을 가정하지 않는다. `authorized_transaction(...permission="read",refresh_idle=False)`를 사용하여 polling·복구 조회가 idle 30분을 연장하지 않는다.

POST는 `alarm_action`인 **operator**만 허용한다. equipment_admin 단독 권한은 알람 확인 권한이 아니다. 정확히 하나의 Origin과 X-CSRF-Token, 세션 결합 CSRF, JSON Content-Type을 요구한다. 실제 Host/origin·no-store·입력 body 전체 상한은 기존 user HTTP boundary도 적용한다. 이 route의 추가 body 상한은 16,384바이트다. actor는 live principal.login과 정확히 같아야 한다. 다른 역할·초기 암호·로그아웃·만료·비활성·권한 변경 세션은 재전송에서도 다시 검사한다.

인증과 business SQL은 같은 caller-owned transaction을 사용한다. shared schema lock과 사용자/세션 잠금을 먼저 유지하고, store의 정렬된 channel/input advisory lock, channel row lock 순서를 그대로 따른다. DB transaction 안에서 network await를 하지 않는다. 실제 사용자 POST만 idle을 갱신하며 실패 시 business 변경과 함께 rollback한다.

## 조회 scope·page

모든 GET은 `line_id`, `source_id`, `stream_id`, `asset_id` query를 정확히 하나씩 요구한다. 설치 가능한 대상은 Sealer analog와 기존 논리 Line의 PLC_FAULT다. 단일 instance·actions·receipt 조회에는 `rule_id`도 필수다. list에서만 rule_id를 생략하여 같은 scope의 온도·압력 등 여러 설치 규칙을 함께 조회할 수 있다.

list의 선택적 query는 다음과 같다.

- `view=attention|all`, 기본 attention. attention은 **조건이 아직 열려 있거나 확인되지 않은 occurrence**로서 복귀 미확인을 포함한다.
- `severity`: immutable instance rule의 severity와 정확히 비교하는 Identifier다. 기존 severity가 enum이 아니므로 새로운 enum을 만들지 않는다. 형식이 잘못되면 422, 형식이 맞지만 없는 값이면 빈 page다.
- `limit=1..100`, 기본 50; `before_alarm_id`.
- 선택적 `rule_id`.

actions page는 limit과 before_action_id를 받는다. 알 수 없는 query·중복 query·범위 밖 limit·다른 scope/rule/severity의 cursor는 422다. occurrence는 불변 `(발생 시각,alarm_id)` 내림차순, action은 불변 저장 순서 내림차순으로 정렬한다. `limit+1`개를 읽어 has_more를 정하며 전체 이력을 응답에 한꺼번에 담지 않는다. attention의 ACK/복귀 상태는 바뀔 수 있으므로 page 전체가 과거 한 시각에 고정된 snapshot이라고 주장하지 않는다. 동일 scope의 cursor occurrence가 ACK로 attention에서 사라져도 그 불변 발생 위치를 cursor로 사용할 수 있다.

```text
GET list response {
  items: [{instance: AlarmInstance, status: AlarmStatus}],
  has_more: boolean,
  next_before_alarm_id: Identifier | null,
  coverage: {
    scope: {line_id, source_id, stream_id, asset_id},
    implemented_kinds: ["TEMPERATURE_DEVIATION", "PRESSURE_LOW"],
    installed_channel_count: number,
    installed_channels: [{rule_id, rule_version, kind, severity}],
    has_more_installed_channels: boolean,
    consumer_status: "UNKNOWN",
    runtime_configuration_status: "UNKNOWN"
  }
}
GET exact instance response {instance: AlarmInstance, status: AlarmStatus}
GET actions response {items: [AlarmAction], has_more, next_before_action_id}
```

`AlarmInstance.status`는 원본 DTO에서 property이므로 HTTP가 명시적으로 별도 필드로 보낸다. ACTIVE_UNACKNOWLEDGED / ACTIVE_ACKNOWLEDGED / RETURNED_UNACKNOWLEDGED / RETURNED_ACKNOWLEDGED의 네 상태를 유지한다. 설정 종료는 CONFIG_CHANGED이고 NORMAL_RETURN과 구분된다.

coverage는 선택한 severity·rule·view 필터와 무관하게 **전체 요청 exact scope**의 설치 상태를 설명한다. 설치 채널 목록은 최대 100개이며 전체 개수와 truncation 여부를 함께 준다. 저장된 채널/버전이 있다는 사실은 현재 runtime consumer/config가 활성이라는 증거가 아니므로 두 runtime 상태는 UNKNOWN이다. 빈 items를 정상·all-clear·모든 알람 종류 감시 중으로 해석해서는 안 된다. 모르는 등록 scope는 404이며 등록 scope에 설치 채널이 없다면 installed_channel_count=0이다.

## 조치 intent와 원래 ID 복구

POST JSON은 extra field를 허용하지 않는 discriminated DTO다. 고정 alias는 immutable JSON의 `alarm_id`를 대상으로 하며, legacy POST는 추가로 path와 같은지 검사한다. `at`·확인 시각·규칙 설정·수집 상태를 client가 제공하지 않는다.

```text
공통 {action_id, scope, rule_id, alarm_id, actor, kind}
ACKNOWLEDGE: 공통만
NOTE_ADD: 공통 + {note_id, text}
NOTE_EDIT: 공통 + {note_id, text, expected_previous_action_id}
```

text는 기존 Reason의 1~1,000자·공백만인 값 금지 규칙을 따른다. NOTE_EDIT는 그 note의 정확한 마지막 action ID를 요구한다. 새 action ID로 예전 parent를 수정하면 409이며 수정 전후 text를 모두 저장한다. 다른 occurrence나 scope의 note ID를 재사용하지 않는다.

```text
POST 및 GET receipt response {
  intent: 원래 공통·kind별 intent,
  recorded_at: 원래 서버 UTC 시각,
  receipt: {
    input_id, disposition, created_alarm_id, changed_alarm_id,
    replayed, notify_current
  },
  action: AlarmAction | null
}
```

서버는 최초 저장 시각을 **정렬된 channel/input 잠금 뒤에 한 번만** 고른다. 동일 action ID 재시도는 원래 receipt를 먼저 찾아 actor·scope·rule·occurrence·kind·note·text·parent가 모두 같은지 검사하고, 기존 시각으로 재적용하여 replay 결과를 반환한다. HTTP 재시도 때마다 현재 시각을 새로 생성하여 같은 intent를 충돌시키지 않는다. 단일 input 잠금을 먼저 잡고 channel 잠금을 나중에 잡는 역전 경로도 만들지 않는다.

GET receipt는 원래 intent/time/receipt를 그대로 조회하며 ID·시각을 생성하거나 `act_on_alarm`을 호출하지 않는다. 원래 POST receipt의 replayed는 false이고 실제 재시도 POST는 replayed=true다. 이미 ACK된 occurrence에 다른 action ID로 다시 ACK하면 NO_CHANGE receipt를 남기되 중복 AlarmAction은 만들지 않는다. 이 경우 action=null이어도 원래 action ID로 복구할 수 있다. 처음의 확인자·시각은 바뀌지 않는다.

응답을 잃었거나 불명확하면 client는 **원래 action ID와 intent를 유지한 채** 이 GET으로 복구해야 한다. 404를 성공으로 표시하지 않으며 새 action ID로 자동 재실행하지 않는다. user가 원래 intent 재시도를 선택해도 서버는 live 권한·CSRF를 다시 검사한다. 과거 occurrence를 ACK해도 현재 재발한 다른 alarm_id는 확인되지 않는다. ACK는 PLC 고장 리셋·조건 정상 복귀·재가동 명령이 아니다.

인증 오류는 401, 역할/CSRF/actor 오류는 403, 정확한 대상/receipt 부재는 404, 동일 ID의 다른 내용·stale note parent는 409, query/schema 오류는 422, 저장소/지원 schema 불가는 503이다. DB·validation·세션·CSRF 원문을 응답이나 로그에 출력하지 않는다.

## 로컬 검증

```sh
.venv/bin/ruff check apps/api/alarm_http.py apps/api/alarm_store.py tests/integration/test_alarm_http.py
.venv/bin/ruff format --check apps/api/alarm_http.py apps/api/alarm_store.py tests/integration/test_alarm_http.py
.venv/bin/pytest -q tests/integration/test_alarm_http.py tests/integration/test_alarm_store.py
```

실제 일회용 PostgreSQL과 인증된 ASGI 시험으로 네 상태·복귀 미확인·재발과 과거 ACK 격리·메모 before/after·stale parent·원래 시각의 복구/재시도·동시 같은 ID 재전송과 경쟁 메모 수정·NO_CHANGE receipt·역할과 mutation 보호·passive idle·live 세션 회수·범위와 cursor 격리·여러 규칙/같은 발생 시각 page·severity·coverage·body/filter 상한을 확인한다. 커밋 직전 실패 시 action/receipt가 함께 rollback되는 것과, 사용자 잠금 뒤에서 기다린 HTTP 요청이 먼저 확정된 비활성 상태를 보고 거절되는 것도 검증한다. 아래 실제 브라우저 회귀와 별개로, 실제 수집부터 표시까지의 성능·지연·장시간·복구 수락은 추가 검증이 필요하다.

## Alias·UI 검증 기록

실행/알람 fixed-query alias는 실제PG **34개(실행13+알람21)**, 기존 회귀는 별도 **67개**를 통과했다. 작업지시/취소/workflow **106개**와 제어 HTTP **35개**는 이와 다른 검증 범위다. 알람 alias 시험은 slash·dot·percent·UTF8, 정확한 scope/rule/occurrence/action ID, 원래 시각·intent·replay 의미, 중복/추가 query422·권한·Origin/CSRF·passive idle/무쓰기를 확인한다.

```sh
.venv/bin/pytest -q tests/integration/test_execution_identifier_aliases.py \
  tests/integration/test_alarm_identifier_aliases.py
```

별도 alias 단위의 웹 **61개 순수 시험·typecheck/lint/build** 및 실제 전체 Chromium/일회용PG/API 검사는 새 알람 alias로 네 상태·과거 건 ACK 응답 유실·원래 actor 복구·note add/edit parent/이력·read-only 역할을 통과했다. 같은 실행의 특수ID6-case 브라우저 검사는 MES recipe/작업지시/원래 cancellation·release 기록이며, 특수 알람 ID 자체는 위 영구PG 시험 범위다. 기존 WS/command/MES/two-job/C1 회귀도 유지했다. 로그 `.local/qa/identifier-browser.log`; 특수 MES 이력 화면 `.local/qa/app-a-identifiers-run-identifiers.png`. trusted alarm fixture는 실제 OPC 알람 수집·지연 수락의 증거가 아니다.


## Line/Sealer 화면 집중 단위

알람 대상 선택은 `Sealer`와 기존 논리 `Line`을 정확한 scope로 조회한다. Line의
coverage는 `PLC_FAULT`, Sealer는 온도/압력 종류만 허용하는 strict parser를 사용한다.
수집 실행·설정 상태 UNKNOWN은 유지한다. 발생 당시 `Line.FaultCode`만 원래 evidence에서
표시하며 이후 capture 값으로 바꾸지 않는다. ACK·조치 메모는 고장 해제나 리셋을 수행하지 않는다.

대상 전환은 원래 조치 ledger를 지우지 않는다. 응답 불명·확인·거절 기록이 남아 있으면
새 조치를 막고, 수동 복구는 현재 선택 대상과 관계없이 원래 scope/alarm/rule/action ID의
GET을 사용한다. 늦은 이전 scope 응답은 abort와 scope/path key로 현재 목록·상세와 분리한다.

웹 순수 시험 **65개**, typecheck/lint/build, browser helper Ruff가 통과했다.
실제 `--focus-line-alarms` CLI·일회용 PostgreSQL·Chromium도 exit0으로 통과했다.
실제 UserAuth와 공개 runtime helper로 설치하고 정상 ingestion의 7개 capture를 두 채널에
원자적으로 처리해 14개 decision과 실제 Line/Sealer 발생 기록을 생성했다. 하드코딩한
QualifiedCondition·가짜 Principal·SQL INSERT/UPDATE로 알람을 만들지 않았다.
양방향 목록/상세 late GET, ACK와 note add/edit commit 응답 유실, 다른 선택 대상에서
원래 GET/404 복구, 원래 actor/ID/메모 parent·before/after, viewer/equipment_admin 단독
403, 기계 명령/사건 기록 불변을 확인했다. 첫 실행의 대기 실패는 Playwright callback이
두 번째 positional 인자에 request를 전달하는 harness 서명 오류였고, 수정 후 통과했다.

```sh
npm --prefix apps/web run test:screen
npm --prefix apps/web run lint
npm --prefix apps/web run build
.venv/bin/ruff check scripts/check-app-a-browser.py scripts/line_alarm_browser_checks.py
.venv/bin/python scripts/check-app-a-browser.py --focus-line-alarms \
  --screenshot .local/qa/app-a-line.png > .local/qa/line-alarm-browser.log 2>&1
```

로그는 `.local/qa/line-alarm-browser.log`, 화면은
`.local/qa/app-a-line-line-alarm.png`다. 기존 전체 browser 단위를 이 집중 실행으로
반복했다고 표시하지 않는다. fixture capture와 실제 API/store/browser의 증거이며
실제 OPC/PLC 수집이나 전체 T05 수락 완료는 별도다.
