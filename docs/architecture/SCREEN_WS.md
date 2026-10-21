# 앱 A 인증 화면 WebSocket 첫 단위

`apps/api/screen_ws.py`는 PostgreSQL 조회를 기존 v1 화면 계약 및 원자적 application envelope로 전달한다. `/ws/v1/snapshot`은 기존 flat `ScreenMessage`를 유지하고 `/ws/v1/screen`은 화면과 health/evidence를 한 frame에 담는다. 명령은 기존 HTTP 경로를 사용한다. 이 backend 단위는 알람·MES stream, OPC 전송 성능이나 DEV-13 전체 완료를 뜻하지 않는다.

## 구성·인증

`mount_screen_ws(app,auth,*,origin,allow_insecure_loopback=False,clock=UTCnow,config=None,session_cookie="hmi_session")`가 `/ws/v1/snapshot`과 `/ws/v1/screen`을 같은 인증·subscription·producer·writer 경로에 연결한다. origin은 경로 없는 명시적 HTTPS origin이며 HTTP는 별도로 허용한 loopback 개발에서만 사용한다. 실제 WS의 scheme/단일 Host와 정확한 Origin을 검사한다. forwarded header나 query string을 인증에 사용하지 않으며 query 자체를 거절한다.

handshake에서 HttpOnly cookie의 live session·변경된 초기 암호·read 역할을 확인하고 accept한다. 이후 기본 3초 안에 다음 JSON 한 개를 받는다.

```text
{line_id, source_id, stream_id, csrf_token}
```

CSRF는 로그인 응답에서 받은 브라우저 메모리 값이며 URL·subprotocol·localStorage에 넣지 않는다. 이 첫 요청은 정확한 Origin과 세션에 결합된 CSRF를 검증하고, 실제 등록된 line/source/stream을 확인한 뒤 첫 화면을 보낸다. App A의 기존 네 역할 모두 read가 가능하다. 현재 존재하지 않는 사용자별 line ACL을 새로 가정하지 않는다. gateway 서비스 token이나 앱 B 세션은 사용할 수 없다.

`UserAuth.authorized_transaction`에 `require_csrf=False`, `refresh_idle=True` 옵션을 추가했다. 기존 호출은 그대로 동작한다. read에 `require_csrf=True`를 주면 동일한 Origin/CSRF 검사를 수행한다. mutation은 이 옵션이 false여도 항상 검사한다. `refresh_idle=False`는 만료·active·auth_version·역할·초기 암호 검사를 유지하면서 idle 갱신만 생략한다. subscription과 자동 재접속, producer/writer의 수동 타이머 확인은 모두 idle을 갱신하지 않는다. 로그인·암호 변경·명시적 사용자 POST 같은 실제 활동이 세션을 갱신한다. 열린 WS 자체가 idle 30분·절대 8시간 정책을 무력화하지 않는다.

## 순서·품질

기존 `ScreenMessage` schema를 변경하지 않는다. 첫 메시지는 새 sync_id와 revision `"0"`의 `SNAPSHOT`이다. 이후 기본 1초마다 같은 sync_id의 `UPDATE`를 보내며 base_revision의 정확한 다음 revision을 사용한다. UPDATE는 **전체 LineView 교체**이고 sparse patch가 아니다. DB 사건 순번·capture 순번을 화면 revision으로 재사용하지 않는다. UInt64는 기존 serializer의 십진 문자열로 보낸다.

단일 producer가 초기 projection과 이후 순차 조회를 담당한다. snapshot 뒤 첫 조회 사이의 커밋도 다음 전체 교체에서 조회하므로 이벤트 구독 attachment 틈이 없다. 이는 최신 화면 표본이며 모든 중간 생산 사건의 전달을 약속하는 event stream은 아니다. 원본 사건·커밋 ACK·정산은 기존 경로가 담당한다.

각 projection은 live read 권한 확인과 같은 짧은 DB transaction에서 생성한다. line shared lock을 먼저 잡아 ingest의 line→source 잠금과 순서를 맞추고 여러 조회가 같은 커밋 상태를 보도록 한다. 반환 후 transaction을 끝내며 network await 동안 사용자·세션·line 잠금을 유지하지 않는다. writer는 queued frame을 보내기 직전에도 live 권한을 다시 검사한다. 이미 승인되어 OS에 전달 중인 frame을 회수할 수는 없지만, 회수된 세션의 뒤따르는 queued frame을 계속 승인하지 않는다.

재접속은 항상 새 sync_id의 전체 snapshot부터 시작한다. 이전 연결 replay나 client resume 요청은 없다. client는 `validate_screen_update`와 같은 규칙으로 gap·중복·역순·다른 line/sync를 거절하고 새 연결로 재동기화해야 한다. subscription 이후 client input은 허용하지 않는다. 명령 전송은 기존 HTTP 경로만 사용한다.

값·원본 StatusCode·acquired_at은 그대로 보존하고 `read_snapshot`의 보수적 derived state와 `control_restricted=true`를 유지한다. 오래되거나 미래인 관측을 정상 값/0으로 바꾸지 않는다. `emitted_at`은 화면 생성 시각이며 새로운 수집이나 생존 신호를 뜻하지 않는다. 기존 flat v1 LineView에는 capture/receipt evidence와 상세 heartbeat health가 없다. `/ws/v1/screen`에서는 다음 application envelope를 사용한다. `screen` 내부의 공유 v1 계약은 변경하지 않는다.

