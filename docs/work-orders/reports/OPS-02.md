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
