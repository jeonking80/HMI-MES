# 로컬 Docker Compose

Docker Engine, Compose v2.22 이상(BuildKit `additional_contexts: service:` 지원)이 필요하다. 저장소 루트에서 실행한다.

```sh
docker compose up -d
# 앱 주소: http://127.0.0.1:8002
python scripts/check-compose.py
# 데이터 유지 종료
docker compose down
# 이 로컬 프로젝트 데이터까지 제거(복구 불가)
docker compose down -v
```

스모크 실행에는 프로젝트 Python 의존성(`uv sync --frozen`)이 필요하다. 이 검사는 무작위 독립 프로젝트와 빈 이름 있는 볼륨을 만들고 build/up → 정상 게스트 인증 → 48개 고유 태그의 GOOD·fresh 및 heartbeat GOOD → 웹 응답 → 분석 제공 여부와 실제 설비 실행·정산·트렌드 검증 → 동일 데이터 init 재실행을 확인한다. 완료한 일회성 secrets/init 컨테이너는 볼륨을 유지한 채 제거하여 검사 중 파일 시스템 공간을 줄인다. 성공·실패 모두 해당 검사 프로젝트만 down -v 한다. 이미 띄운 사용자 프로젝트는 종료하지 않는다.

기본 포트가 사용 중이면 `HMI_COMPOSE_PORT=18002 docker compose up -d`로 바꾸고 정확히 `http://127.0.0.1:18002`를 사용한다. `localhost`는 별도 Origin이므로 사용하지 않는다. 포트는 호스트의 127.0.0.1에만 공개한다. PostgreSQL·OPC UA·내부 API·시뮬레이터 콘솔은 호스트에 공개하지 않는다.

`secrets`가 볼륨에 PostgreSQL 비밀번호와 게이트웨이 토큰을 0600으로 만들고, PostgreSQL 준비 이후 일회성 `init`가 Alembic head·기준 정보·고정 source binding·전용 viewer·개발 인증서와 SQLite를 준비한다. 기존 source가 다르면 거부한다. 재기동은 기존 비밀값·인증서·SQLite·작업·계정을 덮어쓰지 않는다. 기존 게스트 계정은 dedicated marker와 정확한 viewer 역할을 검증한다. 앱의 최초 기동 중 계정 자동 생성은 없다.

기존 plant Dockerfile을 Python 기반 이미지로 재사용하며 별도 Node 단계가 현재 apps/web 소스를 npm ci와 실제 TypeScript/Vite build로 빌드한다. Dockerfile 전용 ignore가 node_modules·.local·비밀 파일·기존 dist를 제외한다. 웹 정적 파일은 사용자 API에 함께 제공한다. simulator/gateway는 api 컨테이너의 네트워크 namespace만 공유하고 각자의 SQLite를 사용한다. loopback 제한 CLI와 상호 인증 OPC UA를 그대로 사용하며 TCP 프록시는 HTTP·WebSocket 바이트를 사용자 API로 전달한다. Origin·CSRF·역할 검사는 사용자 API에 남아 있다. gateway 명령·정산 worker를 모두 활성화한다.

이 스모크는 실제 `/auth/guest`와 업무 HTTP API를 호출한다. 화면에서 버튼을 눌러 수행하는 브라우저 수락 시험을 대신하지 않는다.

이 구성은 개발용이며 운영 배포가 아니다. 전용 게스트만 준비하므로 제어는 viewer 권한으로 거부된다. 제어 계정이 필요하면 기존 명시적 bootstrap-user 절차와 최초 비밀번호 변경을 사용한다. 개발 인증서 유효기간은 30일이다. 만료 후 기존 데이터에 인증서를 자동 덮어쓰지 않으며 인증서 교체는 별도 운영 절차가 필요하다. 비밀값·DSN·계정 비밀번호를 출력하거나 소스에 기록하지 않는다. 공개 클라우드의 게스트 활성화와 무관하며 유료 리소스를 만들지 않는다.

관리형 프록시 환경에서는 환경 정책에 맞는 Compose override로 build.args HTTP_PROXY/HTTPS_PROXY/NO_PROXY와 BuildKit `proxy_ca` 파일 secret을 전달한다. CA는 RUN secret mount로만 읽으며 이미지에 복사하지 않고 TLS 검증을 끄지 않는다. 일반 Docker 호스트에는 override가 필요 없다. 검사 시 `--compose-file /path/to/override.yaml`을 추가한다. 원격 Docker selector는 검사가 제거하고 명시적 unix:///var/run/docker.sock만 사용한다.


## CX-09 실제 실행과 시간 근거 검증

