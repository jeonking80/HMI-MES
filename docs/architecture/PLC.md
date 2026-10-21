# 순수 PLC 모델 · DEV-06a

`apps/simulator/plc.py`는 [상태 전이 계약](../../contracts/STATE_MACHINES.md)의 PLC 상태·명령 허용·실행 경계를 구현한다. 입력과 결과는 불변 Pydantic 모델이며, 같은 입력에는 같은 결과를 반환한다. 시각은 호출자가 제공한다. 파일·DB·OPC UA·네트워크·사용자 인증에 접근하지 않는다. 이 문서의 단위 검증은 실제 HMI·MES 통합, 인증된 제어 경로, 물리 장비의 안전 검증 완료를 뜻하지 않는다.

## 호출 경계

| API | 역할 |
|---|---|
| `check_command(state, request, *, now, config=DEFAULT_CONFIG, authority="REMOTE")` | `GuardDecision`으로 접수 허용·거절을 반환한다. 최초 요청의 TTL·기대 버전·대상·실행·모드·충돌·운전 조건을 검사하며 상태를 변경하지 않는다. |
| `execute_command(state, request, *, config=DEFAULT_CONFIG, authority="REMOTE")` | 이미 접수된 요청의 실제 동작 직전 조건을 재검사한다. `PLCTransition(state, status, reason_code, reason)`을 반환한다. |
| `finish_command(state, request, *, final_boundary_durable=False, config=DEFAULT_CONFIG, authority="REMOTE")` | 진행 중 START·PAUSE·END_RUN의 관측 완료를 판정한다. 완료되지 않았으면 ACCEPTED를 유지한다. |
| `update_inputs(state, inputs, *, config=..., control_mode=None, operation_mode=None, wip_quantity=None, last_event_seq=None)` | 한 관측 경계에서 안전 우선순위·고장 래치·준비 조건·버전을 함께 계산한다. |
| `restart(state, *, config=...)` | 출력 정지와 복구 필요 플래그를 설정한다. 실행·재공·미완료 명령·실제 관측 피드백을 보존한다. |
| `cancel_pending_after_recovery(state, *, config=...)` | 복구 게이트가 닫히고 실제 정지가 확인된 상태에서, 기존 ID의 중단 결과를 대조한 호출자가 진행 예약을 해제한다. |
| `acknowledge_recovery(state, checks, *, config=...)` | 출력·물품 위치·저장 상태·명령 대조·조작자 확인을 모두 요구하는 로컬 복구 게이트다. 성공 후 STOPPED이며 자동 START하지 않는다. |

`PLCState`, `PLCInputs`, `PLCConfig`, `RecoveryChecks`는 `model_dump_json()` / `model_validate_json()`으로 저장·복원할 수 있다. UInt64 제어 버전과 사건 순번은 기존 계약과 동일하게 JSON 십진 문자열이다. 함수 내부의 복사도 검증을 거친다. 상태 모델은 신뢰된 내부 스냅샷이며 외부 제어용 쓰기 인터페이스가 아니다.

접수 확인 자체는 예약을 만들지 않는다. 호출자는 명령 원본·접수 결과·예약을 원자적으로 저장하고 접수부터 실행까지 직렬화해야 한다. START·PAUSE·END_RUN 실행 후에는 `pending_command_id`·`pending_kind`로 새 명령을 차단한다. PAUSE 처리 중 END_RUN도 충돌이다. 동일 ID 재시도·내용 충돌·결과 재조회·재시작 후 원래 ID 대조는 명령 저널의 책임이며, 이 순수 함수를 직접 다시 호출하는 방식은 중복 방지 구현이 아니다.

접수 시 기대 버전과 TTL을 검사하지만, 실행·완료 시에는 원래 기대 버전이나 TTL을 다시 비교하지 않는다. 명령 자신의 전이가 버전을 올릴 수 있고, 접수된 명령은 TTL로 취소되지 않기 때문이다. 대신 실행·대상·현재 모드·안전·준비 조건을 재검사한다. 실행 전 조건 상실은 FAILED이며, START 피드백 전에 고장·모드 변경·준비 상실이 발생해도 FAILED와 안전한 현재 상태를 반환한다.

## 상태와 완료 조건

ESTOP > 래치 FAULT > RECOVERY_REQUIRED > 일반 상태 순서를 적용한다. 가드 개방은 래치 고장이다. 비상정지 해제·고장 원인 해소·RESET_FAULT는 이전 운전을 복원하지 않는다. RESET_FAULT는 복구 플래그를 해제하지 않으며, 복구 확인에는 미완료 명령 대조와 실제 속도 0·운전 피드백 false도 필요하다. `restart`는 실제 피드백을 정지 값으로 바꾸지 않는다. 시뮬레이터 호출자는 실제 모의 정지를 적용한 뒤 새 피드백을 전달해야 한다.

LOAD_JOB는 STOPPED·구동 정지·재공 0·기존 실행 ENDED·이전 정산 완료·새 실행 ID를 요구한다. 설정 적용 후 LOADED이며 예열 조건을 만족하면 READY다. 명령의 `authority`는 LOCAL 또는 REMOTE이며 기본값은 REMOTE다. 요청 출처와 저장된 `control_mode`가 일치해야 한다. LOCAL 출처에도 동일한 안전·실행·설정 검사를 적용하며 START는 두 출처 모두 AUTO를 요구한다. 모드를 임시 변경하지 않는다. 이 인자는 사용자 인증을 대신하지 않으며 신뢰된 호출자가 접수 원래 출처를 실행·완료까지 일관되게 보존해야 한다. 실행 ID는 LOAD_JOB 이외의 명령에서 활성 실행과 일치해야 한다. 활성 실행이 있으면 RESET_FAULT·SET_PARAMETERS도 그 실행 ID를 사용한다.

