# OPS-03 — 공개 운전 기록 1회 초기화 (설정값 유지)

- 발행: 2026-10-26, Claude
- 사용자 결정(2026-10-26): 23차 완성 전에 공개 DB 용량 응급 처치로 "(나) 지금 운전 기록 한 번 지우기", 설정값은 남김("1. 남겨").
- 배경: 공개 DB 386.7MB, 26.1MB/h 증가. 10-26 10:45Z 기준 0.5GB까지 약 4.3시간(`reports/evidence/CX-44-claude-public-db.md`).
- 수행: Codex(runtime 정지·로컬 저장소 초기화·재시작)와 Claude(공개 DB 백업·초기화)가 **아래 순서대로** 번갈아 한다. 단계마다 상대에게 완료를 알린 뒤 다음 단계로 간다.
- 이 작업은 23차 세션 모델을 1회 손으로 적용하는 것이다. 23차 구현을 대신하지 않는다.

## 남기는 것 / 지우는 것 (공개 DB)

- **남김:** users, roles, user_roles, guest_viewer_accounts, auth_audit, sites, lines, assets, tag_definitions, source_streams, item_master, item_audit, recipe_versions, mes_management_audit 중 공정 설정 기록, downtime_reason_history, alarm_channels, alarm_rule_versions, line_queue_policies, registration_id_counters, alembic_version
- **지움:** 트렌드 원시·분 집계, 수집 원본·영수증·시각 근거·probe·보관 종료 기록, 사건·실적·실행·정산, 명령, 작업지시·배포·취소·실행 연결·예약, 대기열 항목·요청·감사, 알람 발생·조치·판단, 로그인 세션
- **재설정:** `stream_progress`의 검증·수신 순번을 0으로, 보관 정리 커서를 처음으로, 수집원 충돌·대조 필요 표시를 false로 되돌린다.
- SQL: `deploy/neon-http/session-reset-0024.json`(49문, 한 트랜잭션). 0024 스키마 임시 DB에서 실행을 확인했다. 끝난 뒤 비활성 트리거는 0개다.

## 순서

1. **Codex — runtime 정지**
   - 게이트웨이를 먼저 정상 종료하고, 그다음 시뮬레이터를 정상 종료한다.
   - 정지 전에 진행 중 실행·미결 명령이 없는지 확인한다. WO-T1은 10-26 10:01Z에 SETTLED됐다.
   - 정지 시각, 마지막 capture_seq, 시뮬레이터 Line.EventSeq를 기록해 Claude에게 알린다.
   - 두 SQLite 파일은 지우지 말고 **사본을 따로 보관**한다.
2. **Claude — 공개 DB**
   - 전체 논리 백업(작업 공간에만 보관, 저장소에 넣지 않음)
   - `session-reset-0024.json` 적용
   - 결과 확인: 지운 표 0행, 설정 표 행 수가 그대로인지, DB 크기
   - Codex에게 완료를 알린다.
3. **Codex — 로컬 저장소 초기화.** 공개 DB 순번이 0부터 다시 시작하므로, 설비 쪽 번호도 처음부터 시작해야 한다.
   - **게이트웨이:** 빈 대기열 파일로 시작한다. capture_generation은 공개 등록값 `1`, gateway_id는 `demo-packaging-gateway-v1`과 같아야 한다. 다르면 시작하지 말고 알린다.
   - **시뮬레이터:**
     - 사건·실행·물품·명령 journal 같은 운전 기록을 비운다. 그래서 사건 순번(Line.EventSeq)이 1부터 다시 시작해야 한다.
     - 수집원 정체성(`demo-packaging-source-v1` / `events-main`), 사용자, 인증서·신뢰 목록, 설정값은 유지한다.
     - 지원되는 명령이 없으면 같은 정체성으로 새로 bootstrap하는 방법을 쓰고, 어떤 방법을 썼는지 보고한다.
   - 비밀번호·토큰은 기록하지 않는다.
4. **Codex — 재시작과 확인**
   - 시뮬레이터 시작 → 복구 5항목 확인 → REMOTE/AUTO → 게이트웨이 시작
   - 공개 화면에서 수집 상태가 "최근 수집 확인"이 되는지 확인한다.
   - 공개 DB `stream_progress`가 1부터 증가하는지, 수집원 충돌·대조 필요 표시가 false인지 확인한다. 이 확인은 Claude가 해도 된다.
   - 재시작 시각과 결과를 알린다.
5. **Claude — 공개 확인**
   - 게스트 화면 관찰, DB 크기 추세 재측정
   - 기존 품목·공정 설정이 남아 있는지 확인한다.
   - 결과를 `docs/DEVELOPMENT_STATUS.md`에 기록한다.

## 되돌리기

- 2단계 이전: 아무것도 바뀌지 않았다. 그대로 재시작하면 된다.
- 2단계 이후: 공개 DB 백업과 두 SQLite 사본으로 이전 상태를 복원할 수 있다. 단, 셋을 함께 되돌려야 한다. 한쪽만 되돌리지 않는다.
- 결과 불명 명령을 새 ID로 다시 실행하지 않는다.
