# 앱 A 웹

React·TypeScript·Vite 기반의 사용자 화면입니다. 로그인·초기 비밀번호 변경, 실제 저장된 48개 태그와 품질 조회, 역할별 운전 명령·원래 ID 결과 추적, 설정 버전·작성 중 작업지시 등록, 원자적 실시간 화면 연결, 명시적 배포·실행 연결 요청과 미연결 작업지시 취소를 구현했습니다. 연결 등록과 PLC 작업 불러오기 결과는 구분합니다. 전체 기능·수락 시험 완료 여부는 [개발 진행 상태](../../docs/DEVELOPMENT_STATUS.md)를 확인합니다.

## 로컬 실행

Node.js 22.12 이상인 22 LTS 또는 Node.js 24 LTS와 npm 10 이상을 사용합니다. 의존성 직접 버전은 `package.json`, 전체 해석 결과는 `package-lock.json`에 고정합니다.

저장소 루트에서:

```sh
cd apps/web
npm ci
npm run dev
```

기본 개발 주소는 `http://127.0.0.1:5173`입니다. 포트가 사용 중이면 Vite가 출력하는 주소를 확인합니다. 이 명령은 프런트엔드 개발 서버만 실행하며 사용자 API 프록시는 구성하지 않습니다. 실제 로그인과 업무 검증에는 아래의 같은 origin 실행 방식을 사용합니다.

## 사용자 API와 함께 실행

먼저 [앱 A 실행 절차](../../docs/architecture/API_RUN.md)에 따라 PostgreSQL schema·기준 정보·사용자 계정을 명시적으로 준비합니다. 저장소 루트에서:

```sh
npm --prefix apps/web run build
.venv/bin/python -m apps.api serve-user \
  --host 127.0.0.1 --port 8002 --origin http://127.0.0.1:8002 \
  --web-dir apps/web/dist
```

`http://127.0.0.1:8002`에서 접속합니다. 쿠키와 API는 같은 origin을 사용하며 `localhost`와 `127.0.0.1`을 바꾸어 쓰지 않습니다. 실행 시 계정 생성·migration은 자동 수행하지 않습니다. 이 실행기는 명시적인 loopback 개발 환경용이며 원격 HTTPS 운영 구성은 별도입니다.

## Vercel의 HTTP 화면 갱신

Vercel 웹/API 배포에서는 빌드 환경에 `VITE_SCREEN_TRANSPORT=polling`을 명시합니다.
예를 들어 `VITE_SCREEN_TRANSPORT=polling npm --prefix apps/web run build`로 빌드합니다.
값이 없거나 정확히 `polling`이 아니면 기존 `/ws/v1/screen` WebSocket을 사용합니다.
이 선택은 빌드 시 적용되며, NeonDB 연결 비밀이나 사용자 인증 정보를 웹 환경 변수에 넣지 않습니다.

Polling은 같은 origin의 `GET /api/v1/screen?line_id=...&source_id=...&stream_id=...`을
쿠키·`no-store`로 조회합니다. 한 요청이 끝난 뒤 1초 후 다음 요청을 시작하며, 진행 중인
요청을 겹쳐 보내지 않습니다. 응답은 최대 131072바이트의 UTF-8이며 매번 새로운 sync ID와
revision0인 atomic SNAPSHOT 전체를 기존 화면 계약으로 검증합니다. scope 변경·중단 후
늦은 응답은 버리고, 세션401/403은 다시 로그인하는 경로로 보냅니다. 요청 timeout은2.5초,
복구 간격은1/2/4/8/15초로 제한하며 계속 실패하면 수동 다시 연결을 기다립니다.

Polling 연결 성공은 새 capture나 heartbeat 정상의 증거가 아닙니다. 서버가 반환한 원래
health·evidence·취득 시각을 그대로 사용하고 기존 품질 aging/watchdog를 유지합니다.
HTTP 화면 갱신은 지속 WebSocket 연결을 제공한다는 뜻이 아니며, 이 빌드 설정만으로
Vercel/NeonDB 배포·수집기 연결·전체 운영 검증이 완료되지는 않습니다.

## 분석 화면 (대시보드·트렌드·보고서·SPC)

