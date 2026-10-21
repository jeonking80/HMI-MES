# DELAY D3 stage: queue와 실제 OPC 발행 경계

이 단위는 승인된 D2 저장 코어를 바꾸지 않고 D1 engine의 실제 mode pass-through, 새
`field_publication.py` adapter, OPC-local 참조 채택 closure를 연결한다. 독립 stage에서만
작업하며 repo의 field V2·D1 코드는 동결되어 있다. UI/HTTP/CLI wiring은 별도 소유 단위다.
D2 저장 통과와 실제 DELAY 발행 지원, 그리고 사용자 콘솔 완료를 구분한다.

## 실제 mode와 revision

engine이 받는 mode는 NORMAL/FROZEN/BAD/UNCERTAIN/READ_FAILURE/LINK_DOWN/DELAY다.
다른 시험 mode를 가짜 NORMAL로 바꾸지 않는다. adapter는 기존 store와 같은 overlay
선택 규칙을 적용한 typed fields를 비-DELAY mode에 pass-through한다. 새 source raw quality가
나쁜 FROZEN의 기존 fallback, BAD/UNCERTAIN severity 결합, 실패 mode의 정확한 코드와
원본 checkpoint 값/시각 보존을 유지한다.

발행기가 관측하지 못한 여러 설정 변경으로 revision이 뛰어도 새 generation으로 queue를
flush한다. 실제 NORMAL 발행을 채택한 경우에만 last_normal_publication을 갱신한다. 다른
mode는 그 증거를 지운다. DELAY 진입은 store와 engine 모두 이전 실제 NORMAL과 정확한
checkpoint를 요구한다. prepare만 한 NORMAL, 과거 checkpoint, 다른 mode 또는 pending
NORMAL을 현재 실제 발행으로 승격하지 않는다.

## preparation과 원자 채택

adapter.prepare는 committed store state를 읽고 각 field/model/runtime view를 검증한다.
완전한 48개 source projection을 받고 engine plan 및 immutable fields/frames/runtime tuple을
준비한다. 실제 UA Value/StatusCode/SourceTimestamp/ServerTimestamp 인코딩은 OPC publisher가
모두 끝낸다. 이 사이 await는 없다. 준비 결과를 버려도 queue·저장 실제발행 proof·UA pointer는
바뀌지 않는다. 단, 서버 시계 관측은 아래의 독립 안전 상태를 유지한다.

publish는 plan owner와 committed policy fingerprint를 재확인한다. engine은 base-version/
callback의 동기 조건을 검사하고 다음 queue/frontier/version을 미리 계산한다. 그 후 OPC-local
closure가 아래 세 참조만 바꾼다.

```python
self._snapshot = prepared_snapshot
store._published = prepared_frames
store._field_publication_runtime = prepared_runtime_tuple
```

closure에는 allocation/model validation/DB/await/기존 store.published 호출이 없다. store runtime
slot은 adapter 생성 시 먼저 준비한다. engine은 closure 직후 이미 계산한 참조를 채택한다.
외부 adapter에 임의 server/store __dict__를 전달하는 API는 없다. 실제 OPC owner의 좁은
신뢰 closure만 pointer와 이미 검증된 store observation을 함께 채택한다.

동일 후보의 중복, 오래된 engine frontier, config CAS 변경, 다른 adapter plan은 pointer를
바꾸기 전에 거절한다. 47번째 DataValue 인코딩 실패 시험은 앞의 46개가 만들어진 뒤에도
기존 UA map·실제발행 proof·queue가 모두 그대로임을 검증한다. callback 자체는 두/세 참조
assignment만 해야 하며 pointer 교체 뒤 예외를 던지는 임의 callback을 허용하는 계약이 아니다.

## transport lifecycle

같은 OPC server 인스턴스의 stop/start도 새 publisher generation이다. 새 start의
already-running 검사를 통과한 뒤 첫 await 전에, stop cleanup에서, 그리고 initial map 뒤
listener.start가 실패한 cleanup에서 adapter queue·store의 실제 NORMAL proof·runtime view를
retire한다. runtime=None/published 비움은 미발행·확인 불가다. durable 설정/checkpoint는
수정하지 않는다. 새 DELAY generation은 원래 checkpoint 값을 Waiting으로 표시하고 새
read/deadline 후에만 완료한다. 이전 delivered/pending/deadline은 재사용하지 않는다.

