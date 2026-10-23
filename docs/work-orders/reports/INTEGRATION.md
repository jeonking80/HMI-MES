# KULS 백엔드 통합 보고

## v1.2 추가 작업 — CX-12 → CX-10 → CX-09 → CX-11

기준 통합본 `a07e91d`를 기존 `codex/kuls-backend` 체크아웃에 fast-forward한 뒤 진행했다. medium이 구현·검증·문서를 맡았고 high는 DB/시간 근거/인증의 독립 검토에만 사용했다. UI·README·AGENTS·Claude 소유 브라우저 스크립트는 수정하지 않았다.

| 작업 | 커밋 | 결과 |
|---|---|---|
| CX-12 서버리스 보관 프로필 | `490faed` | 기본 7일/90일 유지, 배포 설정·오류·실제 retention 확인 |
| CX-10 분석 기능 제공 여부 | `c88f991`, `626e8c6` | 경로·스키마·읽기 권한 확인, head14 false/head17·18 true |
| CX-09 같은 호스트·CX-11 원격 측정 | `626e8c6` | 공통 0018 영속 증거·정규화·nonce·gateway·최소 권한, high 통과 |
| Compose 실제 생산/분석 수락 | `d8df053` | 현재 소스로 실제 START/생산/PAUSE/END/SETTLED·GOOD·가동 시간 확인 |

선택적 envelope `clock_evidence`, 서비스 인증 `POST /internal/v1/clock-probes`, `/auth/config.analytics`만 계약에 추가했다. 기존 1.2 body/digest와 공개 분석 응답 필드·PLC 사건 시각·알람 판정은 유지한다. 시간 정책은 명시적 opt-in이며 Vercel에 같은 커널 주장을 넣지 않는다. 0018은 원시 6시간 정리와 독립된 배치별 불변 근거를 보관하고 사용한 스키마의 근거 손실 downgrade를 거절한다.

검증 근거:

- core·기존 트렌드/보고/SPC 집중49개 통과. gate/grant/guest/Vercel 집중54개 통과. gateway·wire·buffer159개 통과.
- 독립 high: DB/권한/분석 PostgreSQL13개, signed±7시간 보관2개 통과. 인증/nonce 동시성/실제 서비스 회귀22개 통과. 시간 중간값 불일치와 만료 정리 정체는 수정 후 통과했으며 남은 차단 사항은 없다.
- 실제 CLI/Chromium: GOOD132점·RUNNING4.043357초·관측 이전 NO_EVIDENCE만, 실제 생산 정산과 네 분석 화면 통과. Claude 검사 원본 대신 `/tmp` 복사본의 internal API `serve`에 `--trend-clock-policy same-host`를 추가했다. 반영할 인자는 [CX-09](CX-09.md)에 있다.
- 실제 Compose: GOOD44점·RUNNING6.86994초·NO_EVIDENCE만·SETTLED, 게스트/48freshGOOD/웹/init 재실행 통과. 관리형 VFS의 새 이미지 build는 ENOSPC로 실패했다. 검토한 기존 이미지+현재 apps/contracts/dist 읽기 전용 마운트로 검증했고 임의 검사 프로젝트만 정리했다. clean build 통과로 표시하지 않는다. [재현](../../architecture/COMPOSE_RUN.md).
- 전체 검사 최초 시도는 이미지 빌드와 겹친 PG 준비 오류로367통과/17오류에서 중단했다. 별도 분석 집중도18통과/6setup오류였다. 작업 전용 이미지/캐시를 정리하고 전체 `check-local.sh`를 단독 재실행 중이다.

최종 검증 결과:

- Python 수집 목록 **2,246개 전체 범위**를 선행22모듈534개 + 중간31모듈556개 + 마지막1,156개로 분할해 통과했다. 마지막 구간은 **1,156 passed / 355.99초**, 해당 `check-local.sh` 호출은 웹 단계까지 exit0이었다. 누락·중복·실패·오류·skip이 없음을 수집 목록과 JUnit으로 대조했다. 컬렉션마다 생성하는 무효 테스트 토큰 두 사례는 함수/케이스 식별을 유지하며 비교했다. `/tmp/hmi-kuls-clock-coverage.json`, `hmi-kuls-clock-middle.xml`, `hmi-kuls-clock-tail.xml` 참조.
- 웹 화면 시험 **85 passed**, typecheck·ESLint·production build 통과. Python Ruff/format **318파일** 통과. 일반 전체 범위 재현 명령은 `./scripts/check-local.sh`다.
- 단일 전체 명령이 처음부터 끝까지 무수정 exit0이었다고 표시하지 않는다. 공간 확보 뒤 전체 시도는547통과 후 과거 제어 시험의 원본 삭제가 새로운 proof FK에 거절되어 중단했다. 제어/snapshot을 원본 삭제 없이 준비하도록 수정해47개 집중 통과(`1ab9053`). 중간 구간도573통과 후 과거 정산 downgrade가 새18 보호에 거절되어 중단했다. 과거 이관 시험은 기존17에서 원래 보호 장치를 확인하도록 지정했고 정산24개 집중 통과했다.
- 그 과정에서 wire1.0/1.1의 capture_time 없는 아날로그가 schema15/17에서 예외가 나던 실제 호환성 오류를 수정했다(`d375b8d`). 새4조합+이관3개 실제PG7개 집중 통과 후, 새4개·정산·취소·배포를 포함한 마지막1,156개를 다시 실행해 통과했다. 시간 근거 없는 값은 GOOD으로 바꾸지 않는다.
- 원본 로그는 `/tmp/hmi-kuls-clock-check-local-interrupted.log`, `hmi-kuls-clock-check-local.log`, `hmi-kuls-clock-middle-local.log`, `hmi-kuls-clock-tail-local.log`에 남겼다. 앞선 실패·중단은 최종 통과와 구분한다.

이 작업지시서의 추가 백엔드 범위 검증 결과다. 공개 배포 수락·기획서 전체 수락·8시간 운전·최대 보관 규모 성능 완료를 뜻하지 않는다.

공개 운영 경계와 인계:

- `main`은 확인 시 `3117ba9`, Claude 통합 브랜치는 `a07e91d`였다. 새 backend는 이 작업 브랜치에만 푸시한다. 병합은 작업지시서대로 Claude 담당이다.
- 공개 DB 이관/게스트 활성화는 이미 승인된 [OPS-01](../OPS-01_공개배포_이관_활성화.md)을 따른다. main 병합 및 CX-12 배포 선행 조건이 아직 충족되지 않아 실행하지 않았다. 새 승인을 요청하지 않는다.
- 공개에서는 먼저 CX-12의6시간/14일 프로필을 검토·배포하고17이관/기존권한을 적용한 뒤, 원격 시각을 켜려면18과별도 `neon-clock-grants.sql`을 적용한다. 정확한 단계는 [배포 문서](../../architecture/KULS_DEPLOYMENT.md)를 따른다. 무조건 `upgrade head`로17권한 단계를 건너뛰지 않는다.
- Claude가 `/auth/config.analytics`로 메뉴를 연결하고 원본 검사 CLI 인자를 반영해야 한다. 공개 측정은 API env와 gateway 인자를 각각 명시해야 한다.
- 유료 리소스·CI·LLM 비용을 추가하지 않았다. CX-08은 보류다. 0018 근거와 기존 원본 이력은 계속 누적되므로6시간/14일 프로필이 무료 DB 총용량이나 무기한 운전을 보장하지 않는다.

## v1 작업 기록 (이하 당시 결과)