`src/analytics/`의 네 화면은 [작업지시서](../../docs/work-orders/CODEX_KULS_보강_v1.md)의 CX-01~CX-05 서버 API 계약을 사용합니다. 서버 API가 통합·검증되기 전까지 일반 빌드에서는 메뉴가 "예정"으로 비활성입니다. 화면만 확인할 때는 빌드 시 `VITE_ANALYTICS_PAGES=enabled`를 지정합니다.

```sh
VITE_ANALYTICS_PAGES=enabled npm run build
```

`tests/fixtures/analytics/*.json`은 계약 형식의 고정 데이터입니다. 화면 검토와 응답 검증 시험에만 사용하며, 실제 API 결합 검증을 대신하지 않습니다.

- 차트는 외부 라이브러리 없이 SVG로 그립니다(CSP `style-src 'self'` 유지). 단위가 다른 태그는 한 축에 겹치지 않고 단위별 차트로 나눕니다.
- 값이 없는 구간은 선을 잇지 않고 빗금 "데이터 없음"으로 표시합니다. 불명 시간은 가동·정지에 합치지 않고 별도 빗금 색으로 보여줍니다.
- 모든 차트는 표 보기를 함께 제공하고, 추세 차트는 좌우 화살표로 시점을 이동할 수 있습니다.
- 계열 색은 색각 이상 구분 검증을 통과한 고정 순서(파랑·주황·청록·보라)를 쓰고, 태그 선택 순서를 따르므로 다른 태그를 빼도 색이 바뀌지 않습니다.

## 로컬 검증

```sh
npm run test:screen
npm run typecheck
npm run lint
npm run build
```

`test:screen`은 기존 TypeScript 컴파일러와 Node 기본 시험 기능으로 분석 API 응답 계약(누락 값의 0 변환 거부, UInt64 문자열, 관리한계·규격 조건), 메시지 형식·동기화 순번·품질 근거·UInt64 정밀도 및 재연결 판단, 배포·실행 연결의 원래 의도와 영수증, 알람 네 상태·조치 이력·수동 복구, C1 취소의 원래 의도·거절 근거와 명시적 재준비를 검증합니다. 시험 파일들은 서로 다른 임시 디렉터리에서 컴파일합니다. 실제 socket·브라우저 검증은 아래 별도 절차로 수행합니다.

빌드 산출물은 `dist/`에 생성됩니다. `npm run preview`는 로컬 빌드 확인용 서버이며 배포 서버가 아닙니다.

실제 브라우저 검증은 저장소 루트의 `.venv/bin/python scripts/check-app-a-browser.py`를 사용합니다. 로컬 Docker·고정 PostgreSQL 이미지와 Chromium이 필요합니다. 일회용 DB와 실제 사용자 API에서 인증·관측 품질·원래 명령 ID 복구·MES 등록을 검증합니다. 이 시험의 수집 fixture를 실제 OPC 운전 시험으로 계산하지 않습니다.

프록시 환경에서는 제공된 프록시와 CA 신뢰 설정을 유지하고 TLS 인증서 검증을 끄지 않습니다. 기본 npm 캐시 디렉터리에 쓰기 권한이 없다면 `npm ci --cache /tmp/hmi-mes-npm-cache`처럼 쓰기 가능한 캐시를 지정합니다.

## 현재 HTTP 경로와 식별자

앱 A는 path에 업무 ID를 넣는 대신 아래 고정 query-identity 경로를 일관되게 사용합니다. 기존 path API·Identifier·DB 자료는 보존합니다. `URLSearchParams`로 값을 한 번 인코딩하므로 slash, 단독 `.`/`..`, percent literal, 정상 UTF8이 path 정규화나 추가 decoding으로 바뀌지 않습니다.404 후 경로 추측/fallback·자동 POST·새 ID 생성은 하지 않습니다.