검사기는 새 독립 프로젝트의 API 컨테이너 안에서 `scripts/compose_acceptance.py`를 실행한다. 해당 프로젝트의 일회성 볼륨에만 0600 비밀번호 파일을 만들고 기존 `bootstrap-user` CLI로 operator+production_manager 계정을 명시적으로 준비한다. 일반 로그인과 최초 비밀번호 변경을 통과한다. 시뮬레이터도 기존 HTTP 인증·최초 변경과 다섯 복구 확인을 수행하고 REMOTE/AUTO를 선택한다. 이는 테스트가 만든 빈 설비에 대한 명시적인 작업이며 기존 commissioning/public runtime에 연결하지 않는다.

그다음 일반 API로 150°C·10m/min 레시피와 목표 20개 작업을 등록·release·bind한다. gateway의 실제 LOAD_JOB 성공과 기본 물리 모형의 READY를 기다린 뒤 START → 실제 양품 2개 이상 → PAUSE → END_RUN → 원래 execution SETTLED를 확인한다. 명령은 실제 API의 5초 만료·제어 버전·Origin·CSRF·역할 검사를 그대로 통과한다. SQL/SQLite로 생산·텔레메트리·설비 상태·시간 근거를 주입하지 않는다.

추가 조건은 `/auth/config.analytics=true`, 실제 트렌드 `quality=GOOD`가 한 점 이상, 원래 실행 타임라인 `running>0`, 남은 UNKNOWN 사유가 근거 이전의 `NO_EVIDENCE`뿐인 것이다. 보관/시간/운영 조건을 느슨하게 바꾸지 않는다. 같은 호스트 공유 커널 Compose의 명시적인 `--trend-clock-policy same-host`로 검증하며, 공개 Vercel/gateway의 다른 호스트에 이 주장을 재사용하지 않는다.

## 작은 관리형 디스크에서의 재현

일반 Docker 호스트에서는 위 기본 명령이 build/up을 수행한다. 관리형 환경에서 Docker buildx 상태 디렉터리가 읽기 전용이면 `BUILDX_CONFIG=/tmp/hmi-compose-buildx`처럼 작업 전용 경로를 지정하며 기존 Docker 인증 설정은 보존한다. VFS 저장 드라이버는 이미지와 컨테이너 루트 파일 시스템을 크게 복사하므로 충분한 여유 공간을 확보하고 큰 PostgreSQL 회귀 시험과 동시에 빌드하지 않는다.

이미 의존성과 웹을 빌드한 이미지가 있고 현재 소스를 읽기 전용 마운트한 명시적 override를 준비했다면 다음처럼 빌드를 건너뛸 수 있다.

```sh
BUILDX_CONFIG=/tmp/hmi-compose-buildx .venv/bin/python scripts/check-compose.py \
  --no-build --compose-file /tmp/compose-current-code.yaml
```

override는 secrets/init/simulator/gateway의 `image`를 검토한 기존 plant 이미지로, api의 `image`를 기존 web 이미지로 고정한다. 각 서비스의 `/app/apps`, `/app/contracts`에 현재 체크아웃을 `:ro` 마운트하고 api의 `/app/web`에는 현재 `apps/web/dist`를 `:ro` 마운트한다. 기본 compose의 runtime 볼륨과 compose_runtime.py/alembic.ini 마운트는 유지한다. 이 방식은 현재 코드의 실제 Compose 실행 검증이며 새 이미지의 깨끗한 빌드 통과를 뜻하지 않는다.

이번 검증의 clean build 시도는 3GB 여유의 관리형 VFS 디스크에서 `ENOSPC`로 실패했다. 새 검사 프로젝트만 down-v하고 해당 시도에서 생성한 이미지/캐시 ID만 제거했다. 기존 preview PostgreSQL·공개 gateway·다른 프로젝트·보호된 .local 상태는 변경하지 않았다. 실패한 빌드와 아래 캐시 이미지 runtime 검증 결과는 구분한다.

실제 실행 기록:

```sh
BUILDX_CONFIG=/tmp/cx09-buildx .venv/bin/python scripts/check-compose.py \
  --no-build --compose-file /tmp/cx09-compose-cached.yaml
```

이 관리형 세션의 임시 override는 `cx07-check-5d1387215708-plant:local`·`cx07-check-5d1387215708-web:local`과 현재 `/workspace/HMI-MES/apps`, `contracts`, `apps/web/dist`의 읽기 전용 마운트를 사용했다. 새 프로젝트 `cx07-check-7233116416fc`에서 analytics=true, **GOOD 트렌드 44점**, 실제 실행의 **RUNNING 6.86994초**, UNKNOWN 사유 **NO_EVIDENCE만**, 원래 실행 **SETTLED**를 확인했다. 게스트·48 fresh GOOD 태그·웹·init 재실행도 PASS했다. down-v 후 실행 중 컨테이너는 기존 `hmi-mes-preview-postgres`만 남았고 여유 공간은 3.7GB로 복구됐다. 로그는 `/tmp/cx09-compose-acceptance.log`다. 임시 override는 이 세션의 이미지 태그/절대 경로에 맞춰 작성한 것으로 다른 체크아웃에서는 위 지침에 따라 명시적으로 다시 준비한다.
