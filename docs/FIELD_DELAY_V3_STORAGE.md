# DELAY D2: field component V3 저장 이관 checkpoint

이 문서는 독립 `/tmp/hmi-field-delay-v3-stage`의 저장 이관 단위다. repo의 field V2 생산
코드를 바꾸지 않는다. 실제 publisher·HTTP/UI·OPC·게이트웨이·브라우저를 연결하거나 실행하지
않았다. DELAY를 저장할 수 있지만 이 stage의 `prepare()`는 DELAY가 있으면 명시적으로
거절한다. queue 미연결 상태에서 현재 정상값으로 조용히 발행하지 않기 위한 경계다.

## 영속 모델과 frozen 형식

V3는 payload marker 3, DELAY의 strict duration(100..10,000ms, 100ms 단위), 원래 실제 NORMAL
checkpoint를 추가한다. duration은 DELAY에만 있고 queue/deadline/generation/마지막 전달 frame은
저장하지 않는다. FROZEN의 전체 Good 규칙과 READ_FAILURE/LINK_DOWN의 원본 품질 보존 규칙을
유지한다. DELAY도 같은 그룹·직전 revision·마지막 실제 NORMAL 발행만 진입 근거로 쓴다.
원래 NORMAL이 Bad/Uncertain이어도 그 품질을 보존한다. 동일 duration/mode/current CAS는
no-op이고 DELAY duration 변경은 NORMAL commit→실제 발행을 다시 거쳐야 한다.

`_field_channels_v2.py`는 승인된 V2 원본 전체의 byte-identical frozen snapshot이다(SHA256
`1e92e1bfc3e41eb4b9a0f123c472602e1a006e410aff1b70ef0d8e839c5fbb1d`).
과거 parser/DDL/transition을 V3 enum으로 넓히지 않는다. V1 snapshot도 변경하지 않는다.
V1/V2의 엄격한 parser로 먼저 읽고 public 공통 view로만 정규화하며 원문 write-back은 없다.

새 writer는 state와 audit.before/after discriminator를 모두 명시한다. V3 DDL에는 discriminator
DEFAULT가 없고 CHECK IN(1,2,3)가 있다. 기존 1→1, 1→2, 2→2와 신규 1→3, 2→3, 3→3만
허용한다. 내부/외부 marker 불일치·누락·unknown·downgrade를 거절한다. V1의 과거 이력은
기존 의미 연결 규칙을 보존한다. V2/V3 head와 최신 after는 형식과 원문도 일치해야 한다.

## raw staging과 원자성

단일 owner `BEGIN IMMEDIATE`에서 직접 V1→3 또는 V2→3을 수행한다. 중간 V2 commit은 없다.

1. 정확한 입력 DDL·모든 감사/head·고정 UInt64·checkpoint provenance를 검증한다.
2. head rowid/group/revision/JSON BLOB/discriminator, audit_id/group/revision/actor/time/원문
   before·after BLOB/discriminator의 typed streaming digest와 table별 row 수를 확보한다.
   V1에 없는 discriminator는 비교 기준과 copy에서 1로 명시한다.
3. FK 없는 `temp.field_v3_heads_stage`, `temp.field_v3_audit_stage`에 SQL로 원문을 복사한다.
   staging과 원본 digest가 같음을 확인한 뒤에만 파괴 단계를 시작한다.
4. old child audit→faults→metadata 순서로 제거한다. main canonical faults→audit→metadata와
   exact 보호 trigger를 CREATE하고 parent부터 raw copy한다. rename·FK SQL 재작성은 없다.
5. exact DDL/예상 autoindex/FK/전체 mixed history/head와 원문 digest를 다시 검증한다.
   모든 비field DDL/data 및 runtime metadata2/user_version3 불변도 확인한다.
6. temp staging을 제거하고 최종 객체/형식을 재확인한 뒤 한 번 commit한다.

모든 main/temp 접근은 schema를 명시한다. FK OFF, writable_schema, ignore_check_constraints,
executescript, authorizer 교체는 사용하지 않는다. 실제 SQLite COMMIT 거절을 주입하는 caller의
기존 authorizer도 그대로 유지한다. 각 staging/DROP/CREATE/copy/cleanup/최종 검증/COMMIT
실패나 outer rollback은 원래 V1/V2 schema·rowid·원문을 복원해야 한다. 이전 코드로 다시
읽고 새 이관을 재시도할 수 있어야 한다. migration은 가짜 operator audit/revision을 만들지 않는다.

## 안전한 외부 환경과 corruption latch