| 현재 경로 | 필수 query / 본문 |
|---|---|
| `POST /api/v1/recipe-versions`, `POST /api/v1/work-orders` | 기존 등록 JSON |
| `GET /api/v1/recipe-versions`, `GET /api/v1/work-orders` | 기존 bounded 목록 필터 |
| `GET /api/v1/recipe-version-detail` | `recipe_version_id` |
| `GET /api/v1/work-order-detail` | `work_order_id` |
| `POST /api/v1/work-order-actions/release` | `work_order_id` query + 기존 release JSON |
| `POST /api/v1/work-order-actions/bind` | `work_order_id` query + 기존 bind JSON |
| `POST /api/v1/work-order-actions/cancel` | `work_order_id` query + 기존 cancellation JSON |
| `GET /api/v1/work-order-receipts/release` | `work_order_id`, `release_id` |
| `GET /api/v1/work-order-receipts/binding` | `work_order_id`, `command_id` |
| `GET /api/v1/work-order-receipts/cancellation` | `work_order_id`, `cancellation_id` |
| `GET /api/v1/command-record` | `command_id`, `line_id`, `source_id`, `stream_id` |
| `POST /api/v1/commands` | 기존 scoped CommandRequest JSON |
| `GET /api/v1/execution-record` | `execution_id`, `line_id`, `source_id`, `stream_id` |
| `GET /api/v1/alarms` | `line_id`, `source_id`, `stream_id`, `asset_id`; 기존 선택 필터 |
| `GET /api/v1/alarm-instance` | 위 알람 scope + `rule_id`, `alarm_id` |
| `GET /api/v1/alarm-actions` | 위 알람 scope + `rule_id`, `alarm_id`; 선택적 `limit`, `before_action_id` |
| `GET /api/v1/alarm-action-receipts` | 위 알람 scope + `rule_id`, `alarm_id`, `action_id` |
| `POST /api/v1/alarm-actions` | query 없이 원래 scope·rule·alarm·actor가 담긴 immutable intent JSON |

새 alias는 각 필수 query를 정확히 한 번 요구하고 누락·중복·추가·잘못된 Identifier를422로 거부합니다. 목록과 page는 명시된 기존 선택 필터만 받습니다. cookie·역할·scope·Origin/CSRF·같은 transaction의 검증은 그대로이며, 원래 receipt/command/execution/alarm GET은 passive 조회로 idle을 연장하지 않습니다. 작업지시/설정 detail의 기존 idle 정책은 바꾸지 않습니다. 상세한 legacy 경로와 정확한 계약은 [작업지시](../../docs/architecture/WORK_ORDERS.md), [취소](../../docs/architecture/WORK_ORDER_CANCEL.md), [명령](../../docs/architecture/CONTROL_HTTP.md), [실행](../../docs/architecture/EXECUTION_READ.md), [알람](../../docs/architecture/ALARM_HTTP.md)을 따릅니다.

## 배포·실행 연결의 범위

생산 담당자는 작업지시 목록에서 DRAFT를 명시적으로 배포합니다. 작업자와 생산 담당자는 RELEASED 작업지시를 선택하고 설비 운영 화면에서 현재 등록된 라인·수집원의 최신 정상 증거를 확인한 뒤 실행 연결을 요청합니다. 서버가 만든 실행과 원래 LOAD_JOB 명령을 조회하며, 등록 응답만으로 PLC 접수·실행 완료를 표시하지 않습니다.

원래 요청 ID·사용자·요청 내용은 App 메모리에 보관하여 화면 전환·로그아웃·재로그인 후에도 유지합니다. 다른 계정은 허용된 기록을 조회할 수 있지만 원래 사용자의 요청을 재전송하지 않습니다. 결과 불명·404는 새 요청 허가가 아니며 자동 재전송하지 않습니다. 새로고침으로 메모리가 사라지면 작업지시 ID와 원래 배포/명령 ID를 입력해 수동 조회합니다. 쿠키 외 인증 정보와 업무 요청을 브라우저 저장소에 기록하지 않습니다.

연결된 실행을 임의로 초기화하거나 취소하는 버튼은 없습니다. 원래 계정은 실행을 수동 조회하고, 명시적인 보관 클릭에서 새 GET으로 원래 연결·성공한 LOAD·최종 SETTLED 요약·현재 저장 집계·수집원 제한과 사건 구간을 다시 검증합니다. 모두 일치하면 전체 원래 ledger와 응답을 현재 세션 메모리 이력에 남기고 다음 작업 준비를 허용합니다. 이력은 새로고침 영속 보존이 아니며, 보관은 DB 예약 해제나 다음 운전 허가를 뜻하지 않습니다. 다음 bind는 최신 WS 증거와 서버의 이전 실행 정산을 다시 검증합니다. 결과 불명·거부·만료된 LOAD는 임의로 보관·재연결하지 않습니다. PAUSE·END_RUN은 별도 권한·현재 관측·서버 조건을 그대로 검증합니다.

