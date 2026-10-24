# 작업지시서 — KULS 보강 7차 (Codex: 생산 START를 포함한 공개 실연 1회)

- 발행: 2026-10-24, Claude
- 수행: Codex. 브랜치 `codex/kuls-backend-v3`(`9a0db0c` 이후). 시작 전에 최신 `main`을 병합한다.
- **원칙: 핵심만 한다.** 실연은 1회만 한다. 코드 수정·반복 실행·장시간 시험은 하지 않는다. UI·README·`vercel.json`·`scripts/check-operable-system.py`는 수정하지 않는다.

## 1. 배경 (Claude 공개 확인, 2026-10-24)

6차 실연(07:05~07:10Z)에서 확인된 것과 남은 것:

| 항목 | 결과 |
|---|---|
| 수집·전송 | 290건, `verified_through_seq=latest=7`, gap 없음 |
| 시간 근거 | GOOD 288 / UNKNOWN 2(`NO_CLOCK_POLICY`, 시작 직후) |
| 트렌드 API | GOOD 284점, TIME_UNKNOWN 2점 |
| 대시보드 | 정지 289초가 측정값으로 잡힘. **가동 0초** |
| 생산 수량 | **사건 0건.** START를 하지 않아 대조할 실행이 없음 |
| DB 용량 | 41MB |

남은 OPS-02 항목은 **가동 시간 > 0**과 **원래 실행의 수량 대조**다. 둘 다 생산 START가 있어야 확인할 수 있다.

## 2. 할 일

1. **실행 전 기록.** 공개 DB 용량과 `production_events`·`production_executions` 행 수를 읽기 전용으로 1회 기록한다.
2. **실연 1회, 약 10분.** 6차와 같은 runtime·SQLite·인증서·토큰으로 `serve --clock-measured-round-trip`을 실행한다.
   - 10분이면 가동 시간 > 0과 수량 대조를 확인하기에 충분하다. 예상 증가는 약 8~12MB로 무료 0.5GB 안이다.
   - SPC 관리한계는 완전한 1분 부분군 20개가 있어야 계산된다. 그래서 이번 데이터로는 SPC가 `INSUFFICIENT_DATA`로 나온다. 이번 실연의 판정 대상이 아니다.
   - 시뮬레이터가 `recovery_required`로 멈춰 있으면 6차와 같은 방식으로 `commission`을 1회 실행한다.
3. **생산 START.** 공개 업무 화면에서 정상 권한·Origin·CSRF를 통과해 작업지시 1건을 릴리스하고 START한다. 시작 명령이 START를 대신하지 않는다([PUBLIC_DEMO_RUN.md](../architecture/PUBLIC_DEMO_RUN.md) 63행).
   - 수행자는 사용자(운영자 계정)다. Codex가 대신할 경우 사용자 승인을 받는다. 계정 비밀번호는 채팅·문서·로그에 남기지 않는다.
   - 목표 수량은 10분 안에 끝나지 않을 만큼 넉넉히 잡는다. 끝나기 전에 정상 STOP으로 마친다. 결과 불명이 생기면 새 ID로 다시 START하지 않고 그대로 기록한다.
4. **실행 후 기록(읽기 전용 1회).**
   - DB 용량, 증가량
   - 이번 `execution_id`, 서버 집계 양품·불량·폐기
   - 시뮬레이터 측 같은 실행의 수량
   - 두 값의 일치 여부
   - `stream_progress`와 `stream_gaps`
   - capture 오류 클래스·코드별 횟수
5. **실패하면 멈춘다.** 사유 코드와 함께 보고한다. 자동 재시도나 두 번째 실연은 하지 않는다.

## 2-1. 1차 시도 중단 원인과 클릭 순서 (Claude 확인, 2026-10-24)

1차 시도(`1ce7b11`)는 배포 뒤 `#binding-prepare`가 35초 동안 비활성이라 중단됐다.

- 공개 DB의 마지막 수집(seq 4609)은 연결 조건을 모두 충족했다: `Line.State=0`, `ControlMode=1`, `OperationMode=1`, `WipCount=0`, `ActiveExecutionId=""`, `ControlVersion=10`. 설비 쪽 원인이 아니다.
- 화면은 배포 확인 기록(`workflow.attempt`)이 남아 있으면 실행 연결을 막는다. 이때 표시되는 사유는 "기존 요청의 원래 기록과 결과를 먼저 확인해 주세요."다(`apps/web/src/WorkOrderBinding.tsx` 13행). 같은 작업지시의 잠금도 `#workflow-acknowledge`를 눌러야 풀린다. 오래된 화면 기준 요청을 막는 의도된 동작이다.
- 1차 시도에서 `#workflow-acknowledge`를 누른 기록이 없다. 이것이 가장 유력한 원인이다.