지원하지 않는 temp/attached schema, view, virtual/shadow table, unknown nonfield trigger,
owned field table을 부모로 참조하는 외부 FK는 거절한다. FK table 이름은 대소문자를 구분하지
않고 검사한다. 외부 trigger 의존성을 substring으로 추정하지 않는다. storage의 필수 trigger
7개 및 runtime component가 있으면 필수 trigger 2개를 name/type/tbl_name/SQL exact allowlist로
확인하고 다른 nonfield trigger는 허용하지 않는다. owned table의 객체는 별도 exact field DDL로
검증한다. runtime2 및 user_version3도 유지한다.

환경 선검사는 transaction 이전에 읽기만 수행하고 write TX 획득 뒤 다시 확인한다. 별도
connection이 그 사이 DDL을 넣으면 두 번째 검사에서 거절한다. write TX 이후에는 다른
connection의 DDL이 잠금에 막혀야 한다. unsupported 환경은 `FieldEnvironmentUnavailable`
(plain ValueError)로 거절하여 storage의 unqualified recovery latch가 TEMP shadow나 unknown
identity trigger를 실행하지 않게 한다. 외부 데이터를 정리하거나 trigger를 자동 삭제하지 않는다.

live states/prepare/set_fault는 main schema cookie와 무관하게 매번 가벼운 namespace guard를
수행한다. TEMP/ATTACH는 main cookie를 올리지 않기 때문이다. main schema 변경은 full strict
환경/DDL/history 검증을 거치고 성공 뒤에만 cache를 확정한다. 살아 있는 V3 객체가 V1/V2나
전체 table 삭제를 보면 자동 이관/초기화하지 않는다. outer rollback으로 되돌아간 provisional
객체도 반복 거절한다. 유효한 안전 환경에서 발견한 field payload/history 손상은 기존
StorageCorruption/recovery latch 경계를 유지한다. FK data 검사는 안전한 환경과 exact layout
인식 뒤에 수행하며 실제 row 위반도 StorageCorruption이다. 잘못된 외부 FK DDL은
unsupported 환경으로 거절한다. latch는 데이터 복구나 자동 재가동이 아니다.

미래 HTTP wiring은 환경 거절을 safe unavailable 응답으로 매핑해야 하며 schema/SQL details를
노출하지 않는다. 이 checkpoint는 그 HTTP mapping이나 운영 노출을 구현한 것이 아니다.

## engine 연결 전 해결할 계약

D1의 내부 NORMAL/DELAY 모델에 다른 field mode를 가짜 NORMAL로 매핑하지 않는다.
권장 후속 adapter는 실제 mode를 보존하며 비-DELAY mode의 store overlay fields를 pass-through한다.
모든 revision/mode 전환은 queue generation을 flush하고 **실제 NORMAL pointer 채택만**
normal provenance를 갱신한다. 다른 mode는 그 증거를 지운다. DELAY 진입은 predecessor 실제
NORMAL과 정확한 checkpoint를 요구한다. 현재 실제 mode 목록은 NORMAL/FROZEN/BAD/UNCERTAIN/
READ_FAILURE/LINK_DOWN/DELAY다. unknown mode는 거절한다. 이 adapter는 여기서 구현하지 않았다.

D1의 acquisition_frame_id는 내부 원본취득 provenance이며 gateway TagSample.sample_id가 아니다.
새 gateway capture는 반복된 source frame에도 매번 고유 sample ID를 생성해야 한다.

## 검증 명령과 범위

```sh
.venv/bin/pytest -q tests/integration/test_field_v3_storage.py
.venv/bin/ruff check apps/simulator/field_channels.py apps/simulator/_field_channels_v2.py tests/integration/test_field_v3_storage.py
```

영구 시험은 실제 frozen V1/V2 코드로 만든 기존 mode·혼합 1/2 이력·noncanonical JSON·임의
head rowid를 다룬다. staging/rebuild 실패 seam, 실제 COMMIT authorizer 거절, outer rollback,
다른 connection의 DDL 경합, namespace shadow/side-effect 거절, V3 bridge/duration/provenance,
실제 SQLite close/reopen과 RECOVERY_REQUIRED, frozen marker 격리와 raw head 변경을 검증한다.

검증 결과는 실행 로그와 함께 보고하며 저장 단위 통과를 DELAY 사용자 기능·실제 OPC 지연·
전체 T06/장시간 수락 시험 완료로 표시하지 않는다. V3 위험 독립 검토 뒤 publisher wiring과
mode 조합·실제 socket/CLI/브라우저 검증은 다음 checkpoint다.
