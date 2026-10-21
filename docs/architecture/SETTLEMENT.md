# 최소 실행 종료 정산 서비스

`apps/api/settlement.py`는 PostgreSQL의 호출자 트랜잭션에서 실행한다.
HTTP·OPC UA·게이트웨이 전달 경로·화면은 이 작업에 포함하지 않는다.
호출자는 검증된 게이트웨이 인증에서 얻은 gateway/source/stream을 전달해야 한다.
서비스는 등록된 바인딩과 원래 execution_id를 다시 확인하고 line → source → execution
순서로 잠근다. 반환 결과는 호출자 커밋 이후에만 영속 결과다.

- `record_final_summary(connection, summary, *, authenticated_gateway_id,
  authenticated_source_id, authenticated_stream_id, now)`는 실제 최종 `RunSummary`만 받는다.
- `reconcile_execution(connection, execution_id, *, 동일 인증 범위, now)`는 저장된 최종 증거로
  재검산한다. 누락 사건의 수집 트랜잭션을 커밋한 뒤 다시 호출한다.
- `get_settlement(connection, execution_id, *, 동일 인증 범위)`는 마지막 검사 결과를 읽는다.
  다음 작업 연결을 허용하는 제어 경로는 이 저장 결과만 신뢰하면 안 된다.
  같은 트랜잭션 안에서 `reconcile_execution`을 호출하고 `SETTLED`를 확인해야 한다.
  해당 line/source 잠금을 유지한 채 후속 결정을 저장한다. 실제 작업 전환 연동은 후속 범위다.

`SettlementResult`는 execution_id, status, reason_code, 최초 summary, checked_at을 제공한다.
최종 요약이 없으면 `PENDING / FINAL_SUMMARY_REQUIRED`이며 summary와 checked_at은 없다.

| 상태 | 의미 |
|---|---|
| PENDING | 최종 증거·연속 사건·격리 사건 해소·종료 명령 확인 등이 더 필요함 |
| SETTLED | 현재 검산에서 사건 경계·등록 설정·수량이 일치함 |
| MISMATCH | 등록 설정/epoch, 양품·불량·폐기 합계 또는 종료 이후 원래 실행 사건이 불일치함 |
| CONFLICT | 최종 요약 충돌 또는 source 식별 충돌. 자동 해제하지 않음 |

최초 최종 요약 JSON은 변경하지 않는다. `as_of`만 다른 반복 조회는 같은 고정 경계로
취급하며 최초 전체 JSON을 유지한다. 종료 시각·순번·수량·설정 등 다른 내용은 별도
`run_summary_conflicts` 증거로 보관하고 `CONFLICT`를 고정한다. 중복 충돌도 한 번 보관한다.
자동 충돌 해소 또는 관리자 수정 절차는 제공하지 않는다.

`RunSummary` 계약 검증이 ENDED·재공 0·종료 시각·수량 보존을 확인한다.
서비스는 원래 등록된 실행의 recipe/parameters/counter_epoch를 대조하며 등록값을 덮어쓰지 않는다.
source 식별 충돌/reconciliation_required, 최종 순번까지의 영구 누락·격리 사건,
최종 순번 미만의 verified 연속 커서가 있으면 완료하지 않는다.
같은 stream의 과거 누락·격리는 보수적으로 차단하며 현재 실행과 무관하다고 추정하지 않는다.
원래 실행의 명령 종류와 관계없이 REQUESTED/DELIVERED/ACCEPTED/OUTCOME_UNKNOWN인 명령이나
collision_latched가 있는 명령은 `PENDING / EXECUTION_COMMAND_UNRESOLVED`로 둔다.
종료 상태 명령의 충돌도 해소된 것으로 취급하지 않으며, 다른 실행의 명령은 과거 정산을 막지 않는다.
최종 요약만으로 이러한 미확정 명령 결과를 해소하지 않는다. 현장 콘솔에서 종료한 실행처럼 앱 A 종료 명령이
없어도 최종 PLC 증거와 모든 사건이 일치하면 정산할 수 있다.

양품·불량·폐기는 영속 사건 실적과 각각 일치해야 한다. 요약 누적값을 실적에 더하지 않는다.
커서 또는 원래 실행을 변경하지 않는다. SETTLED 이후 원래 실행의 경계 이후 사건이 들어오면
다음 재검산은 MISMATCH가 된다. 지연된 중복 사건은 기존 수집 중복 제거에 따라 원래 실행에 남는다.
서비스가 ACK를 만들거나 커서를 진행시키지 않는다. 별도 수집 트랜잭션이 커밋한 verified 커서와
실적을 확인하므로 최종 요약만 먼저 도착한 상태는 PENDING이다.

마이그레이션 `0006_settlement`는 `0005_command_dispatch` 다음에 두 정산 테이블만 생성한다.
다운그레이드는 두 정산 테이블과 그 증거를 제거하므로 운영 적용 전 백업이 필요하다.
기존 실행·사건·실적·커서·명령·사용자 데이터는 변경하지 않는다.
DDL은 현재 모델 import 없이 고정한다.

검증: `.venv/bin/python -m pytest tests/integration/test_settlement.py -q`.
별도 PostgreSQL 17 컨테이너에서 pending → 역순 수집 → backfill → settled,
수량·설정 불일치, 반복/충돌 고정, 인증 범위, 격리·영구 누락, source 상태,
미확정 실행 명령·종료 상태의 명령 ID 충돌, 최종 경계 이후 사건, 호출자 롤백과 마이그레이션 왕복을 검증한다.
이는 M2 최소 서비스 검증이며 전체 M2·MES 업무·수락 시험 완료를 뜻하지 않는다.
