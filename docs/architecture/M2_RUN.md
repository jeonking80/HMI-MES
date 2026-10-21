# M2 API·게이트웨이 별도 실행

이 실행기는 내부 서비스 수집과 명시적으로 활성화한 명령 전달 경로를 제공한다. 앱 A의 브라우저 사용자 인증·역할·운전 화면·보고서와 전체 M2/T01~T12 완료를 의미하지 않는다. 설비 앱 B 실행은 [SIMULATOR_RUN.md](SIMULATOR_RUN.md)를 따른다. 게이트웨이는 OPC UA와 HTTP만 사용하며 PostgreSQL 연결이나 앱 B SQLite 경로를 받지 않는다.

## 명시적 준비

저장소 루트에서 `./scripts/bootstrap-python.sh`를 실행한다. PostgreSQL은 별도 준비하고 비밀 관리 도구로 `HMI_DATABASE_DSN`을 환경에 주입한다. 연결 기본값과 명령줄 DSN 인자는 없다. 다음 명령은 기존 운영 DB가 아닌 선택한 개발 DB에 적용한다.

```sh
.venv/bin/python -m alembic -c alembic.ini upgrade head
.venv/bin/python -m apps.api.bootstrap
mkdir -p .local/gateway .local/service
chmod 700 .local/service
.venv/bin/python - <<'PY'
import os
import secrets
fd = os.open('.local/service/gateway-token', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as handle:
    handle.write(secrets.token_urlsafe(32) + '\n')
PY
```

토큰 본문은 출력하지 않는다. 기존 파일을 덮어쓰지 않으며 두 프로세스에 동일한 보호 파일을 명시한다. 파일은 현재 실행 사용자 소유 일반 파일, 권한 0600이어야 하며 symlink·공개 권한·과대 파일은 거부된다. 서로 다른 OS 사용자로 배포하면 각 사용자에게 별도 0600 파일로 같은 비밀을 전달하는 관리 절차가 필요하다.

앱 B의 원본 `source_id`, `stream_id`와 일치하는 값을 아래 `source-local`, `events-main` 대신 지정한다. 앱 B 초기 계정 등록 후 아래 운영자용 읽기 전용 조회로 원본 식별자를 확인한다. 게이트웨이 프로세스는 이 조회를 수행하지 않으며 앱 B 저장 파일을 열지 않는다. 새 gateway 큐의 capture generation은 1이다. 기존 큐를 삭제해 진행을 초기화하지 않는다.

```sh
.venv/bin/python - <<'PY'
import sqlite3
from pathlib import Path
path = Path('.local/simulator/state.sqlite3').resolve()
with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as connection:
    print(connection.execute(
        'SELECT source_id, stream_id, counter_epoch FROM simulator_identity WHERE singleton=1'
    ).fetchone())
PY
```

```sh
.venv/bin/python -m apps.api configure-source \
  --source-id source-local --stream-id events-main --gateway-id gateway-local \
  --line-id packaging-line-1 --capture-generation 1
```

등록은 같은 값으로 재실행할 수 있으나 다른 binding으로 덮어쓰지 않는다. 앱 B에 연결할 작업의 실제 execution ID·counter epoch·작업·recipe·파라미터와 동일하게 실행을 명시적으로 등록한다. 아래 예시 ID를 임의의 새 ID로 자동 대체하지 않는다.

```sh
.venv/bin/python -m apps.api register-execution \
  --source-id source-local --stream-id events-main --execution-id execution-001 \
  --work-order-id work-001 --recipe-version recipe-v1 --counter-epoch epoch-001 \
  --temperature-c 180 --speed-m-per-min 10 --target-quantity 100
```

이 명령은 PLC 작업 연결·START를 수행하지 않는다. 미등록 실행 사건은 기존 격리 정책을 따른다. 시작은 `apps/api/database.py`에 정의된 지원 migration revision을 확인하며 자동 migration·bootstrap·사용자·source·execution 생성을 하지 않는다.

## 별도 터미널 실행

API 터미널에만 `HMI_DATABASE_DSN`을 주입한다. 만료 시각은 운영자가 정한 미래 ISO 시각과 timezone으로 지정한다. 아래 만료 예시는 실행 시점에 맞게 교체한다.

