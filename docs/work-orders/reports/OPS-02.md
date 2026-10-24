# OPS-02 — 공개 head18 이관 준비 및 운영 인계

- 기준: `4490a4c` (`430fbf6` 포함), `codex/kuls-backend-v3`.
- 준비 검증: 2026-10-23 08:13 UTC까지. **공개 이관·활성화는 미실행**이다.
- 공개 읽기 확인: `https://hmi-mes.vercel.app/api/ready` HTTP 200, `{"ready":true}`. 이 응답만으로 배포 SHA나 DB revision을 확인했다고 주장하지 않는다.
- 환경 상태의 credentials/identities 항목이 비어 있고 Neon·Vercel 관리 도구가 없다. `HMI_DATABASE_DSN`, `DATABASE_URL`, `NEON_API_KEY`, `VERCEL_TOKEN`, `HMI_SERVICE_TOKEN`은 환경변수에 설정되지 않았다. 비밀 파일을 탐색하거나 기존 보호 runtime의 자격 증명을 전용하지 않았다. 지시서 OPS-02.6에 따라 준비·검증 후 인계한다.

## 재현 준비물

```sh
.venv/bin/python scripts/render_neon_migration.py clock-upgrade > /tmp/hmi-v3-upgrade-0017-0018.json
.venv/bin/python scripts/render_neon_migration.py clock-grants > /tmp/hmi-v3-grants-0018.json
cmp /tmp/hmi-v3-upgrade-0017-0018.json deploy/neon-http/upgrade-0017-0018.json
cmp /tmp/hmi-v3-grants-0018.json deploy/neon-http/grants-0018.json
```

두 `cmp` exit 0. SHA-256:

| 파일 | SHA-256 |
|---|---|
| upgrade-0017-0018.json | `cf8fc04e08bdffe2d1241384f7d26e880eb68185927c4475f5aef43dcd656796` |
| grants-0018.json | `800dbd5ce9cdb183f2753e88e34cb02b64f0a278c5a9335d77019663fcab392b` |

각 파일은 SQL 문자열 배열이다. 파일 전체를 **각각 하나의 원자적 트랜잭션 요청**으로 실행한다. 문장별 독립 HTTP 요청이나 병렬 실행은 허용하지 않는다.

## 독립 high 검토

`gpt-6.1-sol high`가 이관의 exact head17·DB 검사, advisory transaction lock, migrator 소유권, 안전한 search_path, 불변 evidence 트리거, 최소 runtime 권한을 정적으로 검토했다. 문서에 명시된 정상 head17·schema drift 없음 전제에서 새 SQL 차단 결함을 발견하지 않았다. 공개 실행 검증을 대신하지 않는다.

운영 절차 보완:

- 전체 백업 정지 지점은 gateway만 중단해서 확보되지 않는다. API 명령·bootstrap·retention과 simulator의 writer도 멈춘다. PostgreSQL consistent dump 또는 동일 `READ ONLY REPEATABLE READ` 스냅샷의 논리 export와 테이블별 행 수를 대조하고, SQLite·설정과 같은 정지 지점을 보존한다. 일시적인 SQL session temp 데이터만 남기는 것은 복구 가능한 백업이 아니다. 비밀 포함 백업은 Git 밖의 접근 제한·암호화된 보관 위치를 사용한다.
- 권한 거절 대상은 `capture_clock_evidence`의 UPDATE/DELETE/TRUNCATE, probes의 TRUNCATE 및 `expires_at` 등 금지 열 UPDATE, CREATE/ALTER DDL이다. **probes DELETE와 consumed_batch_id/consumed_input_sha256/consumed_at UPDATE는 정상 허용 경로**다. 이를 거절하도록 권한을 줄이면 정상 소비·정리가 깨진다.
- 금지 문장마다 독립된 명시적 BEGIN/ROLLBACK을 사용한다. 한 실패 뒤의 `InFailedSqlTransaction`은 다음 권한 거절의 증거가 아니다. HTTP를 사용하면 BEGIN과 해당 문장·ROLLBACK이 같은 세션·트랜잭션 요청에 있어야 한다. 기존 실제 runtime 시험은 `tests/integration/test_clock_runtime_grants.py`를 따른다.

## Claude 운영 실행 순서

1. 공개 배포 SHA가 `430fbf6` 이상임을 확인하고 UTC 시각·ready·정확히 한 행의 `0017_guest_viewer`·시작 DB 크기를 기록한다.
2. 위 writer 정지와 일관된 전체 논리 백업, 같은 스냅샷의 테이블별 행 수 대조, 별도 복원 가능 경로를 확인한다. 기존 source/generation/sequence와 SQLite·자격 증명을 초기화하지 않는다.
3. 검토 커밋에서 두 파일을 다시 생성·cmp한 뒤 upgrade 전체를 한 트랜잭션으로, grants 전체를 별도 한 트랜잭션으로 적용한다. 실패하면 활성화를 중단한다. revision 전후·배치 결과·UTC 시각을 기록한다.
4. **실제 `hmi_app` 연결**로 신규 테이블 SELECT·정상 접수/프로브 소비를 검증하고 위 금지 문장을 각각 rollback 경계에서 실행한다. runtime 연결을 못 쓰면 이 항목은 미실행이다. 소유자 카탈로그 조회나 SET ROLE 실패를 대체 통과로 적지 않는다.
5. Vercel `HMI_CLOCK_MEASURED_ROUND_TRIP=true`를 설정·재배포한다. ready·기존 로그인·게스트·분석·실제 챗봇 응답 1회를 확인한다. 다른 기존 설정은 유지한다.
6. [공개 시연 절차](../../architecture/PUBLIC_DEMO_RUN.md)의 같은 영속 PC gateway를 `--clock-measured-round-trip`으로 짧게 연결한다. 새 캡처 GOOD·RUNNING > 0·UNKNOWN 근거·원래 execution 수량 대조·큐·전후 DB 크기를 기록한다. 무료 0.5GB 한도를 넘기지 않는다.
7. 오류 시 시연을 정상 종료하고 API/gateway opt-in을 해제한다. 커밋된 근거를 삭제하거나 downgrade로 우회하지 않는다.

