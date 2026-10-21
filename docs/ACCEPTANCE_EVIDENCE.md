# 수락 기준과 현재 검증 근거

기준: [시스템 기획서 §16](HMI_MES_시스템_기획서_v1_0.md#16-수락-시험과-검증-목표),
[개발계획서 §7](HMI_MES_개발계획서_v1_0.md#7-단계별-개발-작업과-완료-조건).
이 문서는 구현·시험 이름을 정적으로 대조하고 **이미 실행한 제한된 검증**을
분류한다. 최신 전체 로컬 기준은 `0014`에서 Python 1,913개·웹 65개와
정적 검사·타입·린트·빌드 통과다. Line UI와 DELAY V3 통합을 포함하며, 최신 실행 상태는
[개발 진행 상태](DEVELOPMENT_STATUS.md)를 따른다. 단위·통합·브라우저
시험 수를 합산하여 T01~T12나 단계 전체를 PASS로 표시하지 않는다.

## T01~T12 대응

아래 근거는 각 기준을 뒷받침하는 **부분 증거**다. 전체 수락에는 동일 실행·사건·명령
ID로 설비 원본, App A DB, 실제 화면과 필요한 보고서를 함께 대조한 별도 결과가 필요하다.

| 기준 | 현재 코드/검증 근거 | 전체 수락까지 남은 범위 |
| --- | --- | --- |
| T01 정상 작업 시작·종료 | `test_simulator_opcua.py::test_secure_methods_produce_real_causal_results_events_and_final_summary`, `test_gateway_pipeline.py::test_network_command_reservation_opc_execution_and_original_id_reconciliation`; 실제 App B Chromium의 LOAD→예열→START→PAUSE→END→최종 수량. 설정·실행 계획·정산은 `test_execution_plan.py`, `test_settlement.py`. | 두 앱의 운영 UI와 최종 보고서까지 같은 실행/설정 버전·세 설비 순서·최종 수량을 함께 대조하는 수락 증적. LOAD 접수/성공만으로 생산·정산 완료를 판정하면 안 된다. |
| T02 시작 조건 위반 | `test_plc.py`, `test_control_http.py`, `test_simulator_opcua.py::test_certificate_gateway_authority_does_not_bypass_local_plc_mode`; 센서 편차 즉시 READY 해제/START 거절은 `test_simulator_fault_inputs.py`. | 실제 가드 개방·LOCAL·미예열 각각에서 원격 요청→PLC 거절→App A 가동 표시 없음까지 한 경로로 기록. API 사전 거절과 실제 PLC 거절은 서로 다른 증거다. |
| T03 명령 응답 유실 | `test_gateway_pipeline.py::test_network_command_reservation_opc_execution_and_original_id_reconciliation`, `test_lost_network_claim_never_submits_after_retry_or_expiry`; 명령 원장과 HTTP의 원래 ID 조회, Chromium의 응답 유실/수동 복구. | 실제 두 앱 화면에서 단절 직전/직후 원래 ID·PLC Submit 횟수·물품 결과를 연결한 반복 수락 결과. 미전달 claim 유실과 PLC 접수 후 응답 유실을 구분해야 한다. |
| T04 확인·복귀 네 상태 | `test_alarm_http.py::test_exact_instance_four_states_returned_unack_attention_and_old_ack_isolation`, `test_alarm_pipeline.py::test_actual_opc_capture_http_pg_alarm_operator_actions_return_and_recurrence`; 브라우저 정확한 과거 발생 건 ACK·메모 변경. | 실제 공정 시험 입력부터 App A 화면까지 두 순서(ACK 먼저/복귀 먼저)의 같은 발생 ID 근거. 최신 TLS OPC 알람 시험은 지속 센서 편차·실제 100ms 열 공정 tick·생산 사건을 포함하지만, 같은 실행의 실제 App A 브라우저까지 묶은 전체 수락 증적은 아니다. 통신·명령 결과 불명 알람 종류도 미완성이다. |
| T05 고장 원인 지속 | `test_plc_fault_pipeline.py` 실제 App B 인증 고장/원인 해제→TLS OPC→gateway/PG Line 알람→App A 인증 ACK·RESET→실제 command worker/PLC 경로. 원인 지속 RESET 거절, 별도 새 RESET 성공과 원본 사건/control version·알람 복귀·자동 START 없음 검증. | 별도 실제 CLI/PG/Chromium에서 Line 화면·원래 고장 코드·ACK/메모를 검증했다. 전체 MES UI와 OPC를 같은 실행으로 묶는 시나리오는 남아 있다. 초기 LOAD/START는 실제 OPC helper로 구성했으므로 전체 화면 수락으로 확대하지 않는다. 설비별 원인 귀속·MES 정지 구간·사용자의 별도 START까지 같은 실행으로 대조해야 한다. |
| T06 필드/OPC 단절 | `test_gateway_opcua.py::test_freshness_uses_good_heartbeat_changes_and_monotonic_receipts_not_source_time`, `test_snapshot.py`, `test_alarm_consumer.py`의 frozen heartbeat·UNKNOWN/활성 유지, 실제 Chromium의 3초 지연/5초 경보 표시. | 5초 통신 상태 표시와 영속 통신 알람 발생은 다르다. 영속 통신 알람·확인/복귀 미구현. 설비별 Good 값/생존 신호 동결·Bad/Uncertain 주입과 콘솔은 구현하고 실제 OPC·게이트웨이·브라우저에서 검증했다. 읽기 실패·설비 채널 단절도 실제 TLS OPC·게이트웨이·CLI Chromium에서 검증했다. 지연도 실제 TLS/콘솔 브라우저에서 검증했다. 실제 OPC 세션 차단은 남아 있으며, 양 단절과 영속 통신 알람을 실제 두 앱에서 대조해야 한다. |
| T07 생산 사건 재전송 | `test_ingestion.py`, `test_gateway_pipeline.py::test_full_network_path_retry_after_commit_does_not_double_count`, 버퍼 원래 배치/ACK 재시도 및 격리 후 원래 실행 재처리. | 수락 시나리오의 원본 사건 ID별 전송 횟수·DB 적용 횟수·최종 PLC 수량을 화면/보고서와 함께 남기는 결과. 짧은 중복 시험을 장시간 복구·보관 완료로 확대하지 않는다. |
| T08 작업 전환·재공 | `test_gateway_pipeline.py::test_managed_two_job_binding_replaces_only_the_exact_settled_reservation`, `test_browser_next_load_waits_for_network_settlement_then_submits_once`; App B 브라우저의 B 초기 수량 0/A 최종 요약 보존. | App A 동일 세션에서 A의 저장 정산을 다시 조회해 기록을 보존하고 B를 연결하는 실제 브라우저/API 경로를 검증했다. 미연결 작업 취소 API·bind 경합과 실제 취소 UI의 응답 유실/원래 ID 복구·권한·거절을 검증했다. 연결 이후 취소는 미구현이다. A의 지연·중복 사건/WIP/최종 경계를 실제 UI까지 이어서 검증해야 한다. |
| T09 배출 확정 중 중단 | `test_simulator_storage.py::test_abrupt_child_exit_at_commit_is_all_or_nothing`, `test_simulator_retention.py::test_actual_process_exit_preserves_atomic_prefix_archive_and_boundary`; 명령 원장·소비자 트랜잭션 rollback 및 재시작 epoch/활성 유지. | 배출 원자성 경계에서 실제 프로세스 중단 후 물품·수량·사건·명령·알람을 두 앱에서 함께 복원하는 수락 묶음. 강제 commit 예외는 실제 프로세스 중단/전원 장애와 같지 않다. |
| T10 버퍼·원본 복구 | `test_gateway_buffer.py`의 시간/논리 byte 한도·정확한 누락 구간·재시작, 실제 네트워크 outage/재전송, SQLite 원본 7일 보관의 보호/정리/복구 시험. | 30분 실부하 버퍼를 5분 안에 복구하는 측정, 실제 한도 초과와 영향 표시, App A 원본/알람 이력 보관 결합. 논리 100MB 한도는 SQLite/WAL 물리 파일 크기 제한이 아니다. |
| T11 보고서 검산 | 실행별 사건·양품/불량/폐기/WIP 정산은 `test_settlement.py`; UTC/서울 일일 경계·UNKNOWN 분 집계 정책은 `test_minute_aggregation.py`. | 정지 구간/사유, 가동+정지+불명 시간, 일일/작업 보고서·잠정/확정·CSV가 없다. 순수 분 집계는 보고서나 영속 트렌드가 아니다. 사건 합계와 확정 PLC 수량을 같은 보고서에서 대조해야 한다. |
| T12 권한·백업 복원 | App A/B 실제 인증·초기 암호 변경·역할/Origin/CSRF·권한 회수·viewer 직접 제어 거절, 인증서 신뢰 거절 및 Chromium 로그인/로그아웃. | coordinated 백업/별도 복원 디렉터리 시험이 없다. PostgreSQL, 설비·게이트웨이 SQLite, 원래 identity·커서·알람·명령·설정/인증서 구성의 복원 후 대조와 자동 재가동 없음까지 필요하다. |

## M0~M5의 현재 경계

| 단계 | 확보한 범위 | 단계 전체로 확대할 수 없는 부분 |
| --- | --- | --- |
| M0 계약·기반 | 48개 태그와 타입/명령/사건/전송 계약 1.0~1.2, 고정 의존성·로컬 검사, 실제 TLS OPC 읽기/메서드 및 인증서 거절, 명시적 DB migration/권한. | 새로운 계약/보관/시간 정책도 실제 배포 범위에서 계속 검증해야 한다. 로컬 green을 출시 수락으로 해석하지 않는다. |
| M1 설비 앱 | 세 설비 물품 모델·PLC·원장·SQLite 원자성/재시작·인증 콘솔·실제 ticker/OPC, 보호된 원본 보관, 지속 센서 온도 편차. | 동결·Bad/Uncertain 주입 콘솔과 실제 통신은 개별 검증했다. 읽기 지연/실패/설비 채널 단절 주입도 실제 TLS·콘솔 브라우저에서 검증했다. 실제 OPC 세션 차단, 일부 자재/검사 순서의 콘솔 조작, 전체 crash 복구 수락 묶음은 남아 있다. |
| M2 연동 | 실제 수집/명령/정산 CLI와 TLS OPC→게이트웨이 SQLite→인증 HTTP→PostgreSQL, 원래 ID 복구·중복/누락·관리형 A/B 예약. | 30분/한도 초과 복구 성능·전체 장시간 관측·운영 복구 절차와 측정 증적. 알람 clock policy는 명시한 동일 호스트 가정이며 원격 시계 검증이 아니다. |
| M3 HMI | 실제 WS 화면 envelope·품질/원래 근거·명령 추적, 온도/공압/논리 Line 고장 소비자·저장·사용자 ACK/메모·브라우저, 규칙 설치/실행 CLI. | 나머지 알람 종류·이력 재구성/최종 복구 대조·통지 전달, 트렌드 영속/API/UI, 규칙 관리 UI 및 전체 역할별 운영 관리. |
| M4 MES | 불변 설정/DRAFT/RELEASED 등록·원자적 실행/LOAD 예약, 원래 결과 조회/LOAD 추적, 실행 읽기·사건 수량/최종 요약 정산. | 같은 App 세션의 다음 작업 전환 UI와 미연결 취소 API/UI는 개별 검증했다. 미연결 취소의 확정 POST 409 재준비도 개별 검증했다. 연결 이후 취소·배포/연결/일반 명령의 409 재준비, 정지 사유·시간·일일/작업 보고서·CSV·자정/epoch 종합 검산은 남아 있다. |
| M5 운영·출시 | 로컬 검사 실행기, 제한된 migration/crash/retention/권한 회귀와 실행 문서. | T01~T12 수락 패키지, 백업/복원·새 설치/기존 데이터 재시작 시연, 성능/8시간/10명/30분 복구 측정·운영 절차. |

## 구체적인 기능·의존성 공백

- **시험 입력과 알람 종류:** 온도 센서 편차는 실제 App B CLI/Chromium과 최신 실제
  100ms 공정 tick→TLS OPC→게이트웨이→HTTP→PostgreSQL→사용자 조치 경로까지 확인했다.
  최신 통합 시험은 50회 이상 tick·지속 편차·양품 생산을 검증한다. 별도 필드 관측 주입 단위는
  선택 설비의 동결·Bad/Uncertain·읽기 실패·채널 단절을 실제 OPC·게이트웨이·콘솔 브라우저에서 검증했다.
  필드 지연은 실제 6초 읽기 지연과 원래 frame 보존을 TLS·콘솔 브라우저에서 검증했다. 실제 OPC 세션 차단은 남아 있다.
  영속 혼합 소비자는 Sealer 온도·공압과 논리 Line PLC FaultCode를 지원하며 실제 OPC 고장 경로와 별도 Line 화면도 검증했다. 5초 통신 무수신과 명령 결과 확인 필요 알람은
  별도 구현이 필요하다. 화면의 UNKNOWN/경보 badge나 명령 OUTCOME_UNKNOWN을 해당 알람으로 세지 않는다.
- **과거/복구 알람:** immutable capture 결정·원래 근거·과거 receipt를 보존하고 현재
  cursor/outbox 재생을 막는다. 별도 과거 occurrence 재구성, 복구 완료 시 여전히 활성인
  현재 알람의 최종 대조와 지연 표시/관련 트렌드는 미구현이다. outbox 생성은 통지 전달이 아니다.
- **트렌드/보관:** 9개 아날로그의 1Hz raw 투영·기간 인덱스·영속 분 집계/90일 보관·스케줄러·
  조회 API/UI가 필요하다. 여러 collector epoch의 기간 병합 정책도 남아 있다. 원본 batch JSON
  저장을 이 기능과 동일시하지 않는다. `alarm_capture_decisions`의 raw-batch FK는 삭제를
  보호하므로 raw 7일/알람 180일 요구를 충족하려면 compact identity ledger 또는 payload
  분리 이관이 선행해야 한다. App B 원본 보관의 구현이 App A 보관 완료를 뜻하지 않는다.
- **MES:** 미연결 취소 API/UI와 같은 App 세션의 A 정산 기록 보관/다음 작업 UI는 개별 검증했다.
  연결 이후 취소의 전달/접수/결과 불명 경합 정책, 배포/연결/일반 명령의 409 이후 새 준비 절차는 남아 있다. 미연결 취소의 확정 POST 409 이후 절차는 실제 브라우저로 개별 검증했다.
  단순 RELEASED·LOAD 성공·저장된 SETTLED는 다음 작업 허가가 아니며 연결 API가 다시 검증한다.
  보관 UI는 현재 App 메모리이므로 새로고침 후 이력 영속과 구분한다. 정지 사건 재구성/사유→시간 집계→잠정/확정 보고서/CSV 순서의
  기능과 감사·검산 근거가 필요하다.
- **운영 수락:** 두 SQLite와 PostgreSQL의 일관된 백업/새 위치 복원, identity·sequence·
  cursor·명령·알람·설정 대조가 없다. 48태그 1초·10명·8시간, 화면 p95≤2초,
  dwell 충족 후 알람 p95≤3초, 7일 이력 4태그/24시간 조회 p95≤3초, 30분 버퍼의
  5분 내 복구는 미측정이다. 실제 4코어/8GB/SSD 환경·표본/분포·누락/중복·CPU/메모리/DB
  사용량을 기록해야 하며 빠른 fixture나 수천 tick은 이 기준의 대체 증거가 아니다.

## 실행 이력과 재현 명령의 해석

이 절은 **실행 이력**이며 이 문서 작성 중 재실행한 결과가 아니다.

| 명령/근거 | 확인된 제한 범위 |
| --- | --- |
| `.venv/bin/pytest -q tests/integration/test_alarm_consumer.py` | 최종 22개 통과(19.99초). 실제 PostgreSQL/인증 API의 원자성·replay·clock 가정·순서/epoch/잠금 경합. 전체 수락이나 원격 시계 검증은 아니다. |
| `.venv/bin/pytest -q tests/integration/test_alarm_pipeline.py` | 이전 실행 1개 통과(31.39초)는 공정 tick 없는 제어 PV fixture였다. 이후 수정한 최신 실행은 **1개 통과(32.63초)**: 실제 100ms 열 공정 tick·지속 `temperature_offset_c`·양품 생산을 유지하며 TLS OPC→GatewayPump/버퍼→인증 HTTP→PostgreSQL→사용자 ACK/메모·복귀/재발을 확인했다. 실제 하드웨어나 전체 두 앱 브라우저/수락·성능 검증을 뜻하지 않으며, 별도 전체 검사 결과와 합산하지 않는다. |
| `.venv/bin/pytest -q tests/unit/test_plc.py tests/unit/test_process.py tests/unit/test_simulator_tags.py tests/integration/test_simulator_controller.py tests/integration/test_simulator_storage.py tests/integration/test_simulator_transactions.py tests/integration/test_simulator_state_events.py tests/integration/test_simulator_console.py tests/integration/test_simulator_fault_inputs.py` | 최종 집중 257개 통과(20.01초). 실제 파일/공정·콘솔 HTTP, offset 및 migration. PostgreSQL·전체 두 앱 수락은 포함하지 않는다. |
| `.venv/bin/python scripts/check-simulator-browser.py --screenshot /tmp/hmi-simulator-temperature-offset.png` | 실제 CLI+Chromium 정상 종료. signed offset·50회 이상 실제 100ms tick·form-only clear/명시 적용과 기존 작업/수량/원래 ID/로그아웃. App A 수집·알람 화면과 동시 연결한 시험은 아니다. |
| `scripts/check-app-a-browser.py`, `scripts/alarm_browser_checks.py` 및 [개발 상태 실행 기록](DEVELOPMENT_STATUS.md) | 실제 사용자 API/PG/Chromium의 WS/권한·원래 ID·알람/작업 연결 화면. 제어된 DB/수집 fixture를 실제 OPC 시험으로 표시하지 않는다. |
| `./scripts/check-local.sh` | Python 전체+웹 순수 시험/타입 검사/린트/빌드를 실행한다. **브라우저 스크립트나 장시간 수락을 자동 포함하지 않는다.** 최신 전체 실행과 이후 변경은 개발 진행 상태 기록을 따른다. |

독립 high 검토의 추가 재현은 위험 경계 검토의 증거다. 임시 검토 파일이나 검사 수를
저장소의 상시 회귀 suite·전체 수락 결과와 합산하지 않는다. 완료 판단은 새 전체 실행,
이 문서의 기능 공백 해소, 원래 수락/성능 목표의 실제 측정 증적을 각각 확인해야 한다.
