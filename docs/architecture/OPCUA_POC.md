# DEV-03a: 실제 OPC UA 호환성 PoC

이 문서는 별도 서버 프로세스와 클라이언트 사이에서 검증한 OPC UA 전송 범위를 기록한다. PLC, 게이트웨이, 명령 영속성, 생산·정산 기능의 구현 완료를 뜻하지 않는다. 기준은 개발계획서 §4, §7 M0, §10과 `AGENTS.md`의 읽기 전용 태그·인증서 검증 원칙이다.

## 실행

저장소 루트에서 고정 의존성이 설치된 Python 3.12 환경으로 실행한다.

```bash
.venv/bin/python scripts/opcua_poc.py
.venv/bin/ruff check scripts/opcua_poc.py tests/integration/test_opcua_poc.py
.venv/bin/python -m pytest tests/integration/test_opcua_poc.py -q
```

각 명령의 실패 종료 코드를 확인한다. CLI는 정상 읽기·메서드·세션 재연결을 확인하며, 인증서·쓰기·잘못된 요청 거절은 pytest가 추가 검증한다. 시험한 라이브러리는 `asyncua==2.1.0`, `cryptography==50.0.2`다.

## 프로세스와 보안

`scripts/opcua_poc.py`의 실행 도구가 임시 디렉터리에 서버·신뢰 클라이언트·미신뢰 클라이언트·다른 서버의 자체 서명 인증서를 생성하고, 별도 Python subprocess에서 OPC UA 서버를 시작한다. TCP 주소는 `127.0.0.1`의 임시 포트다. 클라이언트와 서버는 객체나 DB를 공유하지 않으며 태그와 메서드 교환은 실제 OPC UA 요청으로만 수행한다. 정적 계약 예제 파일은 각 프로세스가 읽는 시험 입력이다.

서버는 `Basic256Sha256`의 `SignAndEncrypt`만 제공한다. OPC UA SecureChannel 보안이며 HTTPS/TLS 검증 설정으로 대체하지 않는다. 클라이언트는 사전에 생성한 서버 인증서를 명시적으로 전달한다. 연결 중 발견한 인증서를 자동 신뢰하지 않는다. 양쪽 인증서 검증은 다음을 적용한다.

- 사전 신뢰한 leaf 인증서와 SHA-256 지문 일치 여부
- 유효기간, ApplicationUri, KeyUsage, 클라이언트/서버 ExtendedKeyUsage
- 클라이언트 측 서버 인증서의 `127.0.0.1` IP SAN 일치 여부

키는 실행마다 생성하며 권한 `0600`인 임시 파일로만 저장한다. 정상 종료와 예외 경로에서 서버를 종료하고, 필요하면 강제 종료 후 회수하며 임시 디렉터리를 삭제한다. 저장소에는 개인키를 쓰지 않는다. 이 시험은 짧은 유효기간의 leaf 직접 신뢰를 사용한다. 운영 CA·인증서 교체·CRL/OCSP·비밀 저장소 배포 절차는 미구현이다.

## 주소와 태그

[태그 계약](../../contracts/tags/tags.v1.json)의 namespace URI `urn:hmi-mes:packaging-line:tags:v1`와 String identifier를 그대로 사용한다. 클라이언트는 연결마다 URI로 namespace index를 조회한다. 특정 index를 하드코딩하지 않는다. `PoC` 객체 아래에 계약의 48개 태그와 네 메서드를 만든다.

48개 태그는 모두 읽기 전용이다. `Boolean`, `Int32`, `UInt32`, `UInt64`, `Double`, `String`의 OPC UA 실제 형식과 DataValue의 정상 품질·원본/서버 시각을 확인한다. UInt64 표본은 최댓값 `18446744073709551615`여서 정밀도 손실도 확인한다. 값은 형식 호환성을 위한 정적 표본이며 실제 측정값이나 서로 일관된 설비 상태가 아니다. `Line.State` 쓰기를 시도해 접근 거절과 값 불변을 확인한다.

## 메서드 전송 계약

