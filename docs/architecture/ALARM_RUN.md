# 앱 A 알람 규칙 등록과 명시적 런타임

알람 처리는 기본 비활성이다. 이 단위는 앱 A PostgreSQL의 Sealer 온도 편차·압력 부족 및 논리 Line PLC 고장 규칙을 실제 사용자 권한으로 등록하고, 내부 batch 수집 서버에 이미 등록한 규칙의 consumer를 명시적으로 연결한다. 앱 B DB를 읽거나 제어 명령을 실행하지 않는다.

## 준비와 실제 관리자 로그인

저장소 루트에서 Python 환경을 준비하고 비밀 관리 도구로 `HMI_DATABASE_DSN`을 환경에 주입한다. 비밀번호가 포함된 DSN을 명령 인자에 넣지 않는다. 운영자가 명시적으로 migration·기준 정보·source 등록을 완료해야 한다. 자동 migration·기본 계정·source·규칙은 만들지 않는다.

```sh
.venv/bin/python -m alembic -c alembic.ini upgrade 0014_mixed_alarm_runtime
```

[API_RUN.md](API_RUN.md)의 절차로 `equipment_admin` 계정을 등록하고 최초 비밀번호 변경을 완료한다. `configure-alarm-rules`는 실제 challenge/login과 live `equipment_manage` 권한 트랜잭션을 사용한다. viewer·operator·production_manager 및 최초 비밀번호를 바꾸지 않은 계정은 거부한다. CLI가 임의 Principal을 만들거나 비밀번호를 바꾸지 않는다. 임시 세션은 성공·실패 모두 finally에서 revoke한다.

현재 관리자 비밀번호는 실행 사용자 소유의 owner-only 일반 파일로 준비한다. 기존 보호 파일 reader를 사용하므로 symlink·FIFO·다른 소유자·그룹/기타 권한·범위를 벗어난 파일은 거부한다. 값은 CLI 인자·JSON 설정·출력에 넣지 않는다. 아래의 origin은 관리자가 사용하는 정확한 인증 origin이다.

## 규칙 파일과 최초 등록

`alarm-rules.json` 예시(식별자는 실제 등록한 source와 일치시킨다):

```json
{
  "scope": {
    "line_id": "packaging-line-1", "source_id": "configured-source",
    "stream_id": "configured-stream", "asset_id": "Sealer"
  },
  "gateway_id": "configured-gateway",
  "capture_generation": "1",
  "operations": [{
    "operation_id": "install-pressure-v1",
    "expected_version": null,
    "config": {
      "kind": "PRESSURE_LOW",
      "rule": {"rule_id": "pressure", "version": "p1", "severity": "HIGH"},
      "low_bar": 4.0, "return_bar": 4.5,
      "activation_seconds": 3.0, "return_seconds": 3.0,
      "max_gap_seconds": 3.0
    }
  }]
}
```

```sh
.venv/bin/python -m apps.api configure-alarm-rules \
  --login equipment.admin --origin http://127.0.0.1:8002 \
  --password-file .local/service/app-a-current-password \
  --config-file .local/service/alarm-rules.json
```

`expected_version`는 필수다. 명시적 null은 최초 설치이며 문자열은 그 원래 버전에 대한 교체다. 한 파일은 1~2개 규칙과 각기 다른 operation ID·rule ID·analog 종류만 허용한다. 온도 규칙의 kind는 `TEMPERATURE_DEVIATION`이며 `setpoint_c`를 명시한다. 규칙 모델의 유한 수치·양의 dwell·hysteresis 계약과 추가 필드 거부를 그대로 적용한다. 설정 파일은 64KiB 이하 일반 파일이며 symlink/FIFO는 허용하지 않는다.

전체 파일을 하나의 권한 트랜잭션으로 처리한다. 잠금은 schema/사용자/세션 다음 gateway→line/source→현재 source의 영구 frontier 전체와 요청 채널의 정렬 union→전체 정렬 input ID이다. 일부 작업이 실패하면 규칙·감사·영수증 모두 rollback한다. source/gateway/generation 불일치나 미해결 source도 거부한다.

같은 operation ID를 재사용할 때 원래 actor·scope·전체 규칙 설정·expected version이 정확히 같아야 저장한 원래 시각을 재사용한다. 이후 규칙이 바뀌어도 최초 설치의 정확한 재요청이 head를 되돌리지 않는다. 다른 사용자·설정·버전에 같은 ID를 쓰면 실패한다. 규칙 교체는 새 operation ID, 현재 expected version, 새 rule version을 명시한다. 런타임의 규칙을 자동으로 바꾸지 않는다.

## 별도 런타임 파일과 내부 서버

