# 운영 보고서

`apps/api/reports.py`는 호출자가 소유하는 PostgreSQL 트랜잭션에서 읽기만 수행한다. `reports_http.register_report_routes`를 사용자 앱에 등록하며 사용자 세션의 `read` 권한을 사용하고 유휴 만료를 연장하지 않는다. 모든 응답은 `Cache-Control: no-store`다. 쿼리는 중복·미등록 키를 거절하며 실행 등록 기간은 `[from,to)` 최대 93일, 페이지 크기는 1~100이다. 커서는 같은 라인과 등록 기간 안의 실행 ID여야 한다. 정렬은 등록 시각과 실행 ID 내림차순이다.

## 근거와 불명

실행 목록은 `production_executions`의 원래 execution_id를 유지한다. 작업지시의 물품은 실제 관리 연결이 있을 때만 사용하며 비관리 실행의 item_id/target_quantity는 null이다. 시작은 Line 상태 사건의 첫 RUNNING, 종료는 STOPPING → STOPPED 사건으로 확인한다. 사건이 없으면 null이다. 타임라인 계산 범위는 등록 시각부터 확인된 종료 또는 조회 시각까지다.

타임라인 상태는 원본 수집 배치의 Line.State와 Line.ActiveExecutionId를 사용한다. RUNNING 외의 알려진 PLC 상태는 정지다. 같은 source/stream은 원본 capture_generation/capture_seq 순서의 최신 관측으로 교체하고, 정상 관측도 최대 3초만 상태를 증명한다. 뒤따르는 관측이 없으면 COMMUNICATION_LOST로 바뀐다. 명시된 통신 이상은 UNKNOWN이며, 이전 상태를 장애 전체에 늘리지 않는다. 영구 stream_gaps는 직전 사건부터 다음 사건까지 UNKNOWN으로 덮는다. 한쪽 사건 경계가 없으면 보고 범위의 해당 끝까지 보수적으로 확장한다. 현재 정산 필요 플래그만으로 과거 정상 근거 전체를 무효화하지 않는다.

시각 품질은 저장된 alarm_capture_decisions의 배치 해시·게이트웨이·clock_epoch에 귀속된 clock_assessment와 ±2초 이내 오차 구간이 있을 때만 알려진 것으로 취급한다. raw capture_time, source_timestamp, 최근 수신 시각만으로 시각 품질을 복구하지 않는다. 보고 기간으로 자르기 전에 source/stream/gateway/generation/clock_epoch별 순서를 확인한다. schema22에서는 보관 종료 뒤에도 남는 `ingest_receipts`의 원래 UTC·monotonic 좌표를 사용하고, 이전 스키마에서는 원본 `ingest_batches`를 사용한다. UTC 또는 monotonic 좌표가 역전된 첫 capture_seq 이후는 새 epoch/generation의 증거가 생길 때까지 TIME_UNKNOWN이며, 날짜 변경이나 뒤이은 정상 모양 표본으로 복구하지 않는다. 평가 없는 배치는 TIME_UNKNOWN이다. 관측 자체가 없는 곳은 NO_EVIDENCE다. 원본 배치 보관 범위 밖에서는 보고서를 정상 시간으로 복구할 수 없다.

라인 일일 보고에 권위 있는 source 선택 계약은 없다. 같은 시각에 여러 source/stream이 알려진 상태를 주장하면, 상태가 같더라도 NO_EVIDENCE로 둔다. 불명 근거가 겹쳐도 불명을 우선한다. 임의로 최신 등록 source를 고르지 않는다. 실행 타임라인과 대시보드의 정확한 scope 조회는 원래 source/stream만 사용한다. 구간의 source_id, stream_id, execution_id와 충돌 구간 evidence_scopes는 추가 근거 필드다. 실행 타임라인에서 소유 실행이 바뀌거나 표본에 실행 소유 근거가 없어지면 이전 실행의 정상 상태 유효 시간을 즉시 끊는다. 이때 execution_id는 null이고 관측된 다른 실행은 observed_execution_id로만 보존한다. 시각 역전은 실행 소유 태그가 없는 디지털·이벤트 전용 배치도 전체 스트림에 적용한다.

구간 경계 sweep으로 전체 시간을 빈틈없이 분할한다. 가동+정지+불명 검산 결과는 check_ok다. 비율의 분모는 불명을 포함한 전체 시간이며, 전체가 0이면 비율은 0이다. 정지 사유는 기본 null이며 CX-03의 annotate_report_segments 콜백이 정확한 원본 구간에 이력을 연결한다. 콜백은 `(db, segments, scope, now=...)`이며 타임라인에서 annotate=None을 주면 원본 구간만 얻는다.

