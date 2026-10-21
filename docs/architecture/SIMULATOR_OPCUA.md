# 실제 시뮬레이터 OPC UA 어댑터

`apps/simulator/opcua_server.py`는 `SimulatorController`의 실제 명령 저널·공정·사건·실행 요약을 노출한다. `scripts/opcua_poc.py`의 고정 응답을 사용하지 않는다. 외부 공개 경계는 읽기 전용 태그 48개와 JSON String 입출력 메서드 네 개다. 여섯 운전 명령은 기존 `SubmitCommand` 계약 안에서만 전달한다.

## 설정과 수명

`OPCUAServerConfig`에는 `endpoint`, `application_uri`, `server_certificate`, `server_private_key`, `trusted_client_certificate`가 필수다. 인증서 두 파일은 DER, 서버 개인키는 암호화되지 않은 PEM이다. 설정 파일은 기존 파일을 명시해야 한다. 프로세스가 인증서·개인키를 자동 생성하거나, 발견한 상대 인증서를 자동 신뢰하거나, 비보안 엔드포인트로 대체하지 않는다. 개인키 접근 권한과 인증서 배포·갱신은 운영자가 관리한다.

```python
adapter = SimulatorOPCUAServer(controller, config)
await adapter.start()
await adapter.publish_snapshot(acquired_at=now, heartbeat=heartbeat)
await adapter.stop()
```

`start()`는 서버 인증서·키 일치와 인증서 유효기간·용도·애플리케이션 URI를 검사하고, 신뢰 클라이언트 인증서를 등록한 뒤 리스너를 시작한다. 어댑터 자체에는 자동 운전·복구·100ms 타이머가 없다. 실행기가 같은 이벤트 루프에서 컨트롤러 틱을 수행하고 성공한 스냅샷을 발행한다. 저장소와 컨트롤러의 수명도 실행기가 소유한다.

## 애플리케이션 인증과 권한 경계

서버는 `Basic256Sha256`의 `SignAndEncrypt`만 허용한다. 상대 인증서는 시간·용도·클라이언트 역할·SAN의 애플리케이션 URI를 검증하고, 미리 설정한 신뢰 리프 인증서의 SHA-256 지문과 일치해야 한다. 설정 외 인증서는 인증서 체인이 유효하더라도 허용하지 않는다. 서버 키/인증서 오류는 시작 실패이며 평문 세션은 제어 권한을 얻지 못한다.

UA 사용자 토큰 형식은 `AnonymousIdentityToken`이지만, 세션 활성화 시 `CertificateUserManager`가 검증된 상대 애플리케이션 인증서를 `gateway_principal`(기본 `gateway`)의 `User` 역할로 다시 매핑한다. 상대 인증서 없이 로그인하거나, 인증서를 통과하지 않은 익명 사용자가 운전 메서드를 호출하는 경로는 없다. 게이트웨이에 Admin 역할을 주지 않는다. 등록된 게이트웨이 애플리케이션은 네 업무 메서드를 모두 호출할 수 있다.

`SubmitCommand`는 항상 `authority="REMOTE"`로 컨트롤러에 전달한다. 요청 JSON의 `requested_by`는 게이트웨이가 전달하는 업무 감사 주체이며, 이 문자열로 권한을 부여하지 않는다. 이 인증서 경계는 운영 사용자·브라우저 인증 또는 역할별 운전 권한 검증을 대신하지 않는다. 게이트웨이/API의 사용자 인증·권한·감사 검증과 서버 인증서/호스트 검증은 운영 앱의 별도 범위다. 인증서 소유 애플리케이션 자체는 신뢰된 제어 전달자이므로 개인키 유출 시 해당 자격을 회수·교체해야 한다.

## 주소와 명령 결과

태그 namespace URI는 [태그 사전](../../contracts/tags/tags.v1.json)의 `urn:hmi-mes:packaging-line:tags:v1`다. namespace index는 고정하지 않는다. 클라이언트는 연결마다 URI를 조회한다. 루트 객체의 문자열 NodeID는 `Simulator`이며, 태그 48개는 사전의 정확한 문자열 NodeID·자료형을 사용한다. 모든 피드백은 쓰기 금지이며 설정값·모드·실행 ID도 태그 쓰기로 변경할 수 없다.

