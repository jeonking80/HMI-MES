# 작업지시서 — KULS 보강 17차 (Codex: 14차 회귀 수정 — 시뮬레이터 저장소 v4와 현장 채널)

- 발행: 2026-10-25, Claude
- 수행: Codex. 브랜치 `codex/kuls-backend-v3`(`b9769b2` 이후)에서 이어서 한다.
- 위험도: **high**. 시뮬레이터 영속 저장과 현장 채널 무결성 검사를 다룬다.
- **14차는 아직 main에 병합하지 않았다.** 이 회귀를 고친 뒤 Claude가 화면 변경과 함께 병합한다. 화면 변경은 `claude/bold-feynman-rpbxfw`(`9b78ef5`)에 있다.
- PR·병합·공개 이관·배포는 Claude가 한다. Codex는 푸시하고 커밋 해시만 알린다. UI 파일은 수정하지 않는다.

## 1. 회귀 내용 (Claude 확인)

14차 서버 변경 자체의 시험은 통과했다(단위 + 변경된 통합 시험 1,293 passed). 그러나 **바뀌지 않은 기존 시험**에서 회귀가 나왔다.

- **실제 시뮬레이터가 시작하지 못한다.** `python -m apps.simulator serve`가 새 DB에서도 아래 오류로 종료한다. 그래서 `scripts/check-operable-system.py` 전체 흐름이 첫 단계에서 실패한다. 운영 runtime도 이 코드로 재시작하면 같은 이유로 뜨지 않는다.
  ```
  apps/simulator/__main__.py:107 serve → FieldChannels(storage)
  field_channels.py:335 _check_environment → FieldEnvironmentUnavailable: Unknown or missing nonfield trigger protection
  ```
- **현장 채널 시험이 대량 실패한다.** 대상은 `test_simulator_field_channels.py`, `test_simulator_field_channels_v2.py`, `test_field_v3_storage.py`, `test_field_delay_console.py`, `test_field_delay_publication.py`. 결과는 **114 failed, 87 passed, 43 errors**다. 대표 오류는 `FieldEnvironmentUnavailable: Unsupported storage schema`다.
- **같은 시험이 main(`ea5ac0e`)에서는 통과한다.** `test_simulator_field_channels.py`와 `test_field_v3_storage.py`가 144 passed다. 따라서 14차 회귀가 확실하다.
- **원인:** CX-35(`201f2cc`)가 `apps/simulator/storage.py`를 v3에서 v4로 올렸다. 이때 `source_events`와 `archived_event_keys`의 CHECK에 `PARAMETERS_CHANGED`를 넣어 테이블을 다시 만들었다. 그런데 `apps/simulator/field_channels.py`는 아직 v3 전제를 그대로 쓴다. 대상은 `_check_environment`의 `user_version != 3`, 허용 트리거 목록 `_NONFIELD_TRIGGERS`, 소유 DDL 다이제스트다.

## 2. 할 일

1. 현장 채널 모듈이 저장소 v4를 받아들이게 고친다. 버전, 트리거 목록, 소유 DDL 다이제스트를 v4 스키마와 정확히 일치시킨다.
   - v3 → v4 이관 직후의 DB와 v4로 새로 만든 DB가 같은 검사 결과를 내야 한다.
   - 모르는 트리거·뷰·가상 테이블을 거절하는 기존 보호는 그대로 둔다. 검사를 느슨하게 하거나 끄지 않는다.
2. 기존 운영 SQLite(v3, runtime 테이블 있음)를 v4로 이관한 뒤에도 현장 채널 검사가 통과하는지 시험한다.
3. **검증 범위를 넓힌다.** 이번처럼 바뀐 시험만 돌리면 회귀를 놓친다.
   - `tests/unit` 전체
   - `tests/integration` 중 시뮬레이터·현장 채널·게이트웨이·정산·작업지시·명령 관련 파일 **전부**(바뀌지 않은 파일 포함)
   - `scripts/check-operable-system.py`를 폴링 빌드로 1회 실행한다(`VITE_SCREEN_TRANSPORT=polling npm --prefix apps/web run build` 다음 `--chromium` 지정). 이 스크립트는 Claude 소유지만 실행은 해도 된다. 실패하면 원문을 보고한다.
   - Ruff·포맷 검사. 14차에서 재검을 하지 못한 CX-33·35도 포함한다.
4. 푸시하고 커밋 해시만 알린다.

## 3. 하지 않을 것

- 현장 채널 무결성 검사 완화·우회
- 운영 SQLite 원본 이관(병합·배포 뒤 runtime 재시작 때 자동으로 이관된다)
- 공개 DB 변경

## 4. 보고

`reports/CX-39.md`: 원인, 수정 내용, 위 시험 결과(통과·실패 수). `docs/DEVELOPMENT_STATUS.md`에 한 줄.