| 공개 실행 항목 | 현재 결과 |
|---|---|
| 배포 SHA / revision 전후 | 미확인 / 미실행 |
| 일관된 전체 백업 / 행 수 대조 | 미실행 |
| upgrade / grants 공개 배치 | 미실행 / 미실행 |
| 실제 공개 runtime 권한 검증 | 미실행 |
| Vercel 활성화·재배포·챗봇 1회 | 미실행 |
| 공개 실연 GOOD·가동·수량 | 미실행 |
| 전후 DB 용량 | 미측정 |

로컬 준비물 검증과 정적 위험 검토만 통과했다. 공개 배포 완료나 프로젝트 완료 판정은 하지 않는다.

## 재개 시점 재확인 — 2026-10-23 11:21 UTC

사용자가 8시간 시험을 제외한 미완료 항목 전체 재개를 지시했다. 최신 main `51f2383`을 병합한 `a3da06141aa2a6e9da5e77398fc58c4fdd437500`에서 두 JSON을 다시 생성해 각각 `cmp` exit0을 확인했고 SHA-256은 위 표와 동일했다. 공개 `/api/ready`는 HTTP200 `{"ready":true}`였으나 배포 SHA·DB revision의 증거로 사용하지 않는다.

현재 환경도 관리 도구·secret/runtime/identity binding이 없고, 위 다섯 named 환경변수의 설정 여부는 모두 false다. 공개 연결 문자열을 비밀 파일에서 탐색하거나 보호된 실습 DB 자격 증명을 대신 사용하지 않았다. 공개 backup/migration/runtime DML/Vercel 재배포·실연은 여전히 미실행이다. 작업지시서 OPS-02.6에 따라 검증된 준비물과 위 운영 실행 순서를 Claude에게 인계한다. 이는 공개 적용 통과 판정이 아니다.
## 공개 적용 결과 (Claude, 2026-10-23)

Codex 인계 후 Claude가 HTTPS `/sql` 배치로 실행했다. 이 환경은 Neon에 TCP로 접속할 수 없다.

| 단계 | 결과 |
|---|---|
| 선행 조건 | Vercel production `a205c6a` READY, `/api/ready` ready, `alembic_version` 1행 `0017_guest_viewer` |
| 준비물 동일성 | 같은 커밋에서 `clock-upgrade`·`clock-grants`를 다시 생성해 커밋된 JSON과 `cmp` 동일 |
| 백업 | 이관 직전 전체 논리 백업: 48개 테이블, 3,923행, 테이블별 행 수 DB와 일치, 33MB. 비밀값이 있어 저장소에 넣지 않고 작업 세션 임시 폴더에만 보관(세션 종료 시 사라질 수 있음) |
| 이관 0017→0018 | 10:57:31Z `upgrade-0017-0018.json` 20문장 한 트랜잭션 성공 |
| 권한 | 10:57:35Z `grants-0018.json` 10문장 한 트랜잭션 성공. 카탈로그 확인: `capture_clock_evidence` SELECT·INSERT만, `capture_clock_probes` SELECT·INSERT·DELETE와 열 단위 UPDATE, 둘 다 TRUNCATE 없음 |
| runtime 금지 DML 실측 | **미실행.** `hmi_app` 연결 시크릿이 `invalid header`로 거절되고, 소유자는 `hmi_app`으로 SET ROLE할 수 없다. 카탈로그 확인으로 대신하지 않는다. |
| 이관 후 앱 | `/api/ready` ready |
| 활성화 | Vercel production `HMI_CLOCK_MEASURED_ROUND_TRIP=true` 설정 후 재배포 READY(10:59:01Z) |
| 활성화 후 확인 | `/api/health` alive, `/api/ready` ready, `/auth/config` guest·assistant·analytics true. 게스트 진입 200(viewer), 조회 200(대시보드 1.7초, 트렌드 0.6초, 실행 0.3초, 일일 1.5초), 챗봇 1회 200(16.2초, 근거: 라인 스냅샷), 게스트 쓰기(비밀번호 변경) 403 |
| 공개 실연 GOOD·가동·수량 | **미실행.** 공개 source와 이어진 영속 runtime(게이트웨이·시뮬레이터 SQLite, 인증서, 서비스 토큰)이 이 환경에 없다. [PUBLIC_DEMO_RUN.md](../../architecture/PUBLIC_DEMO_RUN.md)대로 기존 PC에서 `--clock-measured-round-trip`으로 실행해야 한다. |
| DB 용량 | 이관 전 33MB, 활성화 후 33 MB(실연 전) |

새 캡처가 들어오기 전까지 공개 화면의 기존 데이터는 계속 "시간 불명"이다. 측정 이전 데이터를 GOOD으로 소급하지 않는다. 공개 실연과 runtime 실측이 끝나기 전에는 OPS-02를 완료로 표시하지 않는다.

## 사용자 등록 후 실행환경 점검 — 2026-10-24 00:08 KST

사용자가 백업·0018 이관·실제 권한 검사·재배포·공개 실연을 명시적으로 요청했다. 현재 프로세스에서 값의 존재 여부만 확인한 결과 `HMI_MIGRATION_DSN`, `VERCEL_TOKEN`, `NEON_API_KEY`는 존재하고 **`HMI_DATABASE_DSN`은 존재하지 않는다**. 비밀값은 출력하거나 문서에 저장하지 않았다.