| 문자열 메서드 NodeID | 컨트롤러 경계 |
|---|---|
| `SubmitCommand` | 실제 `submit(..., authority="REMOTE")`의 `CommandSubmission` |
| `GetCommandResult` | 실제 저널 결과 또는 `CommandNotFound` |
| `ReadEvents` | 실제 원본 사건 페이지 또는 순번·스트림 오류 |
| `GetRunSummary` | 실제 실행 요약 또는 `RunNotFound` |

메서드 입력과 출력은 각각 String 하나다. 요청은 공유 Pydantic 프로토콜 모델로 검증하며 잘못된 JSON·필드·인수는 `BadInvalidArgument`다. 접수 응답은 실행 성공을 뜻하지 않는다. 명령 실행은 이후 컨트롤러 틱에서 이루어지며, 클라이언트는 원래 ID로 결과와 실제 피드백을 조회한다. 같은 ID의 동일 요청과 다른 내용의 충돌은 실제 영속 저널이 판정한다. UInt64 JSON 문자열을 포함한 모든 업무 응답은 [프로토콜](../../contracts/PROTOCOL.md)을 따른다.

메서드 핸들러는 async 함수여서 asyncua의 동기 핸들러 워커 스레드를 사용하지 않는다. 검증부터 컨트롤러 트랜잭션 반환까지 await가 없으며, SQLite 연결 소유 스레드에서 끝낸다. 컨트롤러 호출·틱·콘솔 변경·발행은 같은 이벤트 루프에서 직렬화해야 한다.

## 스냅샷·시각·품질

`publish_snapshot`은 컨트롤러 PLC·공정·epoch를 await 없이 함께 읽고 `tags.project_tags`로 태그를 만든다. 48개가 사전과 정확히 일치하는지 확인한 뒤 단일 스냅샷 포인터를 교체한다. 읽기 서비스의 동기 값 콜백은 같은 스냅샷을 읽으므로 한 묶음 읽기에 서로 다른 제어 경계가 섞이지 않는다.

모든 값은 공통 `SourceTimestamp=acquired_at`, 발행 시각인 `ServerTimestamp`, 입력 `status_code`를 보존한다. 기본 품질은 Good이며 실행기는 필드 실패·불명·지연 판정에 맞는 StatusCode와 실제 획득 시각을 전달해야 한다. 어댑터는 오래된 값을 새 정상 획득으로 추정하거나 불명 상태를 Good으로 복구하지 않는다. heartbeat는 성공한 시뮬레이터 주기의 정수이며 정확한 UInt64를 유지한다. 태그 발행은 PLC ControlVersion을 변경하지 않는다.

현재 전송은 기본 계약의 묶음 폴링을 지원한다. 콜백 기반 스냅샷 교체는 구독 데이터변경 알림을 보내지 않으며, 구독 사용을 검증한 것으로 표시하지 않는다.

## 검증과 남은 범위

```sh
.venv/bin/ruff check apps/simulator/opcua_server.py tests/integration/test_simulator_opcua.py
.venv/bin/pytest -q tests/integration/test_simulator_opcua.py
```

시험은 임시 인증서·키와 실제 암호화 TCP 세션을 사용한다. LOAD_JOB 접수 후 틱에 따른 성공, START 후 실제 생산 사건, END_RUN 최종 경계, 원래 ID 중복·내용 충돌, 48개 주소·타입·읽기 전용·공통 시각·품질, 미신뢰·만료 인증서·URI 불일치·평문·잘못된 요청 거절과 인증된 게이트웨이의 LOCAL 모드 우회 거절을 확인한다. 테스트 인증서 생성은 시험 파일 안에서만 수행한다.

이 검증은 게이트웨이·웹 HMI·MES의 업무 통합, 사용자 인증, 인증서 회수 목록/CA 체인 운영, 외부 네트워크 배포, 장시간 부하 및 T01–T12 전체 수락 시험을 대신하지 않는다.
