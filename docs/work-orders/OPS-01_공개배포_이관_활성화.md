# 작업지시서 OPS-01 — 공개 배포 DB 이관과 게스트 활성화

- 발행: 2026-10-23, Claude
- 수행: IDE 에이전트(Neon·Vercel 접근 권한이 있는 환경)
- 사용자 승인: 2026-10-23 "이관하고 활성화해"
- 대상: 공개 화면 <https://hmi-mes.vercel.app>, Neon DB `hmi_mes_demo`
- 참고: [KULS_DEPLOYMENT.md](../architecture/KULS_DEPLOYMENT.md), [GUEST_VIEWER.md](../architecture/GUEST_VIEWER.md), [TREND_HISTORY.md](../architecture/TREND_HISTORY.md), [neon-kuls-grants.sql](../../deploy/neon-kuls-grants.sql)

## 0. 선행 조건 (모두 충족 전에는 시작하지 않는다)

- [ ] `main`에 KULS 보강 병합본이 반영되어 있고, Vercel 운영 배포가 그 커밋으로 정상 동작한다. `/api/health`가 200이고 기존 로그인과 화면이 동작해야 한다.
- [ ] 작업지시서 CX-12(공개 트렌드 보관 환경변수)가 `main`에 반영되어 배포됐다. **CX-12 없이 0015 이상으로 올리면 기본 7일 원시 보관 때문에 Neon Free 0.5GB를 넘을 수 있다.**
- [ ] (권장) CX-10이 반영되어 분석 메뉴가 서버 응답으로 열린다. 없으면 §5의 빌드 플래그 단계가 필요하다.

## 1. 백업

1. Neon 콘솔에서 현재 운영 브랜치의 **복원 지점 브랜치**를 만든다. 예: `pre-kuls-0017-YYYYMMDD`.
2. 현재 `alembic_version`이 `0014_mixed_alarm_runtime`인지 확인해 기록한다.
3. 비밀번호·DSN은 문서·로그·커밋·채팅에 남기지 않는다.

## 2. 보관 설정 먼저 적용 (CX-12)

1. Vercel 프로젝트 환경변수에 공개 배포용 보관값을 넣는다. 예: `HMI_TREND_RAW_HOURS=6`, `HMI_TREND_MINUTE_DAYS=14`. 정확한 이름·허용 범위는 CX-12 결과 문서를 따른다.
2. 재배포한 뒤, 기존 스키마(0014)에서도 앱이 정상인지 확인한다(`/api/health`, 로그인).
3. 줄인 값과 이유(무료 DB 용량)를 `docs/DEVELOPMENT_STATUS.md`의 `### Codex 백엔드 보강` 절에 기록한다.

## 3. 스키마 이관 (0014 → 0017)

[KULS_DEPLOYMENT.md](../architecture/KULS_DEPLOYMENT.md)의 "명시적 적용 순서"를 그대로 따른다.

1. 운영자 연결에서 `SET ROLE hmi_migrator` 후 검토한 커밋의 Alembic으로 `0017_guest_viewer`까지 업그레이드한다. 앱은 마이그레이션을 수행하지 않는다.
2. 별도 운영자 연결에서 `deploy/neon-kuls-grants.sql` **전체**를 실행한다. 조건이 맞지 않으면 파일 전체가 롤백되므로, 오류가 나면 중단하고 보고한다.
3. runtime(`hmi_app`) 연결로 다음을 확인한다.
   - 신규 테이블 SELECT가 허용된다.
   - 금지 DML(정지 사유 이력 UPDATE·DELETE 등)이 거절된다.
   - DDL 권한이 없다.
4. 공개 앱에서 다음을 확인한다.
   - `/api/ready`가 ready다.
   - 기존 로그인·라인 모니터링이 정상이다.
   - 설비가 연결되어 있으면 수집 ACK가 계속 성공한다.

## 4. 게스트 활성화

1. 오프라인 운영자/소유자 연결로 전용 계정을 만든다. runtime 연결로는 만들 수 없다.
   ```sh
   HMI_DATABASE_DSN=<운영자 DSN, 출력 금지> python -m apps.api bootstrap-guest --origin https://hmi-mes.vercel.app
   ```
2. Vercel 환경변수 `HMI_GUEST_VIEWER_ENABLED=true`를 설정하고 재배포한다. 잘못된 값이면 앱 생성이 거절되므로 정확히 `true`여야 한다.
3. 확인:
   - `GET /auth/config` → `{"guest_viewer": true, ...}`
   - 로그인 화면에 "로그인 없이 둘러보기" 버튼이 보이고, 누르면 `guest-viewer`·조회 사용자로 들어간다.
   - 게스트의 쓰기(비밀번호 변경·운전 명령·정지 사유)가 403이다.
   - 게스트 세션은 30분 뒤 만료된다(시간을 기다리지 않고 응답의 `expires_at`으로 확인).

## 5. 분석 메뉴 공개

- CX-10이 반영됐다면 `/auth/config`의 `analytics`가 true인지 확인하고, 메뉴가 열리는지 확인한다.
- CX-10이 없다면 이 단계는 Claude에게 요청한다. Claude가 `vercel.json` 빌드 명령에 `VITE_ANALYTICS_PAGES=enabled`를 추가하는 커밋을 만든다. IDE 에이전트는 UI 파일을 수정하지 않는다.
- 확인: 대시보드·트렌드·보고서·SPC가 오류 없이 열린다. CX-11 전에는 공개 환경의 트렌드 값과 가동 시간이 "시간 불명"으로 표시되는 것이 정상이다.

## 6. 롤백 기준

다음 중 하나라도 발생하면 즉시 `HMI_GUEST_VIEWER_ENABLED=false`로 재배포하고, 필요하면 §1의 복원 지점으로 되돌린 뒤 보고한다.

- 기존 로그인·화면·수집 ACK 실패
- `/api/ready` 비정상
- 권한 확인 실패(runtime이 금지 DML 가능 등)
- 게스트가 쓰기에 성공

## 7. 보고

`docs/work-orders/reports/OPS-01.md`에 다음을 남긴다.
- 단계별 실행 시각
- 확인 결과(HTTP 상태·화면)
- 이관 전후 `alembic_version`
- DB 사용 용량
- 실패와 조치

비밀값은 적지 않는다. 미실행·실패는 통과로 표시하지 않는다.