## 수량과 날짜

실행의 SETTLED 수량은 보존된 최종 요약을 사용하고, 나머지는 execution_totals의 수신 실적과 EXECUTION_NOT_SETTLED 사유를 반환한다. UInt64 수량은 끝까지 정수로 계산한 뒤 JSON 문자열로 보낸다. yield_ratio는 good/(good+reject), 분모 0이면 null이다. 불명 시간이 있으면 UNKNOWN_TIME 사유도 남긴다.

일일 보고는 Asia/Seoul 자정의 UTC 경계로 자르고 24개의 서울 시간 슬롯을 반환한다. 수량은 production_events.occurred_at의 `[from,to)`에 배분하므로 최종 요약 수량을 하루에 복사하지 않는다. 각 사건의 execution_id가 그대로 유지된다. 현재·미래 날짜는 DAY_NOT_FINISHED이며 미래 시간은 total에서 제외한다. 하루 중 관련된 미정산 실행과 불명 시간은 잠정 사유로 남긴다. 알람 수는 요청 시점의 라인 미확인 알람 수다.

## CSV

실행과 일일 CSV는 JSON 조회와 같은 검증·권한·계산을 사용한다. UTF-8 BOM, 한글 헤더, attachment 파일명, 잠정 여부/사유 열을 포함한다. 문자열 셀의 첫 문자가 `=`, `+`, `-`, `@`, 탭, CR이면 작은따옴표를 앞에 붙인 뒤 csv.writer로 인코딩한다. 페이지 CSV도 같은 커서·limit을 따르므로 한 응답은 한 페이지다.

## 검증과 한계

`tests/integration/test_reports.py`는 생성된 임시 PostgreSQL 17 컨테이너에서 실제 마이그레이션·수집·시각 평가·세션을 사용한다. 상태 합계, 통신 장애, 시각 불명, 영구 사건 누락, 서울 자정의 시간/수량 배분, 원래 실행 격리, UInt64 최대값, 정산, 사건 시작/종료, 커서, 복수 source, ASGI 권한·CSV·유휴 만료를 검사한다. 순수 계산과 CSV 모든 위험 접두사는 `tests/unit/test_reports_calculation.py`에서 검사한다. 전체 시스템 장시간/성능 수락을 대체하지 않는다. 다수 SQL 읽기는 호출자 트랜잭션의 격리 수준을 따른다.

## CX-40 보관 종료 원본의 보고 영향

2026-10-25 사용자 결정과 18차 작업지시서는 무료 DB의 무기한 증가를 줄이기 위해
조건을 만족하는 오래된 수집 원본·시각 증거의 삭제를 승인했다. 기본 보관은 7일이다.
실행·생산 사건·실적·정산 최종 요약은 삭제하지 않으므로 `SETTLED`와 수량은 유지한다.
compact receipt와 보관 종료 범위만으로 오래된 실행을 원본 재검증할 수는 없다.

보고서와 타임라인은 겹치는 범위를 `retention_expired`에 반환하고
`raw_evidence.availability`·`reason_code`를 `RETENTION_EXPIRED`로 표시한다.
해당 시간은 `class=UNKNOWN`, `unknown_reason=RETENTION_EXPIRED`이며,
살아 있는 이전 정상 관측이나 통신 장애 근거보다 우선한다. 삭제 구간을 지나
이전 정상 상태를 연장하지 않는다. 실행·일일 보고의 잠정 사유에도
`RETENTION_EXPIRED`를 추가한다. 일반 `NO_EVIDENCE`로 숨기지 않는다.

알려진 시각은 analytical UTC 포락선부터 마지막 관측의 3초 freshness 끝까지
보수적으로 덮는다. 원래 UTC가 달라도 원래 시각으로 구간을 옮기지 않는다.
합쳐진 범위는 연속된 삭제 상태의 정확한 시각 증명이 아니므로
`raw_evidence.coverage_basis=CONSERVATIVE_ENVELOPE`로 표시한다.
시각 불명·analytical 경계 부재이면 요청한 같은 scope 전체를 보수적으로 덮는다.
영구 clock registry에서 뒤늦게 확인된 역행이 보관 종료 범위에 닿으면 조회 시
`time_unknown`으로 승격하여 같은 보수적 처리를 적용한다. 원본이나 시각 proof가
지워졌다는 사실을 정상 시간·통신 연속성으로 복원하지 않는다.
