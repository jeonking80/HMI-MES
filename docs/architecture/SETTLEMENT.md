# 최소 실행 종료 정산 서비스

## 19차 현행 기준 (2026-10-26)

19차 사용자 승인으로 수집 원본 보관은 `HMI_INGEST_RETENTION_HOURS` 기본 168시간·최소 3시간을 사용한다. 3 미만·비숫자는 시작을 거절하고, 구 설정 `HMI_INGEST_RETENTION_DAYS`는 단독·동시·빈 값 모두 거절한다. 현재 runtime 자동 정리는 schema 0023·0024에서 실행하며 schema 0022의 보관 종료 조회·재전송 거절은 호환 지원한다. 아래 18차/CX-40의 일 단위·영구 개별 영수증 설명은 당시 이력이며 이 현행 기준으로 대체된다. 변경 이유와 영향은 [INGEST_RETENTION.md](INGEST_RETENTION.md)를 따른다.

정산·생산 사건·실적·최종 요약·알람·감사를 삭제하지 않는다. 저장된 SETTLED와 원래 실행의 사건·수량·설정·커서·누락/격리·미결 명령 보호 조건은 18차와 같다. 실제 FK가 참조하는 영수증은 개별 보존하고, 원본이 만료된 비참조 영수증만 정확 ID 청크·연속 범위로 압축한다. 압축과 삭제는 같은 제한된 유지보수 트랜잭션으로 커밋하거나 롤백한다.

영수증·범위·누적 해시는 원본 body/ACK·전체 clock proof를 복원하지 못한다. 오래된 실행을 원본으로 재검증할 수 없고 보고서의 `RETENTION_EXPIRED`/UNKNOWN은 수량이나 정산 결과를 정상으로 대체하지 않는다. 매 capture 알람 FK는 사용자 결정 **기존 참조 보존·한계 보고**에 따라 유지하므로 관련 영수증은 사건 수에만 비례하지 않는다.

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
커서 또는 원래 실행을 변경하지 않는다. 최종 경계 뒤 현재 source/stream head까지 사건이 있으면
그 구간 전체가 아래 복구 상태 꼬리 조건을 만족해야 SETTLED를 유지한다.
지연된 중복 사건은 기존 수집 중복 제거에 따라 원래 실행에 남는다.
서비스가 ACK를 만들거나 커서를 진행시키지 않는다. 별도 수집 트랜잭션이 커밋한 verified 커서와
실적을 확인하므로 최종 요약만 먼저 도착한 상태는 PENDING이다.

## 12차 변경: 종료 뒤 복구 상태 꼬리

변경 이유: 정상 종료 뒤 재시작은 STOPPED→RECOVERY_REQUIRED→STOPPED 사건을 남긴다.
수량과 물품에 영향을 주지 않는 이 사건까지 일률적으로 종료 경계 위반으로 취급하면 다음 작업을
영구히 연결할 수 없으므로, 12차 승인에 따라 API 정산과 PLC 원격 근거를 함께 변경했다.

허용 범위는 `(final.last_event_seq, 현재 source/stream head]` 전체다. 사건 종류가
STATE_CHANGED이고 원본 전체가 계약 검증을 통과하며 적용 완료여야 한다. previous_state와
state는 모두 STOPPED 또는 RECOVERY_REQUIRED여야 하고, 순번이 빠짐없이 연속이며 누락·격리가
없어야 한다. 마지막 상태는 STOPPED여야 한다. 빈 꼬리는 현재 head가 최종 경계와 같고
후속 누락 근거도 없을 때만 허용한다. verified 커서는 꼬리 전체를 포함해야 한다.

생산·불량·폐기·다른 사건, RUNNING을 포함한 그 밖의 전이, 마지막 RECOVERY_REQUIRED,
미적용·잘못된 계약·순번 누락은 계속 MISMATCH / EVENT_AFTER_FINAL_BOUNDARY다.
원래 경계까지의 누락·격리·미수신, source 충돌, 미결 명령, 설정·epoch·수량 검사와
최초 최종 요약 불변 규칙은 그대로 유지한다. 원본 사건·귀속·순번·최종 요약을 수정하지 않는다.

영향: 이 판정은 명시된 전체 stream 꼬리에 적용한다. 이후 다른 실행의 정상 생산 사건이
기록된 뒤 과거 실행을 다시 정산하면 그 과거 실행도 MISMATCH가 될 수 있다. 이를 예외 처리해
기준을 넓히지 않는다. 과거의 고정 최종 요약과 생산 귀속·합계는 바뀌지 않으며,
SETTLED는 영구 완료 래치가 아니라 현재 stream 근거로 수행한 재검산 결과다.
API bind 접수만으로 PLC 실행 성공을 뜻하지 않는다. PLC는 별도 물리 조건과 같은 꼬리 근거를
검사하고 admission 시 고정한 head/hash가 실행 tick까지 그대로인지 확인한다.

마이그레이션 `0006_settlement`는 `0005_command_dispatch` 다음에 두 정산 테이블만 생성한다.
다운그레이드는 두 정산 테이블과 그 증거를 제거하므로 운영 적용 전 백업이 필요하다.
기존 실행·사건·실적·커서·명령·사용자 데이터는 변경하지 않는다.
DDL은 현재 모델 import 없이 고정한다.

검증: `.venv/bin/python -m pytest tests/integration/test_settlement.py -q`.
별도 PostgreSQL 17 컨테이너에서 pending → 역순 수집 → backfill → settled,
수량·설정 불일치, 반복/충돌 고정, 인증 범위, 격리·영구 누락, source 상태,
미확정 실행 명령·종료 상태의 명령 ID 충돌, 최종 경계 이후 사건, 호출자 롤백과 마이그레이션 왕복을 검증한다.
이는 M2 최소 서비스 검증이며 전체 M2·MES 업무·수락 시험 완료를 뜻하지 않는다.

## 18차 보관 종료가 정산에 미치는 영향 (이력)

변경 이유: 사용자가 정산 완료 수집 원본을 기본7일 뒤 정리하도록 승인했다. event_inbox·production_events·실적·최종 요약·정산 결과는 삭제하지 않는다. PENDING/MISMATCH/CONFLICT 원본은 정리 대상이 아니다.

정리 함수는 마지막으로 저장된 SETTLED와 원래 실행의 적용 사건·verified 경계·수량·원래 baseline/변경 이력·관련 누락/격리/미결 명령을 재검증한다. 뒤의 다른 실행 사건만으로 옛 원본을 영구 보관하지 않지만, 같은 실행의 의미 있는 종료 후 사건·불완전 복구 꼬리·낡은 SETTLED 불일치는 삭제를 막는다. 기존 정산 API의 전체 stream 재검산/상태 전이 규칙은 변경하지 않는다. 이미 MISMATCH 등으로 갱신된 실행은 삭제하지 않는다.

원본 body/ACK·전체 clock proof가 종료된 뒤에는 오래된 실행을 그 원본으로 다시 검증할 수 없다. 남은 사건/실적/요약을 원래 태그·통신·시각 근거 전체가 남아 있는 것으로 확대하지 않는다. 보고서의 RETENTION_EXPIRED/UNKNOWN 표시는 실적 수량을 삭제하거나 임의로 정상 정산으로 바꾸지 않는다. 상세 기준과 한계는 [INGEST_RETENTION.md](INGEST_RETENTION.md)를 따른다.
