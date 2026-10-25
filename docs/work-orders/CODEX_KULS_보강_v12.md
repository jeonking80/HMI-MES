# 작업지시서 — KULS 보강 12차 (Codex: 종료 경계 뒤 상태 사건 허용 — API 정산·PLC 원격 근거)

- 발행: 2026-10-25, Claude
- 수행: Codex. 최신 `main`(`edde46c` 이후)에서 브랜치 `codex/kuls-backend-v3`로 작업한다.
- 위험도: **high**.
- **이 지시서는 기준 문서 변경을 승인한다.** [CX-29](reports/CX-29.md)가 멈춘 이유는 `docs/architecture/REMOTE_SETTLEMENT_ATTESTATION.md`(25행 "The final event boundary must still equal the current durable source head")와 `SETTLEMENT.md`의 경계 규칙이다. 두 문서를 아래 규칙대로 고친다. 고칠 때는 문서 안에 **변경 이유와 영향**을 적는다(AGENTS.md "조용히 완화하지 않는다").
- **원칙:** 시험·Ruff는 1회씩 돌린다. 시험 데이터 문제는 고쳐서 통과시킨다. 아래 규칙 밖으로 제품 동작을 바꿔야 하면 멈추고 보고한다.
- PR·병합·배포·공개 확인은 Claude가 한다. Codex는 푸시하고 커밋 해시만 알린다. UI 파일은 수정하지 않는다.

## 1. 왜 바꾸는가

생산을 정상 종료하고 시뮬레이터를 재시작하면, 복구 절차가 상태 사건(`STOPPED→RECOVERY_REQUIRED→STOPPED`)을 반드시 만든다. 그러면 두 곳이 다음 작업을 막는다.

1. API `_reconcile`: 최종 경계 뒤의 사건을 종류와 관계없이 `EVENT_AFTER_FINAL_BOUNDARY`로 판정한다. 그래서 bind가 409로 거절된다(공개 seq 820·821).
2. PLC `controller._remote_settlement_final`: `final.last_event_seq != self._head()`이면 원격 정산 근거를 인정하지 않는다. 그래서 LOAD_JOB이 거절된다.

결과적으로 **재시작이 한 번이라도 끼면 다음 작업을 영원히 연결할 수 없다.** 실제 공장에서 정상 종료 → 전원 재투입 → 다음 작업은 기본 흐름이다. 따라서 두 규칙은 과도한 차단이다. 수량·물품의 안전과 관계없는 상태 사건만 허용하면 원래 의도(종료 후 생산이 끼어들지 않았음을 증명)는 그대로 지켜진다.

## 2. 허용 규칙 (API·PLC 공통)

최종 경계(`final.last_event_seq`) 뒤 같은 source/stream의 사건 구간 `(final.last_event_seq, head]`이 **모두** 다음을 만족할 때만 "경계 뒤 상태 꼬리"로 허용한다.

- 사건 종류가 `STATE_CHANGED`이다. 원본 payload가 계약 검증을 통과하고, API 쪽은 적용 완료(`applied_at`)여야 한다.
- 각 전이의 `state`와 `previous_state`가 `{STOPPED, RECOVERY_REQUIRED}` 안에 있다. RUNNING·STARTING·STOPPING·FAULT 등은 허용하지 않는다. 다른 상태가 필요하면 멈추고 보고한다.
- 순번이 연속이고 누락·격리 사건이 없다.
- 생산·불량·폐기·작업·기타 사건은 하나도 없다.
- 구간의 마지막 상태가 `STOPPED`다.

그 밖의 기존 검사는 모두 유지한다: 수량 일치, epoch·설정 일치, verified 커서, 미결 명령 없음, 재공 0, 출력·피드백·속도 0, recovery 해소, conflict 없음.

## 3. 할 일

1. **API(`apps/api/settlement.py`):** `after_boundary` 판정을 위 규칙으로 바꾼다. 허용 꼬리면 SETTLED를 유지하고, 아니면 지금처럼 `MISMATCH / EVENT_AFTER_FINAL_BOUNDARY`로 판정한다.
2. **PLC(`apps/simulator/controller.py`):** `final.last_event_seq == head` 검사를 "같거나, 그 사이 구간이 위 규칙의 허용 꼬리"로 바꾼다.
   - admission 때 근거(허용한 구간의 끝 seq와 그 구간의 해시)를 audit·attestation에 고정한다.
   - 실행 tick 재검증에서 head가 그 근거와 달라졌으면 거절한다.
   - 과거 attestation을 재사용하거나 검사를 단순 삭제하지 않는다.
3. **문서:** `REMOTE_SETTLEMENT_ATTESTATION.md`, `SETTLEMENT.md`에 새 규칙과 변경 이유·영향을 적는다.
4. **시험(각 1회):**
   - 재현(통합): END → 재시작 복구(상태 사건 2건) → API 재정산 SETTLED → bind 200 → PLC LOAD_JOB SUCCEEDED
   - 거절 유지: 경계 뒤 `PRODUCTION_GOOD` 1건 / RUNNING 전이 / 마지막 상태 RECOVERY_REQUIRED / 순번 누락 → API 불일치, PLC 거절
   - attestation 뒤 head가 바뀌면 tick에서 거절
   - 관련 정산·bind·controller 시험, `tests/unit` 전체, Ruff, 포맷 검사
5. **푸시:** `codex/kuls-backend-v3`에 푸시하고 커밋 해시만 알린다.

## 4. 하지 않을 것

- 공개 DB의 사건·정산 행 직접 수정이나 삭제, 새 source·generation, SQLite 초기화
- 위 허용 규칙보다 넓은 완화(모든 상태 사건 허용, 실행별 정산 끄기)
- 실연. 배포 뒤 Claude가 공개 bind를 확인하고 실연을 요청한다.

## 5. 보고

`reports/CX-29.md`에 "12차 구현" 절을 덧붙인다: 변경 내용, 문서 변경 요지, 시험 결과. `docs/DEVELOPMENT_STATUS.md`에 한 줄.