기존 `alarm-runtime.json`의 `AlarmConsumerConfig` 형식은 계속 지원한다. provisioning 파일의 `scope`, `gateway_id`, `capture_generation`과 설치한 `config`를 `rules` 배열로 옮기고, `operations`는 넣지 않는다. 예시:

```json
{
  "scope": {
    "line_id": "packaging-line-1", "source_id": "configured-source",
    "stream_id": "configured-stream", "asset_id": "Sealer"
  },
  "gateway_id": "configured-gateway",
  "capture_generation": "1",
  "rules": [{
    "kind": "PRESSURE_LOW",
    "rule": {"rule_id": "pressure", "version": "p1", "severity": "HIGH"},
    "low_bar": 4.0, "return_bar": 4.5,
    "activation_seconds": 3.0, "return_seconds": 3.0,
    "max_gap_seconds": 3.0
  }],
  "clock_policy": null
}
```

clock_policy null은 시각을 검증했다는 뜻이 아니다. 알람 시간 증거는 UNKNOWN으로 유지하며 정상·확정 dwell로 대체하지 않는다. 유효한 시각 판정을 사용하려면 운영자가 실제 배포 보장을 확인한 후 다음 **모든** 필드를 명시한다. 아래는 형식이며 실제 보장 없이 값을 복사하지 않는다.

| 필드 | 명시 값/의미 |
|---|---|
| `policy_id`, `policy_version`, `reference_id` | 운영자가 부여한 보장·참조 식별자 |
| `method` | `CONFIGURED_API_REFERENCE` |
| `guarantee_kind` | `SAME_HOST_SHARED_KERNEL_UTC` |
| `guarantee_basis` | 실제 동일 호스트·공유 커널 UTC 배포 보장 근거 |
| `gateway_id`, `source_id`, `stream_id`, `capture_generation` | 런타임과 정확히 같은 범위 |
| `clock_epoch` | 현재 gateway의 원래 capture epoch |
| `valid_from_api_utc`, `valid_until_api_utc` | 시간대가 있는 유효 구간 |
| `offset_lower_seconds`, `offset_upper_seconds` | API UTC − gateway UTC의 명시적 유한 하한·상한 |

정책은 동일 호스트 시계 보장에 대한 **운영자 선언**이며 네트워크 측정·원격 시계 검증이 아니다. offset의 기본값이나 자동 생성 경계는 없다. 원래 capture read bracket을 별도로 더해 보수적으로 판단한다. 다른 호스트·알 수 없는 epoch·유효기간 만료·시각/품질 불명은 검증된 시간으로 처리하지 않는다.

인증된 `GET /internal/v1/alarm-clock-context`는 credential의 정확한 범위와 마지막 원래 capture의 epoch·monotonic 값·read span·capture ID/hash를 보여준다. 응답 `clock_assessment`는 `NOT_VERIFIED`다. 이 조회나 전달 지연으로 clock bounds를 만들어서는 안 된다. capture가 없으면 latest_capture는 null이다. discovery는 source 범위를 바꾸는 query 인자를 받지 않는다.

아래 service token 파일·만료 시각은 [M2_RUN.md](M2_RUN.md)의 별도 서비스 credential 절차로 준비한다. 실제 미래 UTC 만료 시각을 환경 변수로 지정한다. 서버는 loopback만 사용하고 forwarded headers를 신뢰하지 않는다.

```sh
.venv/bin/python -m apps.api serve \
  --host 127.0.0.1 --port 8000 \
  --source-id configured-source --stream-id configured-stream \
  --gateway-id configured-gateway --capture-generation 1 \
  --key-id configured-service --token-file .local/service/intake-token \
  --expires-at "$SERVICE_EXPIRES_AT" \
  --alarm-config-file .local/service/alarm-runtime.json
```

CLI 범위와 config가 다르거나 설치한 규칙 head와 다르면 listener를 열기 전에 실패한다. 기존/혼합 JSON 모두 새 `AlarmCoordinator`를 사용한다. 명시적 0014 head가 필요하며, coordinator.start의 source fence·RESTART 기록을 PostgreSQL에 commit한 뒤 explicit consumer를 수집 서비스에 연결하고 HTTP를 제공한다. `--alarm-config-file`을 생략하면 기존 수집 서버와 같이 알람 consumer가 비활성이다. 서비스 credential은 사람의 규칙 관리 권한을 대신하지 않는다.

규칙 교체 후 기존 consumer는 오래된 설정으로 새 capture를 확정하지 않는다. 불일치 요청은 ingestion·alarm 쓰기를 함께 rollback한다. 새 head와 동일한 runtime 파일로 서버를 명시적으로 재시작해야 한다. 재시작은 continuity를 끊고 fence를 새로 설정하며 과거 capture를 현재 dwell로 재활용하지 않는다. SIGTERM/SIGINT는 기존 HTTP graceful 종료와 DB engine 정리를 사용한다.

