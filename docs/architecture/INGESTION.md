# PostgreSQL 사건 수신과 적용

## 19차 현행 기준 (2026-10-26)

19차 사용자 승인으로 수집 원본 보관은 `HMI_INGEST_RETENTION_HOURS` 기본 168시간·최소 3시간을 사용한다. 3 미만·비숫자는 시작을 거절하고, 구 설정 `HMI_INGEST_RETENTION_DAYS`는 단독·동시·빈 값 모두 거절한다. 현재 runtime 자동 정리는 schema 0023에서만 실행하며 schema 0022의 보관 종료 조회·재전송 거절은 호환 지원한다. 아래 18차/CX-40의 일 단위·영구 개별 영수증 설명은 당시 이력이며 이 현행 기준으로 대체된다. 변경 이유와 영향은 [INGEST_RETENTION.md](INGEST_RETENTION.md)를 따른다.

gateway 전체 capture identity는 보관 종료 범위로, 임의 batch ID는 최대 256개 정확 문자열 청크와 GIN으로 검사한다. 이전 generation의 ID도 확인하며, 만료 ID는 압축 뒤에도 기존과 같은 HTTP 410 `RETENTION_EXPIRED`다. 새 ACK를 만들거나 충돌 hold로 바꾸지 않는다. 누적 내용 해시를 유지하지만 그 값만으로 정확 ID 판별을 대신하지 않는다. FK 없는 만료 영수증 압축은 원본 정리와 같은 트랜잭션·시간 제한을 따르고 오류 시 함께 롤백한다.

범위에는 epoch별 clock 양 끝과 최신 `latest_capture`를 남기고 내부 역행은 영속 registry로 보존한다. 실제 알람 FK가 있는 개별 영수증은 유지한다. 매 capture 알람 판단이 FK를 남기므로 영수증이 사건 수에만 비례한다는 목표는 이 경로에서 성립하지 않는다. 사용자의 **기존 참조 보존·한계 보고** 결정을 따른다. 정확 ID 청크도 O(captures) 바이트가 필요하므로 압축을 전체 DB 고정 용량으로 설명하지 않는다.

`apps/api/ingestion.py`는 gateway 배치의 내구성 있는 접수와 생산 사건 적용을 담당한다.
실제 PostgreSQL과 Alembic `0002_ingestion`이 필요하다. 서비스 호출자는 HTTP 계층에서
검증한 gateway/source/stream 범위를 전달한다. 배치의 자기 선언만으로 권한을 부여하지 않는다.

## 등록과 귀속

- 운영 설정으로 source/stream을 기존 line, gateway, capture generation에 먼저 연결한다.
  자동 등록이나 generation 변경은 지원하지 않는다.
- execution은 원래 execution ID, 작업지시, recipe version, parameters, counter epoch와 함께
  명시적으로 등록한다. 기존 등록은 동일 내용의 재시도만 허용한다.
- 설비 ID `Line`은 설정된 line을 뜻한다. `Infeed`, `Sealer`, `Outfeed`는 해당 line 안의
  같은 asset code를 뜻한다. 실제 line/asset ID도 같은 line 소속일 때 허용한다.
  불명·다른 line 설비는 `EQUIPMENT_SOURCE_MISMATCH`로 격리한다. wire 사건 원문은 바꾸지 않는다.

## 원자성과 재시도

한 트랜잭션이 원문 inbox, 생산/설비 사건, execution별 양품·불량·폐기 수량,
적용 오류, 연속 검증 커서, 누락 구간, 배치 원문과 ACK를 함께 저장한다.
ACK는 트랜잭션 commit이 반환된 후에만 호출자에게 반환된다. commit 이전 실패는 전부 rollback된다.
응답 유실 시 원본 보관 기간 안의 같은 배치는 저장된 ACK를 그대로 돌려준다. schema22 보관 종료 뒤에는 RETENTION_EXPIRED로 거절하며 ACK를 재구성하지 않는다.

`(gateway_id, batch_id)`, `(gateway_id, capture_generation, capture_seq)`, 전역 event ID,
`(source_id, stream_id, seq)`는 내용을 바꿀 수 없는 식별자다. 동일 사건을 새 배치에 담아도
생산 수량을 다시 더하지 않는다. 다른 내용으로 식별자를 재사용하면 원래 내용과 충돌 내용을
별도 `ingest_conflicts` 기록과 source hold로 commit하고 예외를 반환한다. 충돌 배치에는 ACK를
발급하지 않으며 그 배치의 정상 사건 일부도 적용하지 않는다. 기존의 정확한 배치 재시도에는
보관 중인 원래 ACK를 돌려주지만 이후 새 작업과 격리 재적용은 충돌 해소 전까지 제한한다.

## 격리와 진행 상태

등록되지 않은 execution, 다른 stream의 execution, epoch 불일치, 수량 초과 등은 원문을
접수하고 명시적인 사유와 함께 격리한다. `QUARANTINED` ACK는 접수 완료이며 실적 정산 완료가
아니다. 격리된 사건을 현재 execution에 대신 귀속시키지 않는다.

