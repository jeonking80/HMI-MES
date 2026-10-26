# 로컬 PC에서 공개 데모에 짧게 데이터 보내기

## 19차 현행 기준 (2026-10-26)

19차 사용자 승인으로 수집 원본 보관은 `HMI_INGEST_RETENTION_HOURS` 기본 168시간·최소 3시간을 사용한다. 3 미만·비숫자는 시작을 거절하고, 구 설정 `HMI_INGEST_RETENTION_DAYS`는 단독·동시·빈 값 모두 거절한다. 현재 runtime 자동 정리는 schema 0023에서만 실행하며 schema 0022의 보관 종료 조회·재전송 거절은 호환 지원한다. 아래 18차/CX-40의 일 단위·영구 개별 영수증 설명은 당시 이력이며 이 현행 기준으로 대체된다. 변경 이유와 영향은 [INGEST_RETENTION.md](INGEST_RETENTION.md)를 따른다.

공개 환경의 수집 원본 보관 목표는 Claude가 별도로 설정할 `HMI_INGEST_RETENTION_HOURS=3`이다. 공개 적용이 완료됐다고 이 문서에서 주장하지 않는다. 원시 트렌드 보관 설정과 별개이며 게이트웨이 원장 2시간보다 길다. 실제 공개 schema·권한·배포 상태는 Claude가 확인하고 `upgrade-0022-0023.json`과 `grants-0023.json`을 적용한다. 0023 이관 자체는 기존 데이터를 삭제하지 않는다. Codex 작업은 공개 DB 읽기 전용 범위이며 환경변수·운영 runtime을 변경하지 않는다.

원본 약 6.5KB·1Hz·3시간은 약 70MB·10,800 capture의 추정이다. 24시간 86,400 capture 가속 적재 및 vacuum 전후 크기·증분 실측은 대기 중이다. 보호된 원본·알람 FK 개별 영수증·업무 자료·인덱스와 bloat는 이 추정 밖이다. 최대 256개 정확 batch ID/GIN 청크는 O(captures) 바이트로 계속 증가하며, 매 capture 알람 판단의 FK 영수증도 남는다. 사용자는 **기존 참조 보존·한계 보고**를 확정했다. 무료 0.5GB나 장시간 정상 상태 용량을 보장하지 않는다. 아래 짧은 시연의 시작 전/후 읽기 전용 용량 확인과 시간 제한은 계속 필요하다.

이 문서는 [2차 지시서](../work-orders/CODEX_KULS_보강_v2.md)의 운영 기준을 따른다. 이하의 head17·게스트·원시 6시간/분 14일 설명은 OPS-01 당시의 운영 이력이다. 실제 공개 schema·보관 프로필은 Claude의 적용 기록과 읽기 전용 조회로 확인한다. 공개 사이트에 항상 연결된 gateway가 있다고 가정하지 않는다. 이 작업에서는 공개 DB·배포 설정·게이트웨이에 접속하거나 수집을 실행하지 않았다. 아래 시작 명령을 사용자가 실행하면 공개 API에 실제 원본 배치가 저장된다.

## 실행 전 조건

Linux PC 또는 Windows PC의 **WSL2 Linux**에서 Python 3.12와 프로젝트 의존성을 준비한다. Windows에서는 PowerShell의 `wsl`로 Linux 셸에 들어가고, 이후 Linux 명령을 그대로 사용한다. 현재 `plant_runtime.py`는 `fcntl`·POSIX 소유자 파일 검사·signal handler를 사용하므로 Windows 네이티브 Python 실행을 지원한다고 주장하지 않는다. 프로젝트와 비밀 파일은 WSL의 Linux 파일 시스템에 두며 `/mnt/c`의 NTFS 권한을 POSIX 0600으로 가정하지 않는다. 공개 HTTPS 인증서 검증을 끄지 않는다.

```powershell
wsl --distribution Ubuntu
```

```sh
cd ~/HMI-MES
uv sync --frozen
command -v timeout
.venv/bin/python --version
```