```sh
.venv/bin/python -m apps.api serve \
  --source-id source-local --stream-id events-main --gateway-id gateway-local \
  --capture-generation 1 --key-id gateway-key-1 \
  --token-file .local/service/gateway-token --expires-at 2026-11-01T00:00:00Z
```

API는 기본 `127.0.0.1:8000`에 loopback HTTP로만 바인딩한다. proxy headers는 신뢰하지 않는다. 서비스 토큰은 gateway/source/stream 하나에 고정되며 DB의 gateway·capture generation 등록과 다르면 시작을 거부한다. readiness는 토큰 인증과 schema 검사를 요구한다. `/health/live`는 프로세스 생존만 나타낸다. 원격 HTTPS 배포와 프록시 운영 구성은 이 로컬 실행기의 범위 밖이다.

게이트웨이 전용 터미널에는 PostgreSQL 비밀이 필요 없다. 개발 인증서는 [앱 B 인증서 준비](SIMULATOR_RUN.md)의 명시적 명령으로 먼저 생성하고 다음 경로를 사용한다.

```sh
.venv/bin/python -m apps.gateway serve \
  --gateway-id gateway-local --source-id source-local --stream-id events-main \
  --queue-file .local/gateway/outbound.sqlite3 \
  --opc-endpoint opc.tcp://127.0.0.1:4840/hmi-mes/ \
  --application-uri urn:hmi-mes:gateway:client \
  --server-application-uri urn:hmi-mes:simulator:server \
  --client-certificate .local/simulator/certs/gateway.der \
  --client-private-key .local/simulator/certs/gateway.pem \
  --trusted-server-certificate .local/simulator/certs/server.der \
  --api-url http://127.0.0.1:8000 --allow-loopback-http \
  --token-file .local/service/gateway-token
```

큐는 앱 B 상태 파일과 별도 파일이어야 한다. 다른 앱의 SQLite schema·변경된 gateway ID는 거부된다. 인증서 application URI와 서버 pin은 정확하게 검증한다. OPC 재연결과 API 전송은 독립적으로 진행하며 API 중단 중에도 수집한다. 원격 commit 여부가 불명확하면 기존 batch ID·본문을 보존해 재전송하고 인증 거부·충돌도 큐를 삭제하지 않는다. 종료 신호는 수집/전송을 멈추고 HTTP·OPC 연결과 SQLite 소유권 잠금을 회수한다. 다시 같은 큐로 시작한다.

## 선택적 명령 전달

위 게이트웨이 명령에 `--enable-commands`를 추가하면 기존 수집·업로드와 독립적으로 명령 claim/조회/보고 루프를 실행한다. 기본값은 비활성화이며 기존 실행 명령은 수집만 수행한다. 명령 활성화는 같은 보호 토큰·source binding·OPC 인증서 설정을 사용한다. 새로운 비밀 인자나 앱 B 저장 파일 경로는 받지 않는다.

명령 API를 사용할 DB에는 `0005_command_dispatch` 또는 이를 지원하는 후속 migration이 필요하다. 서버와 게이트웨이는 명령을 자동 생성하지 않는다. 사용자 승인과 권한 검증을 거친 API 명령 원장을 준비하는 절차는 [명령 설계](COMMANDS.md)를 따른다.

명령 루프는 같은 이벤트 루프의 OPC 클라이언트를 사용하며 한 번에 하나의 `run_once()`만 수행한다. 결과 보고 후에는 0.2초, 작업 없음·연결 차단·결과 불명·제출 보류 시에는 1초를 기다린다. 명령 API 지연이나 결과 불명 중에도 수집·batch 업로드 루프는 계속 진행한다. 수집기의 영구 식별자/저장소 오류가 발생하면 두 루프를 함께 멈춘다.

API가 SUBMIT을 반환하기 전에 원장에 결과 불명을 commit한다. 제출 응답 유실·보고 유실·취소 후에는 원래 ID의 QUERY로 확인하며 SubmitCommand를 자동 재실행하지 않는다. 로그의 `command/reported`는 원장 보고 확인이며 PLC 성공 판정이 아니다. TTL이 claim 중 경과하거나 health가 저하되면 제출하지 않고 API의 결과 불명 상태를 유지한다. 자세한 계약은 [게이트웨이 명령 전송](GATEWAY_COMMANDS.md)을 따른다.

