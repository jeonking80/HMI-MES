# 게이트웨이 영속 버퍼 · DEV-09

`apps/gateway/buffer.py`의 `GatewayBuffer`는 게이트웨이 전용 POSIX SQLite 파일에 원본 수집 배치·전송 상태·확정 ACK·명시적 손실 근거를 저장한다. API나 시뮬레이터 DB를 열지 않는다. HTTP 전송과 OPC UA 읽기는 상위 워커가 담당한다. 버퍼에 `DurableAck` 객체를 전달하는 것만으로 원격 커밋을 입증할 수는 없으므로, 인증된 HTTP 어댑터는 실제 API 커밋 뒤 수신한 COMMITTED 응답만 전달해야 한다.

## 수집·재시도·ACK

새 파일은 권한 0600으로 만들고 WAL·`synchronous=FULL`·외래키를 사용한다. 애플리케이션 ID와 스키마 버전으로 다른 앱의 DB를 거절하며, 경로를 정규화한 파일 잠금으로 단일 프로세스 소유를 강제한다. 하드링크는 지원하지 않는다. 재시작 시 미완료 INFLIGHT 배치를 PENDING으로 돌려 동일 ID·동일 내용으로 재전송한다.

`capture_position`은 영속 capture generation과 마지막 capture sequence다. `enqueue(batch, now=..., latest_read_seq=...)`는 다음 sequence의 불변 배치, 최신 읽기 위치, 필요한 손실 기록을 한 트랜잭션으로 확정한다. 중복 batch ID는 같은 해시이면 기존 상태를 반환하고 다른 내용이면 거절한다. UInt64는 SQLite의 signed integer 범위를 피하는 고정 길이 십진 문자열로 보존한다. generation 변경이나 sequence 고갈은 자동 초기화하지 않는다.

`lease_next()`는 수집 순서의 PENDING 배치를 INFLIGHT로 바꾸고 시도 횟수를 증가시킨다. `retry(batch_id)`는 원본 배치를 PENDING으로 돌린다. 재시도 과정에서 sample ID, 사건 ID, 수집 시각, SourceTimestamp·ServerTimestamp, 품질, 원래 값이나 해시를 다시 만들지 않는다.

`acknowledge(ack)`는 batch/gateway ID·해시·stream·모든 사건의 개별 처리 결과·제출한 gap 근거를 확인한다. 수신 배치 삭제, 불변 ACK 보관, API 진행 위치 갱신은 같은 트랜잭션이다. 확정 원장·ACK의 로컬 보존 기간은 기본 2시간이며, 아래 16차 C안 정리 규칙을 모두 충족한 행은 위치와 관계없이 삭제할 수 있다. QUARANTINED 사건도 API가 영속 인수했으면 원본 전송 배치를 해제하지만, 검증 완료 커서를 사건 위치까지 올리지 않는다. 동일 배치의 확정 ACK가 달라지면 거절한다.

`progress(source_id, stream_id)`는 최신 OPC 읽기 위치와 API의 `latest_ingested_seq`·`verified_through_seq`를 구분한다. API 진행 위치는 뒤로 이동하지 않는다. 과거 ACK는 자신의 원본 receipt로 보관하되 최신 진행 위치를 되돌리지 않는다. 같은 진행 위치라도 이미 수신이 확인된 영역을 다시 missing으로 만들지 않도록 기존 gap과 새 gap의 교집합 및 새 ingestion 영역만 남긴다. 남아 있는 겹친 구간의 irrecoverable 근거·사유와 영향받는 execution ID는 뒤늦은 ACK로 약화하지 않는다. quarantine 때문에 verified 위치가 0에 머물러도 복구한 gap을 뒤늦은 ACK나 벽시계 순서로 다시 열지 않는다. 서로 비교할 수 없는 두 커서 조합은 명시적 대조 오류다.

## 30분·100 MB와 손실 근거

