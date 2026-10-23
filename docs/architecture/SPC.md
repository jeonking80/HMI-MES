# 실행별 통계 기반 SPC

`GET /api/v1/spc/xbar-r`는 `line_id`, `source_id`, `stream_id`, `execution_id`, `tag`를 각각 정확히 한 번 요구한다. 지원 태그는 `Sealer.TemperaturePV`, `Sealer.AirPressure`다. 조회는 `read` 권한 트랜잭션과 `refresh_idle=False`를 사용하며 캐시하지 않는다. `create_user_app`에 등록하므로 로컬과 서버리스에서 같은 경로를 제공한다.

이 기능은 **통계 기반이며 ML이 아니다**. `method: statistical` 및 회귀의 `linear-regression-15min`이 계산 방법을 명시한다. 추세 기반 추정은 규격 도달을 보장하지 않는다.

## 표본·실행 귀속

CX-01의 `analog_history_raw`와 원래 `ingest_batches`를 읽는다. 원래 배치의 GOOD `Line.ActiveExecutionId`가 대상 실행과 일치하고 아날로그와 수집 시각이 같아야 해당 실행의 표본으로 인정한다. 실행 등록 시각과 다른 실행의 수치로 귀속을 추측하지 않는다. 동일 초에서는 기존 `utc-second-latest-capture-v1` 정책의 가장 큰 capture_seq를 선택한다. 선택된 최신값이 BAD여도 그 초의 이전 GOOD로 대체하지 않는다.

시계 증명은 수집 시 저장된 `RawObservation.time_quality`를 사용한다. 이 값은 구성된 시계 정책의 실제 평가 결과에서만 GOOD가 된다. gateway/generation/clock_epoch를 섞은 분, capture_seq 순서의 시계 역전 이후 표본은 TIME_UNKNOWN이다. 서로 다른 epoch는 같은 분포로 합치지 않는다. 첫 완전 부분군의 epoch를 사용하며 이후 다른 epoch의 부분군을 제외한다.

UTC의 완전히 지난 1분마다 0/12/24/36/48초 GOOD 표본을 선택한다. 실행 등록 이전의 부분 분과 아직 완료되지 않은 분은 계산하지 않는다. 유효 표본 하나라도 없으면 부분군을 제외한다. 제외 사유는 분당 하나를 안정된 우선순위(TIME_UNKNOWN, BAD, UNCERTAIN, MISSING)로 센다. 미귀속 표본도 대상 실행에서는 MISSING이다. 보간·0 채우기·직전값 채우기는 없다.

## Phase I·Western Electric·공정능력

대상 실행의 처음 20개 완전 부분군이 Phase I 기준선이다. 후보 기준선의 끝까지 실행 등록 이후의 원본 `ingest_batches`를 같은 SQL 스냅샷에서 대조한다. 대상 태그와 같은 capture의 GOOD `Line.ActiveExecutionId`가 해당 실행을 가리키는 원본 표본 중 하나라도 대응 원시 행이 없거나 7일 조회 범위 밖이면 앞부분을 증명할 수 없다. 이 경우 남은 부분군이 25개 이상이어도 `INSUFFICIENT_DATA`, `baseline/limits/capability: null`, `baseline_limitation: ORIGINAL_PREFIX_RAW_UNAVAILABLE`로 반환하며 WE 표시를 지운다. 원본부터 수집되지 않은 구간은 값이나 누락 원시값을 만들어내지 않는다. 최근 20개를 이동 기준선으로 사용하지 않는다. 20개 미만이면 `INSUFFICIENT_DATA`, `baseline: null`, `limits: null`이다. n=5 상수 A2=0.577, D3=0, D4=2.114, d2=2.326을 사용한다. Xbar 한계는 Xbarbar ± A2·Rbar, R 한계는 D3·Rbar와 D4·Rbar이다.