환경 상태는 spec/observed revision 5, observations_current=true이지만, 구성된 시크릿과 이관용 런타임 변수의 개별 state는 모두 unknown이다. 이 상태만으로 자격 증명 사용 가능 또는 실제 API 권한 확보를 판정하지 않았다. 현재 실행환경의 runtime_variables에는 이관용 변수만 나타난다. 저장된 구성에는 두 DSN 요구사항이 모두 있어도, 현재 프로세스에 앱 연결이 적용됐다는 증거는 아니다. 실행기의 TCP 허용 domain/IP 목록도 비어 있다.

실제 `hmi_app` 연결을 사용한 권한 검사의 선행 조건이 충족되지 않아 공개 작업은 시작하지 않았다. 사용자의 실패 시 중단 지시에 따라 백업·이관·권한 SQL·재배포·실연은 모두 미실행으로 유지한다. 외부 API 인증 요청, 운영 DB 변경, 서비스 재시작, 기존 실패 시험 재실행과 8시간 시험도 하지 않았다. 앱 DSN을 현재 작업 실행환경에 적용한 뒤 재개 시점에 존재 여부와 실제 연결/네트워크 경로부터 다시 확인해야 한다.

## 재실행 — 환경 revision 7

사용자의 재실행 지시에 따라 확인했다. 현재 프로세스에는 두 DSN과 두 관리 API 토큰이 모두 존재한다. 이로써 앞선 앱 변수 누락은 해소됐다. 단, 환경 상태 도구의 개별 자격 증명 state는 여전히 unknown이다.

DSN을 메모리에서 파싱한 결과 DB는 모두 `hmi_mes_demo`, 사용자는 각각 `hmi_migrator`와 `hmi_app`이었다. 이관용은 비풀링 호스트이나 URL password 구성요소가 없고, 앱용은 풀링 호스트이며 password 구성요소가 있다. 비밀번호·전체 DSN·토큰은 출력하지 않았다. 실제 DB 로그인은 실행하지 않았으므로 어느 연결도 인증 성공으로 판정하지 않는다.

상속된 HTTPS 프록시 및 TLS 검증을 유지한 첫 Neon 관리 API `GET /v2/projects` 요청이 `urllib.error.URLError`로 실패했다. 예외 종류만 기록했으며 하위 원인은 수집하지 않아 TLS·네트워크·인증 중 하나로 단정할 수 없다. 사용자 지시에 따라 재시도 및 뒤이은 Vercel 요청을 실행하지 않았다. 백업·0018 이관·실제 runtime 권한 검사·재배포·공개 실연은 모두 미실행이며 운영 변경은 없다. 재개 전 이관용 연결 문자열의 비밀번호 포함 여부와 API 연결 경로를 확인해야 한다.

## 연결 문자열 수정 후 공개 작업 재개 시도

사용자가 이관용 비밀번호·DB 이름·풀링 설정을 수정한 뒤 진행을 지시했다. 직전 형식 점검에서 이관용은 `hmi_migrator` / `hmi_mes_demo` / 비밀번호 있음 / 비풀링, 앱용은 `hmi_app` / `hmi_mes_demo` / 비밀번호 있음 / 풀링이었다. 앞선 비밀번호 누락·잘못된 DB·풀링 설정 문제는 현재 문자열 점검에서 해소됐으나 실제 DB 인증 성공과는 구분한다.

상속된 프록시와 TLS 검증을 유지해 Neon 관리 API `GET /v2/projects`를 한 번 실행했다. 결과는 `URLError`, 하위 예외는 `OSError`이며 errno는 없었다. `SSLCertVerificationError`나 `socket.gaierror`로 분류되지 않았지만, 이것이 TLS 또는 DNS 정상의 증거는 아니다. HTTP 상태나 실제 API 응답을 얻지 못해 토큰의 유효성도 미검증이다. 예외 전체에는 비밀값이 포함될 수 있어 출력하지 않았다. API 접근 경로가 현재 차단 항목이며, 자격 증명 자체가 잘못됐다고 단정하지 않는다.

첫 실패 후 재시도하지 않고 중단했다. 공개 DB 접속·전체 백업·0018 이관·권한 실측·Vercel 요청 및 재배포·공개 실연은 모두 미실행이고 운영 변경은 없다. 8시간 시험과 기존 실패 시험도 실행하지 않았다. 다음 재개에서는 DB 비밀번호를 다시 변경하기보다 환경의 관리 API/프록시 연결 오류 원인을 먼저 진단해야 한다.

## 사용자 요청에 따른 연결 오류 원인 진단

사용자가 원인 진단을 지시해 읽기 전용 네트워크 확인을 수행했다. 상속된 HTTP/HTTPS 프록시는 `proxy:8080`이고 지정 CA 파일은 존재한다. 기존 경로의 Neon API 요청 상세 예외는 `Tunnel connection failed: 403 Forbidden`이었다. 이어 같은 프록시에 자격 증명 없이 `CONNECT api.neon.tech:443`를 요청해 응답 원문에서 `HTTP/1.1 403 Forbidden`, 본문 **`No approved upstream IPv4 address`**를 확인했다. 이는 차단을 우회하는 직접 외부 접속이 아니라 동일한 프록시의 거절 응답 확인이다.

따라서 이번 실패 지점은 Neon 인증 이전의 Codex 프록시 목적지 IPv4 승인 단계다. 연결 문자열이나 Neon 토큰 오류의 증거가 아니므로 비밀번호 재설정으로 해결된다고 안내하지 않는다. 환경의 원하는 허용 도메인 목록에는 `api.neon.tech`가 이미 있으나 실제 네트워크 적용 상태는 unknown이고 세부 network_diagnostics는 제공되지 않았다. 승인 IP가 없는 세부 이유(DNS 결과·IP 정책·설정 적용 불일치)는 이 응답만으로 확정하지 않는다. 지원되는 환경 네트워크 설정/플랫폼 경로에서 이 불일치를 해결해야 하며 프록시 우회·임의 IP 대체·TLS 검증 비활성화는 하지 않았다. 운영 DB와 배포는 변경하지 않았다.

