# CX-34 시작 예약

LOAD_JOB의 `start_when_ready`는 strict boolean이며 기본 false다. 사람이 true로 명시한 성공 LOAD만 예약한다. API의 직접 명령과 bind 모두 load_job·operate 권한을 함께 요구한다. production_manager 권한만으로 운전을 예약할 수 없다. 기존 수동 START와 원격 비상정지 해제 금지는 유지한다.

PLC는 REMOTE/AUTO·안전 허용 조건을 다시 확인하고 READY/LOADED에서 RUNNING으로 한 번만 전이한다. 새 START 명령 ID를 생성하지 않는다. 예약은 원래 성공 LOAD의 execution·요청자·권한·설정·목표와 연결된다. START_RESERVED/FIRED/CANCELLED 감사와 상태·사건·요약·명령 완료는 같은 SQLite 트랜잭션이다. 원래 ID 재조회/재요청으로 새 예약이나 운전을 만들지 않는다.

예열 온도 변화 자체는 취소 사유가 아니다. 가드·비상정지·고장·설비 준비·압력 허용 상태 또는 제어/운전 모드 변화, 재시작·복구는 예약을 취소하고 사유를 보존한다. 유효하게 실행된 수동 START/END도 예약을 소비/취소한다. 거절된 명령은 예약을 변경하지 않는다. 취소 뒤 자동 재무장은 하지 않는다.

이전 LOAD/PLC snapshot은 예약 없음으로 읽는다. canonical 재요청은 auto_end_at_target과 start_when_ready 각각 원래 저장 모양을 기준으로 비교한다. 이전 final/저널을 다시 쓰지 않는다.

변경 이유: 예열 완료를 사람이 반복 확인하지 않고, 명시적으로 승인한 시작만 PLC가 수행한다. 영향: API·gateway·simulator 계약을 함께 배포해야 한다. Claude 소유 UI의 정확한 키 parser는 두 LOAD 정책 필드와 기존 legacy 형태를 함께 지원해야 한다. UI 변경 전 공개 배포 완료로 판단하지 않는다.
