# 로컬 Docker Compose

Docker Engine, Compose v2.22 이상(BuildKit `additional_contexts: service:` 지원)이 필요하다. 저장소 루트에서 실행한다.

```sh
docker compose up -d
# 앱 주소: http://127.0.0.1:8002 (게스트 버튼은 UX-07 결합 후 사용)
python scripts/check-compose.py
# 데이터 유지 종료
docker compose down
# 이 로컬 프로젝트 데이터까지 제거(복구 불가)
docker compose down -v
```

스모크 실행에는 프로젝트 Python 의존성(`uv sync --frozen`)이 필요하다. 이 검사는 무작위 독립 프로젝트와 빈 이름 있는 볼륨을 만들고 build/up → 정상 게스트 인증 → 48개 고유 태그의 GOOD·fresh 및 heartbeat GOOD → 실제 빌드한 웹 → 동일 데이터 init 재실행을 확인한다. 성공·실패 모두 해당 검사 프로젝트만 down -v 한다. 이미 띄운 사용자 프로젝트는 종료하지 않는다.

기본 포트가 사용 중이면 `HMI_COMPOSE_PORT=18002 docker compose up -d`로 바꾸고 정확히 `http://127.0.0.1:18002`를 사용한다. `localhost`는 별도 Origin이므로 사용하지 않는다. 포트는 호스트의 127.0.0.1에만 공개한다. PostgreSQL·OPC UA·내부 API·시뮬레이터 콘솔은 호스트에 공개하지 않는다.

`secrets`가 볼륨에 PostgreSQL 비밀번호와 게이트웨이 토큰을 0600으로 만들고, PostgreSQL 준비 이후 일회성 `init`가 Alembic head·기준 정보·고정 source binding·전용 viewer·개발 인증서와 SQLite를 준비한다. 기존 source가 다르면 거부한다. 재기동은 기존 비밀값·인증서·SQLite·작업·계정을 덮어쓰지 않는다. 기존 게스트 계정은 dedicated marker와 정확한 viewer 역할을 검증한다. 앱의 최초 기동 중 계정 자동 생성은 없다.

기존 plant Dockerfile을 Python 기반 이미지로 재사용하며 별도 Node 단계가 현재 apps/web 소스를 npm ci와 실제 TypeScript/Vite build로 빌드한다. Dockerfile 전용 ignore가 node_modules·.local·비밀 파일·기존 dist를 제외한다. 웹 정적 파일은 사용자 API에 함께 제공한다. simulator/gateway는 api 컨테이너의 네트워크 namespace만 공유하고 각자의 SQLite를 사용한다. loopback 제한 CLI와 상호 인증 OPC UA를 그대로 사용하며 TCP 프록시는 HTTP·WebSocket 바이트를 사용자 API로 전달한다. Origin·CSRF·역할 검사는 사용자 API에 남아 있다. gateway 명령·정산 worker를 모두 활성화한다.

현재 기준 UI에는 게스트 버튼(UX-07)이 아직 없다. 스모크 검사는 실제 `/auth/guest`를 호출하여 조회 세션을 검증하지만 화면에서 버튼을 눌러 진입하는 검증을 대신하지 않는다. UI 담당자가 UX-07을 결합해야 비밀번호 없는 화면 진입을 사용할 수 있다.

이 구성은 개발용이며 운영 배포가 아니다. 전용 게스트만 준비하므로 제어는 viewer 권한으로 거부된다. 제어 계정이 필요하면 기존 명시적 bootstrap-user 절차와 최초 비밀번호 변경을 사용한다. 개발 인증서 유효기간은 30일이다. 만료 후 기존 데이터에 인증서를 자동 덮어쓰지 않으며 인증서 교체는 별도 운영 절차가 필요하다. 비밀값·DSN·계정 비밀번호를 출력하거나 소스에 기록하지 않는다. 공개 클라우드의 게스트 활성화와 무관하며 유료 리소스를 만들지 않는다.

관리형 프록시 환경에서는 환경 정책에 맞는 Compose override로 build.args HTTP_PROXY/HTTPS_PROXY/NO_PROXY와 BuildKit `proxy_ca` 파일 secret을 전달한다. CA는 RUN secret mount로만 읽으며 이미지에 복사하지 않고 TLS 검증을 끄지 않는다. 일반 Docker 호스트에는 override가 필요 없다. 검사 시 `--compose-file /path/to/override.yaml`을 추가한다. 원격 Docker selector는 검사가 제거하고 명시적 unix:///var/run/docker.sock만 사용한다.