## 공개 실연 재개 결과 — 2026-10-24 01:19 KST

사용자 지시에 따라 최신 main `bb45d01`을 `107ed81`로 병합하고 Claude의 위 공개 적용 결과를 보존했다. 백업·0018 이관·권한 변경·Vercel 설정 변경·재배포는 반복하지 않았다. DB 용량과 결과 확인은 Claude 담당으로 남겼다.

기존 `.local/commissioning/runtime`에서 원래 gateway·simulator SQLite와 인증서, 별도 connection 디렉터리의 보호 토큰 파일을 확인했다. 기존 공개 연결 이력은 DEVELOPMENT_STATUS에 있으며 runtime의 source/stream/gateway/line 식별자 검증도 통과했다. 토큰은 소유자 전용 0600 파일이고 감독 잠금은 비어 있었으며 processes.json은 없었다. 시작 전 미전송 큐는 1건·15,074바이트였다. 공개 `/api/ready`는 HTTP200 ready=true였다. 신규 runtime·source·generation·인증서를 생성하거나 기존 큐를 삭제하지 않았다.

문서의 295초 제한 명령에 `--clock-measured-round-trip`을 지정해 실행했다. 시작은 2026-10-23T16:19:21.933747Z, 종료는 16:19:25.898337Z(한국시간 10월 24일 01:19), 종료 코드는 1이었다. 감독 로그에 simulator와 gateway 시작 뒤 gateway의 command/settlement/capture blocked와 gateway exited code=1이 기록됐다. CLI는 상세 예외 대신 일반 오류만 출력하므로 정확한 내부 원인은 미확인이다. 5분 운전·GOOD 트렌드·가동 시간·수량 대조 성공으로 판정하지 않는다.

사용자의 실패 시 중단 지시에 따라 재시도·복구 조작은 하지 않았다. 감독자의 정상 정리 후 두 자식 PID가 존재하지 않고 processes.json도 없음을 확인했다. 영속 SQLite·인증서·토큰은 보존했다. 비밀이 들어갈 수 있는 실행 로그는 Git 밖의 보호 경로 `/tmp/hmi-ops02-public-demo` 및 기존 runtime의 supervisor.log에 남겼다. 공개 수집 요청의 커밋 여부·DB 결과와 용량은 확인하지 않았으며 Claude에게 인계한다. 8시간 시험은 실행하지 않았다.

## 실연 실패 읽기 전용 조사 — 사용자 지정 범위

재실행·복구 조작·runtime 수정 없이 기존 로그와 두 SQLite를 읽었다. SQLite는 `mode=ro`, `PRAGMA query_only=ON`으로 열었다. 사용자의 명시적 결과 기록 요청에 따라 이 보고서만 수정했으며 프로그램·설정·상태 파일은 수정하지 않았다. 독립 medium 에이전트는 로그만 읽어 같은 결과를 확인했다.

### 1. 종료 직전 로그의 증거와 한계

`supervisor.log` 22707~22711행은 gateway 시작, command/settlement/capture의 blocked, gateway exited code=1만 기록한다. 22712행은 감독자 종료와 영속 scope 보존이다. `/tmp/hmi-ops02-public-demo/supervisor-output.log`에는 `Runtime operation failed; inspect private configuration and existing state.`만 있다. 두 로그에 `SourceCursorError`, `CURSOR_AHEAD`, `STREAM_MISMATCH`, `BufferCorruption` 또는 traceback은 없다.

정적 코드 확인에서 gateway CLI는 예외 본문을 출력하지 않고 일반 종료 문구만 출력하며, plant 감독자의 `sanitized_line`은 구조화된 상태를 operation/state만 남겨 오류 클래스도 버린다. 따라서 **기존 로그로 실제 종료 오류 클래스·메시지를 복원할 수 없으며 SourceCursorError와 BufferCorruption을 구분할 수 없다.** blocked만으로 어느 예외라고 단정하지 않는다. 재현 실행이나 로깅 수정은 하지 않았다.

### 2~3. 로컬 영속 상태 조회 결과

| 대상 | 항목 | 읽은 값 |
|---|---|---|
| 시뮬레이터 | source_id / stream_id | demo-packaging-source-v1 / events-main |
| 시뮬레이터 | simulator_identity.recovery_required | 1 |
| 시뮬레이터 | runtime_state.snapshot_json.recovery_required | true |
| 시뮬레이터 | simulator_identity.last_seq | 4 |
| 시뮬레이터 | retained_from_seq | 1 |
| 시뮬레이터 | 실제 source_events 건수 / 첫·마지막 seq | 4건 / 1~4 |
| 게이트웨이 | source_progress source_id / stream_id | demo-packaging-source-v1 / events-main |
| 게이트웨이 | source_progress.latest_read_seq | 3 |
| 게이트웨이 | gateway_identity.last_capture_seq | 4267 |

### 4. 비교 판정

사용자가 제공한 공개 DB 상태는 verified_through_seq=3, max capture_seq=4267, gap=0이다. 이 조사에서는 공개 DB에 접속하지 않았으며 이 값은 독립 실측이 아닌 인계 정보다. 게이트웨이 사건 커서 3과 capture 4267은 각각 해당 공개 값과 일치한다. 시뮬레이터는 1~4를 보존하고 사건 head가 4이므로 게이트웨이 커서 3이 원본 head보다 앞서지 않으며, 다음 사건 4도 보존 범위 안이다. 로컬 source·stream도 서로 일치한다. 따라서 **현재 조회값에서는 CURSOR_AHEAD나 로컬 STREAM_MISMATCH를 뒷받침하는 증거가 없다.** 이는 실패 순간의 예외를 배제하는 판정은 아니다.