기준: 작업지시서 v1, `claude/bold-feynman-rpbxfw`의 `31f07fd`.
작업 브랜치: `codex/kuls-backend`. 작성일: 2026-10-23 (Asia/Seoul).
현재 상태: 구현·개별 검증과 독립 검토를 마쳤다. 아래 전체 실행의 실패와 수정 후 집중 재검증을 구분한다. 프로젝트 전체 수락 완료를 뜻하지 않는다.

## 작업별 변경

| 작업 | 구현 커밋 | 결과 보고 |
| --- | --- | --- |
| CX-01 트렌드 | `57d638e` | [CX-01](CX-01.md) |
| CX-02 보고·CSV | `f58547d` | [CX-02](CX-02.md) |
| CX-03 정지 사유 | `0c2aaa8` | [CX-03](CX-03.md) |
| CX-04 통계 SPC | `4667601`, `d9a18d9` | [CX-04](CX-04.md) |
| CX-05 대시보드 | `ca72d68` | [CX-05](CX-05.md) |
| CX-06 게스트 조회 | `23a936d`, `70826f7` | [CX-06](CX-06.md) |
| CX-07 로컬 Compose | `adfa816` | [CX-07](CX-07.md) |

공통 사용자 앱의 API 등록·정지 사유 callback 연결은 `23a936d`, Vercel 등록 검사는 `d5be46c`이다. 기존 시험의 downgrade 실패 시 시작 revision 보존 검사는 `fb9c697`에서 고정 head14 기대를 수정했다.

UI·README·AGENTS·원본 작업지시서·Claude 소유 브라우저 스크립트는 수정하지 않았다. 새 업무 화면과 게스트 진입 버튼은 UI 담당자의 결합이 필요하다. 통합은 작업지시서대로 Claude가 수행하며 이 작업에서 `main`을 변경하지 않는다.

## 계약과 운영 경계

- 기존 JSON 필드 이름·의미는 유지했다. 타임라인 출처·충돌 근거, 일일 검산, SPC 기준선/보관 제한 등 보고서에 적은 추가 필드만 제공한다.
- 비정렬 최대 길이 트렌드가 점 개수 상한을 넘으면 422다. 누락을 0이나 직전 값으로 채우지 않는다. 시계 역행은 같은 epoch의 다음 표본을 정상으로 만들지 않으며 원본 이력에서 증거를 복원한다.
- 신규 head는 `0017_guest_viewer`이고 공개 Neon은 기존 head14를 유지한다. 새 기능의 공개 결합에는 별도 운영자의 이관·권한 적용·보관 용량 검토가 필요하다. [배포 절차](../../architecture/KULS_DEPLOYMENT.md)를 따른다.
- 게스트 기본값은 비활성이다. 로컬 Compose만 명시적으로 활성화한다. 공개 활성화는 사용자 승인 후 별도 작업이며 이번에는 하지 않았다.
- SAME_HOST 시간 정책을 서로 다른 호스트인 Vercel·게이트웨이에 적용하지 않는다. 유효한 시간 근거가 없으면 트렌드·보고 시간은 불명으로 유지한다. 대규모 보관 이력의 p95 검증은 미수행이다.
- CX-08 챗봇은 계속 보류한다. CI·유료 서비스·LLM 키를 추가하지 않았다. 이 보고는 기획서의 전체 수락·장시간·복구 완료를 뜻하지 않는다.

## 독립 검토와 집중 검증

- medium: 구현·문서·일반 검증. CX-05는 다른 medium 담당자가 실제 알람과 소스 격리를 추가 검증했다.
- high: CX-01/03 DB·시간 근거·격리 검토. 발견한 digital-only/monotonic 역행·기존 이력 최초 이관·캐시 왕복 시 cutoff 유실·보고 실행 소유 변경을 보완했다. 최종 독립 PostgreSQL **13 passed, 1 xfailed**. xfail은 원본을 유지하는 파생 캐시 downgrade 삭제의 비차단 관찰이다.
- high: CX-06 인증·DB 검토. 독립15개와 기존64개 회귀 **79 passed**. 별도 프로세스의 전역50/IP5 상한, 만료·회수·역할·표식·CSRF·감사·제한 runtime 역할을 확인했다.
- Vercel 사용자 앱 경로·기존 mixed migration 집중 **39 passed, 21.94초**. 공개 플랫폼 접속 시험이 아닌 로컬 실제 PostgreSQL·ASGI 검증이다.