일반 FieldPublication 생성자는 기존 live UA map의 관측을 무조건 지우지 않는다. 단순 factory나
prepare/인코딩 실패가 기존 live publication 관측을 바꾸지 않도록 lifecycle reset과 분리한다.
실제 TLS stop/start 및 초기 publish 후 listener 시작 실패/재시도 시험으로 이 경계를 확인한다.

## 서버 clock와 deadline 의미

clock은 서버가 소유하며 기본 time.monotonic을 사용한다. duration 외의 clock/deadline/
generation을 HTTP 요청으로 받지 않는다. adapter의 clock observer는 queue의 committed
frontier와 분리되어 있어, 폐기된 prepare 이후의 실제 관측 역행도 탐지한다. finite/nonnegative/
nondecreasing 위반 또는 clock 예외는 latch하며 그 이후 신규 prepare와 이미 준비한 plan의
publish 모두 거절한다. 새 adapter/generation 없이 자동 정상화하지 않는다. 기존 launcher의
publication task supervision이 실패를 처리해야 한다.

지정 duration은 deadline이다. **deadline 이후 첫 publish 호출에서 완료를 관측**한다.
현재 CLI cycle 목표는 100ms지만 scheduler/인코딩/DB 처리 시간이 있으므로 100ms exact wall
clock 출력이나 duration과 동일한 완료 지연을 보장하지 않는다. 긴 정지 후 catch-up burst를
만들지 않고 당시 현재 raw frame으로 다음 read 하나를 시작한다.

가상 read 시작의 원본 frame 값·SourceTimestamp·HB/raw quality를 그대로 전달한다.
ServerTimestamp만 실제 새 발행 경계의 UTC다. 한 frame 재전달은 새 source observation이
아니며 내부 acquisition_frame_id도 유지된다. 반대로 gateway는 매 capture의 TagSample.sample_id를
계속 새로 만든다. source frame 동일성을 transport receipt/sample identity로 재사용하지 않는다.

## readonly runtime 계약

```python
from apps.simulator.field_publication import (
    PublicationRuntime,
    get_publication_runtime,
)
# get_publication_runtime(store) -> tuple[PublicationRuntime, ...] | None
```

모델은 group_id, published_revision(UInt64 JSON string), published_mode,
waiting_for_first_completion, read_in_flight다. 모두 마지막 **실제 채택된 publication** 기준이다.
None은 미발행/확인 불가이며 정상이라고 해석하지 않는다. getter는 engine tick/queue/adoption을
호출하거나 상태를 바꾸지 않는다. duration/queue item/deadline absolute/generation을 노출하지 않는다.

첫 Waiting frame 채택은 기존 config pending=false와 동시에 runtime waiting=true/in_flight=true가
될 수 있다. 첫 완료 후 waiting=false여도 raw quality가 Bad이면 Bad 그대로다. 완료를 정상/Good
확보라고 표시하지 않는다. 설정이 바뀌고 새 발행 전에는 runtime의 published_revision/mode를
기존 configured/published/pending과 함께 보여 이전 runtime임을 구분한다.

미래 콘솔은 callback=lambda: get_publication_runtime(field_channels)를 주입해 이 tuple을 status의
별도 publication_runtime로 전달할 수 있다. FieldEnvironmentUnavailable은 field_channels의
plain ValueError 계열이므로 운영 HTTP에서는 safe503으로 매핑해야 한다. 이 문서는 해당 UI
소유 작업을 대신 구현하거나 완료했다고 표시하지 않는다.

## 현재 검증의 경계

```sh
.venv/bin/pytest -q tests/unit/test_field_delay.py tests/integration/test_field_delay_publication.py
```