## 혼합 Line·Sealer 파일

기존 Sealer 전용 규칙/실행 파일은 형식을 바꾸지 않아도 된다. 새 혼합 규칙 파일은
`kind: MIXED_ALARM_RULES`와 공통 line/source/stream/gateway/generation을 명시한다.
각 operation에는 대상 `asset_id`, 원래 `operation_id`, 필수 `expected_version`,
전체 `config`를 넣는다. `PLC_FAULT` 대상은 논리 `Line`이고 두 analog 종류는
`Sealer`다. 한 파일은 종류별 최대 하나, 전체 1~3개 operation을 허용한다.
operation ID와 채널 식별자도 중복할 수 없다. 예시:

```json
{
  "kind": "MIXED_ALARM_RULES",
  "line_id": "packaging-line-1", "source_id": "configured-source",
  "stream_id": "configured-stream", "gateway_id": "configured-gateway",
  "capture_generation": "1",
  "operations": [{
    "asset_id": "Line", "operation_id": "install-line-fault-v1",
    "expected_version": null,
    "config": {
      "kind": "PLC_FAULT",
      "rule": {"rule_id": "line-fault", "version": "f1", "severity": "HIGH"}
    }
  }]
}
```

동일한 `configure-alarm-rules` 명령으로 설치한다. 위 operation 배열에 Sealer
규칙을 함께 넣을 수 있다. 혼합 등록은 0014 migration을 명시적으로 요구한다.
실제 관리자 인증, 초기 비밀번호 변경 제한, 임시 세션 revoke, 원래 actor/시각
재요청 비교, 전체 트랜잭션 rollback은 기존 형식과 같다. 처리 context·event proof·
Principal·clock assessment를 설정 JSON으로 주입할 수 없다.

혼합 실행 파일은 같은 공통 범위와 `kind: MIXED_ALARM_RUNTIME`, `channels`를 쓴다.
등록용 operation ID/expected version 대신 설치한 전체 config를 그대로 넣는다:

```json
{
  "kind": "MIXED_ALARM_RUNTIME",
  "line_id": "packaging-line-1", "source_id": "configured-source",
  "stream_id": "configured-stream", "gateway_id": "configured-gateway",
  "capture_generation": "1",
  "channels": [{
    "asset_id": "Line",
    "config": {
      "kind": "PLC_FAULT",
      "rule": {"rule_id": "line-fault", "version": "f1", "severity": "HIGH"}
    }
  }],
  "clock_policy": null
}
```

이 파일도 같은 `serve --alarm-config-file` 옵션을 사용한다. source의 영구
frontier에 이미 속한 채널은 `requires_restart` 여부와 관계없이 모두 포함해야
한다. 예를 들어 기존 Sealer frontier가 있으면 위 Line-only 예시에 해당 Sealer
채널도 추가해야 한다. 생략은 시작 전에 거절하며 자동 비활성화·삭제·복귀는 없다.
하나의 startup epoch와 capture fence를 전체 채널에 원자적으로 설정한다.
원래 JSON 전체의 hash를 사용하므로 설치 규칙뿐 아니라 선언한 clock policy도
실행기 신원에 포함된다. 과거 binary는 0014에서 실행할 수 없으며, 하위 호환은
새 binary가 기존 JSON을 읽는다는 의미다.

직접 조립하는 App A 실행기의 공개 helper는
`configure_alarm_rules(auth, login=..., password=..., origin=..., config=...)`와
`start_alarm_runtime(engine, config, args=...)`다. 후자는 CLI와 동일한 정확한
source binding을 검증하고 startup 트랜잭션 commit 후 coordinator를 반환한다.
등록/시작 어디에서도 사용자·규칙·source·migration·시계 경계를 자동 생성하지 않는다.
알람으로 RESET·END·START를 제출하지 않는다.

## 검증

```sh
.venv/bin/ruff check apps/api/alarm_runtime.py apps/api/__main__.py \
  tests/integration/test_alarm_runtime.py
.venv/bin/pytest -q tests/integration/test_alarm_runtime.py
```

시험은 실제 PostgreSQL과 별도 CLI 프로세스에서 사용자 로그인·권한·초기 변경 제한·보호 파일·원래 시각 replay·교체·실제 HTTP 시작/수집·규칙 교체 후 원자적 거부·정상 종료를 확인한다. 이 실행기 단위만으로 실제 OPC 설비의 알람 dwell 수락을 주장하지 않는다.