## 최종 실행 결과

- `./scripts/check-local.sh`: Python **2,093 passed, 1 failed, 2 warnings, 1,169.87초**. 실패는 기존 coordinator 시험의 `head == 0014` 고정 기대이며 실제 head는17이었다. 시작 revision 보존으로 수정했다(`fb9c697`). 이 전체 명령은 exit1이므로 전체 명령 통과로 표시하지 않는다. 기존 WebSocket deprecation 경고2개는 실패와 구분한다.
- 수정한 coordinator 시험과 신규 runtime 권한3개·독립 high 권한9개를 묶어 **13 passed, 18.27초**로 재검증했다. `/tmp/hmi-kuls-final-focus.log`, `/tmp/guest-high-review/grants-review.xml`. 실제 신규 권한 검사는 원본 Neon14 SQL → migrator17 이관 → 명시적 GRANT → 제한 runtime의 트렌드·게스트·사유 입력과 금지 DML까지 확인한다. preflight에서 암묵적 pg_temp가 카탈로그를 가리는 문제도 수정하고 실제 위조 임시 카탈로그 재현의 거절을 확인했다. 권한 산출물은 `70826f7`이다.
- 전체 명령이 웹 단계 전에 종료되어 웹을 별도로 실행했다: 화면 시험 **75 passed**, TypeScript·ESLint·기본 build·polling build 모두 exit0. UI 소스는 수정하지 않았다.
- `check-operable-system.py --chromium /usr/bin/chromium --screenshot .local/kuls-operable-system.png --evidence .local/kuls-operable-system.json`: **PASS**. 실제 Chromium → API/일회성 PostgreSQL → 게이트웨이 → 암호화 OPC UA → 기본100ms 공정에서 작업 등록·배포·LOAD_JOB·예열·START·생산·PAUSE·END_RUN·SETTLED를 확인했다. 같은 execution의 설비 최종 요약과 저장 실적이 **양품4·불량2·폐기0, 투입6·재공0, 사건 순번13**으로 일치했다. fixture 수량이나 직접 DB 사건 주입을 쓰지 않았다. 이 결과는 공개 배포 조작 시험과 구분한다.
- 커밋 `70826f7`만 `git archive`로 별도 디렉터리에 추출하고 `.local`·가상환경·node_modules·dist가 없는 것을 확인했다. 해당 소스의 `check-compose.py`를 실행해 실제 build/up·게스트 인증·48개 fresh/GOOD·빌드 웹·반복 init을 **PASS**했고 임의 독립 프로젝트 `cx07-check-5d1387215708`을 down-v로 정리했다. 기존 preview나 공개 클라우드는 정리하지 않았다. 로그 `/tmp/hmi-kuls-clean-compose.log`.
- SPC 보관 경계의 추가 계약 검토에서, 실행 앞부분 원시값이 삭제된 뒤 남은 20개를 최초 기준선처럼 사용할 수 있는 문제를 발견했다. `d9a18d9`에서 원본 배치 prefix를 같은 SQL 스냅샷으로 대조하고 증명 불가 시 `INSUFFICIENT_DATA`·기준선/관리한계/공정능력 null로 처리했다. 40개 중 첫15개 삭제 후25개가 남는 실제 PostgreSQL 회귀 포함 **19 passed, 12.06초**(단위16·PG/ASGI3). 정상25개 기준선도 유지된다. API 필드 이름·원래 최초20개 의미를 바꾸지 않았다.
- 최종 Python 정적 검사·형식 검사는 **292 files** 통과했다. 위 전체 실행·브라우저·Compose를 새 SPC 조회 보강 뒤 다시 실행한 것으로 표시하지 않는다. SPC 조회만 바뀐 후에는 해당 API의 집중19개와 정적 검사로 검증했다.

