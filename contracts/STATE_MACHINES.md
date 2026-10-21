# 상태 전이 계약 v1 · DEV-02c

기준: 시스템 기획서 [§06–§12](../docs/HMI_MES_시스템_기획서_v1_0.md), 개발계획서 [§4–§5](../docs/HMI_MES_개발계획서_v1_0.md), [태그 사전](tags/tags.v1.json), [프로토콜 모델](models.py).
이 문서는 M0에서 검토한 전이·허용 조건·완료 근거의 v1 기준선이다. 상태기계 구현이나 실제 운전·복구 시험을 통과했다는 뜻은 아니다.
원문이 정하지 않은 처리 순서·영문 업무 상태·복구 조작은 **초기 결정**으로 표시한다. 수치 범위와 실제 장비의 정지 동작은 후속 구현에서 검증한다.

## 1. 공통 판정과 상태의 소유자

PLC가 실제 제어 상태와 명령 허용을 결정한다. HMI는 성공 결과와 정상 품질의 피드백을 함께 확인한다.
각 전이는 원인·시각·실행 ID·제어 버전을 기록한다. 수량 변경은 물품·재공·카운터·사건과 같은 SQLite 트랜잭션으로 확정한다.
표에 없는 전이는 거절하거나 현재 상태를 유지하며, 새 명령을 조용히 예약하지 않는다.

| 구분 | 상태와 의미 |
|---|---|
| PLC `Line.State` | STOPPED=0, READY=1, RUNNING=2, PAUSED=3, STOPPING=4, FAULT=5, ESTOP=6, RECOVERY_REQUIRED=7 |
| `ControlMode` / `OperationMode` | LOCAL=0·REMOTE=1 / MANUAL=0·AUTO=1; 수치와 이름은 태그 계약의 초기 결정 |
| 운영 앱 표시 | UNKNOWN은 통신·시간 품질에 따른 표시 상태이며 PLC 상태를 바꾸지 않음 |
| 실행 요약 `RunSummary.state` | LOADED, RUNNING, PAUSED, ENDING, ENDED; PLC 고장·비상정지와 별도의 작업 진행 상태 |

**초기 결정:** 원격 START는 REMOTE·AUTO에서만 허용한다. LOCAL의 현장 조작도 같은 PLC 조건 검사를 거친다.
`start_ready`는 유효한 연결 실행·설정, 닫힌 가드, 해제된 비상정지, 고장 없음, 준비·예열 완료를 모두 뜻한다.
`control_ready`는 새 정상 관측과 실행·명령·사건 대조 완료, 미해결 충돌 없음이다. 이는 운영 앱의 전달 제한이며 PLC 검사도 별도로 수행한다.
허용 조건은 접수 시와 실제 동작 직전에 다시 검사한다. 사이에 조건이 바뀌면 접수된 명령도 실행 실패할 수 있다.

## 2. PLC 전이

**초기 결정:** 안전 입력의 표현 우선순위는 ESTOP > FAULT > RECOVERY_REQUIRED > 일반 상태다.
재시작 복구 필요 여부는 별도 영속 플래그로 유지하므로 ESTOP·FAULT 표시에 가려져도 사라지지 않는다.

