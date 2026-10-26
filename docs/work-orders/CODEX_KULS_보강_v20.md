# 작업지시서 — KULS 보강 20차 (Codex: FIFO 복구 시간 회귀와 보관 종료 ID 저장량)

- 발행: 2026-10-26, Claude
- 수행: Codex. 브랜치 `codex/kuls-backend-v3`(`a5e2dd0` 이후)에서 이어서 한다.
- 위험도: **high**. 수집 경로 성능과 중복 판정 저장 구조를 다룬다.
- **14~19차는 아직 main에 병합하지 않았다.** 이 지시서의 1절이 통과해야 병합한다.
- PR·병합·공개 이관·배포·Vercel 환경변수는 Claude가 한다. Codex는 푸시하고 커밋 해시만 알린다. UI 파일은 수정하지 않는다.

## 1. FIFO 복구 시간 회귀 (병합 차단)

### Claude 실행 결과

- 실행 위치: Claude 컨테이너 1대. 병합본 `a5e2dd0`과 main `ea5ac0e`을 같은 컨테이너에서 실행했다.
- **통과:** `tests/unit` 787, 화면 시험 99, Ruff·포맷, `check-operable-system.py` PASS
- **`tests/integration` 전체 1회:** 1 failed, 1876 passed

실패한 시험은 `tests/integration/test_gateway_fifo_recovery.py::test_1800_capture_fifo_drain_with_live_collection`이다.

| 코드 | 결과 | 걸린 시간 | API ingest 호출 / 합계 초 | 300초 시점 남은 배치 |
|---|---|---:|---:|---:|
| main `ea5ac0e` | **통과** | 289.6초 | 2,091 / 223.0 | 0 |
| 병합본 1차 | 실패 `failed_deadline` | 300.2초 | 2,058 / 225.5 | 93 |
| 병합본 2차 | 실패 | 300.5초 | 2,073 / 225.9 | 94 |

- 이 컨테이너는 CX-19 기록(205.8초 통과)보다 전체적으로 30~50% 느리다. 바꾸지 않은 쿼리도 마찬가지다.
- 그래도 **같은 컨테이너에서 main은 통과하고 병합본은 두 번 모두 실패**한다. 14~19차 사이에 늘어난 처리 비용이 원인이다.
- 프로파일에서 새로 생겼거나 늘어난 항목(main 근거 대비):
  - `record_capture_clock` 7.4초 → 14.0초
  - `ingest_batches.SELECT` 호출이 2배(4,116회)
  - 새 `other.(SELECT` 3.9초, `other.SET` 4,116회 1.6초(요청마다 시간 제한 설정으로 추정)
- Claude의 프로파일 JSON(측정용 환경변수 `HMI_RECOVERY_PROFILE`)은 임시 경로에만 있다. Codex가 같은 방식으로 직접 다시 만든다.

### 할 일

1. 원인 커밋을 찾는다(14~19차 중). `HMI_RECOVERY_PROFILE`로 main과 병합본을 **같은 기계에서** 비교한다.
2. 수집 1건당 비용을 main 수준 이하로 되돌린다.
   - 보관 정리·압축·시계 근거·중복 판정의 보호 조건은 약하게 하지 않는다.
   - 요청마다 반복하는 설정·조회를 줄인다. 예: 트랜잭션 단위 `SET LOCAL`을 한 번만, 같은 행 재조회 제거.
   - 보관 정리는 수집 응답 경로 밖에서 실행되는지 확인한다.
3. 시험 기준(1,800건, 300초)은 바꾸지 않는다. 같은 기계에서 main 대비 걸린 시간을 표로 보고한다.

## 2. 보관 종료 ID 저장량 (CX-41 후속)

CX-41 측정에서 `ingest_retired_batch_chunks`는 **297행이 약 196MB**를 차지했다. 정리할 때마다 최대 256개 ID 배열 행을 다시 쓰면서 죽은 공간과 GIN 인덱스가 쌓인 것으로 보인다. 정상 운영에서도 하루 수 MB씩 늘어난다.

