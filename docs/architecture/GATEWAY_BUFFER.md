# 게이트웨이 영속 버퍼 · DEV-09

`apps/gateway/buffer.py`의 `GatewayBuffer`는 게이트웨이 전용 POSIX SQLite 파일에 원본 수집 배치·전송 상태·확정 ACK·명시적 손실 근거를 저장한다. API나 시뮬레이터 DB를 열지 않는다. HTTP 전송과 OPC UA 읽기는 상위 워커가 담당한다. 버퍼에 `DurableAck` 객체를 전달하는 것만으로 원격 커밋을 입증할 수는 없으므로, 인증된 HTTP 어댑터는 실제 API 커밋 뒤 수신한 COMMITTED 응답만 전달해야 한다.

## 수집·재시도·ACK

새 파일은 권한 0600으로 만들고 WAL·`synchronous=FULL`·외래키를 사용한다. 애플리케이션 ID와 스키마 버전으로 다른 앱의 DB를 거절하며, 경로를 정규화한 파일 잠금으로 단일 프로세스 소유를 강제한다. 하드링크는 지원하지 않는다. 재시작 시 미완료 INFLIGHT 배치를 PENDING으로 돌려 동일 ID·동일 내용으로 재전송한다.

`capture_position`은 영속 capture generation과 마지막 capture sequence다. `enqueue(batch, now=..., latest_read_seq=...)`는 다음 sequence의 불변 배치, 최신 읽기 위치, 필요한 손실 기록을 한 트랜잭션으로 확정한다. 중복 batch ID는 같은 해시이면 기존 상태를 반환하고 다른 내용이면 거절한다. UInt64는 SQLite의 signed integer 범위를 피하는 고정 길이 십진 문자열로 보존한다. generation 변경이나 sequence 고갈은 자동 초기화하지 않는다.

`lease_next()`는 수집 순서의 PENDING 배치를 INFLIGHT로 바꾸고 시도 횟수를 증가시킨다. `retry(batch_id)`는 원본 배치를 PENDING으로 돌린다. 재시도 과정에서 sample ID, 사건 ID, 수집 시각, SourceTimestamp·ServerTimestamp, 품질, 원래 값이나 해시를 다시 만들지 않는다.

`acknowledge(ack)`는 batch/gateway ID·해시·stream·모든 사건의 개별 처리 결과·제출한 gap 근거를 확인한다. 수신 배치 삭제, 불변 ACK 보관, API 진행 위치 갱신은 같은 트랜잭션이다. QUARANTINED 사건도 API가 영속 인수했으면 원본 전송 배치를 해제하지만, 검증 완료 커서를 사건 위치까지 올리지 않는다. 동일 배치의 확정 ACK가 달라지면 거절한다.

`progress(source_id, stream_id)`는 최신 OPC 읽기 위치와 API의 `latest_ingested_seq`·`verified_through_seq`를 구분한다. API 진행 위치는 뒤로 이동하지 않는다. 과거 ACK는 자신의 원본 receipt로 보관하되 최신 진행 위치를 되돌리지 않는다. 같은 진행 위치라도 이미 수신이 확인된 영역을 다시 missing으로 만들지 않도록 기존 gap과 새 gap의 교집합 및 새 ingestion 영역만 남긴다. 남아 있는 겹친 구간의 irrecoverable 근거·사유와 영향받는 execution ID는 뒤늦은 ACK로 약화하지 않는다. quarantine 때문에 verified 위치가 0에 머물러도 복구한 gap을 뒤늦은 ACK나 벽시계 순서로 다시 열지 않는다. 서로 비교할 수 없는 두 커서 조합은 명시적 대조 오류다.

## 30분·100 MB와 손실 근거

기본 한도는 대기 배치의 나이 30분과 논리적 저장량 100,000,000 bytes다. 새 배치 수집 시 오래된 배치부터 한도를 적용하고, 제거한 배치마다 `BUFFER_AGE_LIMIT` 또는 `BUFFER_BYTE_LIMIT` 근거를 원자적으로 기록한다. INFLIGHT 배치는 제거하지 않으며, 용량 압박이면 `BufferFull`로 전체 수집 트랜잭션을 취소한다. 단일 배치 자체가 한도를 넘으면 ID를 부여하기 전 수집을 나누어야 한다.

사건 손실은 원래 연속 구간별 inclusive 범위와 execution ID를 보관한다. 1·2·5번 사건을 제거했으면 1–2와 5–5를 기록하며 중간 사건까지 손실로 넓히지 않는다. 샘플 손실은 sample ID·tag·품질·원래 시각을 보관하며 읽기 값은 제거한다. 샘플 과거 값을 복구했다고 표시하지 않는다. 원래 gap 보고도 보존한다.

`pending_event_gaps(source_id, stream_id, limit=100)`는 새 불변 보고 배치에 실을 원본 gap DTO를 반환한다. 겹치는 근거는 별도 배치로 보고한다. `enqueue`가 각 구간을 보고 배치에 연결하고, 해당 배치가 제거되면 보고 대상이 다시 열린다. 확정 ACK 뒤에는 재보고하지 않지만 `losses()`의 손실 이력은 남긴다. 보고 ACK는 사건 복구·정산 완료를 뜻하지 않으며 실제 원본 사건의 재조회와 검증이 필요하다.

영수증·손실·보고 연결·커서의 별도 논리적 예산은 기본 10,000,000 bytes다. 이 예산이 가득 차면 근거를 삭제하는 대신 `METADATA_FULL`로 쓰기 전체를 취소한다. 현재 자동 보관 종료·외부 반출·압축 기능은 없으므로 장기 실행에는 명시적인 대조·보관 정책이 필요하다. 한도는 JSON bytes와 행별 여유분을 계산한 논리적 예산이며 SQLite 파일·인덱스·페이지·WAL의 물리적 디스크 상한은 아니다. `status(now=...)`는 논리적 사용량과 DB·WAL·shared memory의 실제 bytes를 따로 제공한다.

나이 한도는 `enqueue` 시 적용한다. 새 수집이 없으면 타이머가 자동 삭제하지 않으며 `status`의 `age_limit_reached`로 상태를 확인한다. 나이 계산은 호출자가 제공한 UTC 시각을 사용한다. 운영 디스크 모니터링, 벽시계 관리, 재조회·재시도 주기와 backpressure 처리는 실행기의 책임이다.

## 복구 검증과 범위

재개 시 원본 payload와 해시·capture 원장, QUEUED/ACKNOWLEDGED/EVICTED 소유 관계, 커서, 손실 구간, 보고 배치와 ACK의 연결을 대조한다. 손상된 영속 데이터는 정상 값으로 대체하지 않고 `BufferCorruption`으로 쓰기를 차단한다.

```sh
.venv/bin/ruff check apps/gateway/buffer.py tests/integration/test_gateway_buffer.py
.venv/bin/pytest -q tests/integration/test_gateway_buffer.py
```

시험은 enqueue와 ACK 트랜잭션의 커밋 직전·직후 실제 자식 프로세스를 종료하고 파일을 다시 열어 원자성을 확인한다. 원본 재시도, 잘못된 ACK, quarantine, 순서가 바뀐 ACK와 복구 gap, UInt64 최대값·1,000개 사건, 나이·용량·메타데이터 한도, INFLIGHT 보호, 반복 제거된 gap 보고, 영속 데이터 변조도 검증한다. 이 로컬 버퍼 시험만으로 실제 API의 PostgreSQL 커밋, 30분 실제 단절·복구, 장시간 용량 운영 또는 T01–T12 전체 통과를 주장하지 않는다.
