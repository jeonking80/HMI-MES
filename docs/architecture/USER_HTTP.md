# 앱 A 브라우저 인증 HTTP 경계

`apps/api/user_http.py`는 PostgreSQL `UserAuth`를 사용하는 독립 FastAPI 앱을 구성한다. 기본 계정 생성·migration·프로세스 실행기·업무 명령·웹 화면·WebSocket은 이 어댑터 범위에 포함하지 않는다. gateway bearer 수집과 별도이며 앱 B 계정·세션을 인정하지 않는다.

## 명시적 구성

```python
config = UserHTTPConfig(origin="https://hmi.example.test")
app = create_user_app(auth, config)
```

위 URL은 구성 형식을 보이는 예시다. 실제 운영 origin을 명시하고 같은 origin을 `UserAuth.allowed_origins`에 등록해야 한다. 일반 구성은 HTTPS만 허용한다. 로컬 개발에서만 `origin="http://127.0.0.1:8000"`, `allow_insecure_loopback=True`처럼 loopback 개발을 명시할 수 있다. origin에는 경로·사용자정보·query·fragment·wildcard가 들어갈 수 없다.

요청 scheme과 단일 Host가 구성 origin과 정확히 같아야 한다. Host 별칭·암시적 HTTPS 포트와 명시적 `:443`의 차이도 허용하지 않는다. 어댑터는 `X-Forwarded-Host/Proto/For`를 신뢰하지 않는다. 실제 reverse proxy의 trusted proxy/TLS 설정은 실행기 통합에서 별도로 확정해야 한다.

## 인증 경로

| 경로 | 요청 | 성공 결과 |
|---|---|---|
| `POST /auth/challenge` | 정확한 `Origin`, `X-HMI-Request: 1` | CSRF token JSON 및 5분 pre-auth cookie |
| `POST /auth/login` | JSON `login,password`, pre-auth cookie, `Origin`, `X-CSRF-Token` | 사용자·역할·초기 암호 상태·새 CSRF·절대 만료 시각 및 세션 cookie. pre-auth cookie 삭제 |
| `GET /auth/me` | 세션 cookie | 현재 사용자·역할·초기 암호 상태. live session 검증 및 idle 갱신 |
| `POST /auth/password` | JSON `new_password`, 세션 cookie, `Origin`, `X-CSRF-Token` | 모든 기존 세션 취소 후 새 세션 cookie·CSRF·만료 시각 |
| `POST /auth/logout` | 세션 cookie, `Origin`, `X-CSRF-Token` | 현재 세션 취소 및 양쪽 cookie 삭제 |

사용자 JSON 필드는 `login`, 정렬된 `roles`, `must_change_password`다. 로그인/암호 변경 결과는 여기에 `csrf_token`, `expires_at`를 추가한다. 세션 token이나 challenge token을 JSON에 반환하지 않는다. CSRF token은 브라우저 메모리에서 헤더로 전송한다. `/auth/me`는 CSRF를 복원하지 않으므로 메모리의 CSRF를 잃은 클라이언트는 다시 로그인해 새로운 값을 받아야 한다. token·CSRF·암호는 URL·localStorage·로그에 기록하지 않는다.

세션 cookie 이름은 `hmi_session`, 로그인 전 cookie는 `hmi_preauth`다. 둘 다 `HttpOnly; Secure; SameSite=Strict; Path=/`, Domain 없는 host-only cookie다. Secure는 명시적인 HTTP loopback 개발에서만 빠진다. 암호 변경 때 token과 CSRF를 모두 회전한다. cookie의 8시간 수명보다 DB의 idle 30분·절대 8시간·회수 검사가 우선한다.

초기 암호 상태에서 `/auth/me`, 자기 암호 변경, 로그아웃은 가능하다. 향후 업무 route는 반드시 `UserAuth.authorized_transaction(permission=...)`을 통해 초기 암호 변경과 역할·CSRF·Origin 검사를 하고 yield된 같은 연결에서 업무 쓰기를 수행해야 한다. `/auth/me` 결과만으로 업무를 승인하지 않는다.

## 요청·오류·시도 제한

