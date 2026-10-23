# 작업지시서 — KULS 보강 6차 (Codex: OPS-02 공개 실연 `BufferFull` 해소)

- 발행: 2026-10-23, Claude
- 수행: Codex. 브랜치 `codex/kuls-backend-v3`(`4308668` 이후). 시작 전에 최신 `main`을 병합한다.
- **원칙: 핵심만 한다.** 반복 실행·장시간 시험·범위 밖 정리는 하지 않는다. UI·README·`vercel.json`·`scripts/check-operable-system.py`는 수정하지 않는다.

## 1. Claude 진단 (코드 기준, 운영 SQLite 미조회)

게이트웨이를 종료시킨 `BufferFull`은 **전송 경로의 `METADATA_FULL`일 가능성이 가장 높다.**

- `apps/gateway/worker.py`의 `deliver()` 루프는 `DeliveryUncertain`·`DeliveryRejected`만 잡는다. 여기서 `BufferFull`(=`ValueError`)이 나면 `run`이 끝나고 exit 1이 된다. `collect()`는 `ValueError`를 잡으므로 수집 쪽 `BufferFull`은 `blocked` 보고만 하고 종료시키지 않는다.
- 전송 경로에서 `BufferFull`이 나는 곳은 `buffer.acknowledge()` 끝의 `_check_metadata_budget()`(약 781줄)뿐이다.
- `_metadata_bytes()`는 `batch_ledger`·`source_progress`·`missing_ranges`·`loss_intervals`의 `metadata_bytes` 합이다. ACK마다 `batch_ledger`에 ack JSON 크기가 더해진다. **원장을 줄이는 코드가 없다**(`DELETE FROM batch_ledger`·prune 없음).
- 공개 source는 이미 약 3,800개 배치를 ACK했다. 그래서 원장이 10MB(`max_metadata_bytes`)에 닿았고, 남은 1건을 ACK하는 순간 한도를 넘었다는 설명이 관측(수집 `BufferFull` blocked → 약 7초 뒤 전송 `BufferFull` 종료)과 맞는다.
- **미확정이다.** 사유 접두어를 기록하지 않았기 때문이다. `INFLIGHT_PRESSURE`(enqueue)는 수집 쪽에서만 나므로 종료 원인이 될 수 없다.

## 2. 할 일

1. **확인(읽기 전용, 1회).** 공개 게이트웨이 SQLite 사본을 열어 다음 값만 기록한다. 원본은 수정하지 않는다.
   - 표별 `sum(metadata_bytes)`와 행 수
   - `batch_ledger` 상태별 건수
   - `outbound_batches`의 상태·`queued_at`
2. **사유 코드 기록.** `report()`의 오류 값에 클래스 이름과 함께 `BufferFull` 메시지의 고정 접두어(`METADATA_FULL`, `INFLIGHT_PRESSURE` 등 콜론 앞 대문자 코드)만 남긴다. 그 밖의 예외 본문은 계속 기록하지 않는다.
3. **원인이 `METADATA_FULL`이면 근본 수정.** 서버가 확정(`verified_through_seq`)한 범위의 ACK 완료 원장만 정리하는 경로를 만든다. 지킬 조건:
   - 미확정 ACK, `missing_ranges`, `loss_intervals`, 대조 근거는 지우지 않는다.
   - 10MB 한도는 늘리지 않는다.
   - 정리와 커서 갱신은 한 트랜잭션에서 한다.
   - 단위 시험 1개를 추가한다. 한도 근처 원장에서 확정분 정리 뒤 ACK가 성공하고, 미확정분은 남는지 확인한다.
   - 다른 원인이면 그 원인만 최소 수정하고 근거를 적는다.
4. **검증은 한 번씩만.** 관련 게이트웨이 단위 시험, Ruff, 공개 5분 실연 **1회**.
   - 실연 전에 사용자 승인을 받는다.
   - 실패하면 사유 코드와 함께 보고하고 멈춘다. 자동 재시도는 하지 않는다.
   - 성공하면 OPS-03 §1 확인 항목(새 캡처 GOOD, 가동 > 0, 불명 근거, 수량 대조, 전후 DB 용량)을 기록한다.

## 3. 하지 않을 것

- 게이트웨이 SQLite 초기화, 새 source·generation 생성, 한도 완화
- `hmi_app` 금지 DML 실측 재시도(연결이 없으면 계속 미실행으로 둔다)
- 같은 실연의 반복 실행

## 4. 보고

`reports/OPS-02.md`에 덧붙인다.
- 1번 조회 값
- 확정된 사유 코드
- 수정 내용과 시험 결과
- 실연 결과(시각, 용량, 확인 항목)

`docs/DEVELOPMENT_STATUS.md`에 한 줄 요약을 남긴다. 공개 화면 확인은 Claude에게 요청한다.
