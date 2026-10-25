# 작업지시서 — KULS 보강 11차 (Codex: 종료 후 재시작 시 정산 불일치로 다음 작업 연결 거절)

- 발행: 2026-10-25, Claude
- 수행: Codex. 최신 `main`(`a213e28` 이후)에서 브랜치 `codex/kuls-backend-v3`로 작업한다.
- 위험도: **high**. 정산·사건 귀속 규칙을 다룬다.
- **원칙: 핵심만 한다.** 시험·Ruff는 1회씩 돌린다. 시험 데이터 문제는 고쳐서 통과시킨다. 기준 문서를 바꿔야 하면 멈추고 보고한다.
- UI·README·`vercel.json`·`scripts/check-operable-system.py`는 수정하지 않는다. PR 생성·병합·배포는 Claude가 한다. Codex는 푸시하고 커밋 해시만 알린다.

## 1. 현상과 원인 (Claude 확인, 2026-10-25)

공개 사이트에서 `WO-T1` 실행 연결이 계속 실패했다. `demo` 계정으로 같은 요청을 직접 보내 확인했다.

- `POST /api/v1/work-order-actions/bind?work_order_id=WO-T1` → **409 `registration conflicts with immutable content`, 0.39초**
- 라인 조건은 모두 충족했다: 정지(`Line.State=0`), 원격·자동, 재공 0, `ControlVersion=27`, 작업지시 RELEASED·row_version 2, 카운터 구간 일치.
- 거절 지점: `apps/api/work_order_workflow.py` `bind_work_order`가 라인 예약(`line_execution_reservations`)에 걸린 이전 실행 `3ac06c2b-ccd5-4a80-afd5-8c5e26ed204f`를 `reconcile_execution`으로 다시 정산한다. 결과가 SETTLED가 아니면 `Reserved execution is not settled`로 거절한다.
- 정산이 깨진 이유: 이 실행의 최종 요약은 `last_event_seq=819`(09:48:35Z 정상 종료)다. 그런데 오늘 시뮬레이터를 다시 켜면서 **상태 변경 사건 2건이 같은 실행 ID로 붙었다.**

| seq | kind | payload | occurred_at |
|---|---|---|---|
| 819 | STATE_CHANGED | STOPPING → STOPPED, cv 25 | 2026-10-24T09:48:35Z(최종 경계) |
| 820 | STATE_CHANGED | STOPPED → RECOVERY_REQUIRED, cv 26 | 2026-10-25T02:40:19Z |
| 821 | STATE_CHANGED | RECOVERY_REQUIRED → STOPPED, cv 27 | 2026-10-25T02:48:33Z |

- `apps/api/settlement.py` `_reconcile`은 최종 경계 뒤에 같은 실행의 사건이 하나라도 있으면 `MISMATCH / EVENT_AFTER_FINAL_BOUNDARY`로 판정한다. 사건 종류는 구분하지 않는다.
- 가상 PLC는 END_RUN 뒤에도 실행 ID를 비우지 않고 `run_state=ENDED`로만 둔다(`apps/simulator/plc.py` 379행 부근, 10차 commission 수정과 같은 배경). 그래서 재시작 복구 중의 상태 변경이 끝난 실행에 귀속된다.
- 수량은 그대로다(양품 531, 불량 265, 폐기 0). 저장된 `run_reconciliations`는 아직 SETTLED이다. bind 트랜잭션이 409로 롤백되기 때문이다.

**영향:** 생산을 한 번 정상 종료한 뒤 시뮬레이터를 재시작하면, 다음 작업은 항상 연결할 수 없다. 공개 실연과 T09(재시작) 계열 수락에 직접 걸린다.

부수 관찰(이번 범위 아님, 보고서에만 적는다): 화면은 이 409를 실행 연결 "충돌"로 받은 뒤 원래 ID 조회를 반복해서, 사용자에게는 계속 로딩처럼 보였다. UI 쪽은 Claude가 따로 본다.

## 2. 할 일

1. **설계 결정(high 검토).** 아래 두 방법을 검토하고 결정과 이유를 `reports/CX-29.md`에 남긴다.
   - **(나) 정산 규칙(권장 출발점):** 최종 경계 뒤의 사건 중 **수량에 영향이 없는 상태 사건**(예: `STATE_CHANGED`로서 운전 재개가 아닌 것)은 정산 불일치로 보지 않는다. 생산·불량·폐기 등 수량 사건, 또는 RUNNING으로의 전이는 지금처럼 `EVENT_AFTER_FINAL_BOUNDARY`다. 사건은 지우거나 다른 실행으로 옮기지 않는다. 원래 실행 귀속과 기록은 그대로 둔다.
   - **(가) 가상 PLC:** `run_state=ENDED` 뒤의 상태 변경 사건에는 끝난 실행 ID를 붙이지 않는다. 단, 이미 저장된 seq 820·821은 남아 있다. 그래서 (가)만으로는 공개 데이터가 풀리지 않는다.
   - 두 방법을 함께 쓸지는 Codex가 판단한다. 어느 쪽이든 "생산 사건은 원래 execution_id에 귀속", "불명·누락을 정상으로 대체하지 않음"을 지킨다.
2. **구현.** 결정한 방법을 최소 변경으로 구현한다.
3. **시험(각 1회).**
   - 재현: 실행 END → 최종 경계 뒤 `STATE_CHANGED`(STOPPED→RECOVERY_REQUIRED→STOPPED) 2건 → 재정산 SETTLED → 다음 작업지시 bind 성공
   - 보존: 경계 뒤 `PRODUCTION_GOOD` 1건 → 여전히 `MISMATCH / EVENT_AFTER_FINAL_BOUNDARY`, bind 거절
   - 보존: 경계 뒤 RUNNING 전이 → 여전히 불일치
   - 관련 정산·실행 연결 시험과 `tests/unit` 전체, Ruff, 포맷 검사
4. **푸시.** `codex/kuls-backend-v3`에 푸시하고 커밋 해시만 알린다.

## 3. 하지 않을 것

- 공개 DB의 event_inbox·run_reconciliations 직접 수정이나 삭제
- 새 source·generation 생성, 시뮬레이터 SQLite 초기화
- 정산 규칙을 실행 단위로 끄는 우회
- 실연. 배포 뒤 Claude가 bind 응답을 직접 확인하고 실연 재개를 요청한다.

## 4. 보고

`reports/CX-29.md`: 원인, 설계 결정과 이유, 변경 내용, 시험 결과. `docs/DEVELOPMENT_STATUS.md`에 한 줄 요약.
