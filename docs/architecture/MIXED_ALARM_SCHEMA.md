# Mixed 알람 준비 A: schema 경계와 Line fault 증거 인덱스

`0014_mixed_alarm_runtime`은 `0013_work_order_cancel` 다음의 명시적 migration이다. 기존 알람·원본 capture·사건·수량을 변경하지 않고 Line fault 증거 조회용 partial index를 추가한다. 이 단위는 mixed consumer·runtime·CLI 연결을 구현하지 않는다. 기존 JSON을 새 coordinator로 정규화하는 전환과 실제 OPC mixed 경로는 후속 단위다.

## 명시적 revision 경계

새 coordinator의 start·fresh consume·invalidate 쓰기는 별도 `SUPPORTED_MIXED_ALARM_REVISIONS`의 실제 0014 head만 허용해야 한다. 일반 alarm-capture 지원 목록에 0014를 추가하는 것은 기존 자료·라이브러리의 schema 호환을 위한 것이며 새 coordinator의 안전 검증을 대신하지 않는다. 이 단위에서는 그 쓰기 경로가 아직 구현되지 않았다.

배포된 0013 binary의 고정 지원 목록에는 0014가 없으므로 기존 schema admission이 해당 head에서 시작·수집을 거부한다. 새 binary에서 과거 형식 JSON을 허용하더라도 최종 public startup 경로는 전체 durable source roster를 확인하는 새 coordinator를 거쳐야 한다. 중간 단계의 generic 지원 목록 변경을 mixed runtime 완성 또는 배포 승인으로 해석하지 않는다.

```sh
.venv/bin/python -m alembic -c alembic.ini upgrade 0014_mixed_alarm_runtime
```

운영자가 명시적으로 적용하며 API가 migration·source·규칙을 자동 생성하지 않는다.

## Partial index와 원래 증거

```sql
CREATE INDEX ix_inbox_applied_line_fault_seq
ON event_inbox(source_id,stream_id,seq DESC)
WHERE applied_at IS NOT NULL
  AND kind='FAULT_CHANGED'
  AND body->>'equipment_id'='Line';
```

기존 source/stream/seq 일반 인덱스만 사용하면 최신 capture의 EventSeq 아래에서 오래된 Line 고장을 찾기 위해 다수 생산 사건을 역방향으로 검사할 수 있다. 새 인덱스는 **적용된 논리 Line fault 사건만** 대상으로 한다. 최신 fault의 `seq<=ceiling ORDER BY seq DESC LIMIT 1` 및 reset 이전 activation의 `seq<reset_seq` 두 원래 query를 지원한다. 직전 STATE_CHANGED는 기존 고유 source/stream/seq 인덱스로 한 순번을 조회하므로 추가 인덱스를 만들지 않는다.

ceiling은 기존 store가 Good Line.EventSeq와 verified_through_seq 중 작은 경계로 결정한다. 인덱스는 proof의 신뢰 범위·원래 body·사건 순서·규칙을 바꾸지 않는다. 미적용 사건은 인덱스에서 제외되며 적용 후에만 추가된다. 사용자 JSON이나 최신 다른 사건을 원래 증거로 대신 사용하지 않는다.

## Transaction과 downgrade

기존 Alembic env.py가 exclusive schema transaction lock을 획득한다. CREATE INDEX와 revision 갱신을 같은 transaction에서 처리하며 CONCURRENTLY/autocommit를 사용하지 않는다. 뒤 단계 DDL/기록 실패 시 인덱스와 head가 함께 rollback되어야 한다.

`alarm_capture_frontiers` **또는** `alarm_capture_decisions`에 한 행이라도 있으면 downgrade를 거부한다. legacy Sealer-only, requires_restart=true, 현재 alarm 없음도 예외가 아니다. 행의 생존·비활성 여부를 추정하거나 삭제해서 구 binary를 다시 허용하지 않는다. 둘 다 비어 있으면 인덱스를 제거하고 0013으로 되돌릴 수 있으며 기존 규칙·업무 자료를 보존한다.

