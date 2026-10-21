# 태그 계약 v1 (DEV-02a)

[tags.v1.json](tags.v1.json)은 시스템 기획서 [§08·§18](../../docs/HMI_MES_시스템_기획서_v1_0.md)와 개발계획서 [§4·§5](../../docs/HMI_MES_개발계획서_v1_0.md)의 48개 읽기 태그를 정의한다. Line 18, Infeed 8, Sealer 12, Outfeed 10개의 이름과 OPC UA 자료형을 유지한다. 이 산출물은 DEV-02 중 태그 계약에 한정하며 명령·사건 계약이나 OPC 서버 구현 완료를 의미하지 않는다.

## 주소와 버전

고정 namespace URI는 `urn:hmi-mes:packaging-line:tags:v1`이다. 각 `node_id`는 문자열 식별자이며 `Line.State`처럼 `그룹.태그명`을 사용한다. `logical_name`은 그룹 안에서 반복되는 State 등의 이름을 전역에서 구분한다. URI와 문자열 식별자의 조합이 고정 주소다. OPC UA namespace index는 계약에 저장하지 않는다.

서버는 이 URI를 등록하고 반환받은 index로 노드를 만든다. 클라이언트는 연결할 때마다 서버 NamespaceArray에서 URI를 찾아 index를 구한다. asyncua에서는 다음과 같이 사용한다. 아래 코드는 적용 방법이며 아직 통신 구현·검증 산출물은 아니다.

```python
namespace_index = await client.get_namespace_index(contract["namespace_uri"])
node_id = ua.NodeId(tag["node_id"]["identifier"], namespace_index)
node = client.get_node(node_id)
```

서버 재시작·등록 순서에 따라 index가 달라져도 URI와 identifier는 바뀌지 않는다. URI가 없는 서버는 연결 계약 오류로 처리하며 특정 index로 대체하지 않는다. 기존 식별자·자료형·단위·열거값 의미를 바꾸려면 계약의 주 버전과 URI를 바꾼다.

## v1에서 결정한 세부값

원문은 상태 이름과 자료형을 정의하고 OPC 주소·열거값의 정수 표현은 정하지 않았다. 아래 수치와 영문 판정 이름은 이번 계약의 설계 결정이다.

| 열거형 | Int32 값 |
|---|---|
| EquipmentState | 0 STOPPED, 1 READY, 2 RUNNING, 3 PAUSED, 4 STOPPING, 5 FAULT, 6 ESTOP, 7 RECOVERY_REQUIRED |
| ControlMode | 0 LOCAL, 1 REMOTE |
| OperationMode | 0 MANUAL, 1 AUTO |
| CheckResult | 0 UNDECIDED(미판정), 1 GOOD(양품), 2 REJECT(불량) |

네 그룹의 State는 v1에서 같은 EquipmentState를 사용한다. UNKNOWN은 운영 앱의 표시 상태이므로 PLC 열거값에 넣지 않는다. 알 수 없는 정수를 임의의 정상 상태로 치환하지 않는다. OperationMode의 MANUAL/AUTO 선택 역시 v1 설계 결정이며 제어 허용 조건은 별도 PLC·명령 계약에서 정의한다.

단위는 원문의 `A`, `m/min`, `℃`, `bar`, 수량의 `개`를 보존한다. 원문에 단위가 없는 값은 JSON null이다. Outfeed.GoodCounter·RejectCounter에도 수량 의미에 따라 `개`를 명시한다. UInt64 카운터·버전·순번의 정확도를 유지해야 하므로 이후 JavaScript 경계를 통과할 때 부동소수 Number로 변환하지 않는다. [프로토콜](../PROTOCOL.md)과 [전송 계약](../TRANSPORT.md)에 따라 JSON에서는 정규 십진 문자열로 직렬화한다.

모든 태그는 `read_only`다. TemperatureSP·ControlMode·OperationMode·작업 ID도 적용된 값의 피드백이며 태그 쓰기로 제어하지 않는다. 명령은 별도 OPC 메서드를 사용한다. 각 태그는 기본 1000ms 묶음 읽기에 참여한다. `analog_history`는 전류 3개, 속도 3개, 온도 PV/SP 2개, 공압 1개만 true다. false인 상태·수량·생존 신호에도 최신값·변화 사건 보관이 필요하며 이 플래그는 해당 보관을 끄는 설정이 아니다.

## 품질과 검증

공통 quality 항목은 원본 DataValue의 값·StatusCode·SourceTimestamp·ServerTimestamp와 수집·저장 시각 보존을 명시한다. Bad·Uncertain 값은 정상값 계산에서 제외한다. 3초 지연·5초 통신 알람 기준은 정상 읽기 또는 생존 신호 관측에 적용하며 오래된 SourceTimestamp 하나로 통신 장애를 판단하지 않는다. quality는 정책 메타데이터이며 데이터 수집·최신성 판단 코드는 후속 작업이다.

`python contracts/tags/validate.py`는 외부 패키지 없이 JSON 구조·주소·개수·자료형·열거형·기본 수집 정책을 검증한다. `python -m pytest tests/contract/test_tags.py`는 원문 태그 목록·자료형·단위와 대조하고, 주소와 읽기 전용 정책·아날로그 선정·잘못된 계약 거절을 확인한다. 해당 시험은 실제 OPC 접근 권한이나 통신 수집을 검증하지 않는다.
