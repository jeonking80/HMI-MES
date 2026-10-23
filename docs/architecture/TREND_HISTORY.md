# 아날로그 트렌드 이력

CX-01은 9개 `analog_history` 태그를 배치 접수·중복 제거·커서와 같은 PostgreSQL 트랜잭션에 기록한다. `IngestionService`는 schema 14 이하에서 기존 수집을 유지하며, schema 15/16/17에서만 새 저장 경로를 사용한다. 새 API는 `register_trend_routes(app, auth)`로 사용자 앱에 등록하며 read 권한과 `refresh_idle=False`를 사용한다.

## 저장과 시간 근거

`analog_history_raw`는 원래 sample·gateway·generation·capture sequence·clock epoch와 `ClockAssessment`를 보존한다. GOOD 시간은 명시적인 `ConfiguredClockPolicy`를 `IngestionService(trend_clock_policy=...)`로 전달한 경우에만 가능하다. 기존 `assess_capture_clock`의 범위·세대·epoch·유효 기간·±2초 검사를 재사용한다. 기본은 UNKNOWN이다. OPC GOOD, 수신 시각, source timestamp만으로 시간 품질을 GOOD으로 바꾸지 않는다. 미래 2초를 넘는 캡처도 TIME_UNKNOWN이다. 잘못된 시각의 값은 해당 시각 슬롯에 불명으로 표시하며 실제 발생 시각을 추측하지 않는다.

`analog_history_clock_state`는 epoch별 마지막 sequence/시각과 최초 역행 sequence를 보존한다. 아날로그 유무·원시 보관 범위와 무관하게 명시적인 capture clock이 있는 모든 캡처를 기록한다. 지연 캡처는 기존 immutable ingest_batches의 같은 generation/epoch 전후 캡처와 비교한다. UTC 역행뿐 아니라 sequence 증가에 따른 monotonic 좌표의 역행·동일값도 시간 불명으로 처리한다. 이 탐색은 기존 gateway/generation/sequence 고유 인덱스로 방향별 한 건을 반환하며, 원시 정리 뒤에도 digital-only 캡처의 근거가 사라지지 않는다. 역행이 확인되면 같은 epoch의 이후 보존 원시 표본을 UNKNOWN으로 바꾸고 분 집계를 재계산 대상으로 돌린다. registry가 없는 첫 캡처에서는 immutable 배치 이력을 SQL window LAG로 한 번 검사해 UTC/monotonic 최초 역행 sequence를 복원한다. 따라서 기존 schema14에서의 업그레이드와 downgrade→upgrade 후에도 이미 알려진 역행을 GOOD으로 초기화하지 않는다. 이 초기 재구성은 epoch의 전체 이력을 읽으므로 큰 이력에서는 첫 요청이 더 비쌀 수 있다. 이후에는 인접 캡처 탐색을 사용한다. epoch가 바뀔 때까지 자동으로 정상 복구하지 않는다. 이 작은 provenance registry는 원시 보관 종료 이후에도 남겨 역행 증거를 잃지 않는다. generation/epoch가 같은 분에 섞이면 그 분을 TIME_UNKNOWN으로 내보내며 서로 평균하지 않는다.

## 집계와 조회

새 배치가 완료된 UTC 분을 갱신한다. 요청당 최대 32개 tag/minute를 기존 `aggregate_minutes`, `POLICY_VERSION=utc-second-latest-capture-v1`로 계산해 `analog_history_minutes`에 upsert한다. 같은 배치의 재전송은 기존 ACK를 반환하므로 표본·집계를 중복 저장하지 않는다. 지연 배치가 과거 분을 다시 열면 해당 분의 전체 보존 표본으로 재계산한다. 수신 당시 진행 중인 분은 다음 수집에서 처리한다. 이후 수집이 없어도 조회는 원시 근거로 계산할 수 있다.

조회는 보존 집계를 우선 사용하고, 누락·재계산 대기·부분 경계 분만 인덱스 범위 탐색으로 원시값을 읽는다. 원시 일부가 삭제되어도 완성된 집계를 남은 원시 표본으로 덮지 않는다. 슬롯 선택·비GOOD 제외·평균은 기존 정책을 그대로 따른다. 모든 슬롯을 반환하며 값이 없으면 null/MISSING이다. minute의 expected_count는 60이고 부분 경계도 완전한 분처럼 GOOD으로 표시하지 않는다. 부분 경계는 요청 안의 표본만 계산하므로, 원시 보관 이후에 분의 일부를 조회하면 원시 근거가 없는 부분은 MISSING이다.

태그 1~4개, second 최대 1시간, minute 최대 7일이다. auto는 1시간 이하를 second로 선택한다. 알 수 없는 키·중복 스칼라·중복 tag·잘못된 scope·해상도·시각은 422다. UTC 분/초에 정렬되지 않은 최대 길이 요청이 점 수 제한을 넘으면 422다(예: 비정렬 7일은 10,081분과 겹친다). UTC ISO8601 Z 시각을 반환한다.

## 보관과 용량

기본 `TrendRetention()`은 원시 7일, 분 집계 90일, 테이블별 요청당 삭제 최대 1,000건이다. 원시 삭제 경계는 완료된 분 경계로 내린다. 아직 집계하지 않은 정상 과거 표본을 먼저 삭제하지 않는다. 미래 시각 표본도 수신 시각을 기준으로 만료되므로 잘못된 미래 시각으로 무한 보존되지 않는다. `FOR UPDATE SKIP LOCKED`와 제한된 삭제를 사용한다. `ingest_batches`, 기존 알람 FK·결정·생산·인증 테이블은 삭제하지 않는다.

로컬/배포 프로필에서 `IngestionService(trend_retention=TrendRetention(raw_days=1))`을 명시하면 원시 24시간을 선택할 수 있다. 기본값을 변경하지 않는다. Neon Free 같은 0.5GB 환경에서는 기본 7일 원시 보관을 수용한다고 가정하면 안 된다. 9태그×86,400초×7일=5,443,200행, JSONB 근거·인덱스 포함 행당 약 0.8~1.5KB를 가정하면 원시는 약 4.4~8.2GB다. 24시간도 777,600행으로 약 0.62~1.17GB이며 0.5GB를 넘을 수 있다. 원시 6시간(`raw_days=0.25`)은 약 0.16~0.29GB다. 90일 분 집계 1,166,400행도 행당 약 0.4~0.7KB 가정 시 약 0.47~0.82GB여서 별도 프로필·용량 증설 판단이 필요하다. 이는 추정치이며 실제 최대 규모 측정 결과가 아니다. minute_days도 배포 프로필에서만 줄일 수 있다.

원시 보관 경계보다 오래된 지연 배치는 새 트렌드 표본으로 보존하지 않는다. 이미 지워진 원시 근거를 복원할 수 없으므로 과거 분을 남은 일부 값으로 재계산하지 않는다. 생산 사건 수집과 원래 ACK·알람 보존은 그대로 처리된다. 처리 한도보다 많은 과거 재계산·정리 적체는 여러 수집 요청에 걸쳐 해소된다. 시계 역행의 최초 무효화는 정확성을 위해 해당 epoch의 보존 원시 근거를 갱신하므로 큰 이력에서는 일반 수집보다 비쌀 수 있다. 최대 7일·9태그 규모 p95 성능 측정은 별도 운영 검증이다.
