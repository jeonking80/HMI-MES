# 작업지시서 — KULS 보강 2차 (Codex 백엔드)

- 발행: 2026-10-23, Claude
- 수행: Codex (백엔드·운영). UI/UX는 Claude가 맡는다.
- 기준: `main` `68e1f7c` 이후. 1차 지시서 [CODEX_KULS_보강_v1.md](CODEX_KULS_보강_v1.md)의 공통 규칙(§2), 로컬 검증 명령(§4), 보고 형식(§5), 계약 변경 절차(§6)를 그대로 따른다.
- 전제: 개인 교육용 프로젝트다. CI는 쓰지 않고 로컬 검증만 한다.

## 0. 현재 상태 (2026-10-23)

| 항목 | 상태 |
|---|---|
| CX-01~07 | `main` 반영 |
| CX-12 공개 보관 환경변수 | `main` 반영. Vercel production에 `HMI_TREND_RAW_HOURS=6`, `HMI_TREND_MINUTE_DAYS=14` 설정됨 |
| CX-08 챗봇 서버 | Claude가 구현해 `main` 반영(`apps/api/assistant.py`). 이후 백엔드 유지보수는 Codex 소유 |
| OPS-01 공개 이관 | 완료. 공개 Neon `0017_guest_viewer`, 권한 적용, 게스트 활성화. 결과: [reports/OPS-01.md](reports/OPS-01.md) |
| CX-09·10·11 | **미구현** → 이 지시서 |

공개 DB는 이제 head17이다. 1차 지시서의 "공개 배포(head14)에서는 false" 같은 문장은 이 지시서 기준으로 읽는다.

## 1. 소유 범위

| 소유 | 경로 |
|---|---|
| Codex | `apps/api/**`(마이그레이션 포함), `apps/gateway/**`, `apps/simulator/**`, `contracts/**`, `deploy/**`, `tests/**`(웹 시험 제외), `docs/architecture/**`, `docs/work-orders/reports/CX-*.md` |
| Claude | `apps/web/**`, `scripts/check-operable-system.py`, `scripts/capture_portfolio_media.py`, `README.md`, `vercel.json`의 빌드 명령 |
| 공동(편집 전 알림) | `docs/DEVELOPMENT_STATUS.md`(각자 절만), 이 지시서 |

- 브랜치: `codex/kuls-backend-v2`. `main`에 직접 푸시하지 않는다.
- 화면 계약(JSON 필드)은 **추가만** 자유롭다. 이름·의미·단위 변경은 1차 §6 절차를 따른다.
- Claude 소유 파일에 필요한 변경(예: 검사 스크립트 인자)은 보고서에 적으면 Claude가 반영한다.

## 2. 작업 목록과 순서

| 순서 | ID | 내용 | 위험 검토 |
|---|---|---|---|
| 1 | CX-10 | `/auth/config`에 `analytics` 추가 | medium |
| 2 | CX-09 | 같은 호스트 실행의 시간 근거 | **high** |
| 3 | CX-13 | 챗봇 서버 보강 | medium |
| 4 | CX-11 | 서로 다른 호스트의 시간 근거(측정 왕복) | **high** |
| 5 | CX-14 | 공개 배포 실데이터 경로와 용량 정책 | medium |
| 6 | CX-15 | 수락 시험 T01~T12·성능·장시간·복구 현황 정리 | medium |

## CX-10. 분석 기능 제공 여부를 서버가 알림

1차 지시서의 CX-10 요구를 따른다. 다음만 바꾼다.
- `GET /auth/config` 응답에 `"analytics": true|false`를 **추가**한다. 현재 응답은 `{"guest_viewer": bool, "assistant": bool}`이며, 두 필드는 유지한다.
- true 조건: 분석 경로(트렌드·보고·SPC·대시보드·정지 사유)가 등록되어 있고, DB 스키마가 이를 지원하는 리비전(`TREND_REVISIONS`)이다.
- 리비전 확인은 앱 생성 시 1회 또는 짧은 캐시로 한다. 매 요청마다 DB를 조회하지 않는다. DB 오류 때는 false를 반환하고 오류 내용을 노출하지 않는다.

**완료 기준:**
- head14·head17 각각에서 응답값 시험
- Vercel 서버리스 팩토리에서도 같은 값
- 기존 `/auth/config` 시험 갱신

UI 전환은 Claude가 한다(UX-09).

## CX-09. 같은 호스트 실행의 시간 근거

1차 지시서 CX-09의 요구·완료 기준을 그대로 따른다. 추가 사항:
- `scripts/check-operable-system.py`에 필요한 인자를 보고서에 적는다. Claude가 반영하고 `--analytics-dir` 결과를 함께 확인한다.
- 완료 판단 기준은 실제 결합 결과다.
  - 트렌드 GOOD 값이 존재
  - 실행 보고의 가동 시간이 0보다 큼
  - 불명은 근거 없는 구간에만 남음
  - 결과 수치를 보고서에 기록

## CX-13. 챗봇 서버 보강

현재 구현: `POST /api/v1/assistant/chat`. 기존 조회 API를 호출자 세션 쿠키로 프로세스 안에서 GET 호출하는 방식이며, 쓰기 경로는 없다. 시험은 `tests/integration/test_assistant.py`에 있다.

