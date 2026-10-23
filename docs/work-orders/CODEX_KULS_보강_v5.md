# 작업지시서 — KULS 보강 5차 (Codex: T05 409 해소와 릴리스 마무리)

- 발행: 2026-10-23, Claude
- 수행: Codex. 브랜치 `codex/kuls-backend-v3`에서 이어서 작업한다(`7cbeb73` 이후). 시작 전에 최신 `main`을 병합한다.
- **원칙: 핵심만 한다.** 과한 반복 실행, 새 장시간 시험, 범위 밖 정리는 하지 않는다. 8시간 운전은 계속 제외한다.

## 1. T05 409 원인 (Claude 진단 완료)

[KULS-v3-STOP.md](reports/KULS-v3-STOP.md)의 두 번째 `RESET_FAULT` 409는 **제품 결함이 아니라 시험 타이밍 문제**다.

- 고장 원인을 해소하면 PLC `ControlVersion`이 5 → 6으로 오른다. 상태는 FAULT 그대로라 이벤트는 없다.
- 시험은 수집 후 `asyncio.sleep(0.5)`만 기다리고 버튼을 누른다.
- 화면이 새 상태를 받기 전에 누르면 화면은 `expected_control_version=5`로 요청한다. 서버는 수집된 버전 6과 달라 `_preflight`에서 409로 거절한다. 오래된 화면 기준 명령을 막는 의도된 동작이다.
- 재현(중단 시점 코드 + 미완료 patch, 4회):
  - 실패 1회: 보낸 버전 5 / PLC 6 → 409
  - 통과 3회: 6 / 6 → 200

## 2. 할 일

1. **최신 `main` 병합.** Claude가 명령 패널에 `data-control-version` 속성을 추가했다. 위치는 `#command-panel`이고, 값은 화면이 다음 요청에 실어 보낼 제어 버전이다.
2. **시험 수정.** `tests/e2e/browser_boundary.py`의 `command()` UI 경로에서, 버튼을 누르기 전에 다음을 기다린다.
   ```python
   await expect(self.page.locator("#command-panel")).to_have_attribute(
       "data-control-version", str(self.controller.state.control_version)
   )
   ```
   T05의 `asyncio.sleep(0.5)` 고정 대기는 지운다. 보존 중인 미완료 시험 수정 2파일은 필요한 부분만 정리해 커밋한다.
3. **실패 2번째 확인.** 마지막 전체 묶음(release5)은 17 passed / 2 failed였다. T05 외 나머지 1개의 원인을 확인한다. 같은 타이밍 문제면 같은 방식으로 고친다. 제품 결함이면 고치고 원인을 기록한다.
4. **검증은 한 번씩만.**
   - 수정한 시험(T05와 2번째 실패)을 각각 1회 통과
   - 관련 단위 시험과 Ruff
   - CX-22 릴리스 묶음 1회
   - 통과하면 끝이다. 실패하면 원인과 함께 보고하고 멈춘다(자동 재시도 금지).

## 3. 하지 않을 것

- 같은 시험의 반복 실행(통계용 다회 실행)
- CX-19(30분 단절)·CX-20(7일 성능) 재실행. 이미 통과 기록이 있다.
- 8시간 운전, 공개 DB 변경(OPS-03은 이번 범위 아님)
- UI·README·`vercel.json`·`scripts/check-operable-system.py` 수정. UI 변경이 필요하면 보고서에 적는다.

## 4. 보고

`reports/CX-22.md`에 결과를 덧붙인다.
- 수정 내용
- 실행 명령, 통과 수, 실패 원문
- 릴리스 묶음 결과

`docs/DEVELOPMENT_STATUS.md`에 한 줄 요약을 남긴다. 미실행·실패를 통과로 적지 않는다.