```text
ScreenDataFrame {
  schema_version: "1.0",
  scope: {line_id, source_id, stream_id},
  screen: ScreenSnapshot | ScreenUpdate,
  health: SnapshotHealth,
  evidence: [{tag_id, batch_id, capture_generation, capture_seq,
              captured_at, stored_at, fresh}],
  missing_tag_ids: [tag_id, ...]
}
ScreenResetFrame {
  schema_version: "1.0",
  scope: {line_id, source_id, stream_id},
  screen: ScreenResyncRequired
}
```

각 data frame의 화면·health·evidence·missing_tag_ids는 line `FOR SHARE` 아래 **한 `SnapshotRead`**에서 만들어진다. 별도 REST 요청의 시점이나 이전 frame metadata를 섞지 않는다. evidence는 관측 tag마다 정확히 하나이며 missing tag는 중복 없이 알려진 tag로서 관측과 겹치지 않는다. scope의 line은 view와 같고 source/stream은 원본 조회와 같다. RESET은 health/evidence/missing_tag_ids를 포함하지 않으며 client는 이전 projection을 무효화해야 한다. freshness·제어 gate는 이 원자적 metadata를 사용하며 `emitted_at`을 수집 시각으로 사용하지 않는다.

`apps/api/screen_frames.py`가 별도 DTO와 `contracts/schemas/app-screen.schema.json` export를 정의한다. 화면 revision과 evidence capture_generation/capture_seq를 포함한 모든 UInt64는 JSON Schema에서도 canonical decimal **string**이다. subscription의 CSRF·cookie·secret은 frame에 포함되지 않는다.

## 유한 자원·종료

기본 subscription outbound queue는 최대 **32개**다. 크기 제한을 통과한 JSON만 넣고, gap을 숨기는 drop/coalescing을 하지 않는다. flat outbound frame은 최대 65,536바이트, application envelope는 최대 **131,072바이트(128 KiB)**, 첫 inbound frame은 4,096바이트다. Pydantic 직렬화 후 실제 UTF-8 byte 수를 enqueue 전에 검사하며 설정으로 hard bound를 키울 수 없다. 작은 제한은 설정할 수 있다. send 기본 timeout은 2초다. queue 초과 시 producer/writer를 중단하고 전송이 가능하면 `RESYNC_REQUIRED/BACKPRESSURE`를 보낸 뒤 1013으로 닫는다. send timeout이나 전송 불가 때는 바로 닫는다. revision 소진도 새 snapshot이 필요하다. disconnect/실패/취소 시 모든 연결 task를 정리한다.

부모 Uvicorn 구성에서도 `ws_max_size=4096`, `ws_max_queue=1`처럼 frame/입력 queue를 제한해야 한다. application 검사 시점에는 protocol server가 frame을 이미 할당했을 수 있다. HTTP middleware는 WebSocket 인증이나 프레임 상한을 대신하지 않는다.

Origin/세션/초기 암호 오류는 accept 전에 거절한다. accept 후 CSRF·scope·정책/만료/회수는 1008, 과대 frame은 1009, 안전하게 복구할 수 없는 서버 장애는 1011, 압력은 1013이다. DB·validation·cookie·CSRF·요청 원문을 close reason이나 로그에 출력하지 않는다.

## 검증

```sh
.venv/bin/ruff check apps/api/screen_ws.py apps/api/screen_frames.py tests/integration/test_screen_ws.py tests/integration/test_app_screen_ws.py tests/unit/test_screen_ws_flow.py tests/unit/test_app_screen_frames.py
.venv/bin/pytest -q tests/integration/test_screen_ws.py tests/integration/test_app_screen_ws.py tests/unit/test_screen_ws_flow.py tests/unit/test_app_screen_frames.py
```

실제 일회용 PostgreSQL과 WebSocket socket 시험은 handshake/CSRF/scope, snapshot-first·전체 교체·gap·새 sync, 정확한 UInt64, 관측 aging·미래 시계 catch-up, 로그아웃·역할/암호 변경·비활성·idle/절대 만료 종료를 확인한다. 유한 queue·frame·revision 소진은 결정적인 단위 시험으로 확인한다. 느린 writer는 ASGI send만 정지시키고 실제 PostgreSQL을 사용하는 시험으로 queue/send timeout·재동기화/종료와 network 대기 중 독립 DB 연결의 사용자/line 잠금 획득을 확인한다. 브라우저 consumer·실제 OPC→WS 화면 지연·10명 부하·전체 T06/T12 수락 시험은 별도다.

Envelope 추가 시험은 두 route의 정확한 Origin/Host·CSRF·회수/idle/재접속, flat shape 유지, 실제 PostgreSQL 동시 ingest와 원자적 metadata, aging·UInt64·새 sync, metadata 없는 reset, schema drift 및 48 tag의 escaped/다중 byte payload UTF-8 상한을 확인한다.
