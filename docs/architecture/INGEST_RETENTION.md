# 수집 원본 보관 종료와 영수증 압축 (schema 0023)

18차 사용자 결정으로 무기한 수집 원본 보존을 변경했고, 2026-10-26의 19차 승인으로 보관 단위를 시간으로 바꾸고 영수증을 압축한다. 무료 데모의 원본 증가를 줄이기 위한 정책이며, 오래된 실행을 원본 전체로 다시 검증할 수 없다는 영향을 수용한다. 공개 이관·배포는 별도 운영 작업이다.

## 삭제 자격과 실행

`HMI_INGEST_RETENTION_HOURS`는 기본 168시간, 최소 정수 3시간이다. 3 미만·비숫자는 시작을 거절한다. 이전 `HMI_INGEST_RETENTION_DAYS`는 단독·동시 설정·빈 값 모두 시작을 거절한다. 서버 저장 시각을 DB 현재 시각과 비교하며 최소 보관도 게이트웨이 원장 2시간보다 길다. 현재 runtime의 자동 정리는 schema 0023에서만 실행한다. schema 0022의 보관 종료 조회·재전송 거절은 호환 지원하지만, 일 단위 SQL에 시간 설정을 넘겨 정리하지 않는다.

배치의 모든 사건이 적용되고 해당 실행이 SETTLED이며 최종 요약·수량·설정 이력·커서 근거가 일치해야 한다. 사건이 없는 관측 배치도 허용하되 source의 gap·격리·미결 명령이 있으면 보존한다. 실행 없는 제어 사건은 관측 전용으로 취급하지 않는다. PENDING·MISMATCH·CONFLICT와 증명 불충분 자료는 보존한다. 이후 다른 실행이 진행됐다는 이유만으로 이전의 검증된 실행을 계속 고정하지는 않는다.

성공한 수집 커밋 뒤 별도 트랜잭션으로 정리한다. DB 전역 스케줄은 분당 최대 한 번이며 기본 삭제 200건·후보 400건, 함수 상한은 각각 1,000·2,000건이다. 후보 사이 500ms 예산을 확인하고 부분 진행 커서를 저장한다. 호출에는 statement timeout 1,500ms·lock timeout 100ms를 적용한다. 잠긴 scope는 건너뛰며 후보별 savepoint로 불필요한 잠금을 해제한다. 정리 오류는 이미 커밋된 수집을 취소하지 않는다. 오류 트랜잭션의 삭제·만료 기록은 함께 롤백한다.

삭제 대상은 자격이 확인된 `ingest_batches`, 해당 `capture_clock_evidence`와 일치하는 소비된 probe다. 미소비·만료 probe는 별도의 제한된 함수로 정리한다. 요청 scope를 우선해 최대 100건, scope 없는 관리 호출은 최대 400건이며 소비된 proof는 이 경로로 지우지 않는다. 생산 사건·실행·최종 요약·실적·작업지시·명령·알람·감사·계정·분 집계는 삭제하지 않는다.

## 남기는 근거와 재전송

`ingest_receipts` 중 원본이 만료됐고 실제 FK 참조가 없는 행을 압축한다. `pg_constraint`로 참조 테이블을 확인하며 알람·정산·감사 등에서 참조하는 개별 영수증은 남긴다. 참조를 확인할 수 없으면 보수적으로 보존한다. 원본 ACK나 body를 영수증으로 복원하지 않는다.

압축 대상 batch ID는 `ingest_retired_batch_chunks`의 정확한 문자열 배열에 최대 256개씩 보존한다. 키는 gateway/generation/256개 순번 버킷이며 GIN 인덱스로 조회한다. capture identity는 연속 보관 종료 범위로 판정한다. batch ID는 generation을 넘어 gateway 전체에서 검사하므로 임의 UUID 재사용도 놓치지 않는다. 청크는 행·인덱스 비용을 줄이지만 임의 batch ID를 정확히 기억하는 바이트 수는 여전히 O(captures)다. 해시 누적값만으로 ID 목록을 대체하지 않는다.

기존 알람 소비기는 매 capture마다 `alarm_capture_decisions`를 만들고 영수증을 FK로 참조한다. 이 경로의 개별 영수증은 알람 발생 사건 수가 아니라 capture 수에 비례한다. 사용자는 이 충돌에 대해 **기존 참조 보존·한계 보고**를 확정했다. 알람 판단을 삭제하거나 FK를 풀어 사건 수에 비례한다고 주장하지 않는다.

`ingest_retention_ranges`에는 gateway/source/stream/generation, 연속 capture 범위, 건수, 저장·원래·분석 시각 범위, 태그·clock epoch와 `RETENTION_EXPIRED`를 남긴다. 동일 scope에서 인접한 범위만 합친다. 누적 해시는 `SHA256_SUM_MOD_2_256_V1`: 각 PG jsonb 배열 `["HMI_RETENTION_LEAF_V1", gateway, source, stream, generation문자열, capture문자열, batch_id, content_sha256]`의 텍스트를 SHA-256으로 해시하고 정수 합을 2²⁵⁶으로 나눈 나머지다. 이는 삭제 목록의 누적값이며 원문 복구나 인증 증명이 아니다.

만료된 batch ID 또는 capture identity의 재전송은 HTTP 410, `reason_code=RETENTION_EXPIRED`로 거절한다. 원래 ACK를 돌려주거나 충돌 hold를 새로 만들지 않는다. 보관 중 동일 배치는 기존 ACK 규칙을 유지한다. 게이트웨이는 만료 거절을 받으면 같은 배치를 보존하고 중단해 운영 확인을 요구한다. 새 ID 자동 재시도나 가짜 ACK는 없다.