SIGTERM/SIGINT는 수집기와 명령 루프를 취소하고 모두 종료될 때까지 기다린 후 두 HTTP 전송기·OPC 연결·큐 소유권을 정리한다. 재시작 시 같은 큐와 원본 command ID를 사용한다. 표준 상태 출력은 operation/state만 포함하며 payload·토큰·예외 내용을 기록하지 않는다.

```sh
.venv/bin/pytest -q tests/integration/test_gateway_command_launcher.py
```

이 실행 시험은 실제 별도 게이트웨이 CLI 프로세스와 OPC/HTTP socket으로 기본 비활성화·명시적 QUERY/보고·수집 API 중단 중 큐 보존·신호 종료·수집기 영구 오류 시 전체 종료와 잠금 회수를 검증한다. HTTP 명령 원장은 시험 경계로 제공하므로 PostgreSQL 명령 원장 통합 검증을 대신하지 않는다.

## 선택적 최종 실행 요약 조회

위 게이트웨이 실행 명령에 `--enable-settlement`를 추가하면 API가 제공하는 미정산 실행 목록을 원래 execution ID로 조회한다. 기본값은 비활성화이며 `--enable-commands`와 각각 또는 함께 선택할 수 있다. 같은 보호 토큰·source/stream·OPC 인증서를 사용하고 별도 HTTP 전송기를 구성한다. API와 DB에는 [정산 전송](SETTLEMENT_TRANSPORT.md)의 지원 schema와 source 귀속 등록이 필요하다.

조회 루프는 같은 이벤트 루프·OPC 클라이언트를 사용하면서 수집·업로드·명령 루프와 독립적으로 실행된다. 한 번에 한 페이지를 처리하고 다음 cursor를 유지한다. 페이지 처리 후 0.2초, 작업 없음·연결 차단·API 결과 불명 시 1초를 기다린다. API 요청이 지연되어도 다른 루프는 계속 동작하며 다음 요약 조회를 중복 실행하지 않는다.

실제 OPC UA `GetRunSummary`가 반환한 원래 ID·source·stream 일치와 `final=true`를 확인한 요약만 보고한다. 아직 활성 상태인 실행과 `RunNotFound`는 조회 보류이며 가짜 종료·수량·성공을 만들지 않는다. 요약 전송이나 ACK 유실 후에는 다음 순회에서 같은 원래 실행을 다시 조회한다. 이 기능은 `SubmitCommand`·END_RUN 등 제어 메서드를 호출하지 않는다.

`settlement/polled` 로그는 페이지를 조회했다는 뜻이다. 최종 요약의 commit ACK도 실제 사건과 정산 조건을 확인한 `SETTLED` 판정과 구분해야 한다. `PENDING`, `MISMATCH`, `CONFLICT`를 정상 정산 완료로 바꾸지 않는다. stdout 상태는 operation/state만 출력하며 요약 본문·비밀·예외 내용을 기록하지 않는다.

종료 신호 또는 수집기의 영구 오류는 활성화된 모든 루프를 취소하고 종료를 기다린 뒤 요약·명령·수집 HTTP 전송기, OPC 연결, SQLite 큐 소유권을 정리한다. 실행 시험은 실제 게이트웨이 프로세스·OPC/HTTP socket에서 명시적으로 미리 종료한 실행의 원본 최종 요약, 미존재 실행 보류, API/보고 장애 후 재조회, 세 루프 동시 동작, 지연된 요약 HTTP 요청 중 정상 종료를 검증한다. HTTP 원장은 시험 경계이므로 PostgreSQL 정산 통합 검증을 대신하지 않는다.

```sh
.venv/bin/python -m apps.api status --source-id source-local --stream-id events-main
.venv/bin/pytest -q tests/integration/test_m2_launchers.py tests/integration/test_gateway_pipeline.py
```

status는 검증 커서·수신 커서·미해결 gap 및 제어 제한 상태를 보여준다. 최근 수신값이나 통신 성공을 정산 완료로 해석하지 않는다. 실행 시험은 별도 CLI 프로세스 등록·binding 거부·비밀 파일 정책을 검증하고, pipeline 시험은 실제 OPC UA socket→게이트웨이 큐→HTTP socket→PostgreSQL 경로를 검증한다.