실행에 필요한 것은 **이미 공개 source와 이어지는 승인된 영속 runtime**이다. 담당 운영자가 기존 실행을 완전히 중지하고 SQLite checkpoint/백업 상태를 확인한 뒤, 두 SQLite와 인증서·개인 키·로그인 비밀 파일을 함께 안전하게 전달해야 한다. 복사 중인 살아 있는 DB나 원래 PC와 새 PC의 동시 실행을 허용하지 않는다. 이 문서는 현재 보호된 `.local/commissioning` 상태를 복사·수정·초기화하는 명령을 제공하지 않는다.

- `runtime.json`, `simulator.sqlite3`, `gateway.sqlite3`를 그대로 유지한다. gateway의 generation·capture sequence·미전송 배치를 새로 만들지 않는다.
- `certs/server.der`, `server.pem`, `gateway.der`, `gateway.pem`의 기존 상호 신뢰 관계를 유지한다. 인증서 만료/자격 증명 회전은 별도 운영 작업이다.
- `console-initial-password`, `console-password`는 시뮬레이터 로그인/최초 변경용 비밀 파일이다. 사용 중인 비밀번호를 임의로 바꾸거나 초기 파일로 되돌리지 않는다.
- 공개 gateway 서비스 토큰은 담당 운영자가 전달한 owner-only 파일에 둔다. 문서·명령 인자·Git·로그에 내용 자체를 넣지 않는다. 공개 UI의 사용자 비밀번호와 별개다.

예시 경로는 승인된 전용 사본을 가리킨다. 존재하지 않으면 **중지하고 운영자에게 영속 상태/등록을 요청**한다. 이 경로를 만들기 위해 `prepare --initialize`를 실행하면 안 된다.

```sh
export HMI_DEMO_RUNTIME="$HOME/hmi-demo/runtime"
export HMI_DEMO_TOKEN_FILE="$HMI_DEMO_RUNTIME/gateway-token"
.venv/bin/python deploy/plant_runtime.py --directory "$HMI_DEMO_RUNTIME" prepare
```

현재 패키지의 공개 scope는 gateway=`demo-packaging-gateway-v1`, source=`demo-packaging-source-v1`, stream=`events-main`, line=`packaging-line-1`이다. 기존 head17 등록과 일치하는 자격 증명·generation이어야 한다. 새 빈 gateway DB로 같은 scope의 capture sequence를 1부터 다시 보내면 원본과 충돌할 수 있다. 새 PC용 신규 source나 generation 전환은 담당 운영자의 별도 등록·검토가 필요하다. `/internal/v1/source-binding`의 패키지 정책은 head14만 허용하므로 **head17에서 자동 등록 수단으로 호출하지 않는다**. 현재 `plant_runtime.py serve`와 gateway CLI는 이 등록 호출을 자동으로 수행하지 않고 기존 등록에 배치를 보낸다.

## 용량 확인과 시작 허용 기준

DB 사용량은 기존 보호된 `HMI_DATABASE_DSN`을 환경에 주입한 운영자 셸에서만 읽는다. gateway PC에 DB 자격 증명까지 전달할 필요는 없다. DSN이 없는 시연자는 운영자에게 시작 전/후 결과만 요청한다. 환경 전체를 출력하거나 DSN을 셸 명령의 문자열로 직접 쓰지 않는다.

```sh
.venv/bin/python scripts/check-db-capacity.py > /tmp/hmi-capacity-before.json
```

명령은 `SET TRANSACTION READ ONLY`, 5초 statement timeout/1초 lock timeout 아래 `pg_database_size`와 public relation의 `pg_total_relation_size`, `pg_class.reltuples` 추정치만 읽는다. DSN·사용자·host·업무 행·비밀값은 출력하지 않는다. 행 수는 통계 추정치이며 미수집이면 null이다. DB 크기는 인덱스/TOAST와 기타 DB 공간을 포함하므로 relation 합과 같지 않을 수 있다. 다른 수집/정리/트래픽이 있으면 전후 차이를 이 시연의 정확한 단독 사용량으로 해석하지 않는다.

무료 한도를 보수적으로 **500,000,000바이트**로 계획할 때 권장 시작 예산은 DB 사용량 **350MB 이하**, 시연 1회 추가 **최대 25MB 계획 여유**, 종료 후 **최소 100MB 미사용 여유**다. 이는 운영자의 중지 기준이며 저장소가 보장하는 크기나 플랫폼 한도 정의가 아니다. 운영자는 실제 Neon 대시보드의 현재 요금제 한도·다른 데이터 증가와 비교하고, 예상/관측 값이 예산을 넘으면 시작하지 않는다. 350MB를 넘는 DB에 반복 시연을 자동 승인하지 않는다.

