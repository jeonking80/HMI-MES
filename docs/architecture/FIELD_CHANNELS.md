# 설비별 필드 발행 시험: component V2

`apps/simulator/field_channels.py`는 App B의 **설비별 OPC 발행 overlay**다.
Infeed·Sealer·Outfeed 중 선택한 설비의 값/품질에 시험 조건을 적용한다.
OPC 연결은 유지하고 물리 공정, PLC 상태·허용·출력, 명령·물품·수량·원본 사건은
변경하지 않는다. READ_FAILURE·LINK_DOWN은 가상 설비 채널의 명시적 실패 출력이다. 실제 OPC
세션 차단이나 실제 필드 프로토콜 드라이버 예외를 구현한 것으로 표시하지 않는다.

| 모드 | 발행 동작 |
| --- | --- |
| NORMAL | 현재 로컬 PLC/공정 투영값과 원래 품질/시각 발행 |
| FROZEN | 마지막 실제 NORMAL 발행의 **해당 설비 전체 Good typed frame**을 보존. 값·StatusCode·SourceTimestamp·LinkHeartbeat를 함께 동결 |
| BAD | 선택 설비의 현재 typed 값을 유지하고 `BadSensorFailure` 원본 상태를 발행 |
| UNCERTAIN | 선택 설비의 현재 typed 값을 유지하고 `UncertainSensorNotAccurate` 원본 상태를 발행 |
| READ_FAILURE | 마지막 실제 NORMAL frame의 값·SourceTimestamp·heartbeat를 보존하고 정확한 `BadCommunicationError` 출력 |
| LINK_DOWN | 같은 원본 frame을 보존하고 정확한 `BadNotConnected` 출력. 실제 OPC 연결은 유지 |

BAD/UNCERTAIN은 값 0이나 null을 합성하지 않는다. 더 나쁜 원본 품질을 낮은
심각도로 올려 표시하지 않는다. FROZEN은 미발행·Bad·Uncertain·이전 설정 버전의
frame에서 설정할 수 없다. 과거 Good cache를 찾아 Bad를 Good으로 세탁하지 않는다.
같은 모드/현재 revision의 반복 설정은 새 revision/감사 변경을 만들지 않는다.

## 원본 시각·최신성·원자성

48개 node ID·declared type·읽기 전용 계약은 고정이다. 실제 `project_tags` 결과를
먼저 만들고 사전의 설비 그룹에만 overlay를 적용한 뒤 새 DataValue map을 한 번에
교체한다. 선택하지 않은 설비와 Line 값/생존 신호는 계속 진행한다. 동기 UA read
callback에는 sleep·대기·의도적인 네트워크 예외를 넣지 않는다.

FROZEN·READ_FAILURE·LINK_DOWN의 SourceTimestamp는 실제 원래 발행 frame의 시각이다. ServerTimestamp는
현재 OPC 서버 발행 시각이며 48개가 같은 경계로 바뀐다. Gateway acquired_at은
실제로 읽은 새 capture 시각이다. SourceTimestamp가 오래되었다는 이유만으로
단절을 추론하지 않는다. 반대로 Good 값이 읽혀도 동결된 LinkHeartbeat는 더 이상
진행하지 않으므로 기존 단조 age 판정이 3초 DELAYED·5초 ALARM으로 진행한다.
이때 OPC 연결과 정상 read age는 살아 있다. 정상 복원 후 새 Good heartbeat가
관측되어야 최신성이 회복된다. UI의 저장 설정 성공은 아직 다음 OPC 발행 성공이 아니다.

## 저장·재시작·감사

SQLite 독립 component version은 2이며 controller runtime V2와
SimulatorStorage `PRAGMA user_version=3`은 바꾸지 않는다.

- V1 DDL과 parser는 `_field_channels_v1.py`의 frozen snapshot이다. V1 parser가
  새 failure mode/필드를 받아들이도록 넓히지 않는다.
- 시작 시 정확한 V1 DDL·모든 감사 연결·현재 head를 검증한 뒤 한 owner transaction에서
  faults의 `state_payload_version`, audit의 `before_payload_version`·`after_payload_version`
  열만 ADD COLUMN한다. CHECK(version=1)이 있는 작은 metadata table만 V2로 교체한다.
- 이관된 payload discriminator는 1이다. 기존 state/audit JSON TEXT는 공백·필드 순서·
  숫자 표기까지 그대로 두며 audit_id/revision/actor/time도 유지한다. 이관 전후 streaming
  byte digest와 행 수를 비교하며 fault revision·가짜 operator audit를 만들지 않는다.
- 새 mutation after는 내부 `payload_version=2` 및 외부 discriminator 2로 저장한다.
  첫 V1→V2 변경의 before_json은 durable V1 head 원문 그대로다. 허용되는 감사 조합은
  1→1, 1→2, 2→2이고 2→1·잘못 표시된 JSON은 거절한다. 과거 V1 연결은 기존 의미
  비교를 유지하며, 새 V2 head와 latest after는 원문도 일치해야 한다.
- `normal_checkpoint`는 failure 전용이다. 완전한 실제 NORMAL frame의 원래 값·품질·
  시각·frame_id·revision을 저장하고 원본 field를 변경하지 않는다. 별도 출력 복사본에
  failure StatusCode를 적용한다. 단일 UA 품질로 원래 품질과 새 실패 원인을 동시에
  표현한다고 주장하지 않으며 콘솔에는 발행 품질과 보존 원본 품질을 구분한다.