기본 한도는 대기 배치의 나이 30분과 논리적 저장량 100,000,000 bytes다. 새 배치 수집 시 오래된 배치부터 한도를 적용하고, 제거한 배치마다 `BUFFER_AGE_LIMIT` 또는 `BUFFER_BYTE_LIMIT` 근거를 원자적으로 기록한다. INFLIGHT 배치는 제거하지 않으며, 용량 압박이면 `BufferFull`로 전체 수집 트랜잭션을 취소한다. 단일 배치 자체가 한도를 넘으면 ID를 부여하기 전 수집을 나누어야 한다.

사건 손실은 원래 연속 구간별 inclusive 범위와 execution ID를 보관한다. 1·2·5번 사건을 제거했으면 1–2와 5–5를 기록하며 중간 사건까지 손실로 넓히지 않는다. 샘플 손실은 sample ID·tag·품질·원래 시각을 보관하며 읽기 값은 제거한다. 샘플 과거 값을 복구했다고 표시하지 않는다. 원래 gap 보고도 보존한다.

`pending_event_gaps(source_id, stream_id, limit=100)`는 새 불변 보고 배치에 실을 원본 gap DTO를 반환한다. 겹치는 근거는 별도 배치로 보고한다. `enqueue`가 각 구간을 보고 배치에 연결하고, 해당 배치가 제거되면 보고 대상이 다시 열린다. 확정 ACK 뒤에는 재보고하지 않지만 `losses()`의 손실 이력은 남긴다. 보고 ACK는 사건 복구·정산 완료를 뜻하지 않으며 실제 원본 사건의 재조회와 검증이 필요하다.

영수증·손실·보고 연결·커서의 별도 논리적 예산은 기본 50,000,000 bytes다(13차 승인). 이 예산이 가득 차면 근거를 삭제하는 대신 `METADATA_FULL`로 쓰기 전체를 취소한다. 현재 가역적인 ACK·유실 본문 압축과 아래 C안의 확정 원장 보존 종료를 제공한다. 유실 근거는 삭제하지 않는다. 별도 서버 반출·재조회 설계(B안, CX-27 §3)는 미구현이며 C안의 제약을 없애지 않는다. 한도는 JSON bytes와 행별 여유분을 계산한 논리적 예산이며 SQLite 파일·인덱스·페이지·WAL의 물리적 디스크 상한은 아니다. `status(now=...)`는 논리적 사용량과 DB·WAL·shared memory의 실제 bytes를 따로 제공한다.

나이 한도는 `enqueue` 시 적용한다. 새 수집이 없으면 타이머가 자동 삭제하지 않으며 `status`의 `age_limit_reached`로 상태를 확인한다. 나이 계산은 호출자가 제공한 UTC 시각을 사용한다. 운영 디스크 모니터링, 벽시계 관리, 재조회·재시도 주기와 backpressure 처리는 실행기의 책임이다.

## 복구 검증과 범위

재개 시 원본 payload와 해시·capture 원장, QUEUED/ACKNOWLEDGED/EVICTED 소유 관계, 커서, 손실 구간, 보고 배치와 ACK의 연결을 대조한다. 손상된 영속 데이터는 정상 값으로 대체하지 않고 `BufferCorruption`으로 쓰기를 차단한다.

```sh
.venv/bin/ruff check apps/gateway/buffer.py tests/integration/test_gateway_buffer.py
.venv/bin/pytest -q tests/integration/test_gateway_buffer.py
```

시험은 enqueue와 ACK 트랜잭션의 커밋 직전·직후 실제 자식 프로세스를 종료하고 파일을 다시 열어 원자성을 확인한다. 원본 재시도, 잘못된 ACK, quarantine, 순서가 바뀐 ACK와 복구 gap, UInt64 최대값·1,000개 사건, 나이·용량·메타데이터 한도, INFLIGHT 보호, 반복 제거된 gap 보고, 영속 데이터 변조도 검증한다. 이 로컬 버퍼 시험만으로 실제 API의 PostgreSQL 커밋, 30분 실제 단절·복구, 장시간 용량 운영 또는 T01–T12 전체 통과를 주장하지 않는다.

## 13차 한도 변경 근거와 한계

