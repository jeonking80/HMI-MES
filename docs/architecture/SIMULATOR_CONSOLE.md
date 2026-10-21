# 별도 시뮬레이터 콘솔 HTTP 경계

상태: **앱 B의 실제 HTTP 인증·조작 adapter와 ASGI 통합 시험**. `apps/simulator/console.py`의 `create_app(controller, auth, config)`가 기존 controller·인증 모듈을 연결한다. 운영 앱 A 사용자/API·gateway 서비스·실제 OPC 경로의 인증 완료를 뜻하지 않는다. 기준은 [인증 설계](DATA_AUTH.md), [내부 인증 primitive](SIMULATOR_AUTH.md), DEV-08과 최초 제어 경로 인증 조건이다.

## 명시적 구성과 소유권

`ConsoleConfig`는 정확한 `origin`, 선택적 loopback 개발 허용, 로그인·challenge 속도 제한값, UTC clock을 받는다. 기본 설정은 HTTPS이며 cookie는 host-only `HttpOnly; Secure; SameSite=Strict; Path=/`다. `allow_insecure_loopback=True`와 `http://localhost`, `127.0.0.1`, `[::1]`의 명시적 출처를 함께 지정한 개발 구성에만 Secure를 끌 수 있다. wildcard CORS는 없으며 다른 Host·scheme·출처 별칭도 거절한다. 운영 TLS 종료·proxy의 신뢰 설정은 실행 구성에서 별도로 정한다.

일반 app 생성·시작으로 사용자를 만들지 않는다. 호출자가 simulator canonical storage, controller, 인증 schema·계정을 명시적으로 준비해야 한다. `create_app()`은 같은 연결·허용 Origin인지 확인하고 자신의 작은 `console_schema_version`/`console_request_audit`만 추가한다. 서버 시작·수명 관리·주기 tick은 호출자의 책임이며 이 adapter에는 CLI나 자동 시작 코드가 없다.

모든 handler는 `async def`로 작성하고 SQLite 작업을 소유 event-loop thread에서 동기 실행한다. 소유 thread가 달라지면 503이다. body 수신·응답 전송의 `await`와 달리 트랜잭션 안에는 `await`가 없다. 인증 갱신·권한 검사·controller의 중첩 변경·명령 접수·조작자 감사는 하나의 `SimulatorStorage.transaction()`에서 commit된다. DB·메모리·수량·이력을 따로 저장하는 threadpool handler는 사용하지 않는다. 상위 tick task도 같은 소유 event loop에서 실행해야 한다.

## 인증·읽기 경로

| 경로 | 입력·동작 |
|---|---|
| `GET /`, `GET /static/*` | 시험 환경 콘솔 문서·동일 출처 정적 파일. API 데이터는 인증 필요 |
| `POST /auth/challenge` | 정확한 Origin + `X-Simulator-Request: 1`; 5분 pre-auth cookie와 CSRF JSON 발급 |
| `POST /auth/login` | `{login,password}`, pre-auth cookie, `X-CSRF-Token`; 로그인 후 session cookie와 새 CSRF 발급 |
| `GET /auth/me` | 현재 `{login,role,must_change_password}` |
| `POST /auth/password` | `{new_password}`, 세션 CSRF·Origin; 자신의 암호 변경과 기존 세션 전체 취소·새 세션 발급 |
| `POST /auth/logout` | 세션 CSRF·Origin; 현재 세션 취소·cookie 삭제 |
| `GET /snapshot` | 명시적 시험 환경 표시, PLC state·48개 tag·물품 위치/수량·미정 원래 명령 ID |
| `GET /commands/{command_id}` | 원래 ID의 접수·실행 결과. 자동 재요청 없음 |
| `GET /runs/{execution_id}` | 영속 실행 요약·최종 여부·원래 실행별 수량 |

로그인·암호 변경 응답은 login·role·초기 암호 변경 필요 여부·CSRF·절대 만료만 반환한다. 세션·pre-auth bearer token은 JSON에 넣지 않는다. 별도 `simulator_session`/`simulator_preauth` HttpOnly cookie만 사용하며 Authorization bearer·URL token을 인정하는 구현은 없다. 서버 세션·CSRF digest·idle/절대 만료·계정 회수는 내부 인증 모듈이 검사한다. 최초 암호를 변경하기 전에도 읽기·자기 암호 변경·로그아웃은 가능하지만 설비 조작은 거절된다.

