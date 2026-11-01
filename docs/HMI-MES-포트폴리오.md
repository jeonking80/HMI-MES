# HMI-MES 포트폴리오

작성자: **전우석** · 개발 기간: **2026-10-20 ~ 10-28 (9일)** · 최종 갱신 2026-11-01

**[PDF 내려받기](../apps/web/public/portfolio/hmi-mes-portfolio.pdf)** · [HTML 원본](../apps/web/public/portfolio/hmi-mes-portfolio.html) · 공개 주소: <https://hmi-mes.vercel.app/portfolio/hmi-mes-portfolio.pdf>

A4 가로 14쪽입니다. 사용자가 준 구성안 14개 항목을 그대로 따랐습니다.

**제출용 저장소:** <https://github.com/jeonking80/portfolio/tree/main/HMI-MES> — 면접관에게는 이 저장소만 보여 줍니다. PDF 안의 문서 링크는 모두 이 저장소를 가리키고, 코드 저장소(HMI-MES) 링크는 넣지 않습니다. 빌드 스크립트가 코드 저장소 링크를 발견하면 생성을 멈춥니다. PDF를 다시 만들면 `apps/web/public/portfolio/hmi-mes-portfolio.pdf`를 제출용 저장소의 `HMI-MES/hmi-mes-portfolio.pdf`에도 복사합니다.

## 구성

| 쪽 | 항목 | 내용 |
| --- | --- | --- |
| 1 | 표지 | 프로젝트명, 한 줄 소개, 기술 스택, 작성자·기간 |
| 2 | 프로젝트 개요 | 무엇을 하는 시스템인지, 핵심 규칙 3개, 규모 |
| 3 | 시스템 아키텍처 | 브라우저·서버리스 API·PostgreSQL·게이트웨이·설비 앱의 배치와 연결 |
| 4 | 기술 스택과 선택 이유 | 계층별 선택, 이유, 감수한 점 |
| 5 | 데이터 모델 | 기준정보 / 현재 상태 / 지울 수 없는 이력 / 운영 기록 |
| 6 | 데이터 저장 흐름 | 수집 묶음 한 건이 한 트랜잭션 안에서 처리되는 순서 |
| 7 | 핵심 코드 | `apps/api/ingestion.py`의 잠금 순서와 사건 단위 중복 제거 |
| 8 | 보안·권한 구조 | 역할 4종 × 권한 9종, 세션·CSRF·게이트웨이·OPC UA |
| 9 | 동시성과 정합성 | 상황별 막는 장치와 결과 |
| 10 | 자동화와 프론트엔드 | 원격 전원·자동 시작/종료·재전송, 화면 갱신 방식 |
| 11 | 업무 흐름과 주요 화면 | 8단계 흐름과 실제 운전 화면 6장 |
| 12 | 테스트 전략과 결과 | 시험 층, 최신 소스 전체 검사 결과, 과거 장시간·성능 기록 |
| 13 | AI 활용 개발 방식 | 개발·검증 세션 분리, 사용자 결정, 배포 흐름, 직접 찾은 결함 |
| 14 | 한계와 자료 | 하지 않은 것, 저장소·문서 링크 |

## 구성안과 다르게 적은 부분

구성안의 일부 수치와 표현은 다른 프로젝트를 기준으로 한 것이어서, HMI-MES의 실제 값으로 바꿨습니다.

| 구성안 | 이 포트폴리오 | 이유 |
| --- | --- | --- |
| 업무 테이블 13개 | DB 테이블 62개 | 로컬 PostgreSQL에 마이그레이션을 적용해 센 실제 값입니다. |
| 저장 프로시저 11개 | DB 함수 25개 · 트리거 48개 | 이 프로젝트는 저장 프로시저를 쓰지 않습니다. 트랜잭션은 Python 코드가 제어하고, DB 함수는 불변 이력 보호와 보관 정리에 씁니다. |
| 화면 15종 | 메뉴 화면 10종 (로그인·설비 상세 창 별도) | `apps/web/src/navigation.ts`의 메뉴 항목 수입니다. |
| 자동화 테스트 237건 | 최신 전체 검사 결과 | 12쪽과 [test-result.json](assets/portfolio/test-result.json)에 실제 실행 결과를 적었습니다. |
| 6쪽 “저장 프로시저 안의 순서” | 한 트랜잭션 안의 수집 처리 순서 | 같은 역할을 하는 `IngestionService.ingest`를 그렸습니다. |
| 7쪽 “검사 저장 프로시저 발췌” | 수집 트랜잭션 코드 발췌 | 위와 같은 이유입니다. |

## 수치의 출처

- [facts.json](assets/portfolio/facts.json)
  - 테이블·함수·트리거 수: 로컬 Docker PostgreSQL 17에 `alembic upgrade head`(0028)를 적용하고 `information_schema`와 `pg_proc`로 집계했습니다. 공개 Neon DB는 쓰지 않았습니다.
  - 커밋 수: main `8134cb8` 기준입니다.
  - 작업지시서·보고서 수: `docs/work-orders`의 파일 수입니다.
- [test-result.json](assets/portfolio/test-result.json): 최신 소스로 `./scripts/check-local.sh`를 실행한 결과입니다. 실패한 시험도 그대로 적었습니다.
- 화면: 2026-10-29 공개 배포를 실제로 운전하며 촬영했습니다. 작업 실행 3건 모두 정산을 마쳤습니다. 일부 화면은 원본에서 핵심 영역만 잘라 키웠고, 값은 바꾸지 않았습니다.
- 과거 장시간·성능 기록: [CX-19](work-orders/reports/CX-19.md)(30분 단절 후 208.3초 회복)와 [CX-20](work-orders/reports/CX-20.md)(합성 7일 이력 p95 1.34~1.40초)입니다. 둘 다 과거 로컬 구성의 결과이고, 최신 공개 배포에서는 다시 검증하지 않았습니다.

## 다시 만들기

문구와 배치의 원본은 [scripts/build-portfolio.mjs](../scripts/build-portfolio.mjs)와 [scripts/portfolio.css](../scripts/portfolio.css)입니다. 이미지는 HTML 안에 포함되어 있어 별도 파일 없이 열립니다.

```sh
node scripts/build-portfolio.mjs
```

- 필요한 것: Playwright, Chromium, 한글 글꼴 Noto Sans KR.
- 생성 과정은 앱·DB·외부 AI에 요청하지 않습니다.
- 수치는 `facts.json`과 `test-result.json`에서만 읽습니다. 두 파일이 없으면 생성하지 않습니다.
- 각 쪽의 내용이 하단이나 오른쪽을 넘거나 이미지가 비면 PDF를 만들지 않고 실패합니다.
- 코드 발췌는 작업 파일이 아니라 고정 커밋에서 직접 읽습니다.