10MB는 단절 버퍼와 별개로 1초마다 쌓이는 확정 원장을 충분히 반영하지 못했다.
사용자 A안 결정으로 메타데이터 한도만 50MB(50,000,000 bytes)로 늘렸다.
대기 배치의 30분·100MB 한도, 원장·손실 근거 보존, 안전한 쓰기 차단 규칙은 유지한다.

2026-10-25 운영 SQLite의 일관된 사본을 읽기 전용으로 한 번 실측했다.
원장 9,644행/8,854,082B, 커서 1행/656B, 손실 근거 453행/1,145,112B,
손실 연결 0행/0B로 메타데이터 합계는 9,999,850B였다. 대기 배치는 0건이다.
9차 적용 직후의 보존된 실측(원장 4,753행, 합계 5,413,421B)과 비교하면
4,891건 동안 4,586,429B 증가, 약 937.73B/수집 1건이다.
이 평균을 1Hz로 외삽하면 빈 50MB 예산은 약 14.81시간, 현재 사용량 이후의
추가 여유는 약 11.85시간이다. 재전송·손실·압축 상태에 따라 달라지는 관측 평균이며
단일 실행의 고정 증가율 또는 보장 시간이 아니다. 현재 snapshot 평균은 원장 자체
918.09B/행, 전체 예산을 원장 행 수로 나누면 1,036.90B/행이다.

50MB는 장기 고정 용량 해결이 아니다. 원장은 계속 증가하며 한도에 닿으면
METADATA_FULL로 수집 쓰기를 취소한다. 프로세스 자체가 반드시 종료되는 것은 아니다.
장기 운영용 서버 보관 설계(CX-27 §3)는 여전히 미설계·미구현이다. 공개 DB 무료
용량 또한 별도로 확인해야 하며 이 로컬 한도가 공개 DB 운전 시간을 보장하지 않는다.
운영 중인 프로세스의 기본값은 자동으로 바뀌지 않는다. 배포 후 별도 사용자 요청으로
정상 종료·새 코드 재기동할 때 적용하며 기존 SQLite는 초기화하지 않는다.

## 15차 C안 기록: 확정 원장 보존 종료와 체크포인트 (아래 16차가 대체)

변경 이유: 확정 원장을 무기한 로컬 보관하면 정상 통신 중에도 계속 증가한다.
사용자가 승인한 store-and-forward C안으로 서버가 이미 커밋한 오래된 원장만
현장에서 정리한다. 서버 ingest_batches의 기존 `(gateway_id, batch_id)` 고유 키와
내용 해시·원래 ACK 재응답을 유지하며 서버 동작은 변경하지 않는다.

`BufferLimits.ledger_retention` 기본값은 7일이다. 시험은 짧은 값을 명시할 수 있다.
`prune_acknowledged(now=..., limit=1000)`는 다음 조건을 전부 만족하는 연속 앞부분만
삭제한다. ACKNOWLEDGED이고 저장된 전체 DurableAck가 COMMITTED 계약·gateway·batch·
내용 해시·source/stream에 일치해야 한다. UTC now에서 ACK의 서버 committed_at을 뺀
시간이 보존 기간 이상이어야 한다. checkpoint 다음 capture_seq부터 끊김이 없어야 하고,
outbound_batches·missing_ranges.batch_id·loss_intervals.report_batch_id 참조가 없어야 한다.
첫 미확정·EVICTED·참조·보존 기간 안의 행에서 멈추며 그 뒤의 확정 행을 건너뛰지 않는다.

SQLite schema v2의 ledger_checkpoint 1행에 generation·last_capture_seq·누적 SHA-256·
삭제 건수·updated_at·체크포인트 checksum·논리 metadata_bytes를 보관한다. 누적 해시는
gateway 식별과 이전 hash, 각 원장의 generation/seq/ID/내용·envelope 해시·source/stream·
정규화된 전체 ACK를 묶는다. 압축 표현이 달라도 같은 의미의 ACK를 사용한다.
체크포인트 갱신·원장 삭제·메타데이터 감소는 한 트랜잭션이다. 오류면 전부 롤백한다.
이 해시는 보존된 신뢰 상태의 연속성 근거이며 삭제된 원본 ACK를 복원하거나 서버 커밋을
새로 입증하는 수단이 아니다.

