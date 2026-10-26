# CX-44 보충 — 공개 DB 읽기 전용 실측 (Claude, 2026-10-26)

Codex 진단(CX-44)에서 DSN 부재로 차단된 공개 DB 실측을 Claude가 Neon HTTP 읽기 전용 조회로 보충한다. 쓰기·VACUUM·설정 변경 없음. 공개 DB 0024, `HMI_INGEST_RETENTION_HOURS=3`, `HMI_TREND_RAW_HOURS=1`, runtime 수집 중(10-26 09:59Z 재개).

- 측정: 2026-10-26 10:35:15Z → 10:45:17Z (10.0분), `pg_database_size`, `pg_total_relation_size`, `n_live_tup`(추정 행 수)
- DB 전체: **382.4MB → 386.7MB, 26.1MB/h 증가**. 0.5GB(500,000,000B)까지 약 113MB, 이 속도면 약 4.3시간.

| 테이블 | 총 크기 | 추정 행 | 10분 행 변화 | 크기 증가 MB/h |
|---|---:|---:|---:|---:|
| `analog_history_raw` | 176.9MB | 24687 | +5391 | 0.00 |
| `ingest_batches` | 125.0MB | 14574 | -642 | 13.57 |
| `capture_clock_evidence` | 31.3MB | 13374 | -639 | 7.57 |
| `ingest_receipts` | 22.9MB | 16034 | +1602 | 1.18 |
| `event_inbox` | 10.4MB | 10651 | +0 | 0.00 |
| `production_events` | 2.8MB | 10575 | +0 | 0.00 |
| `capture_clock_probes` | 2.1MB | 2755 | +602 | 2.36 |
| `analog_history_minutes` | 1.6MB | 2493 | +90 | 0.20 |

해석(추정, 검증 필요):
- `ingest_batches`·`capture_clock_evidence`는 행 수가 줄어드는데(3시간 정리 동작) 파일 크기는 늘어난다. 삭제 공간이 autovacuum 전까지 재사용되지 않아 새 행이 파일 끝에 붙는 것으로 보인다.
- `analog_history_raw`는 1시간 보관으로 행이 약 7.8만→2.5만으로 줄었지만 파일은 177MB로 그대로다(최고 수위 유지).
- 따라서 행 수 기준 정리만으로는 공개 DB 파일 크기가 수렴하지 않는다. R0/R1 용량 설계는 물리 크기(dead tuple·재사용·VACUUM)를 포함해야 한다.