영속 gateway에 이전 미전송 배치가 있으면 새 1Hz 표본 외에 backlog가 한꺼번에 전송될 수 있다. 시작 전에 아래 **로컬 읽기 전용** 점검 결과를 운영자와 확인한다. backlog가 있으면 전송량·이전 원본 중복 여부를 별도로 예산에 반영하고, 검토 없이 시작하지 않는다. queue를 지워 용량을 맞추지 않는다.

```sh
.venv/bin/python - <<'PY'
import os, pathlib, sqlite3
path = pathlib.Path(os.environ['HMI_DEMO_RUNTIME']) / 'gateway.sqlite3'
with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
    print(dict(zip(('queued_batches', 'accounted_bytes'), db.execute(
        'SELECT count(*),coalesce(sum(accounted_bytes),0) FROM outbound_batches').fetchone())))
PY
```

## 300초 또는 600초로 한정해 시작·중지

시뮬레이터와 gateway는 같은 PC의 기존 인증서 OPC UA를 사용한다. gateway의 실제 `--api-url`은 아래 HTTPS 공개 주소이며 poll 기본값은 **1초**다. 명령/정산 worker도 기존 CLI 구성대로 활성화된다. 시연자는 별도 업무 UI에서 정상 권한·Origin·CSRF를 통과해 작업을 수행하며 이 시작 명령이 생산 START·복구 확인을 대신하지 않는다.

`deploy/plant_service.py`는 지속 hosted supervisor로서 자동 종료 시한이 없으므로 이 짧은 PC 시연의 시작 명령으로 사용하지 않는다. 기존 hosted 서비스를 새로 만들거나 상시 gateway를 켜지 않는다.

300초 예산에서는 295초 뒤 정상 종료 신호를 주고, 남은 5초 안에 끝나지 않으면 GNU timeout이 해당 명령의 프로세스 그룹을 종료한다. `--foreground`·`nohup`·백그라운드 daemon 옵션을 추가하지 않는다. 기존 supervisor는 자신이 시작한 gateway와 simulator만 중지하며 영속 상태를 보존한다. 기한 도달의 timeout 종료 코드 124 또는 강제 종료 시 137은 별도로 기록한다. 강제 종료 시 다음 시작 전 로컬 queue/복구 상태를 운영자가 다시 검토한다.

```sh
timeout --signal=TERM --kill-after=5s 295s \
  .venv/bin/python deploy/plant_runtime.py --directory "$HMI_DEMO_RUNTIME" serve \
  --token-file "$HMI_DEMO_TOKEN_FILE" --api-url https://hmi-mes.vercel.app
```

600초 예산은 위 명령의 `295s`만 **`595s`**로 바꾼다. 운영자가 용량 확인 없이 기간을 늘리지 않는다. 더 일찍 중지하려면 같은 전면 터미널에서 **Ctrl+C**를 누른다. `killall python`·PID 파일의 검토 없는 강제 종료·다른 PC의 기존 gateway 중지는 하지 않는다. 중지 뒤 `processes.json`이 남거나 기록 PID가 살아 있으면 재시작/삭제 대신 운영자에게 확인한다. 중지한 실행의 미정산 결과는 원래 ID로 확인하며 자동 재실행하지 않는다.

로컬 시뮬레이터 콘솔 주소는 기존 runtime.json의 console_port다(표준 준비 값은 `http://127.0.0.1:8011`). 최초 로그인 변경·다섯 복구 확인·REMOTE/AUTO 선택은 기존 콘솔에서 담당 작업자가 확인한다. 실제 운전·정산 시험을 끝낼 시간을 남기고, 상한에 걸려 강제 중지된 결과를 정산 완료로 표시하지 않는다.

종료 후 운영자 셸에서 같은 읽기 전용 명령을 다시 실행한다.

```sh
.venv/bin/python scripts/check-db-capacity.py > /tmp/hmi-capacity-after.json
```

## 예상 증가량과 보존 원칙

실측 원본 배치 약 6KB × 1Hz 가정은 300초 **1.8MB**, 600초 **3.6MB**, 하루 **518.4MB(약 0.5GB)**다. 이는 원본 배치만의 거친 추정이며 통신 재전송·물리 파일 확장·DB 전체 증가량을 보장하지 않는다.

