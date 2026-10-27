# OPS-04 — 새 Neon DB로 공개 사이트 재개와 1회 동작 확인

- 발행: 2026-10-27, Claude. 이 문서가 진행 순서의 기준이다. 이전 대화의 개별 요청은 이 문서로 대체한다.
- 배경: 기존 Neon 프로젝트 `hmi-mes-demo`는 무료 전송량 한도(Network transfer 9.26GB)를 초과해 막혔다. 공개 `/api/ready`는 503이다. 새 무료 프로젝트 `hmi-mes-demo2`(싱가포르, PostgreSQL 18)를 만들었다.
- 사용 방식(사용자 결정): 한 번 실행해서 동작만 확인한다. 1회 1시간 이내, 자주 쓰지 않는다.
- 수행: **Codex가 1~5단계를 끝까지 한다.** Claude는 6단계(공개 확인)를 한다. 사용자는 실행 환경 변수만 제공한다.
- 비밀값(연결 문자열, 비밀번호, 토큰)은 문서·로그·커밋·채팅에 남기지 않는다.

## 실행 환경 변수 (사용자가 Codex 실행 환경에 설정)

| 이름 | 용도 |
|---|---|
| `HMI_MIGRATION_DSN` | 새 Neon 관리자 연결(pooling 끔) |
| `HMI_DATABASE_DSN` | 같은 값(앱 역할을 만들기 전 임시) |
| `HMI_PUBLIC_OPERATOR_PASSWORD` | 공개 운영자 계정 `demo`의 비밀번호 |
| `VERCEL_TOKEN` | Vercel 환경변수 변경·재배포용(짧은 만료) |

## 1. 새 DB 스키마·역할·권한

- main 최신 기준으로 스키마를 0024(`0024_ingest_retired_segments`)까지 만든다.
- 전용 역할 `hmi_migrator`, `hmi_app`을 만들고 기존 grants 배치와 같은 권한을 준다.
- 새 프로젝트의 기본 DB 이름은 `neondb`다. 기존 Neon HTTP 배치의 `hmi_mes_demo` 검사와 다르면 어떻게 맞췄는지 보고한다.
- PostgreSQL 18 호환 문제가 있으면 보고한다.

## 2. 설정값 등록 (이전 공개 DB 백업 기준, Claude 제공)

- 기준 데이터: 라인 `packaging-line-1`, 설비·48태그(`bootstrap_reference_data`)
- 수집원: `demo-packaging-source-v1` / `events-main`, 게이트웨이 `demo-packaging-gateway-v1`, capture_generation `1`
- 계정:
  - `demo`: equipment_admin, operator, production_manager. 비밀번호 `HMI_PUBLIC_OPERATOR_PASSWORD`. 첫 로그인 비밀번호 변경 요구 없음.
  - `viewer`: viewer. 비밀번호는 무작위로 만들고 기록하지 않는다.
  - 게스트: 기존 방식(`bootstrap-guest`). Vercel `HMI_GUEST_VIEWER_ENABLED`는 유지한다.
- 품목(단위 EA, 사용 중): `ITEM-T1`(이름 ITEM-T1), `000-v7-item-20261024T075220304749Z`
- 공정 설정(모두 150℃ / 10 m/min, 버전 v1):
  - `RCP-T1-v1`: 그룹 `RCP-T1`, 품목 `ITEM-T1`
  - `000-v7-recipe-20261024T075220304749Z`: 그룹 `000-v7-family-20261024T075220304749Z`, 품목 `000-v7-item-20261024T075220304749Z`
- 알람 규칙·대기열 정책은 이전에도 없었다.
- 게이트웨이 내부 API 자격 증명(기존 방식)을 새 DB에 다시 등록한다. 시간 근거 정책(`HMI_CLOCK_MEASURED_ROUND_TRIP`)이 DB 쪽 등록을 요구하면 함께 한다.

## 3. Vercel 전환

- `HMI_DATABASE_DSN`(production)을 새 DB의 **`hmi_app` 연결값**으로 바꾼다. 관리자 연결을 앱에 넣지 않는다.
- 다른 환경변수는 그대로 둔다: `HMI_INGEST_RETENTION_HOURS=3`, `HMI_TREND_RAW_HOURS=1`, `HMI_TREND_MINUTE_DAYS`, `HMI_PUBLIC_ORIGIN`, `HMI_GUEST_VIEWER_ENABLED`, `HMI_CLOCK_MEASURED_ROUND_TRIP`, `HMI_ASSISTANT_API_KEY`.
- 운영 배포를 다시 만들고 `/api/ready` 200을 확인한다.
- 게스트 로그인과 `demo` 로그인이 되는지 확인한다(비밀번호는 출력하지 않는다).

## 4. runtime 준비

- 게이트웨이·시뮬레이터 로컬 저장소는 OPS-03에서 초기화한 상태를 쓴다. 이전 실행 기록이 남아 있으면 OPS-03 3단계와 같은 방법으로 비운다.
- 게이트웨이 대상 API가 새 배포를 가리키는지, 새 자격 증명을 쓰는지 확인한다.
- **아직 켜지 않는다.** 1~4단계 결과를 보고하고 Claude의 시작 신호를 기다린다.

## 5. runtime 1시간 실행 (Claude 신호 후)

- 시뮬레이터 → 복구 5항목 → REMOTE/AUTO → 게이트웨이 순서로 시작하고, 시작 시각을 알린다.
- Claude가 공개 확인을 끝내면(최대 1시간) 게이트웨이 → 시뮬레이터 순서로 정상 종료하고, 종료 시각과 게이트웨이 대기열 잔여 건수를 알린다.
- 1시간이 지나도 Claude 신호가 없으면 그대로 정상 종료한다.

## 6. 공개 확인 (Claude)

- `demo` 계정으로 아래를 화면 캡처와 함께 확인한다. 비밀번호는 사용자가 직접 입력하거나 따로 정한다.
  - 로그인
  - 작업지시 등록·배포
  - 실행 연결
  - 운전(자동 종료 목표 소량)
  - 정산
  - 실적 보고서·트렌드
- 수집 상태 깜빡임·지연을 관찰한다.
- 새 Neon 사용량(Network transfer, Compute)을 사용자 화면으로 확인해 1회 사용량을 기록한다.

## 보고

Codex는 1~4단계가 끝나면 아래만 알린다.
- 스키마 버전
- DB 이름 처리
- 설정값 등록 결과(계정 수, 품목 2, 공정 설정 2)
- Vercel 배포 ID와 `/api/ready` 결과
- runtime 준비 상태

5단계는 시작·종료 시각만 알린다.
