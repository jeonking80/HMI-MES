# 시뮬레이터 명령 원장 (DEV-06b)

`apps/simulator/command_store.py`는 [명령 계약](../../contracts/PROTOCOL.md)의 원본 요청·접수·결과를 **기존 시뮬레이터 SQLite 연결**에 저장하는 내부 원장이다. PLC 허용 판정·실제 완료 근거·인증은 호출자가 제공한다. 저장된 접수는 실행 성공이나 운전 재개 허가를 의미하지 않는다.

## 명시적 스키마와 트랜잭션

```python
with storage.transaction():
    CommandStore.initialize_schema(storage.connection)
journal = CommandStore(storage)
```

초기화는 명시적 additive extension migration이다. `command_schema_version(singleton=1, version=1)`과 `command_journal`을 같은 트랜잭션에 생성하며 기존 시뮬레이터 `PRAGMA user_version`을 변경하지 않는다. 알려진 extension version과 열 구성을 확인한다. 버전 없는 기존 원장·알 수 없는 버전·열 구성은 덮어쓰거나 자동 변환하지 않는다. 새 연결이나 두 번째 SQLite 파일을 만들지 않는다.

`CommandStore(storage)`는 트랜잭션을 소유하는 `SimulatorStorage`를 반드시 받는다. 모든 변경은 이미 열린 호출자 트랜잭션을 요구한다. 소유자의 중첩 트랜잭션과 SAVEPOINT는 변경 단위를 보호하고, 호출자가 예외를 잡아도 해당 단위의 쓰기는 취소된다. 원장이 독자적으로 COMMIT하지 않으므로 다음처럼 공정·PLC 상태·완료 결과를 함께 확정할 수 있다.

```python
with storage.transaction():
    # 호출자가 PLC 조건과 실제 완료 근거를 검사한다.
    storage.apply_transition(previous, process_result, occurred_at=at)
    # 같은 connection에 PLC 상태도 저장한다.
    journal.complete(command_id, caller_proven_result)
# 이 지점 뒤에만 완료 응답을 반환한다.
```

`register_execution`·`apply_transition`도 바깥 트랜잭션에 참여한다. SAVEPOINT 해제는 durable commit이 아니다. 호출자는 접수 응답과 새 실행 예약도 **바깥 commit 뒤에만** 반환·수행해야 한다. DB 밖의 물리 동작이나 외부 송신은 SQLite rollback으로 되돌릴 수 없으며 이 원장은 그러한 동작을 실행하지 않는다.

## 접수·중복·최초 만료

`submit(request, received_at=..., decide=...)`는 `JournalSubmission(submission, result, created)`를 반환한다. `decide(request)`는 동기적인 PLC 접수 조건 판정이며 `CommandSubmission`을 반환해야 한다. 명령 ID와 접수 시각이 일치해야 하고 이 callback에서 물리 실행을 수행하지 않는다.

1. 기존 ID를 먼저 조회하고 공유 `canonical_command_payload`로 비교한다. 동일 내용이면 저장된 원래 접수·현재 결과와 `created=False`를 반환하며 deadline·현재 제어 버전 등 새 조건을 검사하지 않는다.
2. 같은 ID의 다른 내용은 `COMMAND_ID_CONFLICT`, `created=False`, `result=None`이다. 기존 원본과 결과는 보존한다. 충돌 응답은 원래 명령의 결과가 아니다.
3. 최초 도착 시각이 deadline 이상이면 PLC에서 이미 전달된 요청을 `REJECTED/REQUEST_EXPIRED`로 영속 저장한다. `delivered_at`과 `completed_at`은 접수 시각이다. 게이트웨이 미전달 상태를 뜻하는 `EXPIRED`를 만들지 않는다.
4. 그 밖에는 `decide`가 버전·대상·실행·모드·안전 조건을 판정한다. 접수 또는 거절과 초기 `CommandResult`를 같은 row에 저장한다. 거절도 이후 동일 ID 재요청에서 재판정하지 않는다.