현재 집중은 순수 queue, 실제 SQLite store, 실제 암호화 OPC socket을 다룬다. 기존 6 mode와
Good/Bad/Uncertain의 store-overlay parity, 실제 7 mode·48 tag/type, UInt64 정밀도와 원본 시각,
DELAY의 초기 Waiting/완료/해제·group isolation, stale/CAS/prepare 폐기·late encoding 실패·clock
latch, readonly runtime tuple을 포함한다.

Gateway 3/5초 판정은 실제 TLS socket에 **제어된 단조 시계**를 사용해 완료 사이 Good HB
공백을 검증한다. 일정한 pipeline latency 자체나 remote clock 정확도의 검증이 아니다.
RUNNING 생산 시험은 65번의 명시적 실제 controller.tick을 실행하여 수량 진행/PLC 유지와
OPC 전달을 확인하며 이를 65번의 실제 wall-clock ticker라고 표시하지 않는다. 실제 CLI/
Chromium·콘솔 권한·실시간 지연 시연·전체 수락 T06·장시간 시험은 이후 별도 검증이다.


## 기존 V2 41개 회귀와 현재 V3 의미의 대응

`test_simulator_field_channels_v2.py`는 import 대상만 실제 frozen V2 코드로 바꾼 역사적
이관 회귀다. V1→V2 원문/DDL/COMMIT/outer rollback 증거를 그대로 유지하며 그 41개를
현재 V3 기능 검증이라고 세지 않는다. 기존 generic `test_simulator_field_channels.py`는
현재 V3 class를 직접 사용하고 손상 fixture의 explicit discriminator만 3으로 맞춘다.
D2 원본 V1/V2 fixture나 과거 JSON/DDL은 변경하지 않는다.

| 보존할 의미 | 실제 current V3 대상의 영구 회귀 |
| --- | --- |
| V1/V2 원문·혼합형식·rowid·이관 실패 | `test_field_v3_storage.test_direct_migration_exact_bytes_ids_nonfield_and_restart`, `test_real_mixed_legacy_history_noncanonical_bytes_and_head_rowids_preserved`, 18 seam×V1/V2, actual COMMIT·outer rollback |
| CAS·actor·정확 그룹·config/audit rollback | 기존 field의 `test_config_audit_cas_and_failure_rollback`, `test_field_http_exact_group_cas_actor_and_passive_result_recovery`, `test_field_http_competing_revision_commits_only_one_group_mutation` |
| 실제 NORMAL 발행 선행·pending/old proof 거절 | D3 `test_revision_jumps_pass_through_and_no_fake_normal_checkpoint`, `test_prepare_discard_stale_config_and_duplicate_swap_fail_before_pointer`, D2 `test_new_v3_bridge_delay_checkpoint_and_storage_only_fail_closed` |
| READ_FAILURE/LINK_DOWN raw quality·값·시각·UInt64MAX·fresh NORMAL | 기존 field의 `test_secure_failure_preserves_raw_frame_and_uint64_then_fresh_normal` (2 mode×3 raw quality, 실제 TLS), D3 기존6mode×3 quality parity |
| 동일 failure mode no-op·config 실패 시 audit/proof/queue 불변 | D3 `test_live_v3_failure_noop_and_config_rollback_keep_proof_and_audit` (2 mode) |
| 반복 손상 거절·cache/live schema·외부 shadow | 기존 field의 `test_live_component_deletion_cannot_bootstrap_normal_state`, `test_failed_live_history_check_cannot_cache_corrupt_schema`, D2 live namespace/FK/raw-head tests |
| 엄격 marker/감사 downgrade | D2 `test_frozen_payload_versions_cannot_admit_future_mode_or_wrong_marker`, D3 `test_live_v3_parser_rejects_valid_payloads_in_downgrade_audit_order` (2→1/3→1/3→2) |
| 재시작·복원된 checkpoint가 새 NORMAL 증거가 아님 | D2 실제 SQLite close/reopen, 기존 failure TLS component 재생성, D3 실제 동일 객체 OPC stop/start/failed-listener-start |

위 대응으로 frozen suite 전환에 따른 live V3 의미 공백만 보완한다. 역사적 이관 검사와
현재 기능 검증을 섞어 같은 시험으로 중복 주장하지 않는다.
