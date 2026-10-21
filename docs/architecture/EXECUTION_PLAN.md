# 실행 목표 수량의 불변 등록

`0007_execution_plan`은 원래 실행의 목표 수량을 보존하는 `execution_plans` 테이블을 추가한다. `execution_id`는 `production_executions`를 참조하는 기본키이며 삭제를 제한한다. `target_quantity`는 BIGINT이고 DB CHECK는 1..4294967295를 허용한다. 기존 실행·생산 수량·사건·cursor는 변경하지 않으며 이전 실행의 목표 수량을 임의로 채우지 않는다.

CLI 등록은 목표 수량을 명시해야 한다. PostgreSQL DSN은 기존과 같이 `HMI_DATABASE_DSN`으로 주입하고 migration과 기준 정보·source 등록을 먼저 수행한다. 다음 예시의 ID와 파라미터는 실제 실행과 일치해야 한다.

```sh
.venv/bin/python -m alembic -c alembic.ini upgrade 0007_execution_plan
.venv/bin/python -m apps.api register-execution \
  --source-id source-local --stream-id events-main --execution-id execution-001 \
  --work-order-id work-001 --recipe-version recipe-v1 --counter-epoch epoch-001 \
  --temperature-c 180 --speed-m-per-min 10 --target-quantity 100
```

이 명령은 PLC 작업 연결이나 START를 수행하지 않는다. 후속 LOAD 검증은 저장된 원래 목표 수량과 명령 인자의 목표 수량을 비교할 수 있다. 계획 등록 자체가 사용자 제어 권한이나 실제 설비 접수를 증명하지 않는다.

`IngestionService.register_execution(..., target_quantity=100)`은 새 실행·초기 totals·계획을 같은 PostgreSQL 트랜잭션에서 등록한다. 계획 쓰기가 실패하면 실행과 totals도 rollback한다. Python 값은 엄격한 정수만 허용하며 bool·float·문자열·0·음수·UInt32 초과 값을 거부한다. 기존 stream 및 execution lock과 전체 실행 필드 비교를 유지한다.

같은 실행 ID와 같은 모든 필드·목표 수량의 재등록은 검증만 한다. 다른 목표 수량 또는 다른 작업·recipe·파라미터·counter epoch 등의 재등록은 충돌이며 기존 계획과 실적 수량을 덮어쓰지 않는다. 이전에 목표 수량 없이 등록한 실행에 수량을 추가하는 자동 보정도 거부한다. 기존 실행의 불명확한 계획은 명시적인 별도 복구 절차가 필요하다.

기존 라이브러리 호출과 이전 schema 호환을 위해 `target_quantity=None`은 선택적 legacy 경로로 남는다. 이 경로는 계획을 읽거나 생성·수정하지 않는다. 이미 등록된 계획이 있어도 legacy 재호출은 기존 실행 필드만 확인하며 저장된 목표 수량을 지우지 않는다. 수량 없이 등록한 실행은 새 LOAD 검증의 계획 증거를 갖지 못한다. CLI는 이 legacy 경로를 제공하지 않는다.

migration DDL은 새 revision에 고정되어 있으며 이전 `INGESTION_METADATA`와 기존 migration을 변경하지 않는다. `0006_settlement`로 downgrade하면 계획 테이블만 삭제하고 실행·totals를 보존한다. 재upgrade는 비어 있는 계획 테이블을 만들므로 계획은 자동 복원되지 않는다. downgrade는 폐기 가능한 시험 DB 또는 별도로 검토한 백업·운영 변경 절차에서만 수행한다.

```sh
.venv/bin/pytest -q tests/integration/test_execution_plan.py
```

실제 PostgreSQL 시험은 동일 등록 재검증·변경 거부·기존 실적 보존·계획 쓰기 실패 시 전체 rollback·이전 불명 계획 보정 거부·엄격한 UInt32 범위·DB CHECK/FK와 migration 전후 실행 보존을 확인한다. PLC LOAD 경로의 실제 통합 검증은 별도 제어 경로 시험이 담당한다.