원문 로그·`.local` 증거는 로컬 검증 산출물이며 비밀값과 함께 Git에 넣지 않는다. 커밋된 시험과 이 보고서의 명령으로 재현한다.
# 2차 백엔드 통합 기록 (2026-10-23)

기준은 Claude `3f7592c`(main `68e1f7c` 이후), 작업 브랜치는 `codex/kuls-backend-v2`다. 아래 기존 1차 결과와 구분한다. 이전 시간 근거 구현을 `69f3755`로 결합했고 UI·README·AGENTS·Claude 소유 원본 지시서/브라우저 검사 소스는 수정하지 않았다. 공개 head17·게스트 활성 상태는 OPS-01을 따르며 공개 DB·환경변수·runtime은 이번 작업에서 변경하지 않는다.

| 변경 | 집중 검증 |
|---|---|
| retained minute 역행 처리와 schema18 canonical 시간 결합 | 실제 PG27개, 21.69초 |
| 통신 장애·실행 종료 범위·보고 출처와 canonical 시간 결합 | 실제 PG28개, 51.52초 |
| CX-10 앱별5초 availability 캐시 | 실제 PG/단위23개, 11.25초 |
| CX-13 조회 도구·시간 초과·게스트 제한·로그 | 단위/실제 PG·ASGI38개, 17.57초. 공급자 모의 응답 |
| CX-14 read-only 용량 도구 | 실제 PG2개, 5.12초 |
| 로컬 supervisor 원격 측정 인자 opt-in | 단위11개, 3.11초 |

중간 일반 검증: `.venv/bin/pytest -q tests/unit tests/contract --junitxml=/tmp/hmi-v2-unit-contracts.xml` → **770 passed, 156 subtests passed, 23.42초**. 최초 명령은 경로를 `tests/contracts`로 잘못 지정해 exit4(`file or directory not found`, 시험0개)였으며 실제 경로 `tests/contract`로 바로잡았다. 이 중간 결과는 뒤이은 경계 오류 수정 뒤 전체 검사 통과를 대신하지 않는다.

새 독립 high의 최초 적대적 재현은 **2 failed, 6 passed, 15.18초**였다. 부분 raw 삭제 후 분 집계의 UNKNOWN→PARTIAL 복구, 미확정 시각의 sampleless 통신 장애를 놓치는 보고서가 재현됐다. 추가 동시성 재현에서는 **1 passed, 1 failed, 5.41초**였고 보고서의 요청 내부 캐시가 동시 커밋된 더 이른 reversal cutoff를 놓쳤다. 오류와 수정 후 결과를 따로 남기며, 이전 high signoff를 새 병합 검토로 간주하지 않는다.

추가 측정 시각 재현은 보고서1실패(5.36초)·SPC1실패(5.18초)였다. 역행 후 과거 GOOD의 canonical 좌표를 원본 시각으로 되돌리는 바람에 이전 정상 근거가 재등장했다. 정확한 바인딩의 과거 GOOD 좌표를 UNKNOWN 차단에만 보존하도록 수정했으며 조회 창 이전 차단도 유지한다. 원본 sample·proof·생산 사건의 시각은 변경하지 않는다.

수정 후 독립 high는 실제PG **21 passed, 27.99초**, 남은 확인된 차단 문제 없음으로 종료했다. 제품 회귀는 raw 일부 삭제 후 second/minute의 정상 값 재등장, 수신 시각의 통신 장애 차단/복구, 동시 커밋 cutoff, 측정 좌표 무효화의 보고/SPC, head14 호환성을 포함한다. 소유자 검증은 retained 기존27개27.77초+신규4개6.64초, reports/SPC 결합7개11.79초 및 최종 경계5개8.26초였다. 신규4개는 최초에 제품 경로 진입 전 fixture의 `AttributeError: 'tuple' object has no attribute 'append'`로 실패해 list로 바로잡은 뒤 통과했다. 편집 중 SQL 두 줄의 E501도 수정하고 전체 검사 시작 시 Ruff/format을 통과했다.