기존 v1 DB는 전체 원래 원장을 먼저 검증한 뒤 빈 체크포인트와 v2 marker를 같은
트랜잭션으로 만든다. v2에서 체크포인트가 없으면 재생성하지 않고 손상으로 차단한다.
시작 검증은 checkpoint+1부터 마지막 capture까지 남은 원장이 연속인지 확인한다.
전체가 삭제됐으면 checkpoint가 저장된 capture 위치와 같아야 한다. 미지 버전·세대·
checksum·계수·잔여 순번 불일치도 BufferCorruption이다. SQLite 초기화나 generation
전환이 아니다. 이전 코드로의 단순 재오픈은 v2 marker를 거절하므로 롤백은 사전 백업이 필요하다.

worker는 monotonic 기준 1시간마다 ACK 경로와 별도 coroutine에서 최대 1,000행을 정리한다.
SQLite 소유 스레드와 짧은 단일 트랜잭션을 유지하며 prefix 후보만 검증한다. 논리 사용량
SUM 계산은 남은 표 크기에 영향을 받으므로 고정 실행 지연을 보장하지 않는다. enqueue는
예상 새 원장·초기 cursor 예산 또는 90% 압력에서 eligible 앞부분 정리를 먼저 시도한다.
압력 정리는 별도 커밋되어 후속 enqueue가 실패해도 되돌리지 않는다. ACK에서는 삭제하지
않는다. 큰 ACK나 첫 보존 행 때문에 부족하면 기존 METADATA_FULL로 쓰기를 차단한다.

영향·제약:

- 보존 기간이 지난 삭제 ID의 로컬 중복/원래 ACK 조회는 사라진다. 새 capture 순번에서
  같은 ID가 오면 서버가 원래 영수증 또는 충돌로 판정한다. checkpoint 이하 순번은 로컬에서
  여전히 거절한다. 미확정 요청을 새 ID로 자동 재전송하지 않는다.
- UTC 호스트 시각과 서버 committed_at의 차이에 의존한다. 미래 ACK는 보존하고 뒤로 가는
  now는 나이를 줄이지만 호스트의 큰 전진 오차를 독립 검증하는 기능은 없다.
- 삭제 논리 bytes는 즉시 줄어도 SQLite 파일·인덱스·WAL 크기가 즉시 줄지는 않는다.
  capture 연결에서 VACUUM하지 않는다.
- EVICTED 또는 유실 참조가 앞부분을 막으면 뒤의 오래된 ACK도 영구히 남을 수 있다.
  유실 기록 삭제나 우회 체크포인트 진행을 제공하지 않는다.
- 13차 평균 937.73B/초에서 기본 7일분은 약 567MB여서 50MB보다 크다. 따라서 기본값으로는
  첫 정리 가능 시점 전에 여전히 한도에 닿을 수 있다. 짧은 시험 보존 기간의 가속 24시간
  일정 용량 결과를 기본 운영값의 장기 용량 보장으로 해석하지 않는다.
- 운영 DB는 이번 구현 작업에서 열거나 이관하지 않는다. 배포 후 별도 승인된 정상 종료·
  백업·재기동 절차로 적용한다. 서버 보관 B안·유실 근거 장기 정책은 미구현으로 남는다.

## 16차 보정: 2시간 보존·중간 예외와 삭제 구간

변경 이유: 15차7일 보존은50MB 예산이 먼저 차고, capture102~554의 EVICTED453행과
유실 근거가 연속 앞부분 삭제를 영구히 막았다. 사용자 승인16차는 15차의 기간과 prefix 규칙을
대체한다. 보존 기본값은2시간이며 30분 대기 한도에 재전송·재시작 여유를 더한 값이다.
`ledger_retention < max_age`이면 설정/시작을 거절한다. 시험의 짧은 보존 기간도 같은 제약을
지켜야 한다. 30분·100MB·50MB 및 METADATA_FULL 차단은 변경하지 않는다.

