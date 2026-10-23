# KULS 확장 배포 준비

공개 배포는 기존 head `0014_mixed_alarm_runtime` 상태를 유지한다. 이 문서와 SQL은 운영자가 검토할 수 있는 수동 배포 산출물이며 운영 DB에 적용하지 않았다. 기존 `deploy/neon-init.sql`은 canonical14 설치 증거로 유지한다.

새 기능을 배포하려면 운영자가 먼저 백업·용량·마이그레이션 변경과 아래 권한을 검토해야 한다. 현재 기본 원시값 7일·1분 집계 90일 보관을 Neon 용량에 맞춰 검토하고, 필요하면 명시적인 배포 보관 프로필을 선택한다. 기준을 조용히 줄이지 않는다. 구체적인 용량과 프로필은 [TREND_HISTORY.md](TREND_HISTORY.md)를 참조한다.

## 명시적 적용 순서

1. 기존 전용 `hmi_mes_demo` DB와 `hmi_migrator` 객체 소유권을 확인한다. 검토한 코드로 운영자 연결에서 `SET ROLE hmi_migrator` 후 명시적으로 Alembic `0017_guest_viewer`까지 업그레이드한다. 앱은 마이그레이션이나 권한 부여를 수행하지 않는다.
2. 별도 운영자 연결에서 [neon-kuls-grants.sql](../../deploy/neon-kuls-grants.sql) **전체**를 실행한다. 해당 운영자는 `SET ROLE hmi_migrator` 권한이 필요하다. 파일은 트랜잭션과 스키마 독점 advisory lock `843001` 아래 DB 이름·정확한 head17·대상 객체 소유자·runtime 비특권/비소유 상태를 확인한 뒤 신규 객체에만 권한을 부여한다. 조건이 맞지 않으면 전체 롤백한다. 재실행 가능하다.
3. runtime `hmi_app` 연결로 기존 일반 인증·트렌드·정지 사유 기능과 DML 거절을 검증한다. 게스트는 다음 provisioning·승인 단계 이후에 검증한다. runtime은 마이그레이션 역할을 상속하거나 DDL 권한을 얻지 않는다.
4. 게스트가 필요하면 오프라인 운영자/소유자 연결로 전용 계정을 명시적으로 provisioning한다. runtime은 전용 계정 표식 테이블에 SELECT만 가지므로 계정 생성 권한이 없다. **공개 게스트 활성화는 별도 사용자 승인 후** 수행한다. [GUEST_VIEWER.md](GUEST_VIEWER.md)의 기본 비활성 정책을 유지한다.

| 새 객체 | runtime 권한 |
|---|---|
| analog_history_raw, analog_history_minutes | SELECT, INSERT, UPDATE, DELETE (보관 정리 포함) |
| analog_history_clock_state | SELECT, INSERT, UPDATE |
| downtime_reason_history | SELECT, INSERT |
| downtime_reason_history_id_seq | USAGE, SELECT |
| guest_viewer_accounts | SELECT |
| guest_rate_windows | SELECT, INSERT, UPDATE, DELETE |
| immutable_downtime_reason() | EXECUTE |

신규 객체의 PUBLIC 권한과 불필요한 runtime 권한은 제거한다. 정지 사유 이력은 UPDATE·DELETE·TRUNCATE, sequence는 UPDATE를 주지 않는다. 기존 테이블 권한·역할·DDL 권한·기본 권한(default privileges)은 변경하지 않는다. trigger 함수 실행 권한은 기존 배포 규칙처럼 명시하며 증거 불변 trigger를 유지한다.

## 시간 증거와 공개 환경의 한계

현재 유효한 clock policy는 `SAME_HOST_SHARED_KERNEL_UTC` 증거다. Compose의 같은 호스트 공유 커널 주장은 서로 다른 호스트에서 실행하는 공개 Vercel API와 클라우드 gateway에 재사용할 수 없다. 승인되고 유효한 별도 시각 증거가 없으면 트렌드·보고서의 `TIME_UNKNOWN`은 의도한 결과이며, 공개 기능 수락 검증은 대기 상태다. 이 권한 파일은 시간 정책이나 환경 설정 기능을 추가하지 않는다.

로컬 검증은 `pytest -q tests/integration/test_kuls_runtime_grants.py`로 수행한다. 고정 PostgreSQL 컨테이너의 disposable DB에서 canonical14 초기화 → migrator 명시적 업그레이드 → 검토용 권한 파일 → runtime 동작과 권한 거절을 확인한다. 운영 DSN은 사용하지 않는다.
