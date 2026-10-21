# 게이트웨이 통신 근거 수집 계약 · 1.1

`contracts/transport.py`의 수집 `BatchBody.schema_version`은 기존 `1.0`과 새 `1.1`을 받는다. 새 버전은 `communication: GatewayCommunication`으로 OPC UA 클라이언트의 실제 단조 시각 기반 읽기·heartbeat 경과시간을 운반한다. 수신·저장·화면·제어 게이트가 이 근거를 연결하는 구현은 별도 계층의 책임이다.

## 전송 필드

```json
{
  "schema_version": "1.1",
  "communication": {
    "state": "GOOD",
    "connected": true,
    "read_age_seconds": 0.0,
    "heartbeat_ages_seconds": {
      "Line.Heartbeat": 0.2,
      "Infeed.LinkHeartbeat": 0.3,
      "Sealer.LinkHeartbeat": 0.4,
      "Outfeed.LinkHeartbeat": 0.5
    }
  }
}
```

위 JSON은 필드 설명용 부분 예시다. 실제 배치에는 기존 배치 ID·capture 위치·수집 시각·sample/event/gap과 전체 원본 본문 해시도 필요하다. `1.1`에서 sample이 하나 이상이면 communication이 필수이며 null을 허용하지 않는다. 사건·gap만 운반하는 배치는 생략할 수 있다. 빈 배치는 계속 거절한다. ACK와 화면 메시지의 버전·필드는 이번 변경으로 바꾸지 않는다.

`1.1`의 모든 sample은 `acquired_at`이 배치의 `captured_at`과 같은 UTC 시각이어야 한다. 통신 근거와 다른 시각의 오래된 관측이나 미래 관측을 현재 capture로 묶지 않는다. `source_timestamp`·`server_timestamp`는 원본 설비 시각이므로 이 동등 조건을 적용하지 않고 그대로 보존한다. 기존 `1.0`의 관측·capture 시각 의미는 변경하지 않는다. 화면에서 여러 배치의 최신 태그를 조합할 때 과거 태그가 남아 있는 것은 별도 최신성 판정 대상이다.

`read_age_seconds`와 네 heartbeat 값은 유한한 0 이상 숫자 또는 미확인 상태를 나타내는 null이다. 숫자 문자열·Boolean·음수·NaN·Infinity는 거절한다. heartbeat 객체는 위 네 논리 태그 이름을 모두 정확히 포함해야 하며 다른 이름을 허용하지 않는다. Python에서는 불변 `HeartbeatAges` 모델로 보관하며 wire 출력에는 위 alias를 사용한다. `GatewayCommunication` 생성 시 `dict(observation.health.heartbeat_ages_seconds)`를 그대로 넘길 수 있다.

경과시간은 게이트웨이의 단조 시계로 관측한 capture 당시 값이다. SourceTimestamp·ServerTimestamp나 API 수신 시각에서 역산하지 않는다. OPC 클라이언트가 최초 읽기·heartbeat를 기다리면서 제공하는 시작 시각 이후의 유한한 경과시간도 원래대로 보존한다. 임의로 null 또는 0을 만들어 대체하지 않는다.

## 상태 일관성

계약은 전달된 상태를 다음 근거와 대조한다. 최대 경과시간은 null을 제외한 읽기·네 heartbeat의 최대값이다.

| 상태 | 허용 근거 |
|---|---|
| GOOD | connected=true, 다섯 경과시간 모두 숫자, 최대값 3초 미만 |
| DELAYED | 알려진 최대값 3초 이상 5초 미만 |
| ALARM | 알려진 최대값 5초 이상 |
| UNKNOWN | 알려진 최대값 3초 미만, 초기 관측 또는 연결·관측 근거가 불완전한 보수적 상태 |

클라이언트는 연결 여부보다 경과시간 임계값을 먼저 평가하므로 disconnected 상태에서도 DELAYED·ALARM이 가능하다. UNKNOWN은 connected=true이고 모든 값이 3초 미만이어도 허용한다. 클라이언트가 아직 유효한 최초 관측을 모두 보았는지는 숫자 fallback만으로 입증할 수 없기 때문이다. 반대로 알려진 경과시간이 3초 이상인데 UNKNOWN으로 표시하거나, 연결이 없는데 GOOD으로 표시하는 모순은 거절한다.

GOOD은 통신 활동만 나타낸다. sample 품질·설비 준비·미정 명령·사건 대조 및 API까지 도착한 뒤의 추가 지연은 별도로 확인해야 한다. capture 당시 GOOD을 현재 제어 허용으로 바로 사용하면 안 된다.

## 1.0 해시 호환성

기존 `1.0`은 communication 필드를 명시적으로 보내는 것을 거절하며 null도 허용하지 않는다. serializer는 `1.0`에서 이 새 필드를 출력하지 않는다. 따라서 기존 원본 본문의 기본값 정규화·UInt64 십진 문자열·UTC 시각·배열 순서·전체 필드 해시 규칙이 그대로 유지되고, 새 null 필드 때문에 재시도 batch ID의 해시가 바뀌지 않는다. `1.1`의 communication은 전체 본문 해시에 포함한다.

`export_transport_schemas()`가 검토 가능한 JSON Schema를 생성한다. 변경된 체크인 파일은 `contracts/schemas/transport-batch.schema.json`이며, 기존 ACK·화면 스키마는 동일하다. wire 스키마는 버전별 communication 필드 허용·필수 여부와 숫자·태그 이름 구조를 검증한다. 상태와 여러 경과시간 사이의 의미적 일관성은 Pydantic 모델 검증에서 수행한다.

## 검증

```sh
.venv/bin/ruff check contracts/transport.py tests/contract/test_transport.py
.venv/bin/pytest -q tests/contract/test_transport.py tests/integration/test_gateway_buffer.py
```

기존 두 1.0 배치 예제의 canonical 본문 bytes와 고정 해시, 1.1 왕복과 해시 결합, 필수 통신 근거, 3초·5초 경계, UNKNOWN 초기 상태, 모순된 상태, 비유한 숫자·잘못된 heartbeat 이름을 검증한다. 버퍼 시험은 기존 영속 배치·ACK·재전송의 회귀를 확인한다. 이 계약 확장 자체로 운영 HMI·원격 제어·전체 수락 시험 완료를 주장하지 않는다.