recovery_required=true는 확인된 상태지만 이것만으로 gateway 종료의 직접 원인이라고 판정하지 않는다. BufferCorruption도 로그에 증거가 없으며 이번 조회는 전체 버퍼 무결성 검증이 아니므로 배제하지 않는다. 정확한 오류 클래스는 미확인으로 유지한다. 사용자 지시에 따라 이 기록 후 멈추며 재시작·큐 수정·커서 초기화·복구 승인·추가 시험을 하지 않는다.

## 클래스 진단 보강 및 사용자 승인 실연 1회 — 2026-10-24 01:34 KST

사용자가 오류 클래스 보강과 단일 재실연, 실행 중 commission 복구 확인 항목을 명시적으로 승인했다. gateway CLI의 최종 stderr와 `_run_workers` 상태 출력, 감독자 `sanitized_line`에 오류 클래스 이름을 보존했다. ASCII 식별자 `[A-Za-z_][A-Za-z0-9_]{0,127}`만 허용하고 메시지 본문·추가 필드·잘못된 클래스 이름은 제외한다. supervisor.log에 최종 종료 클래스를 남기기 위해 gateway/stopped 상태도 허용했다. 단위 시험은 신규 2개를 한 번 실행해 2개 통과했으며 변경 Python 3개 파일의 Ruff check 및 format --check를 각각 한 번 실행해 통과했다. 테스트 실패에 따른 반복 실행은 없다.

실연은 기존 영속 runtime으로 `serve --clock-measured-round-trip`을 295초 상한으로 한 번 시작했다. 로컬 콘솔 준비 직후 `plant-hosting.md`의 기존 console-password 파일과 승인된 다섯 확인 인자를 사용해 commission을 한 번 실행했다. **commission exit0**, `Runtime operation completed; no production command submitted.`를 확인했다. 새 작업이나 생산 START는 제출하지 않았다.

| 항목 | 실측 결과 |
|---|---|
| 시작 UTC / 한국시간 | 2026-10-23T16:34:45.905289Z / 2026-10-24 01:34:45 KST |
| 종료 UTC / 한국시간 | 2026-10-23T16:34:53.024598Z / 2026-10-24 01:34:53 KST |
| 경과 | 7.119초 |
| serve / commission 횟수 | 각각 1회 |
| commission 결과 | exit0 |
| 최종 gateway 오류 클래스 | **BufferFull** |
| 종료 전 capture 오류 클래스 | GatewayReadError 이후 BufferFull |
| serve 결과 | exit1, 5분 운전 실패 |

추가된 감독 로그의 마지막 오류는 `{"operation":"gateway","state":"stopped","error":"BufferFull"}`다. 이번 종료 클래스는 SourceCursorError나 BufferCorruption이 아닌 **BufferFull**로 확인됐다. 메시지 본문은 의도적으로 저장하지 않았으므로 어떤 한도/내부 조건이 발동했는지는 확정하지 않는다. 초기 GatewayReadError와 최종 BufferFull 사이의 인과관계도 별도 추정하지 않는다.

허용된 단일 시도 후 추가 재시도·복구·큐 삭제·커서 조작을 하지 않았다. 감독자 종료 후 두 자식 PID가 없어졌고 processes.json도 없음을 확인했다. 원래 영속 상태는 보존했다. 로그·실행 메타데이터는 Git 밖 보호 경로 `/tmp/hmi-ops02-public-demo-retry1`과 기존 supervisor.log에 있다. DB/권한/배포 설정을 변경하지 않았고 DB 결과·용량은 조회하지 않았다. GOOD·가동 시간·수량 대조·5분 실연은 통과로 표시하지 않으며 이 기록 후 중단한다.

## 6차 지시서: 메타데이터 한도 조사 및 수정 준비 결과

Claude 브랜치의 `CODEX_KULS_보강_v6.md`를 읽고 최신 origin/main을 fetch·병합 확인했다(Already up to date). 기존 미커밋 브라우저 시험 수정은 별도 보존 후 그대로 복구했으며 이번 작업에서 수정·실행하지 않았다.

### 읽기 전용 사본 조사 1회

원본 gateway.sqlite3를 읽기 전용으로 열어 SQLite backup API로 Git 밖 `/tmp/hmi-ops02-v6-diagnosis/gateway-snapshot.sqlite3` 사본을 만들고, 사본을 읽기 전용으로 한 번 열어 다음 값만 조회했다. 운영 원본에 정리·초기화·상태 변경을 하지 않았다.

| 테이블 | 행 수 | sum(metadata_bytes) |
|---|---:|---:|
| batch_ledger | 4,267 | 4,335,235 |
| source_progress | 1 | 652 |
| missing_ranges | 453 | 5,664,074 |
| loss_intervals | 0 | 0 |
| 합계 | — | **9,999,961** |

원장 상태는 ACKNOWLEDGED 3,813건, EVICTED 453건, QUEUED 1건이다. outbound_batches는 PENDING 1건이며 queued_at=`2026-10-23T02:03:35.145959+00:00`, accounted_bytes=15,074이다. 기본 메타데이터 한도 10,000,000바이트까지 **39바이트**만 남았다. 전송 acknowledge 경로는 ACK JSON을 원장에 더한 뒤 예산을 검사하므로 이 ACK의 저장 비용이 여유를 초과하는 `METADATA_FULL` 경로가 코드·저장 상태 비교로 확인됐다. 과거 로그에 사유 접두어가 실제 기록돼 있었다고 주장하지 않는다. 새 사유 코드의 운영 로그 검증은 미실행이다.

### 최소 수정과 데이터 보존 검토