Xbar의 WE1은 3σ 초과, WE2는 같은 쪽 3점 중 2점이 2σ 초과, WE3는 같은 쪽 5점 중 4점이 1σ 초과, WE4는 같은 쪽 8점 연속이다. σ는 Xbar 관리한계 폭/3이며 경계와 같은 값은 초과로 보지 않는다. 위반은 창의 마지막 부분군에 표시한다. 기준선의 부분군에도 같은 진단을 제공한다. R 관리도에는 한계를 제공하며 WE 시퀀스 표시는 Xbar에 적용한다.

규격은 실행의 저장된 `parameters.temperature_c`와 각 원래 capture의 `alarm_capture_decisions.rule_version`으로 확인한 불변 알람 규칙 버전에서 도출한다. 모든 확인 가능한 규칙의 온도 setpoint와 실행 설정 온도가 일치하고 activation_delta가 같아야 한다. 현재 `alarm_channels.current_version`으로 과거 실행의 규격을 추측하지 않는다. 역사 규칙이 없거나 모호하거나 변경되었으면 `spec: null`이다. 압력 규칙은 하한만 정의하여 양측 규격을 도출할 수 없으므로 `spec: null`이다.

규격과 완전 부분군 25개 이상이 있을 때 전체 유효 부분군의 Rbar/d2로 within σ를 추정한다. Cp=(USL−LSL)/(6σ), Cpk=min(USL−전체 Xbarbar, 전체 Xbarbar−LSL)/(3σ)이다. 0 분산에서는 무한대 공정능력을 내보내지 않고 `capability: null`이다. 규격에서 벗어나면 Cpk는 음수가 될 수 있다.

## 회귀와 제한

최근 15분의 동일 epoch GOOD 표본에서 구한 UTC 1분 평균을 회귀한다. 한 분의 유효 표본이 일부여도 그 유효값의 평균을 사용한다. 유효 점 10개 미만, r²<0.6, 규격 없음, 0 기울기, 이미 지난 교차 시각은 모두 `projected_cross: null`이다. 증가 추세는 USL, 감소 추세는 LSL과 교차하는 미래 시각만 내보낸다. 시각은 UTC `Z`, 온도 단위는 계약의 `°C`, 압력 단위는 태그 사전의 `bar`다.

기준선은 영구 동결 저장물이 아니다. 한 SQL 문장 스냅샷을 사용하므로 지연 배치가 앞부분에 추가되면 처음 20개가 바뀔 수 있다. 보관 정리가 실행 앞부분의 원시값을 지우면 기준선을 뒤쪽의 20개로 이동하지 않고 원본 대조 실패로 불충분 처리한다. `baseline_basis`와 `history_limitation`이 이를 명시한다. 원시 기본 보관은 CX-01의 7일이며 배포 프로필에서 줄일 수 있다. SPC 조회는 최대 최근 7일 원시값으로 제한한다. 원시값이 정리된 과거 실행을 90일 분 집계로 복원할 수 없다. 1분 집계에는 오프셋 5개와 실행 귀속 증명이 없어 SPC에 대체 사용하지 않는다. 원시 이력이 충분히 남지 않으면 보관된 부분군은 표시하되 실행의 첫 20개임을 증명하지 못하는 관리한계·기준선·공정능력은 null이다.

추가 응답 필드: `baseline_basis`, `baseline_limitation`, `history_limitation`, `execution_id`, `scope`. 실행이 7일 원시 조회 범위가 시작되기 전에 이미 종료됐으면 계산 창이 비므로 `INSUFFICIENT_DATA`와 `baseline_limitation: EXECUTION_OUTSIDE_RAW_RETENTION`을 반환한다(오류 500 대신). `baseline_basis`는 `first-20-complete-execution-subgroups-prefix-certified`다. 기존 계약 필드는 유지한다.

검증: `.venv/bin/python -m pytest tests/unit/test_spc.py tests/integration/test_spc_http.py -q`.