요구:
1. **알람 조회 도구 추가.** `/api/v1/alarms`는 `asset_id`가 필수라 현재 도구에서 뺐다. 라인의 설비별 활성 알람 요약을 읽는 조회 도구를 추가한다. 기존 조회 API를 쓰거나, 읽기 전용 요약 API를 새로 만든다.
2. **요청 제한의 서버리스 한계.** 현재 제한기는 프로세스 메모리다. Vercel 인스턴스가 여러 개면 합계 제한이 느슨해진다. 다음 중 하나를 고르고 근거를 적는다.
   - DB 기반 창(window)을 둔다. 게스트 `guest_rate_windows` 방식을 참고하고, 마이그레이션이 필요하면 `0018` 이후로 한다.
   - 현재 방식을 유지하고 그 한계를 문서화한다.
3. **게스트 사용 정책.** 게스트도 챗봇을 쓸 수 있다(read 권한). 공유 무료 키를 보호하기 위해 게스트 전용의 더 낮은 한도를 둘지 결정하고 시험한다.
4. **시간 초과.** Vercel 함수 최대 30초(`vercel.json`) 안에서 끝나도록 전체 시한을 둔다. 시한을 넘으면 502가 아니라 "시간 초과"를 구분하는 응답 코드나 본문으로 알린다. 응답 형식 추가는 Claude에게 알린다.
5. 프롬프트·응답 원문·키를 로그에 남기지 않는다는 성질을 시험으로 고정한다(caplog).

**계약(UI가 사용):**
```
POST /api/v1/assistant/chat
요청: {"messages":[{"role":"user"|"assistant","content":"…"}](1~12개, 마지막은 user), "scope":{line_id,source_id,stream_id}|null}
응답 200: {"answer": str, "evidence":[{"tool","label","status","generated_at"?}], "model": str, "generated_at": str}
오류: 401/403(세션·CSRF), 422(형식), 429(한도), 502(공급자 실패)
```
필드 추가는 자유이고, 기존 필드는 유지한다.

## CX-11. 서로 다른 호스트의 시간 근거

1차 지시서 CX-11의 설계 요구·완료 기준을 그대로 따른다. 마이그레이션은 `0018_*`부터 쓴다.

공개 Neon 적용은 OPS-01과 같은 방식으로 한다.
1. `scripts/render_neon_migration.py` 패턴으로 HTTP 배치를 생성한다.
2. 로컬에서 Alembic 결과와 카탈로그가 같은지 검증한다.
3. 권한 SQL을 만든다.
4. 적용은 별도 운영 지시서(OPS-02)로 한다. Codex가 직접 공개 DB를 바꾸지 않는다.

## CX-14. 공개 배포 실데이터 경로와 용량 정책

**배경:**
- 공개 사이트에는 설비 데이터를 보내는 게이트웨이가 상시 연결되어 있지 않다. 지금은 2026-10-23 01:00~02:03 UTC의 수집분만 있다.
- 원본 배치는 실측 건당 약 6KB, 1초 주기면 하루 약 0.5GB다. 무료 DB 0.5GB를 하루 안에 넘는다. 근거: `docs/architecture/TREND_HISTORY.md`.

**요구:**
1. 사용자가 로컬 PC에서 시뮬레이터·게이트웨이를 공개 API에 연결해 짧게 시연하는 실행 방법을 문서로 만든다. 문서에 넣을 것:
   - 필요한 자격 증명을 어디에 두는지(비밀값 자체는 적지 않음)
   - 시작·중지 명령
   - 예상 용량
2. 원본 배치 증가를 제한하는 방법을 제안한다. 감사 근거를 지우지 않는 범위에서 고른다. 예: 시연 프로필의 수집 주기, 압축, 보관 정책. 원본 삭제가 필요하면 기준 문서 변경으로 보고 사용자 결정을 요청한다.
3. 공개 DB 사용량을 확인하는 읽기 전용 명령을 운영 문서에 추가한다.

## CX-15. 수락 시험·비기능 검증 현황

1. 기획서·개발계획서의 수락 시험 T01~T12와 성능·장시간·복구 완료 조건을 표로 정리한다. 각 항목에 대해 다음을 적는다.
   - 현재 자동 시험이 있는지(파일·시험 이름)
   - 마지막 실행 결과
   - 미실행·실패·차단 여부
2. 없는 시험은 우선순위와 함께 구현 계획을 제안한다. 큰 시험(장시간·성능)은 실행 시간과 필요한 환경을 적는다.
3. 결과는 `docs/DEVELOPMENT_STATUS.md`의 `### Codex 백엔드 보강` 절과 `docs/work-orders/reports/CX-15.md`에 남긴다. **미실행을 통과로 적지 않는다.**

## 3. Claude(UI/UX) 쪽 작업 — 참고

| ID | 내용 | 의존 |
|---|---|---|
| UX-09 | 분석 메뉴를 `/auth/config.analytics`로 열기. 그 전까지는 공개 빌드에 `VITE_ANALYTICS_PAGES=enabled` 적용 | CX-10 |
| UX-10 | 챗봇 화면(대화, 근거 표시, 조회 전용 안내, 오류·한도 상태) | CX-08 서버(완료), CX-13 |
| UX-11 | `check-operable-system.py`에 CX-09 인자 반영과 결합 확인 | CX-09 |
| UX-12 | 챗봇·시간 근거 반영 후 README 스크린샷 갱신 | UX-10, CX-09 |

## 4. 보고

- 작업마다 `docs/work-orders/reports/CX-xx.md`를 남긴다(1차 §5 형식).
- 다음 내용을 기록한다.
  - 실행한 명령
  - 통과 수
  - 소요 시간
  - 실패 원문
  - 위험 검토(high 대상) 결과