| 현재 | 트리거·가드 | 다음 | 완료 신호·보존 사항 |
|---|---|---|---|
| 모든 상태 | 프로세스 시작; 안전 입력 먼저 확인 | RECOVERY_REQUIRED 또는 ESTOP/FAULT | 출력 정지, 복구 필요 플래그, 저장 실행·재공·명령 복원 |
| ESTOP 외 모든 상태 | 비상정지 입력 | ESTOP | 운전 출력 해제, 실제 원인 유지; 실행 ID·재공 보존 |
| ESTOP 외 모든 상태 | 고장·가드 개방 등 운전 차단 원인 | FAULT | FaultCode·원인 기록, 운전 출력 해제 |
| ESTOP | 현장에서 비상정지 원인 해소·해제 | FAULT / RECOVERY_REQUIRED / STOPPED | 다른 고장 우선, 복구 플래그 우선; 자동 START 없음 |
| FAULT | RESET_FAULT; 원인 해소·비상정지 해제·리셋 가능 | RECOVERY_REQUIRED / STOPPED | FaultCode 해제와 실제 피드백; 복구 플래그는 유지 |
| RECOVERY_REQUIRED | §7 복구 게이트를 명시적으로 통과 | STOPPED | 복구 확인 기록, 출력 정지; 별도 START 필요 |
| STOPPED | LOAD_JOB; 재공 0·이전 실행 정산 완료·충돌 없음 | STOPPED / READY | ActiveExecutionId·RecipeVersion 일치, 설정 적용; start_ready면 READY |
| STOPPED | 실행 연결됨·start_ready 충족·복구 게이트 해제 | READY | Permissive·Ready와 적용 설정 일치 |
| READY | 준비 조건 소실; 안전 차단 원인 없음 | STOPPED | 원인 표시, 동작 시작 안 함 |
| READY / PAUSED | START; start_ready·control_ready·버전·실행 일치 | RUNNING | 명령 성공과 RUNNING·RunFeedback 일치; 재개는 같은 실행 ID |
| RUNNING | PAUSE; 원격 가능·실행 일치 | STOPPING | 정지 목적 PAUSE를 내부에 보관; §3 수행 |
| STOPPING | 정지 목적 PAUSE; 모든 구동 정지 확인 | PAUSED | 속도 0·RunFeedback false, 재공·작업 보존 |
| STOPPED / READY / RUNNING / PAUSED | END_RUN; 실행 일치·종료 순서 수행 가능 | STOPPING | 정지 목적 END_RUN; 신규 투입 금지, 실행 요약 ENDING |
| STOPPING | 정지 목적 END_RUN; 재공 0·최종 경계 영속 확정 | STOPPED | final=true·ENDED 요약·최종 수량·last_event_seq |
| FAULT / ESTOP | END_RUN; 실행 일치·안전한 재공 정리 가능 | 같은 상태 | 실행만 ENDING→ENDED 가능; 물리 원인·FaultCode·비상정지 유지 |
| STOPPED / READY / PAUSED | SET_PARAMETERS; 허용 범위·원격·실행 일치 | 상태 유지 / STOPPED | 새 RecipeVersion·실제 설정값 일치; 준비 재평가 |

**초기 결정:** SET_PARAMETERS는 STOPPING·RUNNING·FAULT·ESTOP·RECOVERY_REQUIRED에서 거절한다.
기존 실행의 생산이 있으면 새 설정 버전과 적용 구간을 보존하고 기존 생산 기록을 덮어쓰지 않는다.
PLC의 END_RUN 성공은 설비 경계 확정이다. MES의 사건 대조 완료와 구분하며, 다음 LOAD_JOB는 MES 정산 완료 전 전달하지 않는다.
가드 개방은 초기 시뮬레이터에서 래치 고장으로 취급하고, 닫은 뒤 별도 RESET_FAULT를 요구한다.

## 3. PAUSE와 END_RUN의 정지 순서

**초기 결정:** PAUSE는 재공을 보존하는 정지이며 STOPPING에 내부 목적을 기록한다. 태그는 추가하지 않는다.

| 단계 | PAUSE 처리 | 확인·실패 처리 |
|---|---|---|
| P1 | 접수 후 다음 100ms 공정 경계에서 신규 투입 금지 | 아직 접수 응답만으로 PAUSED를 표시하지 않음 |
| P2 | 같은 경계에서 물품의 현재 위치·단계 확정, 세 설비의 이송·검사 진행 정지 | 수량 변화가 있으면 물품·사건·카운터와 함께 확정 |
| P3 | 목표 속도 0, 구동 정지 후 RunFeedback false 확인 | 확인되지 않으면 STOPPING 유지; 고장·비상정지는 해당 상태 우선 |
| P4 | PAUSED와 실행 요약 PAUSED 확정, 원래 execution_id·재공 유지 | 재개는 새 START 검사; 히터는 설정 제어 유지 가능 |

이는 소프트웨어 시뮬레이터의 경계 정지 결정이다. 실제 장비의 안전 정지·잔류 운동 규칙은 별도 검증이 필요하다.
END_RUN은 신규 투입을 금지한 뒤 기존 실행 ID로 재공을 배출한다. 배출 불가 물품은 사유 있는 `WIP_SCRAP` 사건으로만 정리한다.
**초기 결정:** 고장·비상정지 중 이동이 금지되면 정지 위치를 확인한 시뮬레이터 콘솔의 명시적 폐기 조작만 허용한다.
END_RUN 자체가 물품을 자동 폐기하거나 물리 원인을 해제하지 않는다. 안전한 정리 근거가 없으면 접수 거절 또는 ENDING 유지다.
재공 0 후 최종 수량·설정 버전·counter_epoch·last_event_seq·ended_at을 영속 확정해야 END_RUN이 성공한다.
PAUSE 처리 중 새 END_RUN은 충돌 명령으로 거절한다. 기존 명령 결과를 확인한 뒤 새 요청을 만든다.

