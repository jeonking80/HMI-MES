# 앱 A 사용자 초기 등록·로컬 인증 서버

앱 A 계정은 PostgreSQL에 저장하며 앱 B의 SQLite 계정·세션과 독립적이다. 이 문서는 초기 계정 등록과 브라우저 세션 인증 HTTP 서버 범위다. UI·운전 제어·보고서와 전체 M2 완료를 의미하지 않는다. 내부 서비스 수집 API와 게이트웨이 실행은 [M2_RUN.md](M2_RUN.md)를 따른다.

저장소 루트에서 `./scripts/bootstrap-python.sh`를 실행하고 비밀 관리 도구로 `HMI_DATABASE_DSN`을 환경변수에 주입한다. 비밀번호가 포함된 DSN을 명령 인자·셸 이력·로그에 넣지 않는다. 기본 PostgreSQL 연결·SQLite 대체 저장소는 없다.

```sh
.venv/bin/python -m alembic -c alembic.ini upgrade 0003_user_auth
```

마이그레이션은 역할 정의만 등록하며 기본 사용자·비밀번호를 만들지 않는다. `bootstrap-user`는 이 마이그레이션이 이미 적용된 경우에만 작동하며 자동 migration·기준 정보 bootstrap을 하지 않는다.

초기 비밀번호는 비밀 관리 도구에서 현재 실행 사용자 소유의 0600 일반 파일로 준비한다. 대화식 입력으로 새 파일을 준비할 때는 다음 예시를 사용할 수 있다. 비밀번호는 화면·stdout·셸 이력에 출력하지 않으며 기존 파일을 덮어쓰지 않는다.

```sh
mkdir -p .local/service
chmod 700 .local/service
.venv/bin/python - <<'PY'
import getpass
import os
password = getpass.getpass('Initial App A password: ')
if password != getpass.getpass('Repeat password: '):
    raise SystemExit('Passwords do not match')
encoded = password.encode('utf-8')
if not 12 <= len(encoded) <= 1024 or b'\0' in encoded:
    raise SystemExit('Password must have 12..1024 UTF-8 bytes and no NUL')
fd = os.open('.local/service/app-a-initial-password',
             os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'wb') as handle:
    handle.write(encoded)
PY
```

초기 등록은 DB 운영 권한이 있는 관리자가 명시적으로 실행한다. 로그인 ID·역할·정확한 브라우저 origin·비밀번호 파일을 모두 지정한다.

```sh
.venv/bin/python -m apps.api bootstrap-user \
  --login operator.a --role operator \
  --origin http://127.0.0.1:8002 \
  --password-file .local/service/app-a-initial-password
```

역할은 `viewer`, `operator`, `production_manager`, `equipment_admin`이며 여러 역할이 필요하면 `--role`을 반복한다. 상위 역할의 자동 상속이나 기본 관리자 역할은 없다. HTTPS origin 또는 명시적 loopback HTTP origin을 지정하며 scheme·host·port가 정확히 일치해야 한다. 이 값은 초기 등록 검증 설정이며 브라우저 서버에도 같은 허용 origin을 별도로 설정한다.

로그인 ID는 대소문자를 정규화한다. 기존 계정에는 실패하며 비밀번호·역할·활성 상태를 초기화하지 않는다. 새 계정은 최초 비밀번호 변경이 필요하다. 등록 성공 후 초기 파일은 운영 비밀 관리 절차에 따라 회수한다. 비밀번호 본문을 CLI 인자나 JSON으로 받거나 출력하는 경로는 없다.

파일 검증은 현재 사용자 소유·그룹/기타 권한 없음·일반 파일·상한 크기를 요구하며 symlink와 FIFO를 거부한다. API 오류 출력은 비밀번호·DSN·SQL 매개변수·드라이버 예외 내용을 포함하지 않는다. 등록 실패 시 명시적으로 DB 상태와 보호 파일을 확인하며 반복 등록으로 계정을 복구하지 않는다.

```sh
.venv/bin/pytest -q tests/integration/test_api_bootstrap_user.py
```