처음 pre-auth를 만드는 요청에는 아직 session-bound CSRF가 없다. 따라서 정확한 Origin과 CORS safelist 밖의 명시적 header를 함께 검사한다. CORS를 열지 않으므로 다른 출처의 script가 이 header를 넣은 credential 요청을 통과시킬 수 없다. 이후 로그인과 모든 인증된 상태 변경은 해당 pre-auth/사용자 세션에 결합된 `X-CSRF-Token`까지 필수다. 로그인 challenge 발급도 POST이며 일반 GET으로 업무 상태를 바꾸지 않는다. 읽기 인증은 세션 idle 시각만 갱신한다.

UInt64는 JSON 경계에서 정규 십진 문자열이다. 명령은 `CommandRequest.model_validate_json()`으로 검사하여 숫자형 `expected_control_version`을 거절한다. PLC state·결과·실행 요약의 계약 serializer와 tag의 11개 UInt64 채널, 물품 ID·생산 누계·tick에 동일한 문자열 표현을 적용한다. UInt32·Bool·일반 측정값은 계약 자료형을 유지한다.

## 조작 경로와 감사

모든 아래 경로는 현재 `simulator_operator`, 변경된 초기 암호, 정확한 Origin, 해당 세션 CSRF가 필수다. `viewer`의 직접 POST도 403이다. 계정 역할·관리자 권한으로 PLC 안전 조건을 우회하는 경로는 없다.

| 경로 | 검증·행위 |
|---|---|
| `POST /commands` | 전체 `CommandRequest`. `requested_by`가 인증한 login과 정확히 같아야 한다. 서버가 authority=`LOCAL`로 고정하며 PLC의 LOCAL·모드·실행·버전·안전 조건을 적용 |
| `POST /inputs` | `{inputs: PLCInputs, control_mode?, operation_mode?}`. inputs는 부분 patch가 아닌 전체 입력 경계이며 생략한 PLCInputs 항목은 해당 모델 기본값 사용 |
| `POST /recover` | RecoveryChecks의 다섯 boolean을 명시. controller의 정지·위치·DB·원래 명령 대조와 실제 조작자 ID 보관 |
| `POST /reconcile/{command_id}` | 같은 명시적 RecoveryChecks로 원래 미정 명령을 대조. timeout만으로 실패를 만들지 않음 |
| `POST /scrap` | `{item_ids:["..."],reason,positions_verified:true}`. 명시한 실제 WIP만 정지 위치 확인 후 폐기하고 원래 실행 사건·수량 보존 |
| `POST /runs/{execution_id}/settle` | `{operator_confirmed:true}`. 현재 LOCAL 최종 실행·WIP 0·영속 대조를 확인해 다음 작업 허용. 앱 A MES 정산 완료를 의미하지 않음 |

접수 성공과 물리 실행 성공을 구별한다. `/commands`는 controller의 `submission`, 기존/현재 `result`, 새 접수 여부 `created`를 반환한다. 동일 ID·동일 내용은 저장된 추적이며 별도 tick에서 새로 실행하지 않는다. 다른 내용·권한의 ID 재사용은 허용하지 않는다. 원격 모드에서 콘솔 LOCAL 명령은 PLC가 거절한다. 응답 유실 뒤에는 같은 ID 결과를 조회하며 새 ID로 자동 재시도하지 않는다.

조작자 ID는 인증 결과에서만 얻는다. 입력·복구·명령·폐기·로컬 정산의 안전한 대상·근거와 actor를 `console_request_audit`에 같은 트랜잭션으로 기록한다. 예를 들어 감사 INSERT가 실패하면 입력 변경·상태 사건도 rollback된다. 비밀번호·token·cookie·로그인 body는 감사에 넣지 않는다.

인증·권한·속도 거절은 요청의 변경 트랜잭션 rollback 뒤 별도 트랜잭션으로 `REQUEST_REJECTED`를 저장한다. 검증되지 않은 actor는 `anonymous`, 세부는 정적인 route 이름·HTTP 상태만 기록한다. 따라서 실패 로그인도 취소되지 않는 안전한 감사가 남는다. 비밀번호를 포함할 수 있는 framework 기본 validation 입력 echo도 일반적인 422 메시지로 교체한다. 오류 본문에 내부 SQL·credential을 넣지 않는다.

