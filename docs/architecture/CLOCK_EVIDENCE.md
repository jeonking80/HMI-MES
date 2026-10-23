# 분석 시간 근거

CX-09는 운영자가 API와 게이트웨이가 동일한 호스트 커널 UTC를 공유한다고 명시한 경우에만 분석 시간 근거를 만든다. 기본값은 비활성이다. Vercel과 원격 게이트웨이는 같은 호스트로 취급하지 않는다. 이 문서는 CX-09 core와 CX-11의 공통 저장 경계를 설명한다. 전체 CLI/Compose·화면 결합 검증 및 공개 배포 완료를 뜻하지 않는다.

## 명시적인 같은 호스트 정책

`SameHostClockPolicy(gateway_id, source_id, stream_id, capture_generation, valid_from_api_utc, valid_until_api_utc, reference_id)`는 인증된 등록 범위와 명시적인 유효 기간을 요구한다. 실행기는 활성화 시점부터 서비스 자격 증명 만료 시점까지를 전달한다. 활성화 전 캡처를 현재 정책으로 소급 확정하지 않는다.

운영자의 shared-kernel 주장은 유효 기간 안에 게이트웨이가 재시작하며 생성하는 새 clock epoch에도 적용된다. 각 인증된 캡처마다 정확한 epoch에 바인딩한 기존 `ConfiguredClockPolicy`를 생성한다. source/stream/gateway/generation이 다르면 확정하지 않는다. generation 재등록이 필요한 변경은 새 등록 및 정책 구성이 필요하다. 단순히 나중에 캡처를 받았다는 이유로 이전 epoch의 역행 근거를 해제하지 않는다.

기존 `ConfiguredClockPolicy` 직접 주입도 지원한다. 기존 ±2초·유효 기간·read bracket 검사를 재사용하며, 미래 2초를 넘는 원본 같은 호스트 캡처는 UNKNOWN이다. 통신·OPC 품질은 별도 조건이며 이 정책이 BAD/UNCERTAIN 값을 정상 값으로 바꾸지 않는다. gateway 시각 보장은 PLC의 별도 source/server timestamp나 생산 사건 발생 시각의 동기화를 의미하지 않는다.

## 원자적인 영속 근거

schema18의 `capture_clock_evidence`는 analog 유무와 무관하게 접수한 배치마다 한 행을 남긴다. 배치 원본·ACK·표본·시계 근거를 같은 수집 트랜잭션에서 커밋한다. gateway/batch/source/stream/generation/sequence/epoch와 원본 body digest, envelope 입력 digest를 바인딩한다. 결정에는 원본 캡처 시각, GOOD/UNKNOWN·이유, 오프셋 구간, 정규화된 API UTC, 불확실성 구간과 평가 시각·원본 정책 근거를 보존한다. 원래 표본 9개를 다시 복제하지 않는다. 알람 context나 가짜 alarm decision은 만들지 않는다.

행은 UPDATE·DELETE·TRUNCATE를 거절한다. 원시 보관 정리는 이 테이블을 지우지 않는다. 사용한 schema18의 downgrade는 실제 시간 근거 손실을 막기 위해 거절한다. 빈 스키마는 upgrade→downgrade→upgrade가 가능하다. 기존 schema14–17에서는 새 테이블을 조회하지 않고 기존 동작을 유지한다. 스키마 확인 캐시는 같은 DB 트랜잭션 안에서만 재사용한다.

원본 캡처 순서의 UTC 역행과 nonincreasing monotonic 좌표는 `analog_history_clock_state`의 최초 reversal sequence로 유지한다. registry 초기화 때 immutable intake 이력에서 과거 역행을 복원한다. 나중에 지연 캡처가 이전 역행을 입증하면 이미 저장된 GOOD 결정도 조회 시 cutoff를 적용해 UNKNOWN으로 해석한다. 원래 결정 행은 바꾸지 않는다. 현재 시계 정책이나 새 측정으로 명시적인 UNKNOWN을 GOOD으로 재처리하지 않는다.

## 원본과 분석 좌표

`analog_history_raw.acquired_at`과 `observation.sample`은 원본이다. nullable `canonical_utc`에 GOOD 분석 좌표를 별도로 저장한다. 정규화 버전 `api-interval-midpoint-v1`은 최종 검증한 API UTC 구간의 중간값을 사용한다. 미래 API 수신 시각에 맞춰 구간을 좁힌 측정도 원래 오프셋 범위는 보존하면서 최종 구간 중간값을 사용한다.

트렌드의 순수 집계 입력에만 원본 sample을 복사해 `acquired_at=canonical_utc`를 적용한다. 원본 객체나 저장한 JSON을 수정하지 않는다. 분 upsert·조회는 분석 좌표로 슬롯을 선택하고, 기존 집계 정책 버전은 유지한다. 새 epoch 역행이 확인되면 원시 time quality를 불명으로 바꾸고 해당 분을 다시 계산한다. UNKNOWN에는 canonical UTC를 새로 만들어 붙이지 않는다. 원시 보관 정리도 정규화 좌표와 수신 시각으로 제한한다.