현재 선택한 등록 라인·수집원에 알람 기록과 조치 이력을 연결합니다. 알람의 네 상태와 현재 조건/확인은 구분하고, 확인·메모는 원래 발생 건에만 적용합니다. 빈 목록이나 설치된 규칙만으로 전체 설비 정상·실시간 소비 정상이라고 표시하지 않습니다. 알람 조치의 원래 ID·사용자는 App 메모리에 보존하며 응답 불명 복구는 수동 조회만 수행합니다.

## 미연결 작업지시 취소와 명시적 재준비

생산 담당자(`production_manager`)는 DRAFT/RELEASED 작업지시를 선택하고 취소 내용을 명시적으로 확인합니다. **설비에 연결되지 않은 작업만 취소할 수 있습니다.** RELEASED 상태만으로 미연결을 추정하지 않으며, 현재 화면에서 확인된 실행 연결은 취소 검토를 막습니다. 화면에서 연결을 모르는 경우에도 서버가 연결 여부·원래 버전을 다시 검사하고 bound 작업은409로 거절합니다. C1 취소는 업무 메타데이터 변경이며 WS 정상·PLC 정지 조건을 요구하거나 설비 정지·END_RUN·LOAD 취소·예약 해제를 수행하지 않습니다. CANCELLED 작업은 배포·실행 연결 대상으로 선택하지 않습니다.

취소의 원래 ID·actor·버전·영수증은 배포/실행 연결·일반 명령과 독립된 App 메모리에 보존합니다. 화면 전환·로그아웃 뒤에도 원래 기록이 남고, 다른 계정은 권한이 허용하는 원래 기록만 수동 GET으로 조회합니다. 응답 유실·abort·timeout·GET404는 미처리 증거가 아니며 새 요청이나 일반 해제를 허용하지 않습니다. 같은 작업지시의 다른 workflow lease와 원래 LOAD/command tracker도 유지합니다.

단 한 번 보낸 원래 취소 POST에서 완전한409 응답을 확정한 경우에만 **거절 기록 보관 후 작업 다시 확인** 절차를 제공합니다. C1 전용 transport가 JSON 본문을 끝까지 읽고 정확한 정적409 envelope를 검증하여 typed rejection으로 구분합니다. 본문 읽기 실패·다른 envelope·GET409는 이 근거가 아닙니다. 정적 detail은 응답 계약 검증에만 사용하며 stale/bound 등 거절 원인을 추론하지 않습니다. POST409 근거와 이후 GET404 등 조회 관측은 분리하여 보관하므로 화면 상태가 UNKNOWN으로 바뀌어도 원래 거절 근거를 지우지 않습니다.

이 절차는 원래 actor인 생산 담당자의 **원래 로그인 세대**에서만 가능합니다. 로그아웃·재로그인 후에는 같은 계정이어도 재준비 권한을 승계하지 않습니다. 명시적 클릭에서 최신 작업 상세를 GET으로 확인하고, 원래 ledger·관측 이력·최신 작업 응답을 불변 이력에 보관한 뒤 해당 취소 lease만 해제합니다. 별도 취소 확인 화면에서 사용자가 다시 확인할 때 새 ID와 최신 작업 버전으로 한 번 요청합니다. 보관·GET 자체는 POST나 ID 생성을 하지 않으며 bound409 보관도 취소 자격을 만들지 않습니다. 다음 요청은 서버가 다시 판정합니다. 기존 성공 영수증·불명 원래 요청·다른 actor·불일치 복구 기록에는 이 경로를 제공하지 않습니다. workflow release/bind나 일반 command의 재준비는 포함하지 않습니다.

확인된 취소 이력과 거절 이력은 현재 페이지의 메모리에만 있으며 새로고침 영속 보존이 아닙니다. 브라우저 저장소에 인증 정보나 업무 ledger를 저장하지 않습니다. 자세한 API·원자성 경계는 [WORK_ORDER_CANCEL.md](../../docs/architecture/WORK_ORDER_CANCEL.md)를 따릅니다.

### 검증 기록

현재 웹 **65개 순수 시험·typecheck/lint/build**와 helper Ruff가 통과했습니다. backend는 작업지시/취소/workflow의 Identifier alias·기존 회귀 **106개**, 별도 제어 HTTP **35개**가 통과했습니다. 실행/알람의 새 alias **34개(실행13+알람21)**와 기존 회귀 **67개**는 별도 실제PG/ASGI 검증 범위입니다. 서로 다른 실행을 하나의 전체 수락 시험 결과로 합치지 않습니다.

