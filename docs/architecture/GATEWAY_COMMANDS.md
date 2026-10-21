# 게이트웨이 명령 전송 단위

`apps/gateway/commands.py`는 API가 이미 예약한 명령을 OPC UA로 한 번 제출하고 원래 command ID로 조회·보고하는 라이브러리다. CLI의 명시적 `--enable-commands` 옵션으로 수집·전송과 별도 루프에서 실행한다. 실행·종료 절차는 [M2 실행](M2_RUN.md)을 따른다. 사용자 HTTP 권한·API의 PostgreSQL 명령 예약·실제 socket 통합은 이 모듈의 단위 시험 범위 밖이다.

`CommandTransport(IntakeConfig(...))`는 기존 게이트웨이 수집 전송과 같은 서비스 bearer·HTTPS 정책을 사용한다. loopback HTTP는 `allow_loopback_http=True`를 명시할 때만 허용된다. 원격 HTTPS는 환경 proxy·CA 신뢰를 유지하고 loopback은 직접 연결한다. redirect를 따라가지 않으며 HTTP 요청을 자동 재시도하지 않는다. 응답은 `max_response_bytes`로 제한하고 응답 본문·인증 헤더·토큰을 오류에 출력하지 않는다. 소유자가 종료 시 `await transport.close()`를 호출한다.

| 메서드 | 요청 | 응답 |
|---|---|---|
| `claim()` | `POST /internal/v1/commands/claim`, `{}` | `{work: null}` 또는 아래 SUBMIT/QUERY 작업 |
| `report_submission(submission)` | `POST /internal/v1/commands/submission`, 원본 `CommandSubmission` | `{committed: true, command_id: 원본 ID}` |
| `report_result(result)` | `POST /internal/v1/commands/result`, 원본 `CommandResult` 또는 `CommandNotFound` | 동일한 commit ACK |

claim 작업은 `{"action":"SUBMIT","request":CommandRequest}` 또는 `{"action":"QUERY","request":CommandResultRequest}` 형태다. envelope·action·request의 알 수 없는 필드를 거부한다. 보고 ACK는 JSON boolean `true`와 정확히 같은 command ID를 요구한다. 문자열 `"true"`, 숫자 `1`, 다른 ID, 추가 필드는 commit 증거로 인정하지 않는다. HTTP 200만으로 보고 완료를 판단하지 않는다.

## 한 번의 작업

`CommandWorker(opc, transport, clock=...).run_once()`는 먼저 연결하고 전체 태그를 새로 읽은 뒤 실제 게이트웨이 health가 연결됨·GOOD인지 확인한다. 이 조건이 충족되어야 API에서 한 번 claim한다. SUBMIT 작업은 claim 후 현재 시각의 TTL과 health를 다시 확인한다. TTL 경과 또는 health 저하 시 제출하지 않고 `deferred`를 반환한다. API가 이미 예약한 결과 불명 상태를 EXPIRED·FAILED로 바꾸는 가짜 PLC 결과를 만들지 않는다.

SUBMIT 호출은 이 작업에서 최대 한 번이다. 검증된 접수 응답은 그대로 보고한 뒤 원래 ID로 `GetCommandResult`를 호출한다. 접수 응답이나 보고 ACK가 유실되어도 새 ID를 만들거나 SubmitCommand를 반복하지 않는다. 가능한 경우 원래 ID만 조회한다. QUERY 작업은 `GetCommandResult`만 수행한다. OPC 응답도 원래 ID와 비교하며 다른 명령의 결과를 보고하지 않는다. 원본 `CommandNotFound`도 그대로 보고하며 임의로 FAILED·EXPIRED·SUCCEEDED로 해석하지 않는다.

API는 SUBMIT을 반환하기 전에 PostgreSQL에서 결과 불명 예약을 commit해야 하며, 그 명령의 다음 claim은 QUERY만 반환해야 한다. claim 응답 유실·게이트웨이 종료·취소 후에도 이 규칙을 유지해야 한다. 이 모듈은 별도 명령 원장을 만들지 않으므로 프로세스 재시작을 포함한 중복 제출 방지는 API의 원자적 예약 계약에 의존한다. QUERY용 HTTP 재시도는 호출자가 다음 주기에 수행할 수 있지만 기존 SUBMIT을 다시 실행하는 로직을 추가하면 안 된다.

취소는 호출자에게 전파한다. 접수·결과를 만들어 보고하거나 취소된 SubmitCommand를 반복하지 않는다. 소유자는 OPC 연결과 transport 수명을 관리한다. 게이트웨이는 이 경로에서도 앱 B SQLite나 API PostgreSQL에 직접 접근하지 않는다.

## 반환 상태와 검증

`CommandWorkResult`는 `state`, `command_id`, `submission_reported`, `result_reported`를 제공한다. `idle`은 작업 없음, `blocked`는 claim 전 연결/health 문제, `deferred`는 claim 후 제출 보류, `uncertain`은 유효한 조회·보고 확인 불가다. `reported`는 원본 결과의 API 원장 보고가 확인되었다는 뜻이며 PLC SUCCEEDED 판정이나 사용자 운전 성공을 의미하지 않는다.

```sh
.venv/bin/pytest -q tests/unit/test_gateway_commands.py
```

이 시험은 OPC·HTTP를 주입한 **단위 시험**이다. 신선한 health 확인, 원래 ID의 한 번 제출과 후속 QUERY, TTL 경과, 응답/보고 유실, 취소, 다른 명령 응답 거부, 엄격한 DTO·ACK, 응답 크기 제한과 자동 HTTP 재시도 부재를 검증한다. 실제 OPC UA·HTTP socket·PostgreSQL 통합 통과를 주장하지 않는다.
