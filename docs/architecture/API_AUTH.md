# 앱 A PostgreSQL 사용자 인증

앱 A의 인증 서비스와 `0003_user_auth` migration을 구현했다. HTTP·WebSocket·브라우저 통합은 후속 작업이다. 앱 B의 사용자·SQLite·쿠키와 공유하지 않는다. 최초 사용자는 일반 서버 시작과 분리된 명시적 로컬 provisioning으로만 생성하며 기본 계정·암호는 없다.

## 저장·암호·세션

`users`, `roles`, `user_roles`, `auth_sessions`, `auth_login_challenges`, `auth_audit`를 PostgreSQL에 저장한다. Migration은 현재 모델 코드를 import하지 않는 고정 DDL이고 이전 revision은 `0002_ingestion`이다. 네 역할 이름은 `viewer`, `operator`, `production_manager`, `equipment_admin`으로 고정한다. `DATA_AUTH.md`의 설계명 `production`은 구현에서 `production_manager`로 명확하게 명명했다. `simulator_operator`는 앱 B 전용으로 거절한다.

로그인은 ASCII 식별자 1~64자를 소문자로 정규화한다. 암호는 UTF-8 12~1024바이트, NUL 금지이며 사용자마다 임의 16바이트 salt와 scrypt(N=32768,r=8,p=1,출력 32바이트)를 사용한다. 앱 B와 같은 순수 암호 방식이지만 앱 B 모듈이나 저장소를 import하지 않는다. 알 수 없는 계정도 scrypt를 수행하고 로그인 오류는 동일한 메시지다.

세션과 로그인 전 challenge는 각각 독립적인 256비트 난수 token·CSRF token을 발급한다. DB에는 SHA-256 digest만 저장한다. `repr`에는 자격증명이 나오지 않는다. 인증 서비스는 SQL echo를 금지하고 bound parameter를 숨기는 PostgreSQL engine만 받는다. 감사에는 정규화 계정·고정된 행위명·시각만 저장하고 암호·token·cookie를 기록하지 않는다.

로그인 전 CSRF challenge는 5분 만료, 성공 시 1회 소비다. 로그인·암호 변경·로그아웃·권한/계정 변경과 모든 업무 변경은 정확한 Origin allowlist와 해당 challenge/세션에 결합된 CSRF 검사를 통과한다. Origin은 경로·query·fragment·사용자정보·wildcard를 허용하지 않으며 HTTP는 loopback 개발 주소만 허용한다. 문자열이 allowlist와 정확히 같아야 한다.

세션은 idle 30분, 절대 8시간이다. 인증할 때 idle을 갱신하되 절대 만료를 넘지 않는다. 시각 역행은 거절한다. 초기 암호의 세션은 신원 확인·자기 암호 변경·로그아웃만 가능하며 업무 조회도 허용하지 않는다. 암호 변경은 기존 암호 재사용을 거절하고 모든 세션을 취소한 뒤 새 token·CSRF를 발급한다. 역할 교체·비활성/활성 변경은 `auth_version`을 증가시키고 모든 기존 세션을 취소한다. 재활성화해도 취소 세션은 복구되지 않는다.

## Python 서비스 계약

`UserAuth(engine, *, allowed_origins, clock=...)`는 schema나 사용자를 생성하지 않는다. `apps.api.database.create_database_engine`으로 만든 engine을 사용한다. 모든 메서드는 자체 PostgreSQL 트랜잭션을 수행하고 커밋 성공 뒤에만 자격증명을 반환한다.