시험은 실제 별도 CLI 프로세스와 일회용 PostgreSQL에서 등록·로그인·최초 변경 필요·재등록 거부와 원본 보존·안전하지 않은 파일 거부·자동 migration 및 기본 계정 부재를 검증한다.

## 브라우저 세션 인증 서버

같은 PostgreSQL DSN을 주입한 별도 터미널에서 실행한다. 기본 포트는 8002이며 내부 게이트웨이 서비스의 기본 포트 8000과 별도로 사용한다.

```sh
.venv/bin/python -m apps.api serve-user \
  --host 127.0.0.1 --port 8002 --origin http://127.0.0.1:8002
```

실행기는 loopback HTTP에만 바인딩한다. `--origin`은 실제 host·port와 정확히 일치해야 하며 `localhost`와 `127.0.0.1`도 서로 바꾸어 쓰지 않는다. IPv6를 사용하면 `--host ::1 --origin 'http://[::1]:8002'`로 지정한다. schema `0003_user_auth`가 필요하며 시작 시 migration·계정 등록·비밀번호 초기화를 수행하지 않는다. 종료 신호는 HTTP 요청 처리를 종료하고 PostgreSQL 연결 풀을 정리한다. 원격 HTTPS 배포는 별도 운영 구성 범위다.

이 프로세스는 `/auth/challenge`, `/auth/login`, `/auth/me`, `/auth/password`, `/auth/logout`만 제공하며 내부 batch 수집 경로와 서비스 bearer 인증을 포함하지 않는다. 아직 브라우저 UI 페이지는 없다. 브라우저 클라이언트는 다음 순서로 호출한다.

1. `POST /auth/challenge`에 정확한 `Origin`과 `X-HMI-Request: 1`을 보낸다. 응답의 `csrf_token`과 HttpOnly 사전 인증 쿠키를 사용한다.
2. `POST /auth/login`에 같은 `Origin`, `X-CSRF-Token`, JSON `login`·`password`를 보낸다. 비밀번호는 URL·명령 인자·로그에 넣지 않는다.
3. 세션 쿠키로 `GET /auth/me`를 호출한다. 최초 계정은 `must_change_password: true`이며 로그인만으로 운전 권한 검증을 대신할 수 없다.
4. 비밀번호 변경·로그아웃은 세션 응답의 최신 CSRF 토큰과 정확한 Origin을 사용한다. 비밀번호 변경은 기존 세션을 폐기하고 새 세션을 발급한다.

실제 Host와 scheme을 검사하며 forwarded headers를 신뢰하지 않는다. 세션 쿠키는 HttpOnly·SameSite=Strict이며 이 명시적 로컬 HTTP 모드에서는 Secure 속성을 사용하지 않는다. 응답은 비밀번호와 세션 토큰을 로그에 출력하지 않는다.

```sh
.venv/bin/pytest -q tests/integration/test_api_user_launcher.py
```

실제 별도 프로세스·HTTP socket·PostgreSQL 시험으로 명시적 사전 계정 등록 후 challenge/login/me, 내부 batch 경로 부재, Host 거부, 만료 전 최초 비밀번호 변경 필요 상태, origin 불일치 시작 거부, 자동 migration 부재와 정상 신호 종료를 검증한다.

## 같은 주소에서 웹 화면 제공

앱 A 웹을 빌드한 뒤 명시적으로 빌드 디렉터리를 선택한다. API만 실행할 때는 `--web-dir`을 생략한다.

```sh
npm --prefix apps/web run build
.venv/bin/python -m apps.api serve-user \
  --host 127.0.0.1 --port 8002 --origin http://127.0.0.1:8002 \
  --web-dir apps/web/dist
```

브라우저에서 `http://127.0.0.1:8002`를 연다. `/`과 빌드의 `/assets`만 제공하며 API 경로를 HTML fallback으로 바꾸지 않는다. 인증 쿠키와 요청은 같은 origin을 사용한다. 외부 스크립트·인라인 스크립트는 허용하지 않는다. 원격 배포와 TLS 설정은 별도 운영 검증 범위다.