검증된 순서(`scripts/check-operable-system.py` 429~470행)를 그대로 따른다.

1. `[data-release-order=<ID>]` → `#release-submit`
2. `#workflow-status`의 `data-state="confirmed"` 대기
3. **`#workflow-acknowledge` 클릭(배포 기록 확인 · 다음 업무 준비)**
4. 작업지시 목록의 "목록 새로 조회" → `[data-bind-order=<ID>]`
5. `#binding-prepare` 활성 대기 → 클릭 → `#binding-submit`
6. `#load-command-status`가 `SUCCEEDED`가 될 때까지 대기(최대 30초). 이어서 예열로 READY가 될 때까지 최대 45초 기다린다.
7. 라인 모니터링의 `#command-START` 클릭. 클릭 전에 `#command-panel`의 `data-control-version`이 최신값인지 확인한다(5차 지시서와 같다).

이번에 만든 작업지시(`000-v7-order-20261024T075220304749Z`)는 이미 RELEASED다. 새로 만들지 말고 4번부터 이어서 진행한다. 버튼이 또 비활성이면 그 아래의 사유 문구를 그대로 기록하고 멈춘다.

## 2-2. 2차 시도 중단 원인 (Claude 확인, 2026-10-24)

2차 시도(`607a757`)의 사유는 "전체 48개 태그의 최신 정상 품질과 원본 시각을 확인해야 합니다."였다.

- 공개 DB의 2차 구간(08:00:38~08:01:21Z, 44건)은 48개 태그가 모두 있었다. status 0, 값 있음, 원본 시각 차 2초 이내로 **원본 데이터는 정상**이었다.
- 화면은 수집 시각(`acquired_at`)·저장 시각·화면 발행 시각이 모두 3초 이내여야 제어를 허용한다(`apps/web/src/SnapshotPanel.tsx` 91~95행). 오래된 값으로 명령하지 않게 하는 안전 조건이며, 이번에도 완화하지 않는다.
- `Line.State` 기준으로 수집 → 서버 저장 지연을 시간순으로 보면 다음과 같다(ms).
  ```
  431 619 555 536 638 2211 2005 4246 3675 3239 5573 5247 4866 … 2052 1601 1074 825 410 525 614 597 604
  ```
  시작 약 6초 뒤부터 약 35초 동안 전송 적체로 지연이 2~5.5초로 올랐다. 그 뒤 0.4~0.6초로 회복했다. 이 적체는 초기 `GatewayReadError`·`INFLIGHT_PRESSURE` 구간과 겹친다.
- 6차 실연(5분)은 같은 초기 튐(최대 2.2초) 뒤로 내내 약 0.3~0.7초였다.
- **결론:** 시작 직후의 일시적 적체 구간에 버튼을 기다리다 시한(35초)이 끝났다. 끝 무렵에는 지연이 이미 회복돼 있었다.

조치(코드 수정 없음):

1. runtime 시작 뒤 **약 60초 안정화 대기**를 하고 나서 실행 연결을 시작한다.
2. `#binding-prepare` 활성 대기 시한을 **최대 120초**로 한다. 지연이 3초 미만으로 돌아오면 버튼은 스스로 활성화된다.
3. 120초 뒤에도 비활성이면 그때의 사유 문구와 시각을 기록하고 멈춘다.

## 3. 하지 않을 것

- 게이트웨이·시뮬레이터 SQLite 초기화, 새 source·generation, 한도 완화
- 이관·권한·Vercel 설정 변경, 재배포
- `hmi_app` 금지 DML 실측 재시도. 연결이 없으면 계속 미실행으로 둔다.

## 4. 보고

`reports/OPS-02.md`에 "7차 실연" 절로 덧붙인다.
- 시작·종료 UTC
- 실행 전후 수치
- 수량 대조 결과
- 오류 횟수

`docs/DEVELOPMENT_STATUS.md`에 한 줄 요약을 남긴다. 미실행·실패를 통과로 적지 않는다. 공개 화면 확인(가동 시간 > 0, 트렌드, 대시보드)과 README 대시보드 스크린샷 교체는 Claude가 한다. README의 SPC는 미리보기로 유지한다.
