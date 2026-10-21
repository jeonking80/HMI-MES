# 게이트웨이 OPC UA 클라이언트 · DEV-09a

`apps/gateway/opcua_client.py`는 실제 시뮬레이터의 48개 태그와 네 업무 메서드에 연결하는 독립 클라이언트다. 시뮬레이터의 저장소·메모리·컨트롤러를 읽지 않는다. 공정·명령·사건 값은 암호화 OPC UA 소켓을 통과한다. 버퍼, HTTP 전송, 명령 전달 저널, 수집 ACK 또는 자동 타이머는 이 파일의 범위가 아니다.

## 설정·연결

`GatewayOPCUAConfig`의 필수 필드는 `endpoint`, `application_uri`, `client_certificate`, `client_private_key`, `trusted_server_certificate`, `server_application_uri`다. 인증서 파일은 DER, 클라이언트 개인키는 암호화되지 않은 PEM이며 기존 파일을 명시해야 한다. 자동 인증서 생성·동적 신뢰 등록·NoSecurity 대체를 제공하지 않는다. 기본 `poll_interval_seconds`는 1초, 요청 제한시간은 5초다. 실행기가 이 주기를 사용해 `read_tags()`를 호출한다.

`GatewayOPCUAClient(config, utc_clock=..., monotonic_clock=...)`는 `connect()`와 `disconnect()`를 제공한다. 한 이벤트 루프에서 사용하며 연결 생성·해제는 잠금으로 직렬화한다. 연결마다 namespace URI `urn:hmi-mes:packaging-line:tags:v1`를 조회하고 반환된 index와 태그 사전의 문자열 NodeID로 48개 노드를 구성한다. 특정 index로 대체하지 않는다. 재연결 후 index가 달라져도 사전 주소를 다시 찾는다.

전송은 OPC UA `Basic256Sha256`·`SignAndEncrypt`만 사용한다. 서버 인증서의 유효기간·용도·서버 역할·SAN 애플리케이션 URI, 설정한 정확한 서버 URI, 사전 배포한 인증서 SHA-256 지문을 확인한다. 접속 호스트가 IP면 SAN IP 주소, DNS 이름이면 SAN의 정확한 DNS 이름과 대조한다. 와일드카드 이름을 자동 허용하지 않는다. 클라이언트 인증서와 키의 일치·유효기간·클라이언트 용도·설정 URI도 연결 전에 검사한다. 검증 실패 시 보안 수준을 낮춰 재접속하지 않는다. OPC UA 보안 채널이며 별도 HTTP TLS를 사용하는 API 전송과 구분한다.

## 묶음 읽기와 원본 관측

`await read_tags()`는 하나의 UA Read 서비스로 태그 48개를 읽고 `GatewayObservation(acquired_at, samples, health)`를 반환한다. `samples`는 불변 `TagSample` 튜플이며 각 값은 태그 사전의 자료형·열거값·정밀도 조건을 검사한다.

- `source_timestamp`·`server_timestamp`는 원본 값 또는 null을 보존한다.
- `acquired_at`은 게이트웨이의 UTC 관측 시각이며 48개에 공통이다.
- UInt32 `status_code`를 보존하고 Bad·Uncertain 값도 원래 값 또는 null로 유지한다.
- UInt64는 Python 정수와 기존 JSON 십진 문자열 계약을 사용해 최대값까지 정확히 유지한다.
- 관측마다 새 `sample_id`를 한 번 할당한다. 이후 버퍼는 그 ID와 원본 관측을 영속 확정하고 재전송 시 그대로 사용해야 한다.

Bad·Uncertain의 Null Variant는 사전 자료형의 null 값으로 표현한다. Good인데 값이 없거나, 실제 자료형·열거값·수치가 계약을 위반하면 `GatewayReadError`로 해당 묶음 전체를 실패시킨다. 원인은 예외의 `__cause__`로 제공한다. 잘못된 값을 0·정상 상태·이전 Good 값으로 대체하지 않는다. 미완성 관측을 정상 수집으로 표시하지 않는다. 계약 위반 진단의 영속 보존은 상위 실행기·버퍼의 후속 범위다.

연결이 없으면 `read_tags()`가 보안 연결과 읽기만 수행한다. 이미 연결된 읽기가 실패하면 세션을 해제하고 `GatewayReadError`를 반환한다. 이후 호출의 읽기는 새 namespace 조회와 함께 재접속할 수 있다. 읽기 재접속은 운전 메서드를 실행하지 않는다.

## 단조 시각에 따른 통신·heartbeat 상태

