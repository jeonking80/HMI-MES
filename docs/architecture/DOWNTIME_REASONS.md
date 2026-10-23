# 정지 사유와 수정 이력

`0016_downtime_reasons`는 `0015_analog_history` 다음에 적용한다. 시작 시 DDL을
실행하지 않는다. `downtime_reason_history`는 사유 이력과 동등한 업무 감사 기록,
원본 요청, 원본 응답을 한 행에 저장한다. 각 행에 실행, 라인, 소스, 스트림,
원래 구간의 시작·끝, 사유 코드, 200자 이하 메모, 인증된 작업자, 기록 시각을
보존한다. 수정은 새 행을 추가하며 조회 표시는 최신 ID를 선택한다.

DB 트리거는 UPDATE·DELETE·TRUNCATE를 거절한다. 사용한 이력을 버리는
다운그레이드도 거절한다. 빈 테이블에서만 0015로 내렸다 다시 적용할 수 있다.

## 사용자 API

- `POST /api/v1/downtime-reasons`: 작업지시서의 요청 본문을 받는다. `request_id`는
  UUID이고 코드 다섯 개는 MATERIAL_WAIT, EQUIPMENT, CHANGEOVER, PLANNED,
  UNCLASSIFIED로 고정한다. 생략한 메모는 빈 문자열이다.
- `GET /api/v1/downtime-reasons/history?execution_id=&segment_from=`: 필수 쿼리는
  이 두 개뿐이다. 전체 스코프는 각 이력 행에 포함된다. 응답은 execution_id,
  segment_from, 최신순 items를 포함한다.
- 쓰기·이력 항목에는 request_id, execution_id, line_id, source_id, stream_id,
  segment_from, segment_to, code, note, recorded_by, recorded_at을 반환한다.
- 모든 반환 시각은 UTC `Z`이고 성공 응답에 `Cache-Control: no-store`를 붙인다.
  알 수 없는 쿼리나 중복 쿼리, 올바르지 않은 본문은 422로 거절한다.

쓰기 권한 `downtime_annotate`는 operator와 production_manager에만 있다.
Origin·CSRF 토큰 각각 하나와 JSON Content-Type을 요구하며 기존
`authorized_transaction` 안에서 권한 검사·업무 쓰기를 함께 처리한다.
조회는 read 권한과 `refresh_idle=False`를 사용한다.

## 정확한 구간과 멱등성

쓰기는 요청 UUID의 트랜잭션 advisory lock을 얻고 원본 이력을 확인한다. 같은
UUID·같은 정규화 본문·같은 작업자이면 저장한 응답을 그대로 반환한다. 작업자나
본문(실행·전체 스코프 포함)이 다르면 409다. 재요청은 구간이 나중에 바뀌어도
원래 영수증을 반환하며 이력을 추가하지 않는다.

새 UUID는 수집 경로와 같은 순서로 라인, 소스 스트림 행을 잠근다. CX-02의
실행 타임라인을 다시 읽어 segment_from이 현재 구간 시작과 정확히 같은지
검사한다. 정확한 UNKNOWN 구간은 422, 존재하지 않거나 RUNNING인 구간과
스코프 불일치는 409다. 사유·감사·영수증과 인증 세션 갱신은 같은 트랜잭션으로
커밋되며 실패 시 함께 롤백한다.

## 보고서 연결

`register_downtime_routes(app, auth)`를 사용자 앱에 등록하고
`app.state.report_segment_annotator = annotate_report_segments`를 설정한다.
보고서는 콜백에 `now`를 전달한다. 콜백은 원본 `read_execution_timeline`을
`annotate=None`으로 읽으므로 이 경로는 재귀적으로 주석을 붙이지 않아야 한다.

`annotate_report_segments`는 저장한 전체 시작·끝이 현재 STOPPED 구간과
동일할 때만 사유를 표시한다. UNKNOWN에는 표시하지 않는다. 구간 경계가
확장되거나 쪼개지면 옛 사유는 이력에는 남고 현재 화면에서는 적용하지 않는다.
일일 보고에서 자정으로 잘린 구간은 검증한 원래 구간 안에 완전히 포함되고
실행·전체 스코프가 같은 경우에만 사유를 적용한다. 일일 stopped_by_reason은
CX-02가 이 표시값으로 계산하며 사유 없는 정지는 UNCLASSIFIED로 남긴다.

## 검증

`.venv/bin/python -m pytest tests/integration/test_downtime.py -q`는 임시 Docker
PostgreSQL에서 마이그레이션 왕복과 사용 후 다운그레이드 거절, UPDATE·DELETE·
TRUNCATE 거절, 동시 재요청, 롤백, 전체 스코프·작업자 재사용 거절을 검사한다.
실제 UserAuth를 통한 ASGI 시험은 권한·CSRF·이력·쿼리 검증을 확인한다.
실제 수집·clock assessment·알람 coordinator 자료를 사용한 추가 시험은
타임라인 사유 표시와 일일 합계 3초 및 정지 합계 보존을 확인한다.