| 메서드 | 결과·경계 |
|---|---|
| `bootstrap_user(login,password,*,roles)` | 명시적 로컬 provisioning. `Principal` 반환. 기존 로그인 재설정 불가. 초기 암호 변경 필수 |
| `begin_login()` | `LoginChallenge(token,csrf_token)` 반환. 인증 권한 없음 |
| `login(login,password,*,challenge_token,csrf_token,origin)` | `SessionCredentials` 반환. 성공 시 challenge 소비 |
| `authenticate(token)` | 살아 있는 계정·세션·auth_version 검증 및 idle 갱신. `Principal` 반환. 업무 권한 부여 기능은 아님 |
| `authorized_transaction(token,*,permission,csrf_token=None,origin=None)` | context manager. `.connection`과 `.principal`을 가진 `AuthorizedTransaction`을 yield |
| `change_password(token,new_password,*,csrf_token,origin)` | 전체 기존 세션 취소 및 새 `SessionCredentials` 반환 |
| `revoke(token,*,csrf_token,origin)` | 현재 세션 로그아웃 |
| `set_user_roles(token,target_login,roles,*,csrf_token,origin)` | 초기 암호 변경을 마친 설비 관리자만 명시적 역할 집합 교체 |
| `set_user_active(token,target_login,active,*,csrf_token,origin)` | 초기 암호 변경을 마친 설비 관리자만 계정 활성 상태 변경 |

`Principal`은 `login`, `roles: frozenset[str]`, `must_change_password`를 제공한다. `SessionCredentials`는 `token`, `csrf_token`, `principal`, `expires_at`를 제공한다. `AuthenticationError`는 401, `AuthorizationError`는 403에 매핑한다. 입력 오류는 `ValueError`다. transport adapter는 예외에 자격증명·요청 본문을 추가하지 않아야 한다.

## 업무 권한·트랜잭션

역할 상속은 없으며 복수 역할은 명시적으로 부여해야 한다. 설비 관리자는 생산 편집 권한을 자동으로 얻지 않는다.

| permission | 허용 역할 |
|---|---|
| `read` | 앱 A 네 역할 |
| `operate`, `alarm_action` | operator |
| `load_job`, `end_run` | operator, production_manager |
| `production_manage`, `parameters_manage` | production_manager |
| `equipment_manage` | equipment_admin |

알 수 없는 permission은 거절한다. 수량 직접 수정·강제 정산 permission은 없다. 명령 종류·대상·실행·freshness·PLC 조건 등 업무 검증은 해당 업무 서비스에서 추가해야 한다.

업무 변경은 `authenticate()` 결과를 보관했다가 별도 연결로 쓰지 않고 다음과 같이 같은 연결을 사용한다.

```python
with auth.authorized_transaction(
    session_token,
    permission="operate",
    csrf_token=csrf_token,
    origin=origin,
) as unit:
    # 이 연결에서 업무 검증·쓰기·업무 감사까지 수행한다.
    submit_command(unit.connection, requester=unit.principal.login)
```

인증은 사용자 행을 잠근 뒤 세션 행을 잠그고 context 종료까지 유지한다. 사용자 관리가 두 계정을 잠글 때는 로그인 정렬 순서다. 권한 회수·로그아웃·암호 변경이 승인된 업무 쓰기 사이에 끼어들지 못한다. 업무 예외는 업무 쓰기와 idle 갱신을 함께 롤백한다. 호출자는 yield된 연결에서 임의 commit/rollback을 하지 않으며 DB 잠금 중 OPC·HTTP 외부 I/O를 기다리지 않는다. 업무 잠금 순서는 기존 line→stream→execution보다 사용자·세션 잠금이 앞선다.

## 검증·남은 작업

`tests/integration/test_api_auth.py`는 일회용 실제 PostgreSQL에서 migration 왕복·기본 사용자 없음·salted scrypt·digest 저장·일회 challenge·초기 암호 변경·권한표·CSRF/Origin·idle/절대 만료·시각 역행·암호/역할/계정/로그아웃 회수·업무 트랜잭션 롤백·동시 회수 잠금을 검증한다.

```sh
.venv/bin/ruff check apps/api/auth.py apps/api/migrations/versions/0003_user_auth.py tests/integration/test_api_auth.py
.venv/bin/pytest -q tests/integration/test_api_auth.py
```

HTTP 로그인/업무 endpoint, Secure·HttpOnly·SameSite host-only cookie, 요청 제한·로그인 rate limit, WS handshake/주기 재검증과 회수 시 종료, 런타임 secret-file 기반 bootstrap CLI는 아직 구현하지 않았다. `authenticate()`만으로 업무 endpoint를 허용하거나 브라우저 localStorage에 token을 저장하면 안 된다. 이 서비스 시험은 실제 브라우저/제어 경로의 T12 완료를 의미하지 않는다.