- FROZEN·READ_FAILURE·LINK_DOWN 진입은 현재 NORMAL 설정과 같은 revision의 마지막
  실제 NORMAL 발행이 있어야 한다. FROZEN만 전체 Good이 추가로 필요하다. prepare만 한
  frame·재시작 checkpoint·pending NORMAL·다른 시험 모드의 frame은 재사용하지 않는다.
  새 failure는 원래 NORMAL이 Bad/Uncertain이어도 그 원본 품질을 보존한다.
- setter는 관측 메모리를 바꾸지 않는다. config/revision/audit/checkpoint는 한 transaction이다.
  commit/outer rollback에서 모두 복구되고 uncommitted config는 발행하지 않는다.
- 부분/모르는 schema, arbitrary-name 소유 trigger/index, 변경된 constraints/append-only 보호,
  payload/감사 손상·불가능한 전이는 fail closed다. 이관 실패는 V1 schema/증거를 원자적으로
  남기며 기존 corruption latch의 RECOVERY_REQUIRED 유지 정책은 적용될 수 있다.
- startup만 V1 이관을 허용한다. live schema 변경은 정확한 V2 DDL과 전체 이력을 다시
  검증하며 성공 후에만 cache를 확정한다. table 전체 삭제·V1 되돌림을 자동 생성/이관하지 않는다.
- 재시작은 실패 설정·원래 checkpoint를 유지한다. 메모리는 비어 pending이며 저장 frame을
  새 실제 NORMAL 발행으로 등록하지 않는다. 첫 실패 출력 후 pending이 해제된다.
  NORMAL 복원은 현재 acquisition의 값과 raw quality를 쓰며 PLC 자동 재가동은 없다.

손상 거절은 정상 시험 입력과 구분한다. 유효한 overlay는 PLC를 바꾸지 않지만,
저장 손상은 기존 storage의 복구 제한을 적용하는 fail-closed 오류다.

## 조작·조회 경계

실제 CLI는 같은 `FieldChannels` 객체를 OPC publisher와 인증 콘솔에 명시적으로
연결한다. 라이브러리의 기존 constructor/create_app 경로는 선택적 객체를 받는다.

```text
GET  /field-faults
POST /field-faults/{Infeed|Sealer|Outfeed}
     {"mode":"BAD", "expected_revision":"0"}
```

GET은 기존 인증 사용자 조회이고, POST는 기존 simulator_operator·초기 암호 변경·
Origin/CSRF 조건을 거친다. actor·raw tag·value·임의 StatusCode는 body에서 받지 않는다.
정확한 설비만 CAS로 바꾸므로 stale 전체 PLCInputs를 덮어쓰지 않는다.

콘솔은 로컬 PLC 투영과 OPC 시험 발행을 구분하고 저장/실제 발행 모드·revision,
품질과 생존 신호/원래 시각, 적용 대기를 표시한다. 응답 유실/거절 후 자동 재POST를
하지 않으며 적용을 막고 “설정 다시 조회” GET으로 현재 mode/revision을 대조한다.
이 조회는 원래 요청자 receipt 확인을 주장하지 않고 현재 저장/발행 상태를 보여 준다.

## 검증 범위와 실행 명령

```sh
.venv/bin/pytest -q tests/integration/test_simulator_field_channels_v2.py tests/integration/test_simulator_field_channels.py tests/integration/test_simulator_console.py tests/integration/test_simulator_opcua.py tests/integration/test_gateway_opcua.py tests/integration/test_simulator_launcher.py
.venv/bin/python scripts/check-simulator-browser.py --screenshot /tmp/hmi-simulator-field-v2.png
```

V2 영구 시험은 실제 V1 코드로 생성한 SQLite DB를 이관한다. JSON 원문 보존·혼합
payload·모든 이관 실패 seam·실제 COMMIT 거절 rollback·strict V1 parser·live schema/cache
거절과 실제 close/reopen 후 원본 혼합 품질/UInt64MAX checkpoint를 다룬다. 최초 독립
stage의 pure 시험과 이후 통합 검증은 구분하며 stage-only 추출 파일은 저장소에 넣지 않는다.

통합 시험은 실제 암호화 OPC의 48 tag/type/원자 발행, failure mode 2개×원본 품질
Good/Bad/Uncertain, UInt64MAX와 원래 SourceTimestamp/heartbeat, component 재생성 후
pending과 새 발행, NORMAL의 새 acquisition/raw quality 복원을 검증한다. Gateway는
실제 OPC 세션에서 제어된 단조 시계로 3/5초 판정을 확인하며 읽기와 ReadEvents는
계속 성공한다. RUNNING 시험은 각 overlay 동안 65개 명시적 실제 controller tick과
생산 수량 진행을 확인한다. 이는 실제 wall time의 65 ticker 주기 주장과 구분한다.
HTTP 시험은 인증·권한·Origin/CSRF·CAS·actor·passive 결과 조회를 다룬다.

Chromium 명령은 실제 CLI/100ms ticker에서 기존 온도 편차와 FROZEN 시험을 유지하며
새 실패 모드마다 50회 이상 실제 ticker 동안 RUNNING·원본 frame 보존·그룹 격리를
확인한다. READ_FAILURE 응답은 commit 뒤 유실시키고 자동 재POST 금지·GET 대조를
검증한다. NORMAL 해제는 새 원본 시각/Good 품질이어야 하며 후속 PAUSE/END/원래 ID
재조회/정산/다음 작업도 유지한다. 실행 결과는 해당 실행 로그로 확인한다.

지연 queue, 실제 필드 드라이버, 실제 OPC 세션 차단/메서드 응답 지연은 포함하지 않는다.
App A의 영속 통신 알람·전체 T06·8시간 부하/복구도 이 단위 완료 범위가 아니다.