정리는 ACKNOWLEDGED·유효한 서버 COMMITTED ACK·나이 경과·미참조·대기열 없음 조건을
모두 만족한 행에만 적용한다. 중간의 미확정·젊은 ACK·EVICTED·유실 참조 행을 그대로 두고
그 뒤의 적격 행도 검사한다. 삭제 한도 N은 지울 행 수다. EVICTED와 유실 기록은 삭제하지 않는다.

schema v3는 단일 checkpoint 대신 `ledger_pruned_ranges`를 쓴다. 각 행은 generation,
from_capture_seq, through_capture_seq, 누적 SHA-256, 삭제 건수, recorded_at, 구간 checksum,
metadata_bytes를 보관한다. 연속 삭제는 하나로 합치고 새 삭제가 좌·우 기존 구간을 이으면
양쪽 모두 합쳐 불필요한 구간을 남기지 않는다. 구간 수는 남아 있는 예외 덩어리 수+1 이하
정도로 유지되지만 예외 자체가 무한히 늘어나면 증거 크기도 증가한다.

해시 방식: 각 원장의 불변 식별·순번·내용/envelope 해시와 정규화한 전체 ACK를 domain-separated
leaf commitment로 묶는다. 합칠 때는 순서 있는 자식 구간 descriptor(generation/시작/끝/건수/hash)를
별도 merge domain으로 해시한다. 좌측 과거 hash를 포함하고, 우측에 이미 삭제된 구간이 있으면
그 구간의 descriptor/hash도 포함한다. 이는 삭제된 원문 없이 구간을 합칠 수 있는 합성 증거이며
평면 leaf SHA 체인의 재연산이나 삭제 분할 순서와 무관한 hash를 주장하지 않는다.
구간 checksum은 현재 경계·건수·누적 hash·기록 시각·gateway를 별도로 묶고 논리 bytes도 검증한다.

시작 검증은 capture1부터 저장된 head까지 모든 순번이 정확히 한 번씩 남은 원장 또는 삭제
구간에 들어가는지 확인한다. 겹침·빈틈·generation 불일치·checksum/bytes 오류는
BufferCorruption으로 차단한다. 유효한 구간 사이의 원장만 누락돼도 정상으로 만들지 않는다.
v1의 전체 원장 및 v2의 원래 checkpoint+잔여 원장을 먼저 검증한 뒤 v3로 원자적으로 이관한다.
v2의 실제 삭제 prefix는1..last_capture_seq 구간1행으로 바꾸고, 검증된 전체 구 checkpoint
descriptor를 v2-prefix domain으로 묶어 과거 증거를 이어 보존한다. 빈 checkpoint는 구간0행이다.
전환 실패면 table/header/proof 모두 원래 상태로 롤백한다. v3 구간표 누락을 빈 표로 재생성하지 않는다.
원장 삭제·구간 갱신·metadata 감소 역시 같은 트랜잭션이며 실패하면 전부 되돌린다.

시간당 정리는 여전히 트랜잭션당 최대1,000행이다.1Hz에서1,000행/시간만 지우면 증가를
따라잡지 못하므로 한 청크가 꽉 차면 event loop에 양보하고 다음 청크를 처리한다.
적격 행이 남지 않을 때 다음1시간 주기로 돌아간다. ACK 경로에서 삭제하지 않는다.
enqueue 압력 처리도 첫 원장의 EVICTED 여부와 무관하게 적격 행 정리를 시도한다.
SQL 후보/참조 조회와 metadata 합계 비용은 표 크기의 영향을 받으며 일정 지연을 보장하지 않는다.

2시간분 원장은13차 평균938B/초로 약6.8MB다. 실제 사용량에는 최대 한 주기까지의 추가
확정 원장, 보존 예외·유실 근거·구간·커서가 더해진다. 이 값은 용량 계획의 근사치이지 무한
고정 용량 보장이 아니다. 오래된 ID의 판정은 서버가 맡고 로컬 원래 ACK 재조회는 사라진다는
C안의 영향, UTC 시각 의존성, 논리/물리 bytes 차이는 유지된다. 서버 변경·유실 근거 삭제·
운영 원본 이관/정리는 이번 범위가 아니며 운영 적용은 병합 후 별도 요청으로 한다.