Schema lock의 두 순서는 독립적으로 확인한다. shared lock의 writer가 먼저 frontier를 commit하면 migration이 기다린 뒤 사용 상태를 보고 downgrade를 거부한다. 빈 상태 downgrade가 먼저 exclusive lock을 얻으면 뒤 shared reader는 commit된 0013을 본다. 이는 migration의 직렬화 증거이며 **후속 coordinator가 해당 0013에서 실제 쓰기를 거부한다는 통합 증거는 아니다**. 그 검사는 B 단위가 구현된 후 실제 start/consume/invalidate로 수행한다.

## 집중 검증과 한계

```sh
.venv/bin/ruff check apps/api/migrations/versions/0014_mixed_alarm_runtime.py \
  tests/integration/test_mixed_alarm_migration.py
.venv/bin/pytest -q tests/integration/test_mixed_alarm_migration.py \
  tests/integration/test_alarm_consumer.py
```

Unit A 시험은 실제 disposable PostgreSQL과 production Alembic 환경을 사용한다. schema 보존·빈 roundtrip·모든 기존 frontier/decision의 downgrade 거부·late DDL 실패 rollback과 재시도·실제 schema lock 양방향 직렬화를 확인한다. 고정 0013 지원 정책을 실제 ingestion admission에 적용한 거부 시험은 지원 목록의 경계를 검사하며 전체 과거 executable 실행을 주장하지 않는다.

Query-plan fixture는 적용 inbox 120000행 가운데 119997개 생산 사건과 희소 Line fault 순번 10/60000, 직전 Line state 59999를 둔다. 최신 query는 ceiling120000에서, 이전 activation query는 seq60000 아래에서 각각 약 60000개의 무관한 사건을 건너뛰어야 하는 조건이다. 양쪽 exact query에 EXPLAIN(ANALYZE,BUFFERS,FORMAT JSON)을 migration 전후 실행한다. `enable_seqscan=off` 같은 planner 강제 없이 partial index 선택·대량 filter scan 부재·동일 원래 결과를 확인하고 JSON 계획을 시험 임시 디렉터리에 보존한다. 미적용 fault의 제외 및 적용 시 인덱스 유지도 확인한다. 이는 합성 inbox SQL workload이며 실제 생산 ingestion 처리량·장시간 부하 수락 시험과 구별한다.

실제 실행 count·시간과 독립 검토 결과는 root의 개발 상태 기록에 남긴다. coordinator source roster·모든 채널 잠금·원래 replay·UNKNOWN EventSeq highwater·실제 AppA RESET→gateway→OPC→PLC 경로는 이 schema 단위만으로 완료되지 않는다.

### 이 단위의 실행 결과

Owner 집중 검증은 schema/index 10개와 현재 analog consumer 22개, 총 **32 passed in 32.30s**다. 두 기존 migration 시험은 그대로 통과했으며 historical fixture를 변경하지 않았다. 시험이 기록한 정확한 query plan의 이번 일회 측정은 다음과 같다.

| Query | migration 전 | migration 후 |
|---|---|---|
| latest fault | parallel sequential scan, shared hit3521, 35.286ms | partial index, hit1/read1, 0.025ms |
| previous activation | parallel sequential scan, shared hit3521, 16.123ms | partial index, hit2/read0, 0.013ms |

이전 filter 제거 수는 parallel worker loop3의 평균 39999/40000이고 이후 두 query는 0이다. 두 query 모두 원래 결과가 같았다. 시간은 해당 disposable DB·합성 자료·한 실행의 관측이며 일반 성능 보장이나 전체 수집 부하 검증이 아니다. JSON 계획은 pytest 임시 artifact `line-fault-explain.json`으로 보존된다. 향후 실행 시간과 planner 선택은 환경에 따라 달라질 수 있다.