START는 READY 또는 PAUSED에서 설정·안전·예열 준비를 확인한 뒤 출력 요청과 RUNNING 상태를 설정한다. 결과는 운전 피드백 true를 관측할 때까지 ACCEPTED다. PAUSE는 STOPPING 목적을 보존하고 신규 투입·이송 출력을 정지한다. 실제 운전 피드백 false와 속도 0 후 PAUSED로 완료하며 재공과 실행을 보존한다. 공정 호출자는 STOPPING/PAUSE에서 `process.step(..., run=False, admit=False)`를 적용한다.

END_RUN은 ENDING과 STOPPING 목적을 설정하며 신규 투입을 막는다. 정상 상태에서 재공이 있으면 안전 허용 하에 이송만 계속하고, 재공 0이면 출력을 정지한다. 완료에는 재공 0·속도 0·운전 피드백 false와 `final_boundary_durable=True`가 모두 필요하다. 이 플래그는 호출자가 최종 수량·설정 버전·epoch·last_event_seq·ended_at과 최종 ENDED 요약을 같은 저장 경계에서 확정했다는 근거다. 순수 함수 자체는 영속성을 보장하지 않는다. FAULT·ESTOP 중 END_RUN은 정지 피드백을 요구하며, 재공이 있으면 명시적 로컬 폐기와 영속 사건을 기다린다. END_RUN은 자동 폐기나 안전 원인 해제를 수행하지 않는다. 안전 상태에서 실행 종료가 성공해도 FAULT·ESTOP와 원인은 남는다.

SET_PARAMETERS는 STOPPED·READY·PAUSED의 정지된 활성 실행에서만 허용한다. 새 설정 버전과 범위를 검사하며 예열 준비를 다시 계산한다. 과거 설정 적용 구간·생산 기록 보존은 저장 계층의 책임이다.

## 초기 시뮬레이터 수치 결정

원문에 없는 아래 수치는 `PLCConfig`로 교체 가능한 시험 가정이다. 실제 장비 정격이나 안전 등급으로 사용하지 않는다.

| 항목 | 기본값·경계 |
|---|---|
| 온도 설정 | 50–200 ℃, 양끝 포함 |
| 속도 설정 | 1–60 m/min, 양끝 포함 |
| 공압 하한·복귀 | 4.0 bar 미만에서 준비 해제, 4.5 bar 이상에서 준비 복귀 |
| 예열 | 설정 온도 대비 절대 편차 ≤ 5 ℃; 별도 지속시간 없음 |
| 가드·공압·설비 준비 고장 코드 | 1001·1002·1003; 설정 가능 |

예열은 START 준비 조건이다. 정상 재공 배출은 예열 편차와 별도로 현재 안전 허용을 검사한다. 공압·설비 준비는 운전 중에도 적용하는 즉시 이동 차단 조건이다. 출력 요청·운전 피드백·실제 속도 중 하나라도 이동 중임을 나타낼 때 하한 미달 또는 설비 준비 상실을 관측하면 즉시 래치 FAULT로 전환하고 출력을 해제한다. 정지 중 공압 저하는 준비만 해제하며, 안전 허용이 없는 PAUSED·STOPPED에서 END_RUN이 새로 재공 이동을 켜는 것도 거절한다. 공압 복귀·설비 준비 복귀 후 별도 RESET_FAULT를 요구한다. 이 즉시 PLC 인터록과 문서의 공압 3초·생산 온도 5초 알람 검출은 별도 역할이며 알람 지속·복귀 타이머는 이 파일에 구현하지 않는다.

## 제어 버전과 검증

상태·현장/원격·운전 모드·활성 실행·적용 설정 버전/값·안전 입력/고장·Permissive·Ready·복구 게이트 튜플이 바뀐 호출 경계마다 ControlVersion을 정확히 한 번 증가시킨다. 여러 안전·모드 항목이 동시에 바뀌어도 한 번이다. 온도·압력 등 측정값만 바뀌고 판정 결과가 같거나, 재공·사건 순번만 바뀌면 증가하지 않는다. UInt64 최대값 이후 필요한 전이는 예외로 중단하며 래핑하지 않는다. 호출자는 버전 고갈 시 출력을 정지하는 운영 오류 처리를 제공해야 한다.

```sh
.venv/bin/ruff check apps/simulator/plc.py tests/unit/test_plc.py
.venv/bin/pytest -q tests/unit/test_plc.py
```

단위 시험은 정상 로드·시작 피드백·정지·재개·종료, 안전 우선순위, 접수 후 고장, PAUSE/END 충돌, 안전 중 종료와 고장 보존, 명시 복구, 즉시 이동 인터록, 설정/공압/예열 경계, 버전 원자성·UInt64·불변 스냅샷을 검증한다. 실제 OPC 메서드·인증/권한·콘솔·필드 품질·SQLite 원자적 결과 저널과의 통합, T01–T12·장시간 운전·물리 정지 검증은 각각 해당 후속 범위에서 검증해야 한다.
