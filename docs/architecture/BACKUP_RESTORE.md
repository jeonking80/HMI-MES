# 정지 지점 전체 백업 세트와 별도 복원

`deploy/backup_set.py`와 `scripts/backup-set.py`는 로컬 PostgreSQL, 설정,
시뮬레이터 SQLite, gateway SQLite를 하나의 `backup_set_id`로 보관한다.
공개 DB와 기존 배포의 경로에는 기본값이 없다. 모든 입력·출력 경로를 지정한다.

## 운영 순서

1. 신규 사용자 명령, bootstrap, 보관 작업을 중지한다. PLC를 명시적으로 정지/일시정지하고
   결과 불명 명령은 원래 ID로 보존한다. API와 gateway를 중지한 뒤 simulator를 종료한다.
2. SQLite 소유자의 `.lock` 파일과 두 DB 파일을 그대로 둔다. 도구는 기존 lock에 배타 잠금을
   얻어 소유자가 종료했음을 확인한다. PostgreSQL schema advisory 배타 잠금과 모든 public
   테이블의 SHARE 잠금을 획득한 동안 dump와 SQLite backup API를 실행한다.
   비협조 DML writer도 table 잠금으로 차단되며 pg_dump의 ACCESS SHARE 읽기는 허용된다.
3. 별도 비밀 파일/환경에서 `HMI_BACKUP_DATABASE_DSN`을 주입한다. URL은 명시적 loopback
   PostgreSQL DB만 허용하며 URL query 연결 재정의는 거절한다. PostgreSQL 17 클라이언트를
   PATH에 설치한다. 비밀번호는 자식 환경으로만 전달한다.
4. 설정 JSON에는 `app_version`, `contract_version`, `schema_version`, `line_id`, `source_id`,
   `stream_id`, `counter_epoch`, `gateway_id`의 문자열 값과 아래 설명한 typed `controller`를
   넣는다. token, 개인키, 인증서,
   환경 파일 전체를 복사하지 않는다.

```sh
.venv/bin/python scripts/backup-set.py backup \
  --simulator /absolute/stopped/simulator.sqlite3 \
  --gateway /absolute/stopped/gateway.sqlite3 \
  --settings /absolute/nonsecret-settings.json \
  --backup-set-id manual-20261023-01 \
  --destination /absolute/backups/manual-20261023-01

.venv/bin/python scripts/backup-set.py restore \
  --directory /absolute/backups/manual-20261023-01 \
  --database-name hmi_restore_manual_20261023_01 \
  --destination /absolute/restore/manual-20261023-01
```

복원은 새 디렉터리와 새 `hmi_restore_` DB만 생성한다. 기존 DB 삭제·덮어쓰기·`--clean`은
없다. 실패한 별도 DB/경로는 진단용으로 남는다. 운영자가 정확한 대상 이름을 확인한 뒤
정리한다. archive는 압축 추출하지 않는 고정 멤버 디렉터리다. 추가 파일, 누락, symlink,
hardlink, checksum 불일치, 설정/manifest의 set ID 불일치를 거절한다.
SHA256은 손상/혼합 검출용이며 신뢰할 수 없는 발행자의 dump를 인증하지 않는다.
백업은 비밀번호 해시 등 민감한 DB 데이터를 포함하므로 디렉터리 0700/파일 0600으로
보호하고 승인된 보관 저장소의 암호화·접근 통제를 적용한다.

## 대조와 복원 기동

manifest는 실행 snapshot(물품과 세 수량 포함), 원본 사건, simulator command journal,
API 명령, 알람, 연속 커서/누락, gateway 미전송 배치를 저장한다. 복원 후 원본 inventory와
전체 행을 대조한 뒤 API와 console 세션을 취소하고 사용자 auth_version을 회전한다.
기존 서비스 token은 archive에 넣지 않는다. 복원 경로의 새 `service-token`(0600)을
새 key ID와 함께 API/gateway에 명시적으로 주입한다. 예전 key를 새 API에 재등록하지 않는다.