## 4. 명령 접수와 실행

`CommandSubmission.disposition`은 ACCEPTED / REJECTED 두 값이다. ACCEPTED는 실행 성공을 뜻하지 않는다.
`CommandResult.status`는 아래 모델 이름을 그대로 사용한다. 접수 거절과 실행 실패는 별도 결과다.

| 현재 | 트리거·가드 | 다음 | 필수 기록·사용자 의미 |
|---|---|---|---|
| 없음 | API 권한·입력·충돌 검사 통과, 요청 영속 저장 | REQUESTED | command_id·원본 내용·expires_at·expected_control_version |
| REQUESTED | 전달 직전 현재 시각이 expires_at 이상; 미전달 확실 | EXPIRED | completed_at·사유; 전달·늦은 실행 금지 |
| REQUESTED | 유효기간 안에서 게이트웨이가 전송 경계 확정 | DELIVERED | delivered_at 저장 후 OPC 요청; 전송 결과 불명 가능 |
| DELIVERED | PLC가 버전·실행·모드·허용 조건 검사 후 접수 영속화 | ACCEPTED | CommandSubmission ACCEPTED; 아직 실행 완료 아님 |
| REQUESTED / DELIVERED | API 또는 PLC의 확정된 조건 위반 | REJECTED | reason_code·reason·completed_at; 원인을 고친 뒤 별도 요청 가능 |
| ACCEPTED | 실제 완료 조건 충족·상태와 결과를 함께 확정 | SUCCEEDED | completed_at·applied_control_version·evidence |
| ACCEPTED | 실제 실행 불가가 확정됨 | FAILED | 사유·completed_at; 현재 상태와 부분 진행 근거 보존 |
| DELIVERED / ACCEPTED | 응답 유실·결과 대기 초과·재시작으로 결과 미확정 | OUTCOME_UNKNOWN | 사유와 delivered_at; completed_at 없음, 신규 충돌 명령 제한 |
| OUTCOME_UNKNOWN | 같은 ID 조회로 접수·진행을 확인 | ACCEPTED | 기존 명령의 진행 추적 재개; 자동 재실행 아님 |
| OUTCOME_UNKNOWN | 같은 ID의 확정 결과 복원 | SUCCEEDED / FAILED / REJECTED | 원래 결과·시각·증거 반영; 새 ID로 대체 안 함 |
| 알려진 종료 상태 | 같은 ID·같은 내용 재요청 | 상태 유지 | 저장된 결과 반환; 새 동작·사건·버전 증가 없음 |

기본 전달 TTL은 5초다. PLC도 처음 보는 ID의 만료 요청은 동작 전에 거절한다. 게이트웨이에서 미전달이 확실할 때만 EXPIRED다.
전송 경계가 지난 뒤 expires_at이 지나도 EXPIRED로 바꾸지 않는다. 이미 접수된 명령의 실행 완료를 TTL로 취소하지 않는다.
START 접수 후 10초, END_RUN 60초는 초기 결과 대기값이며 초과는 FAILED의 근거가 아니다.
**초기 결정:** 나머지 네 명령의 결과 대기도 10초를 사용하고 후속 통합 시험에서 확정한다.
원래 ID 조회 결과가 없거나 서버 상태를 확인할 수 없으면 OUTCOME_UNKNOWN을 유지하고 복구 절차에서 대조한다.
동일 ID·다른 정규화 내용은 ID 충돌로 거절하며 기존 명령 결과를 덮어쓰지 않는다. 이는 원래 명령의 상태 전이가 아니다.
정규화 범위는 `canonical_command_payload`와 같으며 command_id 외 요청자·시각·만료·대상·실행·인자·기대 버전을 모두 포함한다.

| 명령 | SUCCEEDED에 필요한 최소 완료 근거 |
|---|---|
| LOAD_JOB | ActiveExecutionId·RecipeVersion·적용 설정 일치, 실행 요약 LOADED |
| START | RUNNING·운전 피드백, 해당 execution_id 일치 |
| PAUSE | PAUSED·구동 정지 확인, 같은 execution_id·재공 보존 |
| END_RUN | final ENDED 요약·재공 0·최종 수량·last_event_seq 영속 확정; PLC는 FAULT/ESTOP일 수 있음 |
| SET_PARAMETERS | 새 RecipeVersion·적용 온도/속도 일치, 적용 제어 버전 |
| RESET_FAULT | 원인 해소·FaultCode 해제·정지 또는 복구 상태; 자동 운전 없음 |

`CompletionEvidence`는 state·execution_id·recipe_version·last_event_seq를 제공한다. 설정값·수량·운전 피드백은 태그와 RunSummary로 추가 대조한다.