관리 경로에서 원래 execution 등록 등 원인을 해결한 뒤 `replay_quarantined`를 호출하면
최대 1,000개를 seq 순서대로 재적용한다. 이미 적용된 사건은 다시 적용하지 않는다.
이후 현재 inbox 상태와 커서는 바뀌어도 보관 중인 배치 ACK는 원래 접수 결과로 보존한다. 승인된18차 보관 종료 이후에는 ACK 대신 영구한 식별/hash와 보관 종료 구간을 남긴다.
적용 실패 이력은 지우지 않는다. 이 서비스에는 자동 재시도 스케줄러나 충돌 해제 API가 없다.

`latest_ingested_seq`는 관찰한 사건·누락 구간의 최댓값이다. `verified_through_seq`는
1번부터 빠짐없이 수신하고 적용한 연속 구간만 전진한다. 뒤 사건의 수량 적용이 가능해도
중간 누락이나 격리를 건너뛰어 커서를 전진시키지 않는다. 누락은 구간으로 저장하므로
UInt64 최댓값까지 큰 빈 구간도 개별 번호를 순회하지 않는다. 현재 미해결 구간 상한은
transport v1과 같은 100개이며 초과하는 배치는 rollback된다.

영구 누락 보고는 영향을 받는 execution과 사유를 원래 배치 ACK에 남기고,
`reconciliation_required`를 지속 설정한다. 뒤늦게 누락 사건을 모두 받더라도 이 hold는
자동 해제하지 않는다. source 충돌, 재조정 필요, 미검증 커서가 하나라도 있으면
`control_restricted`가 참이다. PLC counter snapshot을 사건 실적으로 대신 더하지 않는다.

수량·sequence·capture 번호는 PostgreSQL `NUMERIC(20,0)`과 Python 정수로 정확히 처리한다.
JSON wire 값은 계약의 십진 문자열이다. 합계가 UInt64를 넘으면 사건을 격리한다.

## 검증과 남은 범위

```sh
UV_CACHE_DIR=/tmp/hmi-uv-cache uv run pytest -q tests/integration/test_ingestion.py
UV_CACHE_DIR=/tmp/hmi-uv-cache uv run pytest -q tests/integration/test_internal_api.py
UV_CACHE_DIR=/tmp/hmi-uv-cache uv run ruff check apps/api/ingestion.py tests/integration/test_ingestion.py
```

시험은 공유 fixture가 시작하는 인증된 일회용 PostgreSQL 17 컨테이너를 사용한다.
독립 연결에서 commit 전 비가시성과 commit 후 가시성, rollback, 재전송, 동시 재시도,
충돌 hold, 원래 execution으로 격리 재적용, 누락 복구, 영구 누락 hold와 UInt64 경계를 검증한다.

샘플은 배치 원문에 보관하지만 이 모듈은 태그 이력·화면 최신값 projection을 구현하지 않는다.
실제 작업지시/명령 흐름에 의한 execution 등록, 운영자 재조정·hold 해제 UI,
PLC 누계 대사, 장시간 성능·복구·운영 수락 시험은 별도 작업이다.
이 검증만으로 DEV 단계나 프로젝트 전체 완료를 뜻하지 않는다.

## 18차 원본 정리와 재전송 계약 이력

변경 이유: 원본 무기한 보관의 용량 증가를 제한하기 위한 사용자 승인이다. schema22의 기본7일(HMI_INGEST_RETENTION_DAYS, 최소1일)이 지난 저장 원본 중 정산/커서/누락·격리/미결 명령 보호를 모두 통과한 것만 정리한다. 정산된 설정 변경 이력도 정확한 APPLIED 사건/원래 baseline과 대조하며, 이후 다른 작업 진행이나 승인된 generation 전환만으로 옛 정산 원본을 영구 보관하지 않는다.

인증된 gateway/source/stream 확인 뒤 원래 batch ID와 gateway-wide generation/capture_seq의 종료 여부를 먼저 확인한다. 종료된 식별자는 HTTP410, reason_code=RETENTION_EXPIRED이며 ACK·내용 충돌 hold를 만들지 않는다. 다른 ID/순번으로 자동 재전송하지 않는다. 보관 중 원본의 기존 중복/충돌 규칙은 유지한다.

내부 HTTP 수집은 업무 트랜잭션을 커밋한 뒤 별도 유지보수 트랜잭션을 시도한다. 1분당1회·스캔/삭제 상한·500ms soft budget과1500ms statement/100ms lock budget을 사용한다. BUSY/THROTTLED는 정상 건너뜀이다. 오류는 증거/삭제/진행 상태를 롤백하고 안전한 오류 클래스만 기록하며, 이미 커밋한 수집 ACK를 취소하지 않는다. 직접 서비스 호출은 자동 정리하지 않는다.

보관 종료 범위와 batch 식별 기록·관련 clock/probe/raw 삭제는 원자적이다. 일반 runtime이 삭제 권한이나 사용자 GUC로 이 경로를 흉내 낼 수 없다. 원본과 전체 clock proof가 없어 오래된 실행을 원본으로 다시 검증할 수 없다는 영향, 영구 UUID metadata 증가, 보수적인 관측 배치 보호는 [INGEST_RETENTION.md](INGEST_RETENTION.md)에 명시한다.