1. 한 번 쓴 청크 행은 다시 쓰지 않는다. 정리 회차마다 새 행을 넣는 방식 등을 쓴다.
2. ID가 UUID 형식이면 16바이트 이진으로 저장하고, 그 밖의 형식만 텍스트로 저장한다. 정확한 ID 판정(410 `RETENTION_EXPIRED`)은 그대로 둔다. 해시나 확률 필터로 대체하지 않는다.
3. **용량 측정 방법을 바꾼다.** 한꺼번에 적재하면 파일이 최대 크기에 머물러 정상 상태를 보여 주지 못한다. 시간을 압축해서 적재와 정리를 번갈아 돌린다(예: 1Hz 24시간 분량, 정리 주기 1분 상당).
   - 보관 3시간에서 각 테이블의 최종 파일 크기와 실제 데이터 크기를 보고한다.
   - 알람 채널 0개와 1개를 둘 다 측정한다.
   - 하루 증가량과 0.5GB 도달 예상 일수를 적는다.
4. 이관은 추가 방식(필요하면 0024)으로 하고, Neon HTTP 배치·권한 배치와 `cmp` 일치를 확인한다.

## 3. 시험

- 신규·변경 시험
- `tests/unit` 전체
- **`tests/integration` 전체를 한 번에.** 실패하면 원문과 수를 그대로 보고한다.
- FIFO 시험을 main·결과 커밋으로 각각 1회(같은 기계)
- Ruff·포맷
- `check-operable-system.py`(폴링 빌드) 1회

## 4. 하지 않을 것

- FIFO 기준(1,800건, 300초) 완화, 시험 생략
- 중복 판정·보관 조건 완화
- 공개 DB·Vercel·운영 runtime 변경

## 5. 보고

`reports/CX-42.md`. `docs/DEVELOPMENT_STATUS.md`에 한 줄.

## 6. 추가 — 대기열이 작업 종료 뒤 다음 작업을 연결하지 않음 (2026-10-26, Claude 실측)

- **재현:** `scripts/check-operable-system.py --extended-dir <dir>` 실행. 실제 시뮬레이터·게이트웨이·API·브라우저에서 E1(자동 시작·목표 자동 종료)과 E2(운전 중 속도 변경)는 통과한다. E3에서 실패한다.
- **E3 절차:** 직전 실행이 SETTLED되고 라인이 STOPPED·ENDED인 상태에서, 배포된 `queue-1`, `queue-2`를 대기열에 넣고 `auto_advance=true`로 저장한다. 60초 동안 두 항목 모두 `WAITING`이고 `blocked_reason=null`이다.
- **원인:** `apps/api/line_queue.py`의 `_safe_values`가 `Sealer.Ready=True`를 요구한다. 실제 시뮬레이터는 실행이 끝나면 `Sealer.Ready=False`가 된다. 실측값은 Line.State 0, WIP 0, 각 구간 정지·속도 0, 모드 REMOTE/AUTO, 수집 CAPTURE_FRESH, 하트비트 GOOD이다. 그래서 `advance_line_once`가 아무 기록 없이 `None`을 반환하고, 대기열은 영원히 멈춘다. 같은 상태에서 사람이 직접 하는 실행 연결은 성공한다.
- **할 일:**
  1. 대기열 연결 조건을 수동 실행 연결(`bind_work_order`)의 조건과 맞춘다. 종료 직후 실제 물리 상태(예열 전 Sealer.Ready=False)에서도 다음 LOAD가 나가야 한다. 안전 조건(정지·WIP 0·모드·하트비트·수집 신선도)은 유지한다.
  2. 조건을 만족하지 못해 진행하지 않는 경우에는 이유를 `blocked_reason`이나 읽기 응답에 드러낸다. 조용히 멈추지 않는다.
  3. 실제 시뮬레이터 상태로 이 경로를 재현하는 통합 시험을 추가한다. `check-operable-system.py --extended-dir`의 E3도 통과해야 한다(이 스크립트는 Claude 소유이며 실행만 한다).
