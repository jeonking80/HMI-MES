# 수집 원본 보관 종료 (schema 0022)

18차 사용자 결정으로 무기한 수집 원본 보존을 변경한다. 무료 데모의 원본 증가를 줄이기 위한 정책이며, 오래된 실행을 원본 전체로 다시 검증할 수 없다는 영향을 수용한다. 공개 이관·배포는 별도 운영 작업이다.

## 삭제 자격과 실행

`HMI_INGEST_RETENTION_DAYS`는 기본 7일, 최소 정수 1일이다. 서버 저장 시각을 DB 현재 시각과 비교한다. 게이트웨이 원장 보관 2시간보다 길다. schema 0022 이전에는 적용하지 않는다.

배치의 모든 사건이 적용되고 해당 실행이 SETTLED이며 최종 요약·수량·설정 이력·커서 근거가 일치해야 한다. 사건이 없는 관측 배치도 허용하되 source의 gap·격리·미결 명령이 있으면 보존한다. 실행 없는 제어 사건은 관측 전용으로 취급하지 않는다. PENDING·MISMATCH·CONFLICT와 증명 불충분 자료는 보존한다. 이후 다른 실행이 진행됐다는 이유만으로 이전의 검증된 실행을 계속 고정하지는 않는다.

성공한 수집 커밋 뒤 별도 트랜잭션으로 정리한다. DB 전역 스케줄은 분당 최대 한 번이며 기본 삭제 200건·후보 400건, 함수 상한은 각각 1,000·2,000건이다. 후보 사이 500ms 예산을 확인하고 부분 진행 커서를 저장한다. 호출에는 statement timeout 1,500ms·lock timeout 100ms를 적용한다. 잠긴 scope는 건너뛰며 후보별 savepoint로 불필요한 잠금을 해제한다. 정리 오류는 이미 커밋된 수집을 취소하지 않는다. 오류 트랜잭션의 삭제·만료 기록은 함께 롤백한다.

삭제 대상은 자격이 확인된 `ingest_batches`, 해당 `capture_clock_evidence`와 일치하는 소비된 probe다. 미소비·만료 probe는 별도의 제한된 함수로 정리한다. 요청 scope를 우선해 최대 100건, scope 없는 관리 호출은 최대 400건이며 소비된 proof는 이 경로로 지우지 않는다. 생산 사건·실행·최종 요약·실적·작업지시·명령·알람·감사·계정·분 집계는 삭제하지 않는다.

## 남기는 근거와 재전송

`ingest_receipts`는 batch ID와 gateway 전체의 generation/capture 순번 유일성을 영구 보존한다. clock epoch·monotonic·UTC read span 좌표를 남겨 원본 만료 후에도 기존 clock-context 필드를 유지한다. 원본 ACK나 body는 보관하지 않는다. 기존 알람 결정의 참조도 이 영수증으로 유지한다.

`ingest_retention_ranges`에는 gateway/source/stream/generation, 연속 capture 범위, 건수, 저장·원래·분석 시각 범위, 태그·clock epoch와 `RETENTION_EXPIRED`를 남긴다. 동일 scope에서 인접한 범위만 합친다. 누적 해시는 `SHA256_SUM_MOD_2_256_V1`: 각 PG jsonb 배열 `["HMI_RETENTION_LEAF_V1", gateway, source, stream, generation문자열, capture문자열, batch_id, content_sha256]`의 텍스트를 SHA-256으로 해시하고 정수 합을 2²⁵⁶으로 나눈 나머지다. 이는 삭제 목록의 누적값이며 원문 복구나 인증 증명이 아니다.

만료된 batch ID 또는 capture identity의 재전송은 HTTP 410, `reason_code=RETENTION_EXPIRED`로 거절한다. 원래 ACK를 돌려주거나 충돌 hold를 새로 만들지 않는다. 보관 중 동일 배치는 기존 ACK 규칙을 유지한다. 게이트웨이는 만료 거절을 받으면 같은 배치를 보존하고 중단해 운영 확인을 요구한다. 새 ID 자동 재시도나 가짜 ACK는 없다.

## 시간·분석·권한

원본이 없어도 영수증의 시각·순번으로 clock 순서와 알람 frontier를 유지한다. 뒤늦은 clock 역행은 보존된 집계의 시간 품질을 보수적으로 UNKNOWN 처리한다. 트렌드의 살아 있는 분 값은 유지하면서 `raw_evidence`와 만료 범위를 함께 응답한다. 보고서의 해당 시간 구간은 UNKNOWN/RETENTION_EXPIRED이며 수량·정산 결과는 유지한다. 합친 범위는 연속된 관측의 증명이 아니며, 시간이 불명확하면 요청 범위를 보수적으로 덮는다.

런타임은 메타데이터를 조회하고 제한된 정리 함수를 호출할 수 있다. 메타데이터 직접 수정·삭제와 원본 직접 삭제는 허용하지 않는다. SECURITY DEFINER 함수는 고정 search_path와 명시적 관계 이름을 쓰고, 불변성 트리거는 caller 권한에서 owner·만료 증거를 확인한다. GUC 설정으로 우회할 수 없다.

## 이관·용량·복원

`0022_ingest_retention`은 기존 원본을 삭제하지 않고 최소 영수증을 채운다. Neon 배치는 `upgrade-0021-0022.json`, 권한은 `grants-0022.json`이며 renderer로 재생성한다. 공개 적용은 Claude 담당이다. 만료가 한 번이라도 수행된 DB는 원문을 복원할 수 없으므로 downgrade를 거절한다. 미사용 downgrade는 이전 제약·clock 함수를 복원한다. 백업에는 신규 영수증·범위·스케줄과 기존 업무 테이블을 함께 포함한다.

원본 행 수가 일정해져도 DB 전체 크기가 고정되지는 않는다. 영구 영수증은 capture마다 증가하며 DELETE 공간은 vacuum/autovacuum으로 재사용한다. 1Hz·배치 약 6KB로 7일이면 원본만 약 3.6GB다. 이 정책만으로 Neon 무료 0.5GB를 보장할 수 없으며, 실제 운전 빈도·시간과 용량을 별도로 관리해야 한다.
