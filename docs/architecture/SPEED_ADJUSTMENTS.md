# CX-35 운전 중 속도 조정

## 계약과 한계

기존 SET_PARAMETERS 명령을 확장한다. RUNNING/PAUSED에서는 원래 execution_id와 같은 recipe_version, 변하지 않은 온도, 원래 LOAD 레시피 속도의 ±20% 및 PLC 1~60m/min을 모두 만족해야 한다. 연속 조정의 기준은 직전 속도가 아니라 원래 레시피다. PLC 설정 범위도 함께 적용한다. parameters_manage 권한은 그대로 production_manager이며 operator 권한만으로 변경할 수 없다. PLC는 접수와 실제 실행 시점에 안전·권한 경로·모드·상태를 판정한다.

기존 STOPPED/READY의 새 recipe_version 설정 경로는 유지한다. 이번 범위는 운전 중 온도 변경, 안전 해제, 모드 전환을 포함하지 않는다. 레시피 기준이 없는 이전 운전 snapshot은 운전 중 조정을 보수적으로 거절한다.

## 내구성과 정산

PARAMETERS_CHANGED는 원래 실행의 Line 사건이다. 원래 command_id·요청자·recipe_version·원래 baseline·직전/적용 설정·control_version을 기록한다. 물품/생산 수량에는 더하지 않는다. process 설정, 사건 순번, 변경 이력, 감사, 요약, 명령 결과는 같은 SQLite 트랜잭션에 저장한다. 중복 ID는 기존 결과만 돌려주며 새 사건을 만들지 않는다.

시뮬레이터 storage는 v3→v4에서 source/archived 사건 종류 CHECK를 넓힌다. 보존 나이·영구 사건 키·FK·색인·trigger·행 본문을 보존하며 원자적으로 이관한다. runtime v3의 append-only 변경 이력은 원래 성공 명령과 source 사건/보존 hash를 연결해 사건 보존 기간 이후에도 요약 이력을 검증한다. 기존 v1/v2 경로와 final/명령 canonical bytes는 유지한다.

App A의 원래 execution.parameters는 수정하지 않는다. 요약 parameters는 최종 적용 설정, baseline_parameters는 원래 설정, parameter_changes는 순서 있는 전체 변경 사건이다. 없는 선택 필드는 기존 요약 wire에서 생략한다. 정산과 SETTLED 조회는 원래 baseline과 한도·연결된 변경 이력·최종 설정 및 실제 APPLIED inbox 사건의 정확한 일치를 확인한다. 누락/추가/변조 사건은 SETTLED가 될 수 없다. 기존 누락·미확정 명령·수량·최종 경계 조건도 유지한다.

## 변경 영향

기존의 ‘등록 설정=최종 설정’ 검사는 속도 조정의 명시적 사건 근거가 있을 때만 확장된다. 임의 설정 불일치를 허용하는 변경이 아니다. 새 event/summary를 API·gateway·simulator에 함께 배포해야 한다. Claude 소유 UI의 event/요약 exact parser와 원래 설정=최종 설정 가정도 배포 전에 반영해야 한다. 이 작업에서는 운영 SQLite·공개 DB·UI를 변경하지 않는다.

과거 PAUSED에서 새 recipe_version으로 설정을 교체하던 시험/경로는 이번의 ‘운전 중 속도만·원래 레시피 기준’과 충돌한다. PAUSED는 원래 레시피의 허용 속도만 바꾸도록 제한한다. 새 레시피 적용은 기존 STOPPED/READY 경로로 남긴다. 시험도 재공 보존 의도는 유지하면서 같은 레시피·허용 속도·분리된 변경 이력을 확인하도록 맞춘다.