## 제한·검증 범위

프로세스 내부의 유한 window limiter가 실제 peer IP별 로그인·challenge 발급과 정규화 계정별 로그인을 제한한다. 기본은 로그인 5회·challenge 30회/60초, 메모리 key 최대 2048개다. IP는 forwarded header로 대체하지 않는다. 만료 key만 정리하고 용량이 찼으면 새 key를 429로 거절하므로 활성 key eviction으로 제한을 우회하지 않는다. 모든 로그인 시도를 세며 계정은 digest key로 보관한다. 단일 프로세스 재시작 시 제한 기록은 초기화된다. reverse proxy 뒤 실제 peer/공유 IP와 배포 수준 제한은 운영 구성에서 결정한다.

요청 body는 16KiB로 제한한다. 응답은 `Cache-Control: no-store`, `nosniff`, 같은 출처 script/style만 허용하는 CSP를 사용한다. 정적 파일은 framework `StaticFiles`가 경로를 처리한다. 정적 페이지 자체는 공개이며 설비 정보·조작 API는 인증을 거친다. CSRF는 브라우저 메모리에 두며 새로고침 뒤 필요한 재로그인으로 새 CSRF를 얻는다.

로컬 검증:

```sh
.venv/bin/pytest -q tests/integration/test_simulator_console.py
.venv/bin/ruff check apps/simulator/console.py tests/integration/test_simulator_console.py
```

시험은 HTTPX `AsyncClient`/ASGITransport와 실제 canonical simulator SQLite를 같은 event loop에서 사용한다. 401/403/429, cookie 속성·token 미노출·회전·회수·만료, CSRF/Origin, viewer 직접 조작 거절, 계정/IP/challenge 제한, 실패 감사 독립 commit, 감사 실패 시 설비 rollback, LOCAL 명령과 원래 ID 결과, 복구·명시적 폐기·로컬 운전 흐름을 확인한다. 실제 네트워크 TLS·브라우저 자동화·8시간 성능·OPC·앱 A 통합·백업 복원은 이 시험의 범위가 아니며 T12 전체 완료로 확대하지 않는다.

## 식별자 전체 범위의 URL 전달

기존 segment 경로는 호환을 위해 유지한다. 콘솔은 Identifier가 허용하는 slash·dot·percent literal·Unicode를 그대로 전달하기 위해 다음 고정 경로와 `URLSearchParams`를 사용한다. 추가 decoding이나 ID 정규화를 하지 않는다.

| 새 경로 | 정확한 query | 기존 처리 |
|---|---|---|
| `GET /command-record` | `command_id` 한 번 | 원래 명령 결과 조회 |
| `GET /run-summary` | `execution_id` 한 번 | 원래 실행 요약 조회 |
| `POST /run-settlement` | `execution_id` 한 번 | 기존 `{operator_confirmed:true}`와 LOCAL 정산 검사 |
| `POST /command-reconciliation` | `command_id` 한 번 | 기존 RecoveryChecks와 중단 명령 대조 |

필수 누락·중복·추가 query·잘못된 Identifier는 422다. 기존 인증·초기 암호·역할·CSRF/Origin·SQLite 트랜잭션·감사·명시적 확인은 같은 handler를 사용한다. 새 경로가 명령 재전송이나 새 ID를 만들지 않는다.

이 변경은 기존 콘솔 결합 **59개 통과(24.41초)**와 별도 실제 SQLite close/reopen 후 slash/dot 원래 명령 대조 **2개 통과(2.28초)**로 검증했다. 실제 CLI Chromium에서도 `browser/A` 실행의 LOAD→생산→PAUSE→END→로컬 정산→작업 B 전환과 원래 요약 보존을 확인했다. 이 실행에는 기존 온도 편차·필드 READ_FAILURE/LINK_DOWN·응답 유실 후 GET 복구 회귀도 포함했다. 모든 특수 ID를 브라우저로 각각 실행한 결과는 아니며, 6-case 정확 ID 왕복은 HTTP/SQLite 시험이다.