각 메서드 NodeId는 같은 namespace URI와 메서드 이름의 String identifier다. 입력은 OPC UA `String` 인자 하나, 출력도 `String` 하나이며 내용은 JSON 문서다. 입력과 출력을 [공유 모델](../../contracts/models.py)로 검증한다. JSON의 UInt64 값은 계약대로 십진 문자열이며, Int32/UInt32·Boolean 등은 각 계약의 JSON 형식을 따른다.

| 메서드 | 입력 JSON 모델 | 출력 JSON 모델 | PoC 동작 |
|---|---|---|---|
| `SubmitCommand` | `CommandRequest` | `CommandSubmission` | 전달한 command_id로 `REJECTED`, `reason_code=POC_ONLY` 반환 |
| `GetCommandResult` | `CommandResultRequest` | `CommandResult` 또는 `CommandNotFound` | 고정 예제 ID 조회 또는 명시적 `COMMAND_NOT_FOUND` |
| `ReadEvents` | `ReadEventsRequest` | `EventPage` 또는 `EventReadError` | 고정 사건 1개의 스트림에서 after_seq·limit 적용, 다른 스트림·앞선 커서 오류 반환 |
| `GetRunSummary` | `RunSummaryRequest` | `RunSummary` 또는 `RunNotFound` | 고정 예제 execution_id 조회 또는 명시적 `RUN_NOT_FOUND` |

잘못된 JSON이나 입력 스키마 위반은 OPC UA `BadInvalidArgument`다. 유효한 조회의 미존재·스트림 불일치·커서 오류는 OPC 호출 자체의 실패 대신 표에 명시한 JSON 오류 모델을 사용한다. 서버 내부 오류를 정상 응답으로 바꾸지 않는다.

조회 결과는 `contracts/examples`의 고정 문서다. 예제 결과의 `SUCCEEDED`와 요약의 `ENDED`는 그 문서의 직렬화 표본이며 PoC에서 명령이 실행되었다는 뜻이 아니다. SubmitCommand는 항상 거절하고, 제출과 고정 조회 예제 사이에는 상태 전이·인과관계가 없다. 중복 명령 보존, 실행 결과 조회의 업무 의미, 재시작 복원, 사건 원자성, 실제 보관기간·영구 누락 및 정산 판단은 후속 설비 앱·게이트웨이 구현에서 검증해야 한다. 시험 메서드는 신뢰된 애플리케이션 인증서에 한해 호출할 수 있다. OPC UA 사용자 identity token은 `Anonymous`만 제공하며 사용자 로그인·역할별 제어 권한은 구현하지 않는다. 애플리케이션 인증서 검증을 사용자 인증 완료로 간주하지 않는다. 실제 제어를 수행하는 후속 경로에서는 사용자 인증·서버 권한 검사를 반드시 구현하고 검증해야 한다.

## 검증 결과와 한계

통합 시험 10개가 별도 서버 프로세스에 연결하여 다음을 확인한다.

- 신뢰된 인증서로 48개 태그의 값·OPC 자료형·품질·시각 읽기
- 미신뢰 클라이언트의 `BadCertificateUntrusted` 거절
- 잘못된 서버 인증서 pin의 보안 연결 실패, 서버 신뢰 검증기의 별도 `BadCertificateUntrusted` 거절
- 암호화 없는 애플리케이션 세션 거절
- 읽기 전용 태그 쓰기 거절 및 값 불변
- 네 메서드의 실제 요청→응답 모델 전송, 잘못된 JSON 거절, 조회 오류·사건 커서 처리
- 연결 종료 뒤 다른 세션 토큰으로 재연결 및 태그 재조회

이는 정상 종료 후 새 세션을 만드는 재연결 시험이다. 네트워크 단절 감지, 자동 재시도, 실행 중 응답 유실, 서버 재시작, 장시간 운전·처리량·현장 장비 상호운용 시험은 수행하지 않았다. DEV-03a의 전송 호환성 확인 범위이며 DEV-03 전체, M0 전체 또는 프로젝트 완료 선언이 아니다.