모든 요청 body는 기본 16,384바이트까지 stream으로 읽는다. Content-Length를 신뢰하지 않으며 초과 시 413이다. 로그인·암호 변경은 `application/json`만 받고 추가 필드를 거절한다. validation 오류에서 입력·암호가 노출될 수 있는 기본 상세 응답을 사용하지 않는다. 인증 실패 401, Origin/CSRF/권한 실패 403, 잘못된 JSON·암호 정책 422, 잘못된 미디어 타입 415, DB 장애 503은 고정 메시지다. 응답에는 `Cache-Control: no-store`, `nosniff`, 외부 자원·frame을 차단하는 CSP를 적용한다.

기본 시도 제한은 60초 창에서 challenge는 peer IP당 30회, 로그인은 peer IP당 5회와 정규화 계정당 5회다. 실패·성공 모두 시도로 계산한다. 계정 키는 정규화 로그인 SHA-256 digest이고 IP는 ASGI peer만 사용한다. 서로 다른 IP의 대소문자 변형도 같은 계정 제한을 공유한다. 최대 2,048개의 창 키를 보관하며 용량 초과는 fail-closed 429다. 429에는 `Retry-After`를 반환하고 시간이 지난 창을 회수한다.

제한은 thread-safe한 **단일 프로세스 메모리**다. 프로세스 재시작으로 초기화되며 여러 worker 간 공유하지 않는다. proxy 뒤의 단일 peer 제한과 다중 worker 운영 정책은 실행기 배포 때 확정해야 한다. DB·scrypt 호출은 threadpool에서 수행해 ASGI event loop를 점유하지 않는다. endpoint에서 요청 credential이나 예외 내용을 로그에 추가하지 않는다. 성공 인증 감사는 `UserAuth`가 저장한다. HTTP 거절 감사의 별도 저장·보관 정책은 후속 작업이다.

## 검증·남은 작업

`tests/integration/test_user_http.py`는 실제 일회용 PostgreSQL과 ASGI client를 연결한다. cookie 속성·secret JSON 미노출·초기 암호 회전·로그아웃·실제 역할 회수·CSRF/Origin·Host/scheme·challenge/idle/절대 만료·잘못된 입력·stream body 상한·IP/계정/용량 throttling 및 시간 창 회수를 검증한다.

```sh
.venv/bin/ruff check apps/api/user_http.py tests/integration/test_user_http.py
.venv/bin/pytest -q tests/integration/test_user_http.py
```

실제 서버 실행기와 명시적 계정 생성은 [실행 문서](API_RUN.md)에 연결했다. 명령 route는 [제어 HTTP](CONTROL_HTTP.md), WS handshake·주기 재검증·회수 종료는 [화면 WebSocket](SCREEN_WS.md)에 구현·검증 범위를 기록했다. reverse proxy와 운영 배포 검증은 남아 있다. 이 어댑터 검증을 T12 전체·HMI/MES 업무 통합 완료로 표시하지 않는다.

## 인증된 상태 읽기

`GET /api/v1/sources`는 등록된 라인의 이름과 source/stream 식별자를 반환한다. `GET /api/v1/snapshot?line_id=...&source_id=...&stream_id=...`는 해당 라인에 연결된 출처의 영속 관측을 반환한다. 두 경로 모두 초기 암호 변경을 마친 활성 세션과 `read` 권한이 필요하며 인증 트랜잭션의 같은 DB 연결에서 읽는다. source/stream이 다른 라인에 속하거나 미등록이면 404다. 최초 범위는 단일 현장·라인이며 사용자별 다중 현장 접근 모델을 제공하지 않는다.

값의 품질·시각·미수집 항목과 제어 제한은 [스냅샷 읽기](SNAPSHOT.md)를 따른다. 수집이 최근이라고 독립적인 PLC heartbeat 정상이나 제어 허용을 단정하지 않는다.

자동 화면 snapshot·명령 결과 GET 폴링은 `refresh_idle=False`로 세션의 30분 idle 만료를 연장하지 않는다. WS 접속·자동 재접속도 같은 원칙을 적용한다. 화면 REST projection은 같은 인증 트랜잭션 안에서 line 공유 잠금을 획득해 수집 쓰기와 일관된 관측·품질·보류 근거를 읽고, 응답 전 잠금을 해제한다.