- BufferFull의 콜론 앞 고정 대문자 식별자만 `error_code`로 worker 상태·최종 stderr·감독 로그에 전달한다. 다른 예외 본문·잘못된 코드·추가 메시지는 출력하지 않는다.
- ACK 원장 행을 지우면 영구 batch ID 충돌 검사와 원래 ACK 중복 응답 근거가 사라진다는 독립 high 검토 결과에 따라 **행 삭제 대신 확정 ACK JSON의 무손실 압축**으로 저장 표현을 줄였다. 원장 ID·내용/봉투 해시·capture 연속성·원래 ACK 전체 바이트를 보존한다.
- 메타데이터 압력 시 같은 ACK 트랜잭션 안에서만 압축한다. 서버 verified 범위 내의 확정 영수증만 대상으로 하며, 미확정·격리·gap 영수증과 missing_ranges/loss_intervals가 참조하는 대조 영수증은 건드리지 않는다. 한도는 늘리지 않았고 원장·유실 근거는 삭제하지 않는다. 새 ACK와 진행 커서·압축 결과는 함께 커밋 또는 롤백된다.
- 버전 표식과 크기 제한을 둔 압축 복원 경로를 사용하고, 기존 TEXT ACK도 계속 읽는다. 변조된 압축·미완결 스트림·후행 데이터는 거절한다. 운영 SQLite에 새 표현을 적용한 것은 아니며 실제 절감량·5분 지속 가능성은 아직 검증하지 않았다.

### 단일 검증 결과 및 중단

신규 용량 회귀 시험은 1개이며 기존 진단 시험 2개를 사유 코드 검증으로 확장했다. 신규 회귀는 39바이트 여유 상황에서 확정분 압축 후 ACK 성공, 미확정·유실 근거 보존, 커밋 직전 오류의 원자적 롤백, 정확한 원래 영수증 복원·중복 ACK·변조 및 ID 재사용 거절·재개방을 검증한다.

한 번의 pytest 실행으로 진단·신규 용량 회귀·기존 SQLite 버퍼 검사를 함께 실행했다: **37 passed in 1.49s**. Ruff check 한 번은 **실패**했다. `tests/unit/test_gateway_diagnostics.py:50` E501(102자), 같은 파일 69행 B023(반복문 stop 변수 캡처) 두 건이다. 연결된 format --check는 앞선 lint 실패로 실행되지 않았다. 구현 중 버퍼 담당자는 자신의 두 파일에 formatter를 한 번 적용했고, 앞선 uv 호출은 읽기 전용 홈 캐시로 실패했다. 검사 재실행이나 실패 후 수정을 하지 않았다.

지시서의 실패 시 중단 규칙에 따라 여기서 멈췄다. 공개 실연은 별도 사용자 승인 이전이며 실행하지 않았다. 운영 큐 정리·복구·시작, DB/Vercel 변경, 8시간 시험은 모두 하지 않았다. 전체 수정 통과나 OPS-02 완료로 판정하지 않는다.

### 사용자 승인 후 Ruff 수정·재검사

사용자가 위 Ruff 두 건의 수정·재검사를 승인했다. 진단 시험 파일에서 긴 dict를 여러 줄로 정리하고, 반복문 내부 비동기 함수의 stop 이벤트를 기본 인자로 고정했다. 제품 코드와 운영 파일은 추가 변경하지 않았다.

관련 세 시험 파일을 한 번 재검사해 **37 passed in 1.45s**, 변경 Python 6개 파일의 Ruff check와 format --check도 한 번씩 실행해 **모두 통과**했다. 운영 SQLite 추가 조회·정리·복구·공개 실연은 하지 않았다. 지시서의 별도 실연 승인 조건에 따라 공개 5분 실연은 아직 승인 대기이며 OPS-02 전체 완료는 아니다.

### 6차 수정 후 사용자 승인 공개 실연 1회 — 2026-10-24

사용자가 위 공개 5분 실연을 승인했다. 코드 `8ae8c98`의 기존 commissioning runtime·SQLite·인증서·토큰을 그대로 사용해 `serve --clock-measured-round-trip`을 한 번 실행했다. 콘솔 준비 후 승인된 다섯 확인 항목과 기존 console-password 파일로 commission을 한 번 실행해 exit0을 확인했다. 새 작업·생산 START는 제출하지 않았다. 백업·이관·권한 변경·재배포는 반복하지 않았다.

| 항목 | 실측 결과 |
|---|---|
| 시작 | 2026-10-24T07:05:12.400184Z / 한국시간 16:05:12 |
| 종료 | 2026-10-24T07:10:08.289396Z / 한국시간 16:10:08 |
| 경과 | **295.889초** |
| serve / commission 횟수 | 각 1회 |
| commission | exit0 |
| serve 종료 | **124: GNU timeout의 295초 상한 TERM** |
| 새 capture collected | **290회** |
| 새 delivery committed | **290회** |
| METADATA_FULL / 최종 gateway 오류 종료 | 기록 없음 / 기록 없음 |
| 종료 정리 | supervisor stopped; persistent scope retained, 두 자식 PID 없음, processes.json 없음 |

새 로그는 시작 오프셋 1,203,567 이후만 집계했고 독립 medium 관찰자도 같은 수치를 확인했다. blocked는 capture 4회·command 1회·settlement 1회였다. capture 오류 클래스/코드는 GatewayReadError 1회, **BufferFull / INFLIGHT_PRESSURE 3회**이며 이후 수집·COMMITTED가 계속됐다. 이를 오류 0회로 표시하지 않는다. 이번 전체 실행의 재시작·수동 재시도는 없었고 supervisor는 상한까지 살아 있다가 정상 종료 신호로 자식을 정리했다. 큐 삭제·커서 초기화·source/generation 교체·추가 복구 조작은 하지 않았다.

**판정: 약 5분의 공개 수집·전송 지속과 종료를 확인했다.** 이전처럼 METADATA_FULL로 즉시 종료되는 현상은 이번 실행에서 재발하지 않았다. 단, 로그의 COMMITTED는 새 캡처 GOOD 트렌드·가동 시간 >0·불명 근거·원래 실행 수량 대조를 대신하지 않는다. 이번에는 생산 START를 하지 않았으며 해당 업무 지표와 전후 공개 DB 용량은 조회하지 않았다. 사용자가 지정한 대로 Claude가 공개 결과·용량·화면을 확인할 항목으로 인계한다. 따라서 OPS-02 전체 수락 완료는 아직 판정하지 않는다.