외부 OPC 연결과 제어를 차단한 상태에서 simulator를 연다. 실제 controller는 재시작 시
`RECOVERY_REQUIRED`와 출력 OFF로 시작한다. 복원 도구는 프로세스 시작, 자동 START,
명령 전달, recovery 승인 작업을 수행하지 않는다. 별도 인증·인증서를 준비한 다음 실제
API/OPC에서 예전 세션·서비스 키 거절, 새 키의 중복 ACK 회복, 원래 수량/커서를 확인한다.
자동 명령 dispatcher를 시작하기 전 운영자의 명시적 대조/재개 판단이 필요하다.

## 검증

```sh
.venv/bin/python -m pytest tests/integration/test_backup_set.py -q
HMI_ACCEPTANCE_ARTIFACTS=/tmp/restore-evidence .venv/bin/python -m pytest \
  tests/e2e/test_restore_acceptance.py -q --junitxml=/tmp/restore-junit.xml
```

통합 실행은 기존 disposable PostgreSQL fixture의 고정 PG17 이미지를 재사용하고, 해당
fixture의 loopback 포트로 식별한 컨테이너에서 실제 pg_dump/pg_restore를 실행한다.
새 이미지를 build/pull하지 않는다. 실제 소요 시간과 실행 결과는
[CX-18 보고서](../work-orders/reports/CX-18.md)에 기록한다.

복원 검증 API에는 `deploy.backup_set.recovery_control_gate(app)` wrapper를 추가로 적용할 수
있다. 이 wrapper는 command/work-order mutation에 423을 반환하고 조회·수집을 허용한다.
필수 복원 제어 제한과 해제는 아래 DB marker와 검증 resume CLI 절차를 따른다.
wrapper를 제거하는 것만으로 DB marker 제한은 해제되지 않는다.

## 최종 복원 제어 경계와 명시적 재개

복원 직후 DB 소유자 전용 PostgreSQL database COMMENT에
`HMI_MES_RECOVERY_REQUIRED:<backup_set_id>`를 기록한다. 일반 `create_internal_app`과
사용자 API가 사용하는 command enqueue/claim/submission/result 경계는 매 트랜잭션에서
공유 DB catalog를 읽어 이 marker가 있으면 거절한다. 기존 pool 연결이나 session SET으로
해제되지 않는다. 위 ASGI wrapper는 추가 방어이며 실제 필수 경계는 DB marker 검사다.

설정 JSON의 최종 키에는 위 8개 metadata와 `controller`가 필수다. `controller`에는
`process_config`(ProcessConfig의 모든 필드), `plc_config`(PLCConfig), `seed`만 넣는다.
`controller_settings(runtime)`으로 생성하고 `load_controller_settings(settings)`으로
재구성한다. 원래 실행 중 객체에서 설정을 가져오지 않는다. source/stream/counter_epoch,
gateway ID, line binding, schema revision은 백업 장벽 안의 실제 저장소와 일치해야 한다.
인증서/개인키/서비스 secret은 별도로 재주입하며 archive에 넣지 않는다.

재개 전 기존 배치 ACK를 회복하고 gateway와 simulator를 다시 종료한다. 운영자가 대조를
확인한 뒤 복원 DB를 가리키는 `HMI_BACKUP_DATABASE_DSN`으로 실행한다.

```sh
.venv/bin/python scripts/backup-set.py resume \
  --directory /absolute/backups/manual-20261023-01 \
  --restored-directory /absolute/restore/manual-20261023-01 \
  --operator-id verified-operator \
  --acknowledge-verified-recovery
```

이 owner 작업은 set/DB/manifest, 기존 실행·수량·명령·알람·커서, simulator 원본 사건과
정지 PLC, drained gateway/미해결 누락 없음을 다시 검증한다. 원본 사건 변경은 거절하며
재기동의 추가 STATE_CHANGED→RECOVERY_REQUIRED만 허용한다. DB COMMENT 제한만 명시적으로
해제하고 PLC는 여전히 RECOVERY_REQUIRED·출력 OFF다. 명령·START를 생성하거나 실행하지
않는다. 현장 조건 확인과 PLC 복구 승인·운전 재개는 별도 명시적 작업이다.
