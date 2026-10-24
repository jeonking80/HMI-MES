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
2. **실연 1회, 약 20분.** 6차와 같은 runtime·SQLite·인증서·토큰으로 `serve --clock-measured-round-trip`을 실행한다.
   - 20분으로 정한 이유: README의 대시보드·SPC 미리보기를 실제 화면으로 바꾸는 데 쓸 데이터 기준이다. 예상 증가는 약 16~24MB로 무료 0.5GB 안이다.
   - 시뮬레이터가 `recovery_required`로 멈춰 있으면 6차와 같은 방식으로 `commission`을 1회 실행한다.
3. **생산 START.** 공개 업무 화면에서 정상 권한·Origin·CSRF를 통과해 작업지시 1건을 릴리스하고 START한다. 시작 명령이 START를 대신하지 않는다([PUBLIC_DEMO_RUN.md](../architecture/PUBLIC_DEMO_RUN.md) 63행).
   - 수행자는 사용자(운영자 계정)다. Codex가 대신할 경우 사용자 승인을 받는다. 계정 비밀번호는 채팅·문서·로그에 남기지 않는다.
   - 목표 수량은 20분 안에 끝나지 않을 만큼 넉넉히 잡는다. 끝나기 전에 정상 STOP으로 마친다. 결과 불명이 생기면 새 ID로 다시 START하지 않고 그대로 기록한다.
4. **실행 후 기록(읽기 전용 1회).**
   - DB 용량, 증가량
   - 이번 `execution_id`, 서버 집계 양품·불량·폐기
   - 시뮬레이터 측 같은 실행의 수량
   - 두 값의 일치 여부
   - `stream_progress`와 `stream_gaps`
   - capture 오류 클래스·코드별 횟수
5. **실패하면 멈춘다.** 사유 코드와 함께 보고한다. 자동 재시도나 두 번째 실연은 하지 않는다.

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

`docs/DEVELOPMENT_STATUS.md`에 한 줄 요약을 남긴다. 미실행·실패를 통과로 적지 않는다. 공개 화면 확인(가동 시간 > 0, 트렌드, 대시보드·SPC)과 README 스크린샷 교체는 Claude가 한다.
