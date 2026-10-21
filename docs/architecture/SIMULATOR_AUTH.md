# 시뮬레이터 콘솔 인증 기본 구성

상태: **내부 인증 primitive 구현·단위 검증**. HTTP·웹 콘솔·OPC 제어 인증 완료를 뜻하지 않는다. 기준은 [인증 설계](DATA_AUTH.md) §5~7, 개발계획서 DEV-08·최초 제어 경로 인증 조건이다. 공개 endpoint·기본 계정·관리자 우회 역할은 추가하지 않았다.

## 저장소와 트랜잭션

`apps/simulator/auth.py`의 `SimulatorAuth`는 `SimulatorStorage`가 소유하는 기존 `sqlite3.Connection`을 받는다. 별도 파일·연결·프로세스 소유권을 만들지 않는다. 소유자의 `transaction()` 내부에서 `initialize_schema()`를 명시적으로 호출한다. 자체 `auth_schema_version(singleton=1,version=1)`과 `console_users`, `console_sessions`, `console_login_challenges`, `console_auth_audit`만 추가하며 기존 simulator `PRAGMA user_version`은 변경하지 않는다. 생성자는 계정·스키마를 만들지 않는다.

모든 변경 함수는 호출자 트랜잭션이 필수다. 함수별 savepoint로 실패한 단위의 부분 변경을 되돌리며 외부 트랜잭션을 commit하지 않는다. 따라서 인증·권한 검사, PLC 변경, 명령 결과·감사를 동일한 연결·트랜잭션에 묶을 수 있다. `authenticate()`도 idle 시각을 갱신하므로 트랜잭션이 필요하다. 이 구성은 단일 simulator writer를 전제로 한다.

## 내부 API

| 함수 | 동작 |
|---|---|
| `SimulatorAuth(connection, allowed_origins=..., clock=...)` | 정확한 출처 allowlist와 선택적 시험용 UTC clock 지정 |
| `initialize_schema()` | 별도 버전의 additive schema 생성·기존 지원 버전 확인 |
| `bootstrap_user(login, password, role=...)` | 별도 로컬 provisioning 도구에서만 명시 호출. 기존 계정 변경·암호 재설정 거절 |
| `begin_login()` | 5분 만료 pre-auth token·CSRF token 반환 |
| `login(login, password, challenge_token=..., csrf_token=..., origin=...)` | challenge·Origin·CSRF·암호 검사 후 사용자 세션 발급. 성공한 challenge 재사용 거절 |
| `authenticate(token)` | 현재 계정·역할·auth_version·세션 상태 확인, idle 기한 갱신 |
| `authorize_mutation(token, csrf_token=..., origin=...)` | 변경된 암호를 사용하는 `simulator_operator`만 콘솔 변경 허용 |
| `change_password(token, new_password, csrf_token=..., origin=...)` | 자신의 암호 변경, 기존 모든 세션 취소, 새 token·CSRF 발급 |
| `revoke(token, csrf_token=..., origin=...)` | 자신의 세션 로그아웃 |

권한은 별도 앱의 `viewer`, `simulator_operator`만 인정한다. `viewer`는 읽기 인증·자신의 암호 변경·로그아웃을 할 수 있지만 조작은 거절된다. 부트스트랩 계정은 초기 암호 변경 전 조작을 거절한다. 현장 조건·비상정지·가드·모드의 최종 판정은 PLC가 수행한다. 앱 A 계정·쿠키·gateway token을 이 모듈로 가져오는 경로는 없다. `bootstrap_user()`는 내부 로컬 provisioning API이며 사용자 요청 handler에 연결하면 안 된다. 사용자·역할 관리 endpoint는 없다.

로그인 이름은 공백 없는 ASCII identifier 1~64자로 제한하고 소문자로 정규화한다. 암호는 정규화·trim하지 않으며 UTF-8 12~1024 bytes, NUL 제외로 제한한다. 입력 오류에는 입력값을 넣지 않는다. 기존 계정을 반복 bootstrap해 암호·역할을 덮어쓰지 않는다. 명시적인 secret 파일 읽기 CLI는 [실행 절차](SIMULATOR_RUN.md)에 제공하며 초기 파일의 회수는 운영자가 수행한다.

