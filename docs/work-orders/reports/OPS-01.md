# OPS-01 결과 — 공개 배포 DB 이관과 게스트 활성화

- 수행: Claude (2026-10-23, UTC 기준). 이 환경은 Neon에 TCP로 접속할 수 없어 HTTPS `/sql` 배치를 사용했다.
- 상태: **이관·권한·게스트 활성화 완료.** `hmi_app` 연결로 금지 DML을 직접 실행하는 실측은 미수행(아래).

## 단계별 결과

| 단계 | 결과 |
|---|---|
| 백업 | Neon 브랜치 존재 여부는 확인하지 못했다(Neon API 키 없음). 대신 이관 직전 전체 논리 백업을 받았다: 42개 테이블, 3,898행, 테이블별 행 수 DB와 일치, `alembic_version=0014_mixed_alarm_runtime`. 비밀값이 들어 있어 저장소에 넣지 않고 작업 세션의 임시 폴더에만 두었다. |
| CX-12 보관 환경변수 | Vercel production에 `HMI_TREND_RAW_HOURS=6`, `HMI_TREND_MINUTE_DAYS=14` 설정 |
| 코드 배포 | PR #1 병합(`68e1f7c`), Vercel production READY. 0014에서 `/api/health` 200, `/api/ready` `{"ready":true}` |
| 이관 0014→0017 | `deploy/neon-http/upgrade-0014-0017.json`(마이그레이션 모듈에서 생성, 로컬에서 Alembic 결과와 카탈로그 동일 검증) 20문장을 한 트랜잭션으로 적용. 이후 `alembic_version=0017_guest_viewer` |
| 권한 | `deploy/neon-http/grants-0017.json`(원본 권한 파일과 결과 동일 검증) 13문장 적용 |
| runtime 권한 확인 | 카탈로그 조회(`has_table_privilege` 등)로 확인: 신규 테이블 권한이 KULS_DEPLOYMENT.md 표와 일치, `hmi_app`은 DB·public 스키마 CREATE 없음, 특권 없음, `hmi_migrator` 비소속. **`hmi_app` 연결로 실제 DML 거절을 실행하는 시험은 수행하지 못했다**(운영 계정 시크릿 헤더 오류, 소유자는 `hmi_app`으로 SET ROLE 불가). |
| 이관 후 앱 | `/api/ready` `{"ready":true}` |
| 게스트 계정 생성 | 처음에는 세션 자동 권한 정책이 거절했다. 사용자가 권한 모드를 바꾼 뒤 승인을 받아 `bootstrap_guest`와 같은 SQL(사용 불가 해시, 이미 있으면 전체 롤백)을 HTTP 배치 한 트랜잭션으로 실행했다. 확인: `guest-viewer` 활성, 비밀번호 변경 요구 없음, 역할 `viewer`만, 표식 1건. |
| 게스트 활성화 | Vercel production `HMI_GUEST_VIEWER_ENABLED=true` 설정 후 재배포 READY. `/api/ready` ready, `/auth/config` `{"guest_viewer":true,"assistant":false}` |
| 게스트 동작 | 공개 앱에서 `POST /auth/guest` 200(`guest-viewer`, `viewer`, 만료 30분 뒤), `/auth/me` 200, 조회 200: sources·dashboard·trends(신규 테이블)·executions·downtime history(신규 테이블). 쓰기: 비밀번호 변경 403. 정지 사유 쓰기는 빈 본문이라 입력 검증 422가 먼저 나와 권한 거절의 증거가 아니다. |

## 남은 일
- 공개 빌드에 분석 메뉴 플래그(`VITE_ANALYTICS_PAGES=enabled`)가 없어 대시보드·트렌드·보고서·SPC 메뉴가 "예정"으로 닫혀 있다(CX-10 또는 빌드 플래그 필요).
- 챗봇 공개 활성화: Vercel `HMI_ASSISTANT_API_KEY` 설정(사용자 입력 필요)과 챗봇 화면 구현.
- `hmi_app` 연결로 금지 DML 거절 실측
- DB 사용량 기록(이관 직전 32MB)