HTTP migration 재생/기존 이관/권한20개18.27초·독립 high5개6.36초 통과. 공개 Neon 전송은 하지 않았다. 실제 CLI 수락은 GOOD141점·RUNNING4.461093초·UNKNOWN1.033524초(NO_EVIDENCE만)·SETTLED 양품5/불량2/폐기0으로 설비 원본과 일치했다. 현재 소스·실제 Chromium·API/PG·암호화 OPC UA 경로이며 기존 웹 소스와 원본 검사 스크립트는 수정하지 않았다. 상세는 [CX-09](CX-09.md)·[CX-11](CX-11.md)를 따른다.

최종 전체 로컬 검사와 현재 소스 Compose runtime 검사는 진행 중이다. T01~T12와 대형 성능·8시간·전체 복원 시험의 미완료 범위는 [CX-15](CX-15.md)를 따른다.

## 최신 main 선행 병합과 CX-16 추가

사용자 지시에 따라 `main`의 PR #3~#5가 포함된 `c681b21`을 먼저 받아 `fd92ea8`로 병합했다. 충돌은 assistant와 해당 통합 시험 두 파일에서 해결했다. upstream UI·Vercel60초·원본 v2 지시서 CX-16은 그대로 받았으며 UI/UX 소스를 별도로 수정하지 않았다.

- CX-13은 이제 단일 `deadline_seconds=50`(최대50), 함수60초 기준이다. 최초 인증을 포함한 외부 시한과 모델 반복의 잔여 예산에 같은 설정을 사용한다. 날짜/KST 프롬프트, Nemotron `enable_thinking=false`, 일시 오류1회 재시도, 대시보드 모델 입력 축소와 기존504를 보존했고 우리 활성 알람·게스트2회/300초·로그 보호를 결합했다. 최신 조합 단위/실제PG·ASGI **43 passed, 21.42초**. 실제 공급자는 호출하지 않았다.
- 병합 전 전체 명령 `PYTEST_ADDOPTS=--junitxml=/tmp/hmi-v2-full.xml ./scripts/check-local.sh`는 정적338파일을 통과했으나 **292 passed, 27 errors, 195.39초**, 사용자 새 지시 후 중단(exit2)이었다. 실패 원문: `psycopg.errors.DiskFull: could not create file "base/16384/39227": No space left on device`. VFS Docker에서 전체 PG와 Compose를 겹쳐 실행한 자원 실패다. 전체 명령 성공으로 표시하지 않으며 `/tmp/hmi-v2-check-local.{log}`, `/tmp/hmi-v2-full.xml`, `/tmp/hmi-v2-check-local-result.json`에 보존했다. 이후 새 검증은 큰 Docker 작업과 직렬로 실행한다. 기존 preview·보호된 영속 상태·전역 Docker 자원은 삭제하지 않았다.
- 기존 CLI/Compose v2 수락 증거는 최신 main 병합 **이전** 결과다. Compose는 GOOD41·RUNNING6.84785초·NO_EVIDENCE만·SETTLED 및 guest48freshGOOD/웹/반복init을 통과했으며 cached image+현재소스RO mount 방식이다. clean build ENOSPC 한계를 없앤 결과로 해석하지 않는다.
- 새 CX-16은 로컬 일회용 DB의 약3,800배치 규모에서 SQL 계획·응답 전후값을 측정하고 결과를 유지하며 최적화한다. 1Hz3,800건은 약63분20초이므로 전체24시간 규모나 공개Neon p95 성능으로 확대하지 않는다. 구현·검증은 진행 중이다.