## 시간·분석·권한

원본이 없어도 개별 영수증과 범위의 `clock_frontiers`(epoch별 첫·마지막 sequence/UTC/monotonic 좌표), `latest_capture`(최신 clock-context 메타데이터)를 함께 조회해 clock 순서와 알람 frontier를 유지한다. 압축 전에 `analog_history_clock_state`에 최초 역행을 영속 기록한다. 범위 양 끝만으로 내부 역행을 재발견할 수 없으므로 registry의 역행 cutoff를 권위 있는 근거로 병합한다. 최신 알람 context는 남은 영수증과 범위의 `latest_capture` 중 원래 capture 순서로 선택하며 오래된 개별 행으로 후퇴하지 않는다. 뒤늦은 clock 역행은 보존된 집계의 시간 품질을 보수적으로 UNKNOWN 처리한다. 트렌드의 살아 있는 분 값은 유지하면서 `raw_evidence`와 만료 범위를 함께 응답한다. 보고서의 해당 시간 구간은 UNKNOWN/RETENTION_EXPIRED이며 수량·정산 결과는 유지한다. 합친 범위는 연속된 관측의 증명이 아니며, 시간이 불명확하면 요청 범위를 보수적으로 덮는다.

런타임은 메타데이터를 조회하고 제한된 정리 함수를 호출할 수 있다. 메타데이터 직접 수정·삭제와 원본 직접 삭제는 허용하지 않는다. SECURITY DEFINER 함수는 고정 search_path와 명시적 관계 이름을 쓰고, 불변성 트리거는 caller 권한에서 owner·만료 증거를 확인한다. GUC 설정으로 우회할 수 없다.

## 이관·용량·복원

`0022_ingest_retention`은 기존 원본을 삭제하지 않고 최소 영수증을 채운다. Neon 배치는 `upgrade-0021-0022.json`, 권한은 `grants-0022.json`이며 renderer로 재생성한다. 공개 적용은 Claude 담당이다. 만료가 한 번이라도 수행된 DB는 원문을 복원할 수 없으므로 downgrade를 거절한다. 미사용 downgrade는 이전 제약·clock 함수를 복원한다. 백업에는 영수증·범위·스케줄과 기존 업무 테이블을 함께 포함한다.

추가 이관 `0023_ingest_compaction`은 기존 자료를 삭제하지 않고 clock endpoint·최신 context·영속 역행 근거를 보강한다. 실제 압축은 이관 뒤 제한된 유지보수 트랜잭션에서 수행한다. Neon HTTP 배치는 `upgrade-0022-0023.json`, 권한은 `grants-0023.json`이며 renderer 재생성과 일치를 검증한다. 공개 적용·배포·환경변수 변경은 Claude 담당이고 이 작업은 공개 DB를 변경하지 않는다. 압축 ID 청크가 있거나 각 종료 범위의 영수증 수가 보존 건수와 다르면 downgrade를 거절한다. 압축이 없고 영수증이 완전한 경우에만 22의 원래 함수·권한을 복원하고 23 전용 메타데이터를 제거한다. 기존 영속 시계 역전 기록과 더 낮은 이관의 손실 방지 검사는 유지한다. 백업에는 `ingest_retired_batch_chunks`, 범위의 clock 메타데이터와 영속 clock registry도 포함한다.

원본 행 수가 일정해져도 DB 전체 크기가 고정되지는 않는다. 약 6.5KB × 1Hz × 3시간은 원본 약 70MB·10,800 capture라는 추정이다. 보호 조건에 걸린 원본·FK 참조 영수증·정확 ID 청크·업무 사건·인덱스/TOAST/bloat는 별도다. DELETE 공간은 vacuum/autovacuum으로 재사용한다. 1Hz 24시간 분량 86,400 capture의 가속 적재와 vacuum 전후 각 테이블 크기·증분 측정은 아직 대기 중이며, 추정을 측정 결과로 표시하지 않는다. Neon 무료 0.5GB 수용이나 DB 전체 고정 크기를 보장하지 않는다.

## 18차 설계 이력과 변경 이유

18차 schema 0022는 일 단위 기본 7일·최소 1일과 capture별 영구 영수증을 도입했다. 이는 당시 승인된 이력이며 현행 설정이 아니다. 1Hz에서 원본 7일은 약 3.6GB이고 영수증도 계속 증가해 무료 DB 용량 목표를 만족하지 못했다. 19차는 이를 시간 단위 최소 3시간과 비참조 영수증 압축으로 변경한다. 정확 ID 보존의 선형 바이트 증가와 매 capture 알람 FK 보존이라는 한계는 남는다. 보호 조건·업무 사건·정산·알람·감사 보존과 원본 종료 후 재검증 불가는 유지한다.

19차 독립 가속 측정은 86,400 capture·평균6,858.87B 본문에서 3시간 경계 정리 후 원본/영수증10,575행·구간1행·ID청크297행을 확인했다. 일반 VACUUM ANALYZE 뒤 DB 물리 크기는540,038,835B로 무료0.5GB를 넘었다. 빠른 반복 갱신의 파일 여유 공간·dead tuple과 인덱스를 포함한 측정이며 행 수 감소를 파일 축소로 오인하지 않는다. 실제알람FK활성환경은 추가증가할 수 있다. 세부 수치·한계는 [CX-41](../work-orders/reports/CX-41.md)을 따른다.