오직 `created=True`이고 접수가 ACCEPTED인 새 기록만 새 실행 예약의 후보이다. 최종 예약 여부와 인증·충돌·복구 제한은 상위 제어 계층 책임이다. 다른 ID는 같은 payload여도 별개 요청이다.

## 완료와 재시작

`complete(command_id, result)`는 ACCEPTED를 호출자가 증명한 SUCCEEDED/FAILED로만 바꾼다. 원래 `delivered_at`을 보존하고 시각 역전을 차단한다. 성공 evidence와 applied version은 계약 검증을 거치지만 원장이 실제 PLC 피드백을 만들어 주거나 그 근거를 대신 검증하지 않는다. 같은 최종 결과의 재저장은 멱등이고 다른 최종 결과로 덮어쓰기는 거절한다. 대기 초과나 재시작만으로 FAILED를 확정하지 않는다.

`lookup`은 정규 요청과 canonical payload·접수·결과 ID·전달 시각·거절 근거의 정합성을 검사하고 불일치에는 `StorageCorruption`으로 실패한다. 저장 손상은 `storage.mark_corrupt()`로 보고한다. 바깥 트랜잭션이 없으면 recovery gate를 즉시 영속화하며, 트랜잭션 안이면 해당 전체 단위를 poison하여 호출자가 예외를 잡아도 다른 쓰기를 함께 commit할 수 없다. `pending_records()`는 ID 순서의 미확정 ACCEPTED 기록을 반환하고 저장된 잘못된 ID도 손상으로 처리한다. 두 메서드는 원장 조회이며 운전 허가가 아니다. 재오픈해도 미확정 접수는 그대로 남고 timer·작업 재실행·START·자동 실패 확정은 없다. 상위 제어 계층이 원래 ID와 물리 상태를 대조해야 한다.

UInt64 기대 제어 버전·적용 버전·evidence 사건 순번은 공유 계약의 정규 십진 JSON 문자열로 TEXT에 저장한다. SQLite INTEGER/REAL 변환이나 JavaScript Number를 거치지 않는다. Python에서는 정확한 정수로 복원한다.

## 로컬 검증과 범위

```sh
.venv/bin/ruff check apps/simulator/command_store.py tests/integration/test_command_store.py
.venv/bin/ruff format --check apps/simulator/command_store.py tests/integration/test_command_store.py
.venv/bin/python -m pytest -q tests/integration/test_command_store.py
```

2026-10-20: targeted **29개 통과**. 실제 임시 SQLite 재오픈, 원본 요청 내용 충돌·동일 재접수, deadline/버전 판정 앞의 중복 조회, 영속 거절, 명시적 스키마 rollback과 버전 거절, callback 오류의 SAVEPOINT rollback, 불변 최종 결과와 미확정 접수 복원, 2^53+1·2^63·UInt64 최대값을 검사했다. 저장 손상 예외를 잡아도 전체 결합 트랜잭션을 취소하고 recovery를 latch하는 회귀와 저장된 잘못된 ID도 검사했다. pending 조회는 실행을 다시 예약하지 않는다.

별도 Python 프로세스를 `os._exit()`로 공정 snapshot+완료 결과의 바깥 COMMIT 직전·직후에 실제 종료했다. 재오픈 결과가 이전 공정+ACCEPTED 또는 새 공정+최종 결과 중 하나로만 유지됨을 확인했고 SQLite integrity/FK 검사도 통과했다. 이 시험은 호출자 실패/완료 evidence fixture를 사용하는 **저장 원자성의 부분 증거**이다. 실제 PLC 운전 성공·인증·OPC 응답 유실·전체 T03/T09 통과를 의미하지 않는다. 실제 원격 경로, 인증/권한과의 연결, 물리 완료 대조, 운영 복구 절차와 보관 정리는 해당 상위 구현·통합 시험에서 확인해야 한다.