보고서·SPC 소비자는 같은 영속 근거를 사용해야 한다. `effective_capture_clock`는 원본 바인딩·digest·epoch·정규화 좌표를 검사하고 현재 reversal cutoff를 적용한다. 행이 없는 경우만 legacy fallback을 허용하며, 저장된 UNKNOWN이나 바인딩 실패를 현재 정책 또는 알람 근거로 덮지 않는다. SPC의 execution identity 동시성 검사는 원본 시각으로 수행한 후 분석 UTC로 부분군을 만든다. PLC에서 생성한 `production_events.occurred_at`에 gateway 오프셋을 무조건 적용하지 않는다.

v2 결합에서는 이미 받아들인 GOOD 좌표가 뒤늦은 역행으로 무효화될 때 그 좌표에 **UNKNOWN 차단 근거**를 남긴다. 이를 오프셋 전 원본 시각으로 옮겨 이전 가동 구간이나 같은 SPC 슬롯의 오래된 GOOD 표본이 다시 나타나게 하지 않는다. 원래 바인딩이 검증된 과거 GOOD 증명에만 적용하며 새 GOOD이나 사건 발생 시각을 주장하지 않는다. 보고서의 요청 내부 역행 캐시는 실제 캡처 조회문과 같은 DB snapshot의 영속 reversal cutoff로 보완한다.

시간 근거 없는 sampleless 통신 장애는 API 저장 시각부터 이전 가동 근거를 차단한다. `unknown_time_basis: API_RECEIPT_FENCE`는 장애가 실제 발생한 gateway/PLC 시각이 아니라 안전하게 확인한 수신 경계다. 이전 날짜에 받은 차단 근거도 새 창에서 유지하며, 그 이후에 실제로 확인된 더 높은 capture 순번의 GOOD 근거만 해제할 수 있다. 유효한 과거 canonical 좌표를 무효화하는 경우는 `INVALIDATED_CANONICAL_COVERAGE`로 구분한다. 이 추가 메타데이터는 불명 구간에만 붙인다.

## 측정 방식의 저장 준비

0018은 `capture_clock_probes`도 함께 만든다. nonce 해시, 서비스 key ID와 전체 scope/generation/epoch, API 수신 UTC, gateway 전송 UTC·monotonic 좌표, 60초 이하 만료, 단일 소비 배치와 입력 digest를 저장할 수 있다. 이 core 변경 자체는 nonce를 발급하거나 측정 기능을 활성화하지 않는다. 프로브 행은 독립적인 제한 정리 대상이고, immutable 결정에는 측정 사실을 복사해 프로브 정리 이후에도 근거가 남도록 한다.

CX-11은 별도의 인증 경로·bounded 발급/정리·원자적인 nonce 소비를 구현하고, 검증된 측정 결과를 공통 writer에 전달한다. legacy alarm `ClockAssessment`의 enum이나 알람 경로는 변경하지 않는다. 측정의 canonical 좌표도 최종 불확실성 구간의 중간값이다. signed 오프셋 범위를 절댓값이나 ±2초로 자르지 않는다.

## 용량과 검증 경계

per-batch 영속 근거는 원시 6시간 설정과 별도로 누적된다. 1Hz에서 metadata와 인덱스가 행당 수백 바이트라면 하루 수십 MB가 추가될 수 있다. Neon Free 총용량이나 장기 보관 가능성을 이 core의 작은 시험으로 증명하지 않았다. 운영 적용 전 실제 크기·증가율을 측정해야 한다. 별도 보관 승인 없이 근거를 지워 용량을 맞추지 않는다.

core PostgreSQL 시험은 정책 활성/비활성·generation 불일치·새 epoch·미래 시각·만료·원자적 rollback·중복·digital-only 캡처·원시 정리 후 증거 보존·역행 cutoff·immutable 쓰기 거절·schema17 fallback 및 빈 migration 왕복을 확인한다. CLI/Compose·분석 화면 수락 결과와 별도 high 검토 결과는 작업별 보고서에 기록한다.

## 실행 설정과 인증된 측정

로컬 내부 API의 `serve --trend-clock-policy same-host`가 운영자의 같은 커널 시계 주장을 활성화한다. 생략하면 비활성이다. 서비스 시작 UTC부터 `--expires-at`까지, 명시한 gateway/source/stream/generation에만 적용한다. Compose 실행기는 이 옵션을 명시한다. Claude 소유 브라우저 검사에는 동일 인자를 추가해야 한다. Vercel 구성에는 이 옵션이 없다.

원격 방식은 API에서 `HMI_CLOCK_MEASURED_ROUND_TRIP=true`, 게이트웨이에서 `serve --clock-measured-round-trip`을 각각 명시한다. 로컬 내부 API 시험은 `serve --clock-measured-round-trip`으로 같은 측정 경로를 연다. 기본은 모두 비활성이고 환경변수는 정확한 `true` 또는 `false`만 받는다. 같은 호스트 정책과 측정 정책을 한 캡처에 동시에 적용하지 않는다. 공개 환경에는 이번 작업에서 적용하지 않았다.