보호된 원본 메타데이터와 출력은 `/tmp/hmi-ops02-v6-public-demo-once`, 상태 로그는 기존 runtime의 supervisor.log에 보존했다. 이 보고서 기록 외에 추가 시험·실연 반복·운영 변경은 하지 않았다.

## 7차 실연 — 생산 실행 연결 전 중단

Claude 브랜치의 `CODEX_KULS_보강_v7.md`(`4c64649`)를 확인하고 최신 main 반영 상태를 확인했다. 사용자에게 생산 조작 대행 승인을 받았고, 모바일 설정 제약으로 **공개 DB 실행 전후 조회·용량 확인은 Claude 담당으로 유지**하는 범위 변경도 명시적으로 승인받았다. 따라서 이번 작업에서 SQL·이관·권한 변경·배포는 실행하지 않았고, 공개 DB 행 수·용량을 새로 실측했다고 표시하지 않는다.

사용자가 제공한 운영계정 비밀번호는 숨김 표준입력으로 메모리에서만 사용했다. 코드·문서·환경변수·파일·실행 로그에 저장하거나 출력하지 않았다. 정상 Origin·로그인 challenge·CSRF·세션 쿠키를 거쳐 demo의 production_manager/operator 권한을 확인했고, 실연에서는 실제 공개 UI를 조작했다. Chromium 요청은 기존 정상 인증서 검증과 상속 프록시를 사용하는 HTTPS 브리지로 전달했으며 모의 응답·권한 우회·TLS 검증 해제는 하지 않았다.

기존 runtime·SQLite·인증서·보호 토큰으로 `serve --clock-measured-round-trip`을 한 번 시작하고, 콘솔 준비 후 승인된 commission을 한 번 성공시켰다. 공개 UI에서 레시피·목표 100,000개 작업지시를 각각 한 건 만들고 배포(릴리스)를 확인했다. **배포 후 해당 작업지시를 선택했지만 `#binding-prepare`가 35초 안에 활성화되지 않아 AssertionError로 중단했다.** release_id를 확보했으므로 배포 자체의 실패라고 판정하지 않는다. helper의 단계 이름 `ui_release`는 배포 뒤 실행 연결 준비까지 포함한다. 버튼 비활성 사유 문구는 기존 결과에 저장되지 않았으므로 직접 원인은 미확인이다.

| 항목 | 기록 |
|---|---|
| 시작 UTC / KST | 2026-10-24T07:52:11.236183Z / 16:52:11 |
| 종료 UTC / KST | 2026-10-24T07:53:09.958969Z / 16:53:09 |
| 경과 | **58.7초** |
| 실행·commission | 각각 1회, commission 성공 |
| 레시피 | 000-v7-recipe-20261024T075220304749Z |
| 작업지시 | 000-v7-order-20261024T075220304749Z |
| release_id | 43b472e6-0142-4ed5-b34d-ed79f95adf9b |
| LOAD_JOB / START / END_RUN | **모두 미제출** |
| execution_id / 수량 대조 | 실행 미생성·미확보 / 해당 없음 |
| 종료 원인 | 실행 연결 준비 버튼 활성화 대기 실패(AssertionError), 직접 비활성 사유 미확인 |
| 신규 capture / COMMITTED | 52 / 52 |

이번 supervisor.log 구간(시작 byte 1,286,178)만 집계했다. capture blocked는 GatewayReadError 1회, **BufferFull / INFLIGHT_PRESSURE 3회**다. command·settlement에도 초기 blocked가 각각 1회 있고 클래스·코드는 없다. 이후 수집·COMMITTED가 계속됐고 최종 gateway 오류 종료는 없었다. **이번 중단을 METADATA_FULL이나 BufferFull에 의한 종료라고 해석하지 않는다.** 독립 medium 관찰자도 로그·실행 메타데이터만 읽어 같은 수치를 확인했다.

허용된 한 번의 시도 후 재시도·수동 복구·큐 삭제·새 ID로 명령 재전송은 하지 않았다. 감독자 정상 종료 뒤 두 자식 PID가 존재하지 않고 processes.json이 없음을 확인했다. 이미 생성·배포된 작업지시와 영속 상태는 보존했고 취소·삭제하지 않았다. 원본 안전 메타데이터는 `/tmp/hmi-ops02-v7-production-once/run.json`에 남겼다.

**판정: 7차 생산 실연 실패·미완료.** 생산 START를 제출하지 못해 가동 시간 >0·원래 실행의 수량 대조는 이번 시도로 검증하지 못했다. 공개 DB 용량·행 수·화면 확인은 승인된 분담대로 Claude에게 인계하며, 이 기록 후 중단한다. 제품 코드·UI·README·Vercel 설정과 보호된 기존 브라우저 시험 수정은 건드리지 않았다.

### 7차 §2-1 수정 지시에 따른 기존 작업지시 재개 1회

사용자가 전달한 `d4ddcad`의 수정 지시서를 읽고 기존 RELEASED 작업지시 `000-v7-order-20261024T075220304749Z`를 이어서 사용했다. 레시피·작업지시를 새로 만들거나 release POST를 다시 보내지 않았다. 기존 release ID `43b472e6-0142-4ed5-b34d-ed79f95adf9b`를 정상 공개 UI의 원래 요청 조회로 확인한 뒤 `#workflow-acknowledge`를 한 번 클릭했다. **확인 버튼이 사라지고 `#workflow-recovery` 폼이 다시 나타난 것을 명시적으로 기다려 ack_clicked=true, ack_processed=true를 기록했다.** 그 후 작업지시 목록 새로 조회 → 기존 작업지시 실행 연결 선택 순서로 진행했다.

그러나 `#binding-prepare`는 35초 안에 활성화되지 않았다. 시작 직후와 대기 종료 뒤 `#binding-reason`의 같은 문구를 직접 읽어 기록했다.

