# KULS 확장 배포 준비

현재 공개 상태는 [OPS-01 결과](../work-orders/reports/OPS-01.md)에 기록한 `0017_guest_viewer`·게스트 활성·원시6시간/분14일 배포 프로필이다. 아래 canonical14→17 절차는 기존 설치 경로이며 이미 적용한 공개 DB에 반복하지 않는다. 기존 `deploy/neon-init.sql`은 canonical14 설치 증거로 유지한다. 아직 공개에 적용하지 않은 head18 시간 근거는 별도 [OPS-02 인계안](OPS-02_CLOCK_MIGRATION.md)을 따른다.

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

Compose의 `SAME_HOST_SHARED_KERNEL_UTC` 주장은 서로 다른 호스트인 공개 Vercel API와 클라우드 gateway에 재사용할 수 없다. 원격 `MEASURED_ROUND_TRIP`은 CX-11에서 구현했으며 schema18·추가 권한과 API/gateway 양쪽의 명시적 opt-in이 필요하다. 활성화 전에는 트렌드·보고서의 `TIME_UNKNOWN`을 유지한다. 이 권한 파일 자체는 시간 정책을 활성화하지 않는다. [시각 근거와 설정](CLOCK_EVIDENCE.md)을 따른다.

로컬 검증은 `pytest -q tests/integration/test_kuls_runtime_grants.py`로 수행한다. 고정 PostgreSQL 컨테이너의 disposable DB에서 canonical14 초기화 → migrator 명시적 업그레이드 → 검토용 권한 파일 → runtime 동작과 권한 거절을 확인한다. 운영 DSN은 사용하지 않는다.

## head18 시각 근거의 추가 runtime 권한

`0018_clock_evidence` 배포에는 [neon-clock-grants.sql](../../deploy/neon-clock-grants.sql)이 별도로 필요하다. 기존 head17 절차를 대체하지 않는다. 적용 순서는 **canonical14 → migrator로 head17 업그레이드 → 기존 neon-kuls-grants.sql 전체 실행 → migrator로 head18 업그레이드 → neon-clock-grants.sql 전체 실행**이다. 각 단계는 검토한 운영자의 명시적 작업이며 앱 시작이나 runtime 요청에서 실행하지 않는다. head17 권한 파일은 정확한 head17만 받으므로 head18 업그레이드 전에 적용해야 한다.

새 파일은 트랜잭션과 스키마 독점 잠금 `843001` 아래 정확한 `hmi_mes_demo`·head18·`hmi_migrator` 소유자·격리된 비특권/비소유 runtime 역할을 검증한다. `pg_temp`를 search_path 끝에 명시하여 임시 객체가 카탈로그 점검을 가리지 못하게 한다. 잘못된 DB/head/소유자/역할이면 전체를 거절하며 재실행 가능하다. 기존 head17 파일·기존 테이블 ACL·역할/DDL 권한·default privileges는 바꾸지 않는다.

| head18 새 객체 | `hmi_app` 권한 |
|---|---|
| `capture_clock_evidence` | SELECT, INSERT |
| `capture_clock_probes` | SELECT, INSERT, DELETE; UPDATE(consumed_batch_id, consumed_input_sha256, consumed_at) |
| `immutable_capture_clock_evidence()` | EXECUTE |

두 새 테이블과 trigger 함수의 PUBLIC 권한을 제거한다. 확정 시각 근거에는 UPDATE·DELETE·TRUNCATE를 주지 않고 불변 trigger도 유지한다. Probe는 소비 결과 세 열만 갱신할 수 있고, scope·발급/만료 시각·nonce 등 원본 측정 열은 갱신할 수 없다. 갱신·삭제는 1회용 사용 상태와 제한된 보관 정리를 위한 것이다. 두 테이블 모두 runtime TRUNCATE·DDL 권한은 없다. 원시 테이블의 새 `canonical_utc` 열은 기존 명시적 raw 테이블 권한에 포함되므로 별도 확대하지 않는다.

이 파일은 계정·nonce·측정 근거를 만들거나 시간 측정을 활성화하지 않는다. head18 공개 적용은 OPS-02 운영 작업이며 이번 변경에서 실행하지 않았다. 실제 PostgreSQL 검증 명령은 `pytest -q tests/integration/test_clock_runtime_grants.py`다. 테스트는 disposable canonical14 초기화와 head17 권한을 거친 뒤 head18의 실제 `hmi_app` 내부 접수·근거 읽기·probe DML·금지 DML/DDL·실패 원자성을 확인한다. high 검토 결과는 별도 주 에이전트 보고를 따른다.

검증 기록: head18 권한 시험과 기존 게스트·Vercel·분석 제공 여부 시험을 함께 실행해 실제 Docker PostgreSQL **54개 통과(32.70초)**를 확인했다. 새 권한 시험은 명시적 17→18 적용 순서, 재실행, 실제 runtime 접수/읽기, probe 소비 결과 열 갱신·삭제, 원본 만료 열 갱신 거절, 확정 근거 변경·삭제·TRUNCATE·DDL 거절, 기존 ACL 보존과 DB/head/소유자/임시 카탈로그 위장 거절을 확인한다. 별도 high 검토는 주 에이전트에서 결과를 통합한다.