인증된 `POST /internal/v1/clock-probes`는 기존 bearer 서비스 자격 증명으로 전체 수집원 범위와 등록 generation을 검사한다. API 요청 진입 UTC를 DB 잠금 대기 전에 기록하며, 1회용 nonce의 해시·최초 인증 key·scope·epoch·전송 좌표를 DB에 보존한다. 응답은 `nonce`, `api_utc`이다. 게이트웨이는 같은 OPC 캡처 시계의 전송/수신 UTC·monotonic 좌표와 nonce를 **다음** 배치 envelope의 선택적 `clock_evidence`에 고정한다. 기존 1.2 body·body digest는 그대로다. 증거가 없는 기존 envelope는 새 필드를 직렬화하지 않는다. 공개 화면 응답 계약에는 불확실성 필드를 추가하지 않았다.

측정 작업은 수집 루프와 독립적이다. HTTP는 최대 2초·8KB 응답으로 제한하며, 장애나 느린 probe가 OPC 수집을 기다리게 하지 않는다. 증거는 다음 캡처 직전에 한 번만 가져오고, 캡처 도중 완료한 probe는 그다음 캡처에만 붙인다. 이미 대기 중인 과거 배치에 새 probe를 붙이지 않는다. 로컬 SQLite는 envelope digest까지 원자적으로 보존하여 재시도·ACK 이후·프로세스 재시작 후 증거 변경을 탐지한다. 기존 DB 이관은 증거가 없었다는 사실이 확인된 행만 보완한다.

API는 signed 오프셋 `[api_utc-t1_utc, api_utc-t0_utc]`를 계산한다. 왕복은 2초 이하, 캡처/서버 측정 나이는 60초 이하, 같은 gateway/source/stream/generation/epoch, UTC와 monotonic 경과 잔차 50ms 이하, 캡처 read span 50ms 이하를 모두 요구한다. 이 잔차 상한은 60초 내 시계 drift 허용 가정이다. 캡처 가능 구간에 잔차·read span을 포함하고 API 접수 이후를 제외한 뒤 최종 구간 중간값을 분석 UTC로 사용한다. 미래·역행·불연속·오래된 증거는 GOOD이 되지 않는다. 오프셋의 절댓값을 2초로 제한하지 않으므로 +3.7초처럼 서로 다른 호스트의 실제 차이도 표현한다.

새 배치의 nonce 소비·원본 저장·분석 결정·트렌드·ACK는 한 트랜잭션이다. 다른 배치의 nonce 재사용, 다른 범위/최초 발급 key, 변경된 probe 사실은 거절한다. 동일 배치의 동일 body/envelope 재전송은 기존 ACK만 돌려주며 새 판정을 만들지 않는다. nonce 정리 뒤 같은 수집원 범위의 교체 key도 이 불변 ACK를 조회할 수 있다. 최초 인증 key의 감사 근거는 proof에 남는다. 입력 증거를 바꿔 과거 UNKNOWN을 GOOD으로 승격할 수 없다.

발급 테이블은 전체 10,000행, gateway/source/stream/generation/epoch당 120행으로 제한한다. 발급 때 만료 행 최대 100개를 요청 범위 우선으로 정리하여 다른 범위의 적체 때문에 만료된 현 범위가 계속 막히지 않게 한다. 남은 용량이 없으면 429이며 자의적으로 미만료 측정을 지우지 않는다. 정리·한도 검사·발급은 DB advisory lock으로 여러 Vercel 인스턴스 사이에서도 직렬화한다. 증거 원본은 이 제한 정리와 독립적이다.

## 적용과 검증 기록

0018은 0017 이후의 명시적 마이그레이션이다. 기존 canonical14 초기화 SQL은 변경하지 않는다. 운영자는 먼저 head17과 기존 권한을 적용한 뒤 head18 및 별도 `deploy/neon-clock-grants.sql`을 적용한다. 자세한 순서는 [KULS_DEPLOYMENT.md](KULS_DEPLOYMENT.md)를 따른다. runtime은 근거 SELECT/INSERT와 probe 소비 결과 세 열 갱신·제한 삭제만 허용받으며 DDL·확정 근거 변경은 금지된다. 자동 공개 이관이나 환경변수 변경은 수행하지 않는다.

실제 CLI/Chromium 수락에서는 OPC UA부터 생산·정산과 분석 화면까지 연결했다. GOOD 트렌드 132개, 실행 RUNNING 4.043357초를 확인했고 UNKNOWN 0.560636초는 첫 관측 전 NO_EVIDENCE였다. 이는 로컬 같은 호스트의 증거이며 공개 Vercel 수락을 대신하지 않는다. 독립 high 검토는 마이그레이션·보관·정규화·최소 권한 및 인증·nonce 동시성·오프셋 경계를 별도로 검증했다. 최종 명령·결과와 Compose 검증은 [CX-09](../work-orders/reports/CX-09.md), [CX-11](../work-orders/reports/CX-11.md)에 기록한다.