## 5. ControlVersion과 경합

**초기 결정:** PLC는 아래 제어 튜플의 영속 변경 트랜잭션마다 ControlVersion을 정확히 한 번 증가시킨다.
튜플은 PLC 상태, 현장/원격·운전 모드, 활성 실행, 적용 설정 버전·값, 안전 입력·고장·Permissive·Ready, 복구 게이트다.
한 트랜잭션에서 여러 항목이 바뀌어도 한 번 증가한다. heartbeat·측정값·생산 카운터·EventSeq 증가만으로는 증가하지 않는다.
알람 확인·화면 선택·명령 조회·조건 거절·동일 ID 재조회는 증가 원인이 아니다. 예열이 Ready 판정을 바꾸면 증가한다.
expected_control_version은 새 명령 접수 시 현재 값과 같아야 한다. 불일치는 접수 거절이며 최신값을 읽어 사용자 의도로 새 요청한다.
**초기 결정:** 접수와 실행 예약은 직렬화한다. 중간 안전 상태 변경은 완료 직전 재검사하며 실행 실패 또는 안전 상태로 전환한다.
명령 자체의 STOPPING→PAUSED 같은 전이도 버전을 증가시키므로 완료 시 원래 기대 버전과 다시 단순 비교하지 않는다.
applied_control_version은 성공 근거를 확정한 버전이다. UInt64는 JSON에서 프로토콜 모델의 십진 문자열 표현을 따른다.

## 6. 알람의 네 상태

아래 영문 상태명은 **초기 결정**이며 아직 별도 알람 전송 스키마는 아니다. 알람 확인은 운전 명령이 아니다.

| 현재 | 트리거·가드 | 다음 | 사건·완료 근거 |
|---|---|---|---|
| 인스턴스 없음 | 발생 조건과 지속시간 충족 | ACTIVE_UNACKNOWLEDGED | 새 알람 ID·발생 시각·값·규칙 버전 |
| ACTIVE_UNACKNOWLEDGED | 권한 있는 사용자 확인 | ACTIVE_ACKNOWLEDGED | 확인자·시각·메모; 물리 원인 유지 |
| ACTIVE_UNACKNOWLEDGED | 정상 복귀 조건과 지속시간 충족 | RETURNED_UNACKNOWLEDGED | 복귀 시각·근거; 미확인 유지 |
| ACTIVE_ACKNOWLEDGED | 정상 복귀 조건과 지속시간 충족 | RETURNED_ACKNOWLEDGED | 복귀 근거·기존 확인 기록 보존 |
| RETURNED_UNACKNOWLEDGED | 사용자 확인 | RETURNED_ACKNOWLEDGED | 확인자·시각; 이력 완료 |
| ACTIVE_* | 같은 이상 지속 | 같은 상태 | 동일 알람 인스턴스 유지, 중복 발생 안 함 |
| RETURNED_* | 발생 조건 재충족 | 새 ACTIVE_UNACKNOWLEDGED | 이전 인스턴스 보존, 새 ID 생성 |

통신 단절·Bad·Uncertain은 기존 설비 알람의 복귀 근거가 아니다. 새 정상 관측으로 원인 회복을 확인해야 한다.
온도는 생산 중 ±10℃ 이탈 5초 발생·±7℃ 안 5초 복귀, 공압은 설정 하한 미달 3초·복귀 하한 이상 3초다.
통신은 정상 읽기/생존 신호 3초 중단 시 지연·5초 이상 알람이며 SourceTimestamp 미변화만으로 발생시키지 않는다.
설정 변경에 따른 종료는 정상 복귀와 다른 사유를 보존한다. 늦게 수신한 과거 알람은 이력에 반영하고 현재 알림을 일괄 재생하지 않는다.

## 7. 재시작과 명시적 복구 게이트

**초기 결정:** 복구 확인은 인증된 설비 콘솔의 별도 조작이다. 여섯 OPC 명령에 새 RECOVER 명령을 추가하지 않는다.
RESET_FAULT·알람 확인·재연결·프로세스 재시작만으로 복구 플래그를 해제하지 않는다. START·LOAD_JOB·설정 변경은 게이트에서 차단한다.