> 전체 48개 태그의 최신 정상 품질과 원본 시각을 확인해야 합니다.

START 버튼 아래 사유도 같은 문구였다. 이번 실패를 배포 기록 미확인으로 판정하지 않는다. 위 문구는 UI의 품질·원본 시각 guard가 차단했다는 직접 증거이나, 48개 중 어떤 태그·시각 조건이 불충족이었는지까지는 이번 기록으로 특정하지 못한다. 이전 시도의 직접 원인을 이 결과로 소급 확정하지 않는다. 버튼 상태를 우회하거나 요청을 직접 전송하지 않았다.

| 항목 | 결과 |
|---|---|
| 시작 UTC / KST | 2026-10-24T08:00:35.147803Z / 17:00:35 |
| 종료 UTC / KST | 2026-10-24T08:01:22.373643Z / 17:01:22 |
| 경과 | **47.2초** |
| runtime / commission | 각 1회, commission 성공 |
| 배포 기록 확인 | 클릭 및 처리 완료 확인 |
| 실행 연결 | binding_ready / AssertionError |
| LOAD_JOB / START / END_RUN | **모두 미제출** |
| 실행 ID·수량 대조 | 없음 / 해당 없음 |
| capture | collected 42, uncertain 2, blocked 1 |
| delivery | COMMITTED 44, idle 16 |
| 오류 클래스·코드 | GatewayReadError 1회, 사유 코드 기록 없음 |

command·settlement에는 초기 blocked가 각각 1회 있고 상세 클래스·코드는 없다. BufferFull이나 최종 gateway 오류 종료 기록은 없었다. 독립 medium 관찰자는 이번 시작 byte 1,297,561 이후의 supervisor.log와 `/tmp/hmi-ops02-v7-existing-order-once/run.json`만 읽어 위 수치를 확인했다. 최종 `supervisor stopped; persistent scope retained`, 두 자식 PID 없음, processes.json 없음도 확인했다.

허용된 이번 1회 재개 이후에는 재시도·복구 조작·큐/커서 수정 없이 멈췄다. 기존 작업지시·runtime을 보존했으며 앱 코드·UI·이관·권한·배포는 변경하지 않았다. 공개 DB 전후 확인은 앞선 사용자 승인대로 Claude 담당으로 유지한다. **생산 START와 원래 실행 수량 대조는 여전히 미완료**다.

### 7차 §2-2: 60초 안정화·120초 버튼 대기 재개 1회

사용자가 `ae4141e`의 §2-2대로 한 번 재개하도록 지시했다. 최신 main 반영 상태를 확인했고, 기존 runtime·RELEASED 작업지시·release ID를 유지했다. 앱 코드·안전 조건·한도는 변경하지 않았다. 기존 정상 로그인·Origin·CSRF 절차와 숨김 메모리 입력을 사용했고, 운영계정 비밀번호는 출력·파일 저장하지 않았다.

| 항목 | 실측 |
|---|---|
| runtime 시작 | 2026-10-24T08:13:00.322340Z / 17:13:00 KST |
| commission | 1회, exit0 |
| 안정화 완료 | 08:14:00.324683Z, 시작 후 **60.002초** |
| 배포 기록 확인 | 기존 기록 조회 후 클릭·처리 완료 확인 |
| 버튼 대기 시작 | 08:14:10.390076Z |
| 버튼 대기 종료 | 08:16:10.410237Z |
| 실제 버튼 대기 | **120.020초** |
| runtime 종료 | 08:16:12.102939Z / 17:16:12 KST |
| 전체 경과 | 191.8초 |
| LOAD_JOB / START / END_RUN | **모두 미제출** |
| execution_id·수량 대조 | 없음 / 해당 없음 |

`#binding-prepare`는 시한 안에 활성화되지 않았다. 대기 시작과 종료 시의 직접 사유는 동일했다.

> 전체 48개 태그의 최신 정상 품질과 원본 시각을 확인해야 합니다.

**추가 운영 증거: 이번 수집 로그에서 BufferFull / METADATA_FULL이 88회 기록됐다.** 새 로그 구간(시작 byte 1,306,539)은 capture collected 98회·uncertain 2회·GatewayReadError blocked 1회와 METADATA_FULL blocked 88회, delivery COMMITTED 101회를 포함한다. command 초기 blocked 1회에는 클래스·코드가 없고 이후 idle 134회다. 이는 capture에서 한도 오류를 잡아 blocked로 남긴 경우이며 최종 gateway 오류 종료 기록은 없다. 감독자는 버튼 대기 실패 뒤 정상 종료됐다. 시작 적체만으로 이번 전체 결과를 설명하거나, 6차 압축만으로 운영 메타데이터 한도가 해소됐다고 판정하지 않는다. 어떤 저장 항목·전이에서 다시 한도를 초과했는지 추가 SQLite 조회나 재현은 하지 않았다.

독립 medium 관찰자가 `/tmp/hmi-ops02-v7-stabilized-once/run.json`과 해당 supervisor.log 구간만 읽어 수치·시각·사유를 확인했다. 최종 `supervisor stopped; persistent scope retained`, 두 자식 PID 없음, processes.json 없음도 확인했다. 기존 작업지시·영속 상태는 보존했고 새 recipe/order/release를 생성하지 않았다.

**판정: 승인된 1회 재개 실패.** 60초 안정화와 최대 120초 대기를 실행했지만 생산 실행 연결까지 진행하지 못했다. 안전 조건 우회·추가 시도·큐/커서 변경·원장 정리·추가 복구·DB/배포 변경은 하지 않고 이 기록 후 멈췄다. 공개 DB 전후 수치·화면 확인은 기존 사용자 승인대로 Claude 담당이며, 가동 시간 >0·원래 실행 수량 대조와 OPS-02 전체 수락은 미완료다.
