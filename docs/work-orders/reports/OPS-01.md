# OPS-01 결과 — 공개 배포 DB 이관과 게스트 활성화

- 수행: Claude (2026-10-23, UTC 기준). 이 환경은 Neon에 TCP로 접속할 수 없어 HTTPS `/sql` 배치를 사용했다.
- 상태: **일부 완료.** 이관·권한 적용은 끝났고, 게스트 계정 생성·활성화는 수행하지 않았다(아래 차단).

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
| 게스트 계정 생성 | **차단.** HTTP 배치로 `bootstrap_guest`와 같은 SQL을 실행하려 했으나 이 세션의 권한 정책이 공유 DB 계정 생성을 거절했다. |
| 게스트 활성화 | 미수행(계정 생성이 선행 조건) |

## 남은 일
- 게스트 계정 생성(`python -m apps.api bootstrap-guest` 또는 같은 SQL) → Vercel `HMI_GUEST_VIEWER_ENABLED=true` → 재배포 → `/auth/config`, 게스트 진입, 쓰기 403 확인
- `hmi_app` 연결로 금지 DML 거절 실측
- DB 사용량 기록(이관 직전 32MB)