| 순서 | 필수 확인 | 통과 근거·불통과 처리 |
|---|---|---|
| R1 | 출력 정지, 안전 입력·물품 위치 확인 | EStop·고장 원인이 남으면 해당 상태 유지 |
| R2 | SQLite 실행·재공·카운터·epoch·사건 순번·명령 결과 복원 | 부분 반영·모순이면 RECOVERY_REQUIRED 유지, 자동 초기화 금지 |
| R3 | 미완료 명령을 기존 ID로 대조, 실행·설정·수량·마지막 연속 순번 일치 | 결과 불명·사건 누락·counter_epoch 불명은 복구 미완료 |
| R4 | 운영 앱 연결 시 새 정상 관측·최신성·시간 품질·실행 대조 | 통신만 회복돼도 충돌성 제어 제한 유지 |
| R5 | 조작자가 대조 결과를 확인하고 복구 확인을 명시적으로 수행 | 사용자·시각·대조 근거 영속화 후 STOPPED; 별도 START 필요 |

설비 앱은 운영 앱 없이 R1–R3·R5를 통해 단독 복구할 수 있다. 운영 앱의 원격 제어 게이트는 R4까지 충족해야 열린다.
영구 누락을 기록하고 최신 수집을 재개하는 일과 실행 정산 완료는 다르다. 근거가 없으면 해당 실행은 종료 대기·대조 보류로 남긴다.

## 8. MES 작업지시와 실행

아래 작업지시 영문명과 로컬 준비 상태는 **초기 결정**이다. 프로토콜의 RunSummary 다섯 상태와 구분한다.
라인당 준비 중·연결·진행·종료 대기 실행을 합쳐 하나만 허용한다. 선택 화면 변경은 전이를 발생시키지 않는다.

| 작업지시 현재 | 트리거·가드 | 작업지시 다음 | 실행·완료 신호 |
|---|---|---|---|
| DRAFT | 필수 품목·목표·라인·설정 검증 후 배포 | RELEASED | 아직 실행 없음 |
| RELEASED | 정지·재공 0·이전 정산 완료, 실행 ID 할당·LOAD_JOB 요청 | PREPARING | 로컬 준비 기록; PLC 연결 전 RunSummary 없음 |
| PREPARING | LOAD_JOB 성공과 실행·설정 피드백 일치 | PREPARING | RunSummary LOADED; START 가능 표시 |
| PREPARING | 확정된 연결 거절·미전달 만료, 취소 요청 없음 | RELEASED | 시도·명령 이력 보존; 활성 실행 예약 해제 |
| PREPARING / PAUSED | START 성공·RUNNING 정상 피드백 | IN_PROGRESS | 같은 execution_id, RunSummary RUNNING |
| IN_PROGRESS | PAUSE 완료 또는 고장·비상정지로 구동 정지 확인 | PAUSED | RunSummary PAUSED; 정지 사유, 물품·실적 보존 |
| 연결된 비완료 상태 | END_RUN 접수 또는 연결 후 취소 처리 | ENDING | RunSummary ENDING; 신규 시작·작업 교체 금지 |
| ENDING | PLC 최종 경계 수신 | ENDING | RunSummary ENDED·final=true; MES는 대조 진행 |
| ENDING | 최종 순번까지 중복 제거·수량·설정·epoch 대조 일치 | COMPLETED | 실행 정산 완료 기록; 활성 예약 해제 |
| DRAFT / RELEASED | 미연결·LOAD_JOB 미전달이 확실한 취소 | CANCELLED | 대기 명령 전송 차단과 취소를 함께 확정 |
| PREPARING / ENDING | 연결 후 취소, 생산·폐기 0·재공 0·최종 대조 완료 | CANCELLED | 연결됐었다면 END_RUN 성공과 final ENDED 요약 필수 |

LOAD_JOB 전달 후 취소는 먼저 원래 ID의 결과를 확인한다. 접수 거절·미연결이 확정되면 취소할 수 있고, 불명이면 준비/종료 대기를 유지한다.
연결이 확인되면 END_RUN과 대조를 거친다. 경합으로 생산·폐기 이력이 있으면 CANCELLED로 숨기지 않고 COMPLETED로 실적을 보존한다.
고장·비상정지 중 END_RUN으로 실행이 ENDED가 되어도 PLC FAULT/ESTOP와 활성 알람은 남을 수 있다.
MES 완료에는 `투입=양품+불량+폐기+재공`, 최종 재공 0, 원본 사건 합계=최종 수량, 마지막 연속 처리 순번 충족을 모두 요구한다.
카운터는 사건 합계에 더하지 않으며, 지연·중복 사건은 원래 execution_id에만 반영한다. 불일치·누락은 ENDING 유지다.
재시작·통신 장애는 작업을 새 실행으로 교체하지 않는다. 실행 진행 상태와 PLC 안전 상태를 별도로 복원하고 §7을 통과한다.