## 암호·세션·변경 보호

이 작은 구현은 새 의존성 없이 Python 표준 `hashlib.scrypt`를 사용한다. 계정마다 CSPRNG 16-byte salt, `N=32768,r=8,p=1,dklen=32,maxmem=64MiB`를 고정하고 암호 파생값만 저장한다. 설계의 Argon2id 후속 라이브러리 선택을 scrypt로 구체화한 범위이며 이 수치만으로 배포 성능·외부 보안 검토 통과를 주장하지 않는다. 알고리즘 변경 시 명시적 인증 schema migration이 필요하다. 암호 검사는 `secrets.compare_digest()`를 사용하고 존재하지 않는 계정도 동일한 scrypt 비용을 치른다. 로그인 실패는 공통 `AuthenticationError`를 반환한다. 모든 분기의 전체 실행 시간이 동일하다는 보장은 아니다.

pre-auth·session·CSRF token은 각각 `secrets.token_urlsafe(32)`의 256-bit 난수다. 서버 DB에는 SHA-256 digest만 저장하며 세션 반환 객체의 repr에서도 token을 제외한다. 세션은 idle 30분·절대 8시간, 만료 경계에서 거절한다. 매 인증에 현재 계정의 활성 상태·역할·auth_version을 재확인하며 불일치·취소·시계 역행을 거절한다. 암호 변경은 auth_version을 올리고 모든 이전 세션을 취소한다. 프로세스 재구성 뒤에도 커밋한 세션 상태는 유지된다.

로그인·암호 변경·로그아웃·조작 검사는 모두 정확한 Origin allowlist와 해당 pre-auth 또는 로그인 세션에 결합된 CSRF token이 필수다. Origin 누락·`null`·wildcard·다른 출처를 허용하지 않는다. HTTPS를 사용하며 HTTP allowlist는 명시적 loopback 개발 출처에만 허용한다. Origin의 경로·사용자 정보·fragment·query를 허용하지 않는다. 생성한 CSRF digest는 timing-safe 비교한다. 다른 세션 CSRF token은 거절한다.

인증 예외 `AuthenticationError`는 [콘솔 HTTP 어댑터](SIMULATOR_CONSOLE.md)에서 401, 권한·Origin·CSRF 예외 `AuthorizationError`는 403에 대응한다. 계정 생성·로그인 성공·암호 변경·로그아웃 감사는 같은 트랜잭션에 남기며 비밀번호·token·cookie를 포함하지 않는다.

## 검증과 남은 범위

로컬 검증 명령:

```sh
.venv/bin/pytest -q tests/unit/test_simulator_auth.py
.venv/bin/ruff check apps/simulator/auth.py tests/unit/test_simulator_auth.py
```

실제 SQLite를 사용해 명시적 bootstrap·반복 거절·salt 차이·평문 비밀 미저장·초기 암호 변경·역할 거절·잘못된 자격증명·idle/절대 만료·회수·계정 상태 변경·pre-auth 재사용/만료·세션별 CSRF·정확한 Origin·savepoint/외부 rollback·기존 simulator schema version 보존을 확인한다. 결과는 상위 개발 상태 문서에 합산한다.

공개 콘솔 handler·HttpOnly/Secure/SameSite=Strict cookie·계정/IP 속도 제한·독립 실패 감사·pre-auth 발급량 제한은 [콘솔 어댑터](SIMULATOR_CONSOLE.md)에 구현했다. 비밀번호 secret 파일 CLI는 [별도 앱 실행](SIMULATOR_RUN.md)을 따른다. 만료 행 보관 정리·WS 취소·원격 TLS 배포 환경 검증은 남아 있다. 이 단위 시험을 T12 전체·OPC 인증·안전 조건 검증 완료로 표시하지 않는다.