`health()`는 원본 시간과 분리된 수신/heartbeat 상태 `GatewayHealth`를 반환한다. `connected`, 마지막 정상 형식 묶음 읽기의 경과시간, Line·Infeed·Sealer·Outfeed 네 heartbeat별 경과시간을 제공한다. heartbeat는 Good 품질에서 값이 바뀐 경우만 새 활동으로 인정한다. Bad·Uncertain heartbeat 변화는 새 정상 활동이 아니다.

이 네 heartbeat를 모두 확인하는 것은 초기 게이트웨이의 명시적 판정이다. 마지막 읽기와 각 heartbeat 중 가장 오래된 활동이 3초 이상이면 DELAYED, 5초 이상이면 ALARM이다. 아직 정상 관측 근거가 부족하거나 연결이 해제되었고 임계시간에 도달하지 않았으면 UNKNOWN이다. 나머지는 GOOD이다. 새 연결이나 성공한 읽기만으로 그대로인 heartbeat의 경과시간을 초기화하지 않는다. 시계는 `time.monotonic`이며, SourceTimestamp가 오래되거나 벽시계가 앞뒤로 바뀐 사실만으로 통신 활동을 갱신·차단하지 않는다.

이는 통신 활동 판정이며 개별 태그 품질·설비 준비·실행/명령/사건 대조를 포함한 원격 제어 허용 판정이 아니다. GOOD 통신 상태라도 Bad 태그나 미정 명령이 있으면 상위 제어 게이트가 계속 제한해야 한다. 개별 태그의 원래 품질과 시각은 별도로 남는다.

## 네 메서드와 결과 불명

| 클라이언트 API | 입력 → 응답 |
|---|---|
| `submit_command` | `CommandRequest` → `CommandSubmission` |
| `get_command_result` | `CommandResultRequest` → `CommandResult` 또는 `CommandNotFound` |
| `read_events` | `ReadEventsRequest` → `EventPage` 또는 `EventReadError` |
| `get_run_summary` | `RunSummaryRequest` → `RunSummary` 또는 `RunNotFound` |

입력·출력은 공유 모델로 검증하며 메서드 NodeID와 JSON String 계약을 사용한다. 응답의 명령 ID·실행 ID·사건 커서/스트림·페이지 크기도 원래 요청에 결합한다. STREAM_MISMATCH 오류의 실제 소스/스트림 표시는 보존한다. 입력 객체나 command_id를 수정·새로 생성하지 않는다. 운전 메서드는 연결을 자동 생성하거나 요청을 재시도하지 않는다. 호출자는 먼저 연결하고, 원래 ID의 접수/결과를 영속 추적해야 한다. 최초 전달 전 만료 검사는 상위 명령 워커가 수행한다. 결과 대기 초과·재연결 뒤 새 ID 자동 실행을 제공하지 않는다.

RPC·응답 해석 실패는 `GatewayMethodUncertain(method, command_id, delivery_possible)` 예외다. 연결이 없어 호출 전에 실패하면 `delivery_possible=False`, 호출을 시작한 뒤 검증된 응답을 얻지 못하면 보수적으로 true다. 서버가 접수한 후 잘못된 응답을 보낸 경우도 true이며, 원래 ID로 결과를 조회해야 한다. 예외를 PLC FAILED·REJECTED로 조작하지 않는다. `COMMAND_NOT_FOUND` 역시 실행되지 않았다는 증거로 해석하지 않는다. 호출 취소를 포함한 전달 경계의 영속 기록과 OUTCOME_UNKNOWN 상태 전이는 상위 명령 워커의 책임이다.

## 검증

```sh
.venv/bin/ruff check apps/gateway/opcua_client.py tests/integration/test_gateway_opcua.py
.venv/bin/pytest -q tests/integration/test_gateway_opcua.py
```

시험은 실제 시뮬레이터 서버·클라이언트 암호화 소켓과 임시 인증서 파일을 사용한다. 48개 묶음·정확한 UInt64 최대값·원본 Bad/Uncertain/null·누락 시각, heartbeat 정지 3초/5초와 벽시계 변경, namespace index 변경 후 재연결, 실제 명령·생산 사건·최종 요약, 접수 뒤 해석 불가능하거나 명령 ID가 다른 응답과 원래 ID 조회, 미신뢰 서버·URI·키·호스트명 실패, 잘못된 열거값 거절을 검증한다. 빠른 단조 시각 주입은 경과시간 판정만 대체하며 OPC UA 통신·PLC·저장 경계를 대체하지 않는다.

이 단계는 API 수집·DB 정산·화면 동기화·인증된 운영 명령 전달 전체나 T01–T12 전체 완료를 뜻하지 않는다.
