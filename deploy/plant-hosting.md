# 배포된 화면에 가상 설비 연결

Vercel Hobby의 웹·API와 Neon Free DB는 유지한다. 별도 설비 앱과 게이트웨이는 이미 제공된 클라우드 환경 또는 사용자 PC에서 실행하며 추가 유료 서비스를 만들지 않는다. 두 프로세스는 암호화 OPC UA로 통신한다. 게이트웨이는 HTTPS로 Vercel 수집 API에 전송하고 PostgreSQL이나 설비 SQLite를 직접 열지 않는다.

## 현재 검증 경계

공개 앱의 DB 준비 검사와 사용자 로그인·암호 변경은 사용자 결과로 확인했다. 별도 임시 DB·실제 CLI·브라우저·OPC UA의 작업 등록/배포/연결/예열/시작/생산/일시정지/종료/SETTLED 대조는 `scripts/check-operable-system.py`로 통과했다. 공개 앱의 실제 설비 수집·명령과 영속 호스팅은 이와 별도로 확인해야 한다.

## 연결 파일 생성과 수동 적용

전용 런타임 `deploy/plant_runtime.py`가 사용하는 ID는 아래와 같다. 기존 preview 저장소는 사용하지 않는다.

| 항목 | 값 |
| --- | --- |
| Source | `demo-packaging-source-v1` |
| Stream | `events-main` |
| Gateway | `demo-packaging-gateway-v1` |
| Line | `packaging-line-1` |
| 초기 capture generation | 1 |

토큰은 아래 생성기로 새 보호 디렉터리에 한 번 생성한다. 기존 파일을 덮어쓰거나 기존 큐를 지우지 않는다. 실제 simulator/gateway 저장 identity와 일치하는 값이어야 한다.

```sh
.venv/bin/python -m deploy.generate_gateway_setup \
  --source-id demo-packaging-source-v1 --stream-id events-main \
  --gateway-id demo-packaging-gateway-v1 --line-id packaging-line-1 \
  --key-id demo-plant-key-1 --capture-generation 1 \
  --expires-at 2027-01-20T00:00:00+00:00 \
  --output-directory .local/commissioning/connection
```

관리자가 수행할 실제 적용은 두 가지다.

1. Neon SQL Editor에서 DB `hmi_mes_demo`, 역할 `neondb_owner`로 `prepare-source.sql` 전체 실행. 기존 초기화 SQL이나 계정 seed를 다시 실행하지 않는다. 같은 binding 재시도는 기존 커서·복구 상태를 보존하며 다른 binding은 거절한다.
2. Vercel 프로젝트의 환경변수에 `HMI_GATEWAY_CREDENTIALS_JSON`을 **Production** 범위로 추가하고 `vercel-gateway-credentials.json` 전체를 Value에 입력한다. 환경변수 저장 후 Production을 **Redeploy**한다. 기존 DSN·Origin은 변경하지 않는다.

`token.secret` 원문은 게이트웨이에만 전달한다. Vercel JSON에는 해시만 들어 있다. 토큰이나 앱/DB 비밀번호를 채팅·Git에 복사하지 않는다. 제공한 날짜 이후에는 운영자가 명시적으로 토큰을 교체해야 한다.

## 이 클라우드에서 연결 확인

준비된 `.local/commissioning/runtime`은 영속 파일을 보존하지만 **클라우드 환경이 중단되면 프로세스도 중단될 수 있다**. 이를 상시 호스팅으로 간주하지 않는다. 외부 접속 정책에 `hmi-mes.vercel.app` 허용도 필요하다.

```sh
.venv/bin/python deploy/plant_runtime.py --directory .local/commissioning/runtime prepare
.venv/bin/python deploy/plant_runtime.py --directory .local/commissioning/runtime serve \
  --token-file "$PWD/.local/commissioning/connection/token.secret"
```

새 저장소에 한해서 `prepare --initialize`를 명시한다. 일반 재실행은 `prepare`만 사용한다. 기동 후에는 설비가 복구 필요 상태로 멈춘다. 정지 출력·물품 위치·저장소·미결 명령을 실제 확인한 운영자가 별도 `commission` 명령으로 복구·REMOTE/AUTO를 설정한다. 이 명령은 START나 작업을 만들지 않는다. 현재 암호 파일을 지정하며 최초 암호 변경 후에는 `console-password`를 사용한다.

```sh
.venv/bin/python deploy/plant_runtime.py --directory .local/commissioning/runtime commission \
  --password-file .local/commissioning/runtime/console-initial-password \
  --new-password-file .local/commissioning/runtime/console-password \
  --outputs-stopped --positions-verified --storage-reconciled \
  --commands-reconciled --operator-confirmed
```

## 사용자 PC에서 추가 유료 서비스 없이 실행

Linux 또는 Windows의 WSL에서 저장소를 준비한다. `plant_runtime.py`는 POSIX 파일 잠금(`fcntl`)을 사용하므로 Windows 기본 Python에서는 실행하지 않는다. Python 3.12와 `uv`가 필요하며, 현재 프로젝트는 Python 3.13 이상을 지원하지 않는다. 기존 Vercel·Neon을 사용하므로 Docker나 새 PostgreSQL은 필요 없다.

저장소 루트에서 고정된 의존성을 설치한다.

```sh
uv sync --python 3.12 --frozen --no-dev --no-install-project
```

아직 어느 환경에서도 해당 source를 초기화하지 않은 새 저장소에만 아래 초기화를 한 번 실행한다. 기존 클라우드 실행을 PC로 옮긴다면 새로 초기화하지 않는다. 먼저 기존 소유 프로세스를 종료하고 저장소 전체와 미전송 큐·원본 사건·인증서를 보존하여 이전한 뒤 `prepare`만 실행한다. 같은 source의 복사본을 다른 PC나 클라우드에서 동시에 기동하지 않는다.

```sh
.venv/bin/python deploy/plant_runtime.py --directory .local/commissioning/runtime prepare --initialize
```

위 연결 파일 생성·수동 적용의 SQL과 Vercel 설정 두 가지를 완료하고, 같은 `token.secret`을 PC의 `.local/commissioning/connection/token.secret`에 보호 파일로 전달한다. 파일은 현재 실행 사용자 소유의 일반 파일이고 권한은 `0600`이어야 한다. 토큰 원문을 명령줄이나 채팅에 넣지 않는다. 기존 보호 파일을 덮어쓰지 않는다.

이후 위의 `serve` 명령으로 실행한다. 로컬 설비 콘솔은 `http://127.0.0.1:8011`이며 계정은 `commissioning.operator`다. 최초 비밀번호는 런타임 디렉터리의 `console-initial-password` 보호 파일에 있다. 최초 준비의 실제 확인과 `commission`은 위 절차를 따르며, 기존 작업이 남은 재기동은 [설비 앱 실행 절차](../docs/architecture/SIMULATOR_RUN.md)의 명령·위치·수량 대조 절차를 따른다. 앱 A에서 값이 갱신되는 것을 확인한 후 [작업자 사용 순서](../docs/OPERATOR_GUIDE.md)를 따른다.

사용자 PC는 실행 중이고 인터넷에 연결되어 있어야 한다. 절전·종료 또는 클라우드 환경 중단 시 설비·게이트웨이 프로세스도 멈추므로 이 경로는 24시간 상시 실행을 보장하지 않는다. 생성된 개발 OPC 인증서는 30일 유효하다. 장기 운영에는 중단·백업·양측 인증서 교체·검증을 포함하는 별도 인증서 관리가 필요하다. 이 실행 절차만으로 전체 기획의 운영·장시간 수락이 완료되는 것은 아니다.
