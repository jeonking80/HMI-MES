# CX-36 라인별 다음 작업 연결

## 명시적 승인과 범위

대기열은 기본 비활성·비어 있음이다. 사람이 배포된 작업을 순서대로 선택하고 auto_advance를 켜야 동작한다. 기존 배포 작업을 자동으로 등록하지 않는다. 승인은 라인·gateway·source·stream·capture_generation과 사람 계정에 묶이며, 실제 작업 시에도 계정 활성 상태와 현재 권한을 확인한다. source/generation 변화나 결과 불명·확정 실패는 사람의 확인 대상으로 남긴다.

자동화 범위는 다음 작업 LOAD_JOB 연결까지다. start_when_ready=false이며 자동 START를 승인한 것으로 취급하지 않는다. 원격 안전 해제·모드 전환은 계속 불가하다.

## 진행 조건과 내구성

원래 관리 실행의 SETTLED와 동일 scope/물리 실행, 최신 완전 수집, STOPPED·재공0·구동 정지 및 제어 모드를 확인한 뒤 기존 bind_work_order 경로를 사용한다. PENDING·누락·불명은 연결 조건을 만족하지 않는다. 잘못된 선두 작업을 조용히 건너뛰지 않는다.

각 대기 항목의 LOAD ID와 원래 binding receipt를 저장한다. 이미 전송될 수 있는 요청은 원래 ID로만 확인하며 새로운 ID로 자동 재실행하지 않는다. 등록·순서 변경·승인·자동 연결·차단을 감사에 남긴다. 수동 요청 재시도는 actor-bound 멱등 키로 원래 결과를 조회한다.

실행 경로는 gateway의 인증된 /internal/v1/commands/claim 요청이다. 자동 연결을 별도 트랜잭션에서 커밋한 뒤 기존 claim을 수행한다. Vercel의 지속 백그라운드 실행이나 새 유료 서비스를 요구하지 않는다. 큐를 지원하지 않는 이전 schema에서 내부 claim은 기존 동작을 유지한다.

## API와 배포

GET/PUT /api/v1/lines/{line_id}/queue로 조회·명시적 순서/설정을 관리한다. 조회는 read 권한, 변경은 production_manage와 기존 session·Origin·CSRF 보호를 요구한다. 변경에는 request_id·expected_revision·source_id·stream_id·auto_advance와 work_order_id/expected_row_version 목록을 넣는다. UInt64는 기존 계약대로 JSON decimal 문자열이다.

0020_line_queue 이관은 기존 기록을 바꾸지 않고 새 정책/항목/불변 요청 영수증/감사를 추가한다. 사용된 큐 근거가 있으면 downgrade를 거절한다. 이관·최소 runtime grants·HTTP batch는 준비만 하며 공개 적용은 Claude가 한다. UI 조회/순서/승인 화면도 Claude 담당이다. 전체 프로젝트 완료나 공개 자동화 검증 완료를 뜻하지 않는다.
