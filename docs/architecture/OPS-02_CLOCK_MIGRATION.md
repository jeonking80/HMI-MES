# OPS-02 인계안 — 공개 시간 근거 이관·활성화

이 문서는 CX-11의 운영 인계 산출물이다. **공개 적용은 실행하지 않았다.** Claude 소유 작업지시서를 대신 수정하지 않고, 운영 담당자가 사용할 검토 가능한 절차를 제공한다. 기존 OPS-01 결과에 따르면 공개 DB는 `0017_guest_viewer`이고 게스트 및 원시 6시간/분 14일 프로필이 적용됐다. OPS-01을 다시 실행하거나 게스트 계정을 다시 생성하지 않는다.

## 준비와 적용 순서

1. `codex/kuls-backend-v2`를 Claude가 통합하고, 해당 코드의 Vercel 배포·기존 로그인·수집 ACK가 정상인지 확인한다. 원격 측정 설정은 아직 끈 상태로 둔다. 현재 `alembic_version`이 정확히 한 행의 `0017_guest_viewer`이고 기존 head17 권한이 적용되어 있어야 한다.
2. 쓰는 gateway를 정상 종료하고 simulator/gateway의 영속 SQLite·인증서·서비스 토큰을 보존한다. 기존 수집원·generation·sequence를 초기화하거나 새 PC에서 동시에 실행하지 않는다. DB와 영속 runtime의 일관된 백업을 안전한 위치에 보관하고 복원 경로를 확인한다. 비밀값이 있는 백업은 Git·채팅·일반 로그에 넣지 않는다.
3. [용량 점검 명령](PUBLIC_DEMO_RUN.md)을 읽기 전용으로 실행하고 시작 전 값을 기록한다. 새 proof는 raw 6시간 정리와 별도로 누적되므로 짧은 시연 예산을 적용한다.
4. 아래 생성물과 검토한 커밋이 일치하는지 확인한다. 출력은 **SQL 문자열의 JSON 배열**이며, Neon `/sql`의 문장 목록으로 각각 **하나의 트랜잭션** 안에서 실행해야 한다. 배열을 병렬 요청이나 문장별 독립 트랜잭션으로 실행하지 않는다. 연결 자격 증명은 기존 비밀 저장소로 전달하고 요청/응답 헤더를 출력하지 않는다.

   ```sh
   .venv/bin/python scripts/render_neon_migration.py clock-upgrade > /tmp/hmi-upgrade-0017-0018.json
   .venv/bin/python scripts/render_neon_migration.py clock-grants > /tmp/hmi-grants-0018.json
   cmp /tmp/hmi-upgrade-0017-0018.json deploy/neon-http/upgrade-0017-0018.json
   cmp /tmp/hmi-grants-0018.json deploy/neon-http/grants-0018.json
   ```

5. `SET ROLE hmi_migrator`가 가능한 운영자 연결에서 **upgrade-0017-0018.json 전체**를 원자적으로 적용한다. 정확한 시작 revision, advisory lock, migrator 소유권 경계를 유지한다. 같은 업그레이드 재실행은 시작 revision 불일치로 거절되는 것이 정상이다. 실패하면 중단하고 원인을 기록한다. TCP가 가능한 환경은 같은 커밋의 Alembic `0018_clock_evidence` 업그레이드를 대안으로 사용할 수 있으나 두 방식을 중복 적용하지 않는다.
6. 이어서 **grants-0018.json 전체**를 별도 원자적 배치로 실행한다. 원본은 [neon-clock-grants.sql](../../deploy/neon-clock-grants.sql)이다. 정확한 DB·head18·소유자·격리된 runtime 조건을 검사한다. 실패하면 측정 활성화를 진행하지 않는다. 기존 head17 권한 파일은 head18에서 실행하지 않는다.
7. `alembic_version=0018_clock_evidence`, `/api/ready`의 `ready:true`, 기존 로그인·분석 조회를 확인한다. `hmi_app` 연결로 두 신규 테이블 SELECT와 허용된 접수 경로를 확인하고 금지 UPDATE/DELETE/TRUNCATE/DDL 거절을 확인한다. 금지 DML 검사는 명시적 트랜잭션에서 수행하고 성공 여부와 관계없이 롤백한다. 카탈로그 권한 조회만으로 실제 DML 거절을 시험했다고 표시하지 않는다. 실제 runtime 연결을 사용할 수 없다면 그 검증은 미실행으로 남긴다.

## 원격 측정 활성화와 짧은 시연

API에서 `HMI_CLOCK_MEASURED_ROUND_TRIP=true`를 설정하고 재배포한다. 정확한 소문자 `true`만 사용한다. Vercel과 PC는 다른 호스트이므로 `same-host` 정책을 사용하지 않는다. 기존 보관·게스트·챗봇 키 설정은 이 절차의 변경 대상이 아니다.

같은 커밋의 로컬 PC supervisor는 아래 **선택적 인자**를 gateway에 전달한다. 기본은 꺼짐이다. [PC 시연 문서](PUBLIC_DEMO_RUN.md)의 기존 영속 상태·자격 증명·용량·WSL2 조건을 먼저 만족한 뒤에만 실행한다. 이는 명령 예시이며 이번 작업에서 공개 사이트로 보내지 않았다.

```sh
timeout --signal=TERM --kill-after=5s 295s \
  .venv/bin/python deploy/plant_runtime.py --directory "$HMI_DEMO_RUNTIME" serve \
  --token-file "$HMI_DEMO_TOKEN_FILE" --api-url https://hmi-mes.vercel.app \
  --clock-measured-round-trip
```

API와 gateway 둘 다 활성화되어야 한다. 기존 SQLite 대기 배치에는 새 측정을 붙이지 않는다. 프로브 실패·왕복 2초 초과·60초 초과 근거·clock 불연속은 UNKNOWN으로 남으며 수집을 기다리게 하지 않는다. 측정 이전의 과거 UNKNOWN을 GOOD으로 소급 변경하지 않는다.

짧은 실제 운전에서 새 캡처의 GOOD 트렌드, RUNNING 시간 > 0, 불명 구간의 근거, 원래 실행의 수량 대조를 확인한다. `/api/ready`나 OPC 품질 GOOD만으로 분석 시간 검증을 대신하지 않는다. 사용자 화면 결합은 Claude와 확인하며 공개 운전 요청은 기존 권한·Origin·CSRF와 작업자 판단을 따른다. 종료 후 DB 용량과 gateway 대기량을 다시 기록한다.

## 중단과 복구

수집 ACK·로그인·권한·시간 근거 검증이 실패하면 시연을 정상 종료하고 신규 원격 측정을 끈다. API의 opt-in을 끄고 재배포하며 gateway 인자도 제거한다. 이미 커밋된 시간 근거는 지우거나 재판정하지 않는다. schema18은 실제 근거가 있으면 downgrade를 거절한다. 복구에 DB 되돌림이 필요하면 백업 이후 신규 원본과 SQLite 큐의 정합성을 검토한 **명시적 전체 복원**을 사용한다. `downgrade -1`이나 테이블 삭제로 해결하지 않는다.

운영 결과는 실행 커밋·UTC 시각·revision·배치 단위 성공/실패·runtime 권한 실측 여부·시연 수치·전후 용량을 기록한다. 미실행을 통과로 바꾸지 않는다. 이 절차와 로컬 카탈로그 동등성 검사는 공개 배포 완료의 증거가 아니다.
