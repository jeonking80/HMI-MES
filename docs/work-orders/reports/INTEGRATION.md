# KULS 백엔드 통합 보고

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
