# 별도 설비 앱 로컬 실행

이 실행기는 앱 B의 실제 SQLite 제어기·인증된 콘솔·암호화 OPC UA 서버를 하나의 별도 프로세스에서 실행한다. 앱 A HMI/MES의 완성이나 전체 T01~T12 통과를 의미하지 않는다. 모든 시작은 복구 필요 상태이며 자동으로 START하지 않는다.

## 준비

저장소 루트에서 `./scripts/bootstrap-python.sh`를 실행한다. 콘솔 서버는 고정된 uvicorn을 사용한다. 로컬 실행기는 loopback HTTP에만 바인딩한다. 원격 배포의 HTTPS·프록시·운영 인증서 배포는 별도 검증 대상이다.

```sh
mkdir -p .local/simulator
.venv/bin/python -m apps.simulator init-dev-certificates --directory .local/simulator/certs
```

이 명시적인 명령은 새 디렉터리에만 30일 유효한 개발용 자체 서명 인증서와 개인키를 만든다. 기존 인증서는 덮어쓰지 않는다. 서버 키는 앱 B, gateway 키는 앱 A 게이트웨이 전용으로 관리한다. 운영 CA·회전·폐기 체계의 대체가 아니다.

초기 비밀번호는 소유자만 읽을 수 있는 일반 파일(0600)로 준비한다. 명령 인자·셸 이력에 비밀번호 본문을 넣지 않는다. 아래 `/secure/initial-password`는 사용자가 준비한 실제 파일 경로로 바꾼다.

```sh
.venv/bin/python -m apps.simulator bootstrap-user \
  --database .local/simulator/state.sqlite3 \
  --origin http://127.0.0.1:8001 \
  --login operator --role simulator_operator \
  --password-file /secure/initial-password
```

기존 계정을 반복 초기화하지 않으며 일반 서버 시작은 계정을 만들지 않는다. 최초 로그인 후 비밀번호를 변경해야 제어할 수 있다. 초기 파일은 비밀 관리 절차에 따라 회수한다.

## 실행

```sh
.venv/bin/python -m apps.simulator serve \
  --database .local/simulator/state.sqlite3 \
  --origin http://127.0.0.1:8001 \
  --server-certificate .local/simulator/certs/server.der \
  --server-private-key .local/simulator/certs/server.pem \
  --trusted-client-certificate .local/simulator/certs/gateway.der
```

콘솔은 `http://127.0.0.1:8001`, OPC는 `opc.tcp://127.0.0.1:4840/hmi-mes/`다. 서버 application URI는 `urn:hmi-mes:simulator:server`, 제공된 gateway URI는 `urn:hmi-mes:gateway:client`다. 지정한 gateway 인증서만 신뢰하며 암호화되지 않은 OPC 세션이나 태그 쓰기를 허용하지 않는다. gateway의 브라우저 사용자 검증은 앱 A의 별도 책임이다.

콘솔에서 로그인·비밀번호 변경 후 저장 상태·물품 위치·명령을 확인하고 명시적으로 복구한다. LOCAL/AUTO에서 작업을 연결하고 예열 완료 후 시작한다. 같은 명령 ID의 결과를 추적하며 응답 불명 시 새 ID로 자동 반복하지 않는다. 종료 후 재공 0과 확정 요약을 확인하고 로컬 정산 확인 후 다음 작업을 연결할 수 있다. 로컬 확인은 MES 보고서 정산 완료를 주장하지 않는다.

100ms 공정 주기는 같은 이벤트 루프에서 실행된다. 콘솔·OPC 제어가 SQLite 트랜잭션 중간에 서로 섞이지 않는다. 종료 신호는 HTTP·OPC를 종료하고 SQLite 소유권 잠금을 회수한다. 재시작은 복구 게이트를 다시 설정한다.

## 선택적 사건 보관 정리

사건 정리는 기본적으로 꺼져 있다. 명시적으로 활성화하려면 기존 실행 인자에 `--enable-retention`을 추가한다.

```sh
.venv/bin/python -m apps.simulator serve \
  --database .local/simulator/state.sqlite3 \
  --origin http://127.0.0.1:8001 \
  --server-certificate .local/simulator/certs/server.der \
  --server-private-key .local/simulator/certs/server.pem \
  --trusted-client-certificate .local/simulator/certs/gateway.der \
  --enable-retention
```

활성화 시 별도 유지보수 작업이 같은 이벤트 루프에서 `controller.prune_events(limit=1000)`을 호출한다. 시작 시 한 번 확인한 뒤 각 확인이 끝난 시점부터 monotonic 60초 이상 기다린다. 밀린 확인을 연속 실행하지 않으며 SQLite를 별도 스레드에서 사용하지 않는다. 공정 주기와 유지보수 일정은 독립적이고, 실제 정리는 제어기와 저장소의 한 트랜잭션 안에서만 수행한다.

정리에는 현재 소유 프로세스의 monotonic 가동 시간 7일과 원본 사건의 영속 기록 나이 7일이 모두 필요하다. 프로세스 재시작은 7일 가동 대기를 다시 시작한다. UTC·monotonic 불일치는 정리만 동결하며 원본 수집을 정상 값으로 대체하지 않는다. 초기 가동 대기와 시계 동결은 전체 사건 이력 검사보다 먼저 확인한다. CLI에는 보관 나이·시계·가동 대기를 우회하는 인자가 없다.

복구 필요, 미종료 작업, 운전·고장·미정 명령, 초기 7일 대기 및 시계 동결은 삭제 없는 보류다. `RECOVERY_REQUIRED`, `STARTUP_HOLD`, `CLOCK_FROZEN` 등의 제한된 사유 코드만 기록하고 같은 보류 코드가 계속되면 중복 기록을 줄인다. 이 보류는 서버와 공정 주기를 종료하지 않는다. `StorageCorruption` 등 예상하지 못한 오류는 서버를 종료하며 복구를 자동 승인하지 않는다. 정상 종료 신호는 유지보수 작업을 취소한 뒤 HTTP·OPC와 저장소를 정리한다.

세부 정지·확정 요약·완료 사건 경계 검사와 정리 후 `RETENTION_GAP` 동작은 [제어기 보관 경계](CONTROLLER_RETENTION.md)를 따른다. gateway ACK는 로컬 7일 보관의 삭제 권한이 아니다.

## 검증 범위

`tests/integration/test_simulator_launcher.py`는 별도 실제 프로세스에서 초기 계정 등록·콘솔 인증·복구 필요 상태·OPC 포트·종료 후 잠금 회수를 검증한다. 실제 OPC 명령·생산·최종 수량은 [OPC 시험](SIMULATOR_OPCUA.md), HTTP 권한·작업 흐름은 [콘솔 시험](SIMULATOR_CONSOLE.md)을 따른다.

`tests/integration/test_simulator_maintenance.py`는 제어된 시계와 실제 SQLite로 60초 일정·초기 가동 대기·공정 주기 지속을 확인하고, 오류 주입으로 데이터 손상 시 서버 종료와 자원 회수를 확인한다. 실제 CLI 프로세스에서는 기본 비활성·명시적 활성화·복구 보류·SIGTERM·원본 이력 보존을 검증한다. 이는 실제 7일 연속 가동 측정이 아니다.

설비 고장·안전 상태에서 실제 히터 출력과 `HeaterOn` 태그는 공통 허용 조건을 사용한다. 전류는 `운전 중 1.2 + 0.1 × 속도`의 결정적인 시뮬레이션 값이며 실제 하드웨어 계측값이 아니다.
