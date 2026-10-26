# 작업지시서 — KULS 보강 19차 (Codex: 시간 단위 원본 보관·영수증 압축·통합 시험 회귀)

- 발행: 2026-10-26, Claude
- 수행: Codex. 브랜치 `codex/kuls-backend-v3`(`2cea66c` 이후)에서 이어서 한다.
- 위험도: **high**. 수집 원본 삭제, 중복 판정, 이관을 다룬다.
- **사용자 결정(2026-10-26): "시간 단위 보관 + 영수증 압축".** 18차의 보관 단위(일)와 영구 영수증 설계를 아래와 같이 바꾸는 것과, 그에 따른 기준 문서 변경을 승인한다. 문서마다 변경 이유와 영향을 적는다.
- PR·병합·공개 이관·배포·Vercel 환경변수는 Claude가 한다. Codex는 푸시하고 커밋 해시만 알린다. UI 파일은 수정하지 않는다.

## 1. 배경 (CX-40 보고와 Claude 검증)

- 18차 설계 오류는 Claude 책임이다. 1Hz 운전에서 원본은 건당 약 6.5KB다. 7일 보관하면 약 3.6GB이고, 최소값인 1일도 약 550MB다. 그래서 Neon 무료 0.5GB에 들어가지 않는다.
- 영구 영수증은 capture마다 약 760B다(CX-40 가속 적재: 8,000건에 6.1MB). 1Hz면 하루 약 66MB씩 끝없이 늘어난다.
- Claude 검증(병합본 `f1f0ec4`):
  - `tests/unit` 771 passed
  - 현장 채널 시험 251 passed
  - `scripts/check-operable-system.py` PASS. 화면 버그 2건(첫 품목 등록, 정책 플래그 파싱)은 Claude가 고쳤다.
  - `tests/integration` 전체 1회: **4 failed, 1853 passed**. 아래 3절 참조.

## 2. 할 일 — 보관과 압축

1. **보관 단위를 시간으로 바꾼다.**
   - 설정은 `HMI_INGEST_RETENTION_HOURS`. 기본값은 168(7일), 최소값은 **3**이다. 게이트웨이 원장 보관(2시간)보다 반드시 길게 한다. 3 미만이거나 숫자가 아니면 시작을 거절한다.
   - `HMI_INGEST_RETENTION_DAYS`는 없애거나, 같이 설정되면 거절한다. 둘 중 무엇을 썼는지 모호한 상태를 허용하지 않는다.
   - 공개 환경은 Claude가 `HMI_INGEST_RETENTION_HOURS=3`으로 설정한다.
2. **영수증을 압축한다.** 보관이 끝난 capture의 개별 영수증은 연속 보관 종료 구간(18차의 범위 기록)으로 합치고 지운다.
   - 압축 뒤에도 아래는 그대로 유지돼야 한다.
     - 같은 capture_seq·batch_id 재전송이 410 `RETENTION_EXPIRED`가 되는 것
     - 게이트웨이 전체 capture 유일성
     - 내용 해시 누적값
   - 알람·정산·감사 등 **다른 행이 FK로 참조하는 영수증만** 개별로 남긴다. 그 수는 사건 수에 비례해야 한다. capture 수에 비례하면 안 된다.
   - 압축은 18차 정리와 같은 트랜잭션·제한 시간 규칙을 따른다. 오류가 나면 롤백한다.
3. **정상 상태 용량을 측정한다.** 가속 적재로 1Hz 24시간 분량(86,400 capture, 건당 실제 크기에 가까운 본문)을 보관 3시간으로 돌린다.
   - 남은 원본 수, 남은 영수증 수, 범위 행 수, 각 테이블 크기를 보고한다. vacuum 전후 둘 다 적는다.
   - 원본 수는 약 10,800에서, 영수증 수는 사건 참조분에서 일정하게 유지돼야 한다.
4. 이관은 추가 방식(필요하면 0023)으로 한다. Neon HTTP 업그레이드·권한 배치도 만들고 렌더러 재생성 결과와 `cmp`로 일치를 확인한다. 기존 데이터를 이관 단계에서 지우지 않는다.

## 3. 할 일 — 통합 시험 회귀 (Claude 실행 결과)

1. `tests/integration/test_item_migration.py`: `test_new_item_without_recipe_also_refuses_lossy_downgrade`, `test_queue_updates_continue_on_new_item_head`
   - 원인: 0022가 생긴 뒤에도 head를 `0021_item_master`로 기대한다.
   - head가 바뀌어도 의도대로 검사하도록 고친다.
2. `tests/integration/test_mixed_alarm_migration.py::test_explicit_mixed_feature_set_rejects_old_heads_without_claiming_coordinator_coverage`
   - 실제 `SUPPORTED_MIXED_ALARM_REVISIONS`에 0019~0022가 들어 있는데, 시험은 0018까지만 기대한다. 14차부터 실패하던 것으로 보인다.
   - 새 revision이 정말 그 집합에 속하는지 판단한다. 속하면 시험을 고치고, 속하지 않으면 제품을 고친다. 판단 근거를 보고한다. 검사를 느슨하게 하지 않는다.
3. `tests/integration/test_gateway_fifo_recovery.py::test_1800_capture_fifo_drain_with_live_collection`
   - 시스템이 켜진 지 1,800초가 안 됐을 때 `mono = capture monotonic - 1800`이 음수가 되어 `CaptureTime` 검증에 걸린다. 병합본 전체 실행에서 실패했다. Claude의 단독 재실행 2회 중 1회 실패, 1회 통과였으나 통과한 1회는 실수로 main(`ea5ac0e`) 체크아웃에서 실행돼 병합본 근거가 아니다.
   - 제품 결함이 아니라 시험의 시계 가정 문제다. 시스템 가동 시간과 무관하게 통과하도록 시험만 고친다.
4. **시험(각 1회):**
   - 신규·변경 시험
   - `tests/unit` 전체
   - `tests/integration` **전체를 한 번에** 실행한다. 분할 실행으로 대체하지 않는다. 실패하면 원문과 수를 그대로 보고한다.
   - Ruff·포맷 검사
   - `scripts/check-operable-system.py`(폴링 빌드) 1회. 최신 `claude/bold-feynman-rpbxfw` 화면(`f1f0ec4`)을 병합한 상태에서 실행한다.

## 4. 하지 않을 것

- 실적·정산·생산 사건·작업지시·알람·감사 삭제
- 3시간 미만 보관 허용, 중복 판정 완화
- 공개 DB·Vercel·운영 runtime 변경

## 5. 보고

`reports/CX-41.md`: 변경, 문서 변경 요지, 정상 상태 용량 수치, 시험 결과(통과·실패 수). `docs/DEVELOPMENT_STATUS.md`에 한 줄.