실제 전체 Chromium·일회용 PostgreSQL·사용자 API 검사는 기존 auth/WS/command/MES/two-job/alarm/C1 회귀를 새 alias로 통과했습니다. 추가6-case(ASCII, slash, `.`, `..`, percent literal, UTF8)는 실제 UI recipe/작업지시 commit 응답 유실 후 원래 GET 복구, 특수 작업지시의 UI 취소, 실제 API로 저장한 특수 original cancellation/release ID의 UI수동GET 복구와 추가 POST 부재를 확인했습니다. 특수 command/LOAD·execution·alarm ID 자체는 영구PG/순수URL 시험 범위이며, 실제 브라우저는 기존 원래 command/LOAD 추적·실행조회/보관·알람조치 회귀를 수행했습니다.

```sh
npm --prefix apps/web run test:screen
npm --prefix apps/web run typecheck
npm --prefix apps/web run lint
npm --prefix apps/web run build
.venv/bin/ruff check scripts/check-app-a-browser.py scripts/cancellation_browser_checks.py \
  scripts/identifier_browser_checks.py scripts/browser_identity_urls.py
.venv/bin/python scripts/check-app-a-browser.py \
  --screenshot .local/qa/app-a-identifiers-run.png \
  > .local/qa/identifier-browser.log 2>&1
```

최신 실제 전체 검사 로그는 `.local/qa/identifier-browser.log`,6-case의 원래 ID 취소 이력 화면은 `.local/qa/app-a-identifiers-run-identifiers.png`입니다. 이전 C1 재준비 증거 `.local/qa/c1-reprepare-browser.log`, `.local/qa/app-a-reprepare-run-reprepare.png`와 최초 C1 증거 `.local/qa/c1-browser.log`, `.local/qa/app-a-cancellation.png`도 보존했습니다. 실제 브라우저 후 launcher에는 동작을 바꾸지 않는 Ruff format만 적용했으며 새 브라우저 시험을 반복한 것으로 표시하지 않습니다. 이 검증은 fixture 수집·실제 API·journal·DB 기록을 대상으로 하며 실제 OPC/PLC 운전·설비 취소나 전체 수락 시험 완료를 뜻하지 않습니다.


### Line/Sealer 알람 대상 선택

“알람 대상”에서 실링기 온도/압력과 논리 라인 PLC 고장을 명시 선택합니다. Line에서는
발생 당시 코드를 원래 근거에서 표시합니다. 알람 확인과 조치 메모는 고장 해제나
리셋을 수행하지 않습니다. 대상 전환으로 원래 조치 기록을 지우지 않으며, 응답 유실 후
다른 대상을 보고 있어도 원래 범위·ID의 기록만 GET으로 복구합니다. coverage UNKNOWN과
빈 목록의 의미도 유지합니다.

65개 순수 시험과 typecheck/lint/build, helper Ruff가 통과했습니다. 별도 실제 CLI·PG·
Chromium 집중 검사는 다음 명령으로 실행합니다. 기존 인증/서버/일회용 PG/로그 정리
경계를 사용하며 전체 monitor/MES browser 단위를 중복 실행하지 않습니다.

```sh
.venv/bin/python scripts/check-app-a-browser.py --focus-line-alarms \
  --screenshot .local/qa/app-a-line.png > .local/qa/line-alarm-browser.log 2>&1
```

집중 실행은 exit0으로 통과했습니다. 실제 UserAuth/ingestion으로 만든 두 채널의 발생,
대상 전환 중 늦은 GET 응답 격리, ACK·메모 응답 유실 후 원래 GET 복구·추가 POST 부재,
viewer/장비관리자 단독 조치403을 확인했습니다. 로그는 `.local/qa/line-alarm-browser.log`,
화면은 `.local/qa/app-a-line-line-alarm.png`입니다. 이 fixture/API/store/browser 검증은
실제 OPC 수집·자동 리셋·전체 T05 완료의 증거가 아닙니다. 상세 범위는
[알람 HTTP](../../docs/architecture/ALARM_HTTP.md)의 Line/Sealer 집중 단위를 따릅니다.