9태그 raw 표본은 별도로 300초 2,700행, 600초 5,400행이다. 기존 0.8~1.5KB/행 가정을 적용하면 약 **2.16~4.05MB / 4.32~8.10MB**가 더 든다. 분 집계는 45/90행 수준으로 약 **0.02~0.03MB / 0.04~0.06MB**를 예상한다. 원본+raw+분의 부분 합은 약 **4.0~5.9MB / 8.0~11.8MB**다. 알람/생산/감사/인덱스·TOAST/bloat·정리 적체·향후 head18 proof/nonce 근거 증가는 이 부분 합 바깥이다. head18 시각 근거 행 크기는 아직 공개 최대 규모 실측이 없으므로 25MB 계획 여유에도 맞는지 전후 측정으로 검토한다. 이를 무료 0.5GB 수용 보장으로 해석하지 않는다.

기존 원시6시간·분14일 프로필 자체는 ingest_batches를 지우지 않았다. 2026-10-25 사용자 결정으로 공개 원시1시간은 Claude가 적용했다고 보고했으며,18차 schema22는 별도로 정산 완료 수집 원본·해당 시각 근거를 기본7일 뒤 정리한다. 1Hz를 유지하며 짧은 세션과 시작 전/후 용량 측정으로 원본 증가를 제한한다. 2초를 넘는 poll 지연은 freshness·heartbeat·제어/시간 구간의 불명 증가를 일으킬 수 있으므로 용량 절감 명령으로 제시하지 않는다. 생산 사건·실적·정산·알람·감사는 이 정리에서 삭제하지 않는다. 서버 원본/해당 clock proof 삭제는18차에 명시된 조건·기간·보관 종료 기록을 통과한 것만 허용한다. 압축/외부 archive는 무결성·조회·복구·비용 검토가 필요한 향후 설계이며 현재 구현된 용량 보장 수단이 아니다. 18차 범위 밖 원본/업무 증거 삭제는 승인된 것으로 확대하지 않는다. 오래된 실행은 종료된 원본으로 재검증할 수 없으며 API는 RETENTION_EXPIRED를 표시한다. 이 작업에서는 공개 DB를 직접 정리하거나 환경변수를 변경하지 않는다.

공개 head17에는 서로 다른 호스트의 측정 시간 근거가 아직 이관되지 않았다. OPC 데이터가 fresh/GOOD여도 분석 시간은 `TIME_UNKNOWN`일 수 있다. 공개에서 GOOD 시간이나 24시간 운영을 이 문서로 보장하지 않는다. head18 적용/활성화는 별도 OPS-02 운영 절차다.

## 18차 운영 변경 영향 (이력)

변경 이유는 무료 DB의 무기한 수집 원본 증가를 제한하기 위한 사용자 승인이다. 공개 적용 전 실제 schema를 확인하고 필요한 이관/grants를 순서대로 적용한다. 0022는 기존 자료를 지우지 않으며 배포된 서버가 조건을 통과한 과거 자료만 제한 함수로 정리한다. runtime은 원본 임의 DELETE 권한을 받지 않는다. HMI_INGEST_RETENTION_DAYS=7(최소1일)은 원시 트렌드1시간과 다르며 gateway 원장2시간보다 길다.

이 정책은7일의 원본 분량이나 영구 UUID/사건/감사 metadata가 무료0.5GB 안에 들어간다는 보장이 아니다. 기존 실측6KB/1Hz라면 원본7일만 약3.6GB다. 전후 DB 용량·적체·실제 수집 빈도·영구 식별 기록 증가를 따로 측정한다. 정리됐던 원본을 같은 ID로 다시 보내면 HTTP410 RETENTION_EXPIRED로 거절되고 gateway는 원래 대기열을 보존한 채 중단/명시적 복구를 요구한다. 새 ID·generation을 자동 생성해 회피하지 않는다.

원본·clock proof가 종료된 실행은 원본으로 다시 검증할 수 없다. 백업에는 compact 식별/보관 종료 기록을 포함한다. 상세 정책·API·롤백은 [INGEST_RETENTION.md](INGEST_RETENTION.md)를 따른다.
