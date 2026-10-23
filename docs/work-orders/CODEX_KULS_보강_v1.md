# 작업지시서 — KULS 지원용 보강 (백엔드·데이터·실행 환경)

- 발행: 2026-10-23, Claude (주 에이전트·UI/UX 담당)
- 수행: Codex (IDE 에이전트)
- 기준 브랜치: `claude/bold-feynman-rpbxfw`
- 작업 브랜치: `codex/kuls-backend` (기준 브랜치에서 분기)
- 기준 문서: `AGENTS.md`, `docs/HMI_MES_시스템_기획서_v1_0.md` §정지 시간과 정지 사유·§보고서, `docs/HMI_MES_개발계획서_v1_0.md` §5.5·§6, `contracts/*.md`

## 0. 목적

이 프로젝트는 KULS ICT 사업부(스마트팩토리·생산운영 웹 SaaS) 지원 포트폴리오다. KULS의 HMI 솔루션 페이지는 주요 기능을 **모니터링·알람·트렌드·레포팅** 네 가지로 소개하고, MES 페이지는 **실시간 생산 모니터링·불량률 추적·SPC·대시보드**를 내세운다. 현재 저장소에는 모니터링·알람만 있고 트렌드·레포팅·SPC·대시보드 데이터가 없다.

이 작업지시서는 그 공백을 메우는 **백엔드·데이터·실행 환경** 작업을 정의한다. 화면(UI/UX)은 Claude가 같은 계약을 기준으로 병렬 구현한다. 아래 응답 계약은 UI가 그대로 사용하므로, 바꿔야 하면 §6 절차를 따른다.

기존 기획서 범위 안의 항목(F08 정지·보고서, 트렌드, 정지 사유)을 우선 구현한다. SPC·대시보드 요약·게스트 조회·compose는 기획서에 없던 항목이므로 각 작업의 "기준 문서 영향"을 `docs/DEVELOPMENT_STATUS.md`에 기록한다.

## 1. 소유 범위와 브랜치 규칙

같은 파일을 동시에 고치지 않기 위해 소유 범위를 고정한다.

| 담당 | 수정 가능 | 수정 금지 |
|---|---|---|
| Codex | `apps/api/**`, `apps/gateway/**`, `apps/simulator/**`, `contracts/**`, `tests/**`, `scripts/*.py`·`scripts/*.sh`(아래 Claude 소유 제외), `deploy/**`, `compose.yaml`, `docs/architecture/**`, `docs/ACCEPTANCE_EVIDENCE.md`, `docs/work-orders/reports/**` | `apps/web/**`, `README.md`, `docs/ui/**`, `docs/work-orders/*.md`(이 문서), `AGENTS.md` |
| Claude | `apps/web/**`, `README.md`, `docs/ui/**`, `docs/assets/screenshots/**`, `scripts/capture_portfolio_media.py`, `docs/work-orders/*.md` | 위 Codex 소유 경로 |
| 공동 | `docs/DEVELOPMENT_STATUS.md` | 각자 자기 절(`### Codex 백엔드 보강`, `### Claude UI/UX 보강`)에만 **추가**한다. 남의 절은 고치지 않는다. |

- `AGENTS.md`는 사용자 승인 없이 누구도 바꾸지 않는다.
- Codex는 `codex/kuls-backend`에서 작업하고 **작업 하나(CX-xx)당 커밋 하나 이상**으로 나눠 푸시한다. rebase·force-push로 공유 이력을 바꾸지 않는다.
- 통합은 Claude가 기준 브랜치로 merge commit을 만들어 수행한다. 충돌이 나면 Claude가 해결하고 Codex에 알린다.
- CI는 만들지 않는다. 모든 검증은 로컬 명령으로 하고 실행 결과를 보고서에 남긴다.

## 2. 공통 규칙

`AGENTS.md`의 구현 불변 조건을 그대로 따른다. 이번 작업에서 특히 지킬 것:

1. **불명은 불명으로 둔다.** 통신 단절·시간 품질 불명·누락 구간을 0, 직전 값, 정상 상태, 가동 시간으로 채우지 않는다. 응답에 `MISSING`·`UNKNOWN` 구간을 명시적으로 내보낸다.
2. **확정과 잠정을 구분한다.** 정산(`run_reconciliations.status`)이 `SETTLED`가 아닌 실행의 수량은 "수신 기준 잠정값"으로 표시할 수 있게 상태를 함께 반환한다.
3. **원래 execution_id에 귀속한다.** 일일 보고에서 실행을 날짜로 나눌 때도 사건의 `execution_id`를 바꾸지 않는다.
4. **권한.** 조회 API는 모두 기존 `auth.authorized_transaction(..., permission="read", refresh_idle=False)`를 사용한다. 쓰기 API는 기존 쓰기 경로와 같은 Origin·CSRF·JSON·멱등 요청 ID 규칙을 따른다.
5. **직렬화 관례.**
   - UInt64 수량은 기존 `contracts.models.UInt64`처럼 **JSON 문자열**로 보낸다.
   - 시각은 UTC ISO 8601(`...Z`)로 보낸다.
   - 기간은 `[from, to)`(시작 포함·끝 제외)이고, 일일 경계는 Asia/Seoul 00:00이다.
6. **쿼리 검증.** 알 수 없는 쿼리 파라미터, 중복 파라미터, 범위 초과는 422로 거절한다(기존 `/api/v1/screen` 방식). 응답에는 `Cache-Control: no-store`를 붙인다.
7. **배포 경로.** 새 사용자 API는 `create_user_app`에 등록해 로컬 실행기와 Vercel(`apps/api/serverless.py`)에 모두 노출되게 한다. `vercel.json`은 이미 `/api/*`를 함수로 보내므로 수정하지 않는다. 등록 여부는 `tests/integration/test_vercel_app.py`에 시험을 추가해 확인한다.
8. **비밀값.** 비밀번호·토큰·DSN·LLM 키를 소스·문서·로그·시험 출력에 남기지 않는다.
9. **추론 수준.** `AGENTS.md`의 서브에이전트 규칙(모델·추론 수준)을 따른다. 구현은 medium으로 한다. DB 마이그레이션(CX-01·CX-03)과 인증·권한 변경(CX-03·CX-06)은 구현 뒤 **별도의 high 검토**를 받고 결과를 보고서에 적는다.
10. **문서 완화 금지.** 기획서 수치(예: 원시값 7일·1분 집계 90일 보관)를 배포 환경 제약 때문에 줄여야 하면, 기본값은 그대로 두고 **배포 프로필 설정**으로만 줄인다. 그 이유와 영향을 문서에 기록한다.

## 3. 작업 목록과 순서

| ID | 작업 | 기획 근거 | 선행 | 위험 검토 | 우선 |
|---|---|---|---|---|---|
| CX-01 | 아날로그 트렌드 저장·조회 API | 기획 §보고서 "설비 트렌드", 계획 §5.5 | — | high (마이그레이션) | 1 |
| CX-02 | 작업별 실적·상태 타임라인·일일 보고·CSV | F08, 기획 §보고서 | — | medium | 1 |
| CX-03 | 정지 사유 입력·이력 | 기획 §정지 시간과 정지 사유 | CX-02 | high (마이그레이션·권한) | 2 |
| CX-04 | SPC X̄-R 관리도·추세 경고 | 신규(KULS MES의 SPC 대응) | CX-01 | medium | 2 |
| CX-05 | 대시보드 요약 API | 신규(KULS MES의 대시보드 대응) | CX-01, CX-02 | medium | 2 |
| CX-06 | 게스트 조회 세션(로그인 없이 둘러보기) | 신규(공개 데모 접근성) | — | high (인증) | 1 |
| CX-07 | `docker compose` 한 명령 로컬 실행 | 계획 §3.2 "Docker Compose" | — | medium | 3 |
| CX-08 | 조회 전용 운영 챗봇 | 신규(공고의 챗봇·AI 에이전트) | CX-02, CX-04, CX-05 | — | **보류** |

- **1차(병렬):** CX-01, CX-02, CX-06
- **2차:** CX-03, CX-04, CX-05
- **3차:** CX-07
- **CX-08은 사용자가 LLM 공급자·비용을 승인하기 전에는 착수하지 않는다.**

---

## CX-01. 아날로그 트렌드 저장·조회 API

### 목적
9개 아날로그 태그(`contracts/tags/tags.v1.json`의 `analog_history: true`)의 기간 추세를 조회한다. KULS HMI의 "트렌드" 기능과 기획서의 설비 트렌드 보고서에 대응한다.

### 범위
1. **마이그레이션 `0015_analog_history`**
   - 원시 1초 표본 테이블과 1분 집계 테이블을 만든다. 테이블 이름은 Codex가 정한다.
   - 1분 집계 정책은 기존 순수 함수 `apps/api/minute_aggregation.py`(`POLICY_VERSION = "utc-second-latest-capture-v1"`)를 그대로 사용한다. 새 집계 정책을 만들지 않는다.
2. **저장 시점과 원자성**
   - 원시 표본은 수집 배치를 커밋하는 **같은 트랜잭션**에서 저장한다(`apps/api/ingestion.py`). 배치 중복 제거·커서와 원자적이어야 한다.
   - 같은 배치를 다시 받아도 표본이 중복 저장되지 않아야 한다.
3. **1분 집계 갱신**
   - 서버리스(Vercel)에는 백그라운드 작업이 없다. 따라서 수집 경로에서 **완료된 분**을 멱등 upsert하거나, 조회 시 부족한 분만 계산해 저장하는 방식 중 하나를 고른다.
   - 지연·재전송 배치가 과거 분에 들어오면 그 분을 다시 계산한다.
   - 키는 (scope, tag, minute, policy_version)로 한다.
4. **보관과 용량**
   - 기본값은 원시 7일·1분 집계 90일(계획 §5.5)이다.
   - Neon Free(0.5GB) 배포는 배포 프로필로 원시 보관을 줄일 수 있게 한다(예: 24시간). 줄인 값과 예상 용량을 `docs/architecture/TREND_HISTORY.md`에 기록한다.
   - 서버리스 정리는 수집 요청 안에서 **건수 제한이 있는 삭제**로 한다.
   - `ingest_batches`와 기존 알람 FK(`alarm_capture_decisions`)는 건드리지 않는다. 새 테이블만 정리한다.
5. **조회 API**

```
GET /api/v1/trends?line_id=&source_id=&stream_id=&tag=Sealer.TemperaturePV&tag=Sealer.AirPressure&from=2026-10-23T00:00:00Z&to=2026-10-23T01:00:00Z&resolution=auto
```

- `tag`: 1~4개, 9개 아날로그 태그만 허용
- `resolution`: `second`(기간 1시간 이하) | `minute`(기간 7일 이하) | `auto`(1시간 이하이면 second, 아니면 minute)
- 범위를 넘으면 422

```json
{
  "scope": {"line_id": "packaging-line-1", "source_id": "…", "stream_id": "…"},
  "from": "2026-10-23T00:00:00Z",
  "to": "2026-10-23T01:00:00Z",
  "resolution": "minute",
  "policy_version": "utc-second-latest-capture-v1",
  "series": [
    {
      "tag_id": "Sealer.TemperaturePV",
      "unit": "°C",
      "points": [
        {"t": "2026-10-23T00:00:00Z", "value": 149.82, "min": 149.10, "max": 150.31,
         "quality": "GOOD", "valid_count": 60, "expected_count": 60},
        {"t": "2026-10-23T00:01:00Z", "value": 150.02, "min": 149.70, "max": 150.40,
         "quality": "PARTIAL", "valid_count": 41, "expected_count": 60},
        {"t": "2026-10-23T00:02:00Z", "value": null, "min": null, "max": null,
         "quality": "MISSING", "valid_count": 0, "expected_count": 60}
      ]
    }
  ],
  "generated_at": "2026-10-23T01:00:02Z"
}
```

- `value`는 minute에서 평균, second에서 해당 초의 선택 표본이다. second에서는 `min = max = value`, `expected_count = 1`이다.
- `quality`는 `GOOD` | `PARTIAL`(유효 표본이 일부만 있음) | `UNCERTAIN` | `BAD` | `MISSING`(표본 없음) | `TIME_UNKNOWN`(시간 품질 불명) 중 하나다.
- **표본이 없는 슬롯도 반드시 점으로 내보낸다.** 화면이 선을 끊어야 하기 때문이다. `value`는 `null`이며 0으로 채우지 않는다.
- 시리즈당 점 수는 최대 10,080개(7일 minute)다.

### 완료 기준
- 실제 PostgreSQL 시험으로 다음을 확인한다:
  - 배치 커밋과 표본 저장의 원자성(커밋 실패 시 표본도 없음)
  - 재전송 시 중복 없음
  - 지연 배치가 들어오면 해당 분 재계산
  - 누락 분의 `MISSING` 표시
  - 시간 품질 불명 표본 제외
  - 7일·1시간 경계 422
  - viewer 조회 허용, 비로그인 401
- 마이그레이션 왕복(upgrade→downgrade→upgrade)과 기존 마이그레이션 시험이 통과한다.
- 보관 정리가 건수 제한을 지키고, 원시·집계 외의 테이블은 삭제하지 않는다.
- `docs/architecture/TREND_HISTORY.md`를 작성한다: 저장 경로, 집계 갱신 방식, 보관, 용량 추정, 제한.

---

## CX-02. 작업별 실적·상태 타임라인·일일 보고·CSV

### 목적
KULS HMI의 "레포팅"과 기획서 F08(가동·정지·불명 시간, 일일·작업별 실적과 CSV)에 대응한다.

### 계산 규칙 (기획서 그대로)
- PLC `Line.State`의 RUNNING은 가동이고, 그 밖의 **알려진** 상태(STOPPED, READY, PAUSED, STOPPING, FAULT, ESTOP, RECOVERY_REQUIRED)는 정지다. 정지에는 상태 코드를 함께 남긴다.
- 통신 단절(`stream_gaps`, 수집 무수신)과 시간 품질 불명 구간은 **불명**이다.
- 가동 + 정지 + 불명 = 보고 대상 전체 시간이 되도록 검산하고, 응답에 `check_ok`로 표시한다.
- 가동 비율 = 가동 ÷ 전체 시간이다. **불명을 분모에서 빼지 않는다.**
- 일일 보고는 Asia/Seoul 00:00 기준으로 자르며, 날짜를 넘는 실행은 해당 날짜의 사건·시간만 배분한다.
- 정산이 `SETTLED`가 아닌 실행이나 시간 품질이 회복되지 않은 구간이 있으면 보고서를 잠정(`provisional: true`)으로 표시하고 사유를 함께 준다.

### API

**(1) 작업별 실적 목록**

```
GET /api/v1/reports/executions?line_id=&from=&to=&limit=50&cursor=
```

- 기간은 실행 `registered_at` 기준이며 최대 93일이다. 페이지는 커서 방식이다.

```json
{
  "items": [
    {
      "execution_id": "…",
      "work_order_id": "work-001",
      "item_id": "item-001",
      "recipe_version": "recipe-001-v1",
      "target_quantity": 20,
      "registered_at": "…Z",
      "started_at": "…Z",
      "ended_at": "…Z",
      "totals": {"good": "20", "reject": "2", "scrap": "0"},
      "yield_ratio": 0.9091,
      "settlement_status": "SETTLED",
      "provisional": false,
      "provisional_reasons": [],
      "durations_seconds": {"running": 812, "stopped": 95, "unknown": 0, "total": 907},
      "stopped_by_state": {"PAUSED": 60, "FAULT": 35}
    }
  ],
  "next_cursor": null
}
```

- `started_at`과 `ended_at`은 상태 사건에서 도출한다. 근거가 없으면 `null`이다.
- `yield_ratio` = good ÷ (good + reject)이며, 분모가 0이면 `null`이다.

**(2) 실행별 상태 타임라인**

```
GET /api/v1/reports/executions/{execution_id}/timeline?line_id=&source_id=&stream_id=
```

```json
{
  "execution_id": "…",
  "from": "…Z",
  "to": "…Z",
  "segments": [
    {"from": "…Z", "to": "…Z", "class": "RUNNING", "state": "RUNNING", "fault_code": null, "stop_reason": null},
    {"from": "…Z", "to": "…Z", "class": "STOPPED", "state": "FAULT", "fault_code": 3,
     "stop_reason": {"code": "EQUIPMENT", "note": "히터 점검", "recorded_by": "operator1", "recorded_at": "…Z"}},
    {"from": "…Z", "to": "…Z", "class": "UNKNOWN", "state": null, "fault_code": null, "stop_reason": null,
     "unknown_reason": "COMMUNICATION_LOST"}
  ],
  "durations_seconds": {"running": 0, "stopped": 0, "unknown": 0, "total": 0},
  "check_ok": true
}
```

- `class`: `RUNNING` | `STOPPED` | `UNKNOWN`
- `unknown_reason`: `COMMUNICATION_LOST` | `TIME_UNKNOWN` | `NO_EVIDENCE`
- `stop_reason`은 CX-03 전까지 항상 `null`이다.

**(3) 일일 운영 보고**

```
GET /api/v1/reports/daily?line_id=&date=2026-10-23
```

```json
{
  "date": "2026-10-23",
  "timezone": "Asia/Seoul",
  "from": "2026-10-22T15:00:00Z",
  "to": "2026-10-23T15:00:00Z",
  "durations_seconds": {"running": 0, "stopped": 0, "unknown": 0, "total": 86400},
  "running_ratio": 0.0,
  "unknown_ratio": 0.0,
  "stopped_by_reason": {"MATERIAL_WAIT": 0, "EQUIPMENT": 0, "CHANGEOVER": 0, "PLANNED": 0, "UNCLASSIFIED": 0},
  "totals": {"good": "0", "reject": "0", "scrap": "0"},
  "hourly": [{"hour_start": "2026-10-22T15:00:00Z", "good": "0", "reject": "0", "running_seconds": 0, "unknown_seconds": 0}],
  "unacknowledged_alarms": 0,
  "executions": ["…"],
  "provisional": true,
  "provisional_reasons": ["DAY_NOT_FINISHED"]
}
```

- `hourly`는 서울 기준 24개 구간이다.
- 수량은 `production_events.occurred_at` 기준으로 배분한다.
- 오늘 날짜를 조회하면 `DAY_NOT_FINISHED`로 잠정 표시하고, 아직 오지 않은 시간은 `total`에서 제외한다.

**(4) CSV 내보내기**

```
GET /api/v1/reports/executions.csv?…     # (1)과 같은 파라미터
GET /api/v1/reports/daily.csv?…          # (3)과 같은 파라미터
```

- Excel에서 한글이 깨지지 않도록 UTF-8 BOM을 붙인다. 헤더 행은 한글로 쓴다.
- `Content-Disposition: attachment; filename="..."`를 붙인다.
- **CSV 수식 주입 방지:** 셀이 `=`, `+`, `-`, `@`, 탭, CR로 시작하면 앞에 `'`를 붙인다.
- 잠정 여부 열을 포함한다.

### 완료 기준
- 실제 PostgreSQL 시험으로 다음을 확인한다:
  - 가동+정지+불명 검산
  - 통신 단절 구간의 UNKNOWN
  - 자정을 넘는 실행의 일일 배분과 작업 보고 단일성
  - 작업 A 사건이 작업 B에 섞이지 않음
  - 미정산 실행의 잠정 표시
  - CSV 주입 방지와 BOM
  - 권한(viewer 허용, 비로그인 401)
- `docs/architecture/REPORTS.md`를 작성한다.

---

## CX-03. 정지 사유 입력·이력

### 목적
기획서 "정지 사유는 자재 대기, 설비 문제, 작업 전환, 계획 정지, 미분류에서 선택하고 작업자가 보완한다"를 구현한다.

### 범위
1. **마이그레이션 `0016_downtime_reasons`**: 정지 구간에 대한 사유 기록을 추가 전용(append-only) 이력으로 저장한다. 표시값은 최신 기록이다.
2. **사유 코드(고정 계약):**

| 코드 | 표시 |
|---|---|
| `MATERIAL_WAIT` | 자재 대기 |
| `EQUIPMENT` | 설비 문제 |
| `CHANGEOVER` | 작업 전환 |
| `PLANNED` | 계획 정지 |
| `UNCLASSIFIED` | 미분류 |

3. **쓰기 API**

```
POST /api/v1/downtime-reasons
```

```json
{"request_id": "uuid", "execution_id": "…", "line_id": "…", "source_id": "…", "stream_id": "…",
 "segment_from": "…Z", "reason_code": "EQUIPMENT", "note": "히터 점검 (최대 200자)"}
```

   - 대상은 CX-02 타임라인의 `class: "STOPPED"` 구간만이다. `segment_from`이 실제 구간 시작과 정확히 일치해야 하며, 그렇지 않으면 409다.
   - **UNKNOWN 구간에는 사유를 넣을 수 없다**(422). 불명을 사유로 덮지 않는다.
   - 권한: `auth.PERMISSIONS`에 `downtime_annotate = {"operator", "production_manager"}`를 추가한다. 이는 권한 표 변경이므로 high 검토 대상이다.
   - CSRF·Origin·JSON 검사를 하고 `mes_management_audit`(또는 동등한 테이블)에 감사 기록을 남긴다.
   - 멱등성: 같은 `request_id`와 같은 본문이면 기존 결과를 돌려주고, 같은 `request_id`에 다른 본문이면 409다.
4. **조회:** CX-02 타임라인의 `stop_reason`을 채우고, 일일 보고의 `stopped_by_reason`을 계산한다. 사유가 없는 정지 시간은 `UNCLASSIFIED`로 집계한다.

```
GET /api/v1/downtime-reasons/history?execution_id=&segment_from=
```

   - 위 조회로 수정 이력을 확인할 수 있게 한다.

### 완료 기준
- PostgreSQL·ASGI 시험으로 다음을 확인한다:
  - 권한별 허용·거절(viewer 403)
  - CSRF 누락 403
  - 멱등 재요청
  - 내용 충돌 409
  - UNKNOWN 구간 422
  - 이력 보존과 최신값 표시
  - 일일 합계 반영
- 마이그레이션 왕복을 확인하고 high 검토 결과를 기록한다.

---

## CX-04. SPC X̄-R 관리도·추세 경고

### 목적
KULS MES 페이지의 SPC("공정 데이터를 통계적으로 분석해 이상 징후를 사전에 파악")와 공고의 "AI 기반 데이터 분석과 예측"에 대응한다. 머신러닝이 아닌 **통계 기반**임을 응답과 문서에 명시한다.

### 계산 규칙
- **부분군:** 1분 창마다 0·12·24·36·48초 오프셋의 유효(GOOD) 1초 표본 5개를 뽑는다(n = 5). 하나라도 없으면 그 부분군은 제외하고 사유별로 센다.
- **관리한계:** 단계 I 기준선 = 대상 실행의 처음 20개 완전 부분군이다. 표준 상수(n = 5: A2 = 0.577, D3 = 0, D4 = 2.114, d2 = 2.326)로 X̄·R 관리한계를 계산한다.
- **이상 규칙:** Western Electric 1~4를 적용한다(`rules_version: "western-electric-1-4-v1"`).
- **규격·공정능력:**
  - 규격(USL/LSL)은 해당 실행의 설정 온도와 활성 알람 규칙 임계값에서 도출한다. 도출할 수 없으면 `spec: null`이다.
  - Cp/Cpk는 완전 부분군이 25개 이상이고 규격이 있을 때만 계산하고, 아니면 `null`이다.
- **추세 경고:**
  - 최근 15분의 1분 평균을 선형 회귀해 기울기와 규격 한계 도달 예상 시각을 계산한다.
  - 결정계수가 0.6 미만이거나 유효 점이 10개 미만이면 `projected_cross: null`이다.
  - 이는 "추세 기반 추정"이며 예측 보장이 아니다.

### API

```
GET /api/v1/spc/xbar-r?line_id=&source_id=&stream_id=&tag=Sealer.TemperaturePV&execution_id=
```

- 대상 태그는 우선 `Sealer.TemperaturePV`와 `Sealer.AirPressure`다.

```json
{
  "tag_id": "Sealer.TemperaturePV",
  "unit": "°C",
  "method": "statistical",
  "subgroup_size": 5,
  "rules_version": "western-electric-1-4-v1",
  "status": "OK",
  "baseline": {"from": "…Z", "to": "…Z", "subgroups": 20},
  "limits": {
    "xbar": {"ucl": 151.2, "cl": 150.0, "lcl": 148.8},
    "range": {"ucl": 2.8, "cl": 1.3, "lcl": 0.0}
  },
  "spec": {"usl": 155.0, "lsl": 145.0, "target": 150.0, "source": "recipe+alarm_rule"},
  "capability": {"cp": 1.42, "cpk": 1.31, "subgroups": 32},
  "subgroups": [
    {"t": "…Z", "mean": 150.1, "range": 1.2, "n": 5, "violations": []},
    {"t": "…Z", "mean": 151.4, "range": 1.0, "n": 5, "violations": ["WE1"]}
  ],
  "excluded": {"count": 3, "reasons": {"MISSING": 2, "BAD": 0, "TIME_UNKNOWN": 1}},
  "trend_warning": {
    "method": "linear-regression-15min",
    "slope_per_minute": 0.08,
    "r2": 0.81,
    "projected_cross": {"limit": "USL", "at": "…Z"}
  },
  "generated_at": "…Z"
}
```

- `status`: `OK` | `INSUFFICIENT_DATA`(기준선 부분군이 20개 미만이면 `limits: null`)

### 완료 기준
- 순수 계산 모듈(`apps/api/spc.py` 등)을 만들고 단위 시험으로 다음을 확인한다:
  - 손으로 계산한 고정 표본과 관리한계·Cp/Cpk 일치
  - 각 WE 규칙의 양성·음성 사례
  - 결측 부분군 제외
  - 기준선 부족 시 `INSUFFICIENT_DATA`
  - 회귀 경고 조건
- PostgreSQL 결합 시험 1개 이상, 권한 시험을 둔다.
- `docs/architecture/SPC.md`를 작성한다. "통계 기반이며 ML이 아님"과 한계를 명시한다.

---

## CX-05. 대시보드 요약 API

### 목적
첫 화면의 KPI 카드와 시간대별 차트를 **요청 한 번**으로 그린다. 계산 규칙은 CX-02를 재사용하고 새 규칙을 만들지 않는다.

```
GET /api/v1/dashboard?line_id=&source_id=&stream_id=&date=2026-10-23
```

```json
{
  "date": "2026-10-23",
  "timezone": "Asia/Seoul",
  "generated_at": "…Z",
  "freshness": {"last_receipt_at": "…Z", "status": "LIVE"},
  "production": {"good": "120", "reject": "6", "scrap": "1", "yield_ratio": 0.9524, "basis": "RECEIVED_EVENTS", "provisional": true},
  "active_execution": {
    "execution_id": "…", "work_order_id": "work-001", "item_id": "item-001",
    "target_quantity": 200, "good": "120", "progress_ratio": 0.6,
    "line_state": "RUNNING", "settlement_status": "PENDING"
  },
  "time": {"running_seconds": 0, "stopped_seconds": 0, "unknown_seconds": 0, "total_seconds": 0,
           "running_ratio": 0.0, "unknown_ratio": 0.0},
  "oee_partial": {"availability": 0.0, "quality": 0.9524, "performance": null,
                  "note": "PERFORMANCE_NOT_DEFINED"},
  "alarms": {"active_total": 1, "active_by_severity": {"high": 1}, "unacknowledged": 1},
  "hourly": [{"hour_start": "…Z", "good": "0", "reject": "0", "running_seconds": 0, "unknown_seconds": 0}]
}
```

- `freshness.status`: `LIVE` | `STALE` | `NO_DATA`. 기준은 기존 스냅샷의 3초 지연·5초 경보 규칙과 같게 한다. 설비가 멈춘 공개 데모에서 화면이 "데이터 없음"을 정직하게 보여주는 근거가 된다.
- `active_execution`은 실행이 없으면 `null`이다.
- `oee_partial`:
  - 기획서는 OEE를 "계획 생산시간·이상적 사이클·양품 기준 합의 후" 추가하도록 정했다. 그래서 **성능효율은 `null`로 두고** 가용률·품질률만 참고 지표로 낸다.
  - `availability`는 `running_ratio`와 같은 값이다(불명을 분모에 포함).
- `active_by_severity`의 키는 알람 규칙의 `rule.severity` 값(자유 식별자) 그대로다. 위 키는 예시이며, 활성 알람이 없는 심각도는 생략한다.

### 완료 기준
- PostgreSQL 시험으로 다음을 확인한다:
  - 데이터 없음·지연·정상 세 상태
  - 실행 없음/있음
  - CX-02 일일 보고와 같은 날짜의 수량·시간이 일치
  - 권한
- 응답 크기가 64KB 이하임을 시험으로 확인한다.

---

## CX-06. 게스트 조회 세션

### 목적
면접관이 비밀번호 없이 공개 데모를 **조회만** 할 수 있게 한다. 현재 `demo` 계정은 비공개이고, 공용 계정은 최초 로그인 시 비밀번호 변경이 강제되어 공유할 수 없다. 공개 문서에 비밀번호를 쓰지 않기 위해 비밀번호 없는 게스트 세션을 둔다.

### 범위

```
GET  /auth/config   → {"guest_viewer": true|false}
POST /auth/guest    → 기존 /auth/login과 같은 세션 응답(쿠키 + CSRF 토큰), roles = ["viewer"]
```

- 기본값은 **비활성**이다. 배포 설정(로컬 실행기 인자, Vercel의 `cloud_plant.json`과 같은 고정 패키지 정책 또는 환경변수)으로만 켠다.
- 전용 계정 하나(예: `guest-viewer`)에 세션을 발급한다. 시작 시 그 계정의 역할이 정확히 `{viewer}`가 아니면 기능 전체를 거절한다.
- 이 계정은 비밀번호 로그인과 비밀번호 변경을 할 수 없다. `must_change_password` 흐름에 들어가지 않는다.
- 세션 절대 만료는 30분, 유휴 만료는 기존과 같다.
- 기존 Origin 검사를 적용한다. 요청 제한은 IP별과 전역 동시 게스트 세션 상한(예: 50)을 둔다.
- 감사 기록을 남긴다(`auth_audit`, 방식 `GUEST`).
- 모든 쓰기 API는 기존 권한 검사로 403이 나야 한다. 이를 시험으로 고정한다.

### 완료 기준
- 시험으로 다음을 확인한다:
  - 비활성 시 404 또는 403
  - 활성 시 viewer 세션 발급
  - 명령·작업지시·알람 조치·정지 사유 쓰기 403
  - 역할이 잘못 설정된 계정이면 시작 거절
  - 요청 제한
  - 만료
- high 검토 결과를 기록한다.
- `docs/architecture/GUEST_VIEWER.md`를 작성한다.
- **공개 배포에서 실제로 켜는 것은 사용자 승인 후 진행한다.**

---

## CX-07. `docker compose` 한 명령 로컬 실행

### 목적
면접관이 `git clone` 후 `docker compose up` 한 번으로 설비 → OPC UA → 게이트웨이 → API → 화면 전체를 띄운다. 계획 §3.2의 "Docker Compose"가 아직 없다.

### 범위
- 저장소 루트에 `compose.yaml`을 둔다. 구성 서비스: `postgres`(시험과 같은 고정 digest), 일회성 `init`(마이그레이션, 기준 정보, 개발 인증서, 계정·토큰 생성), `simulator`, `gateway`(`--enable-commands --enable-settlement`), `api`(사용자 + 내부), 웹 정적 파일.
- 생성된 비밀번호·토큰은 이름 있는 볼륨이나 `.local/compose/`의 권한 600 파일에만 둔다. 로그에 출력하지 않는다. 게스트 조회(CX-06)는 compose에서 기본 활성으로 해도 된다.
- 기존 `deploy/plant-service.Dockerfile`과 실행기 CLI를 재사용한다.
- 스모크 검사 `scripts/check-compose.py`를 만든다. 동작: compose 기동 → 로그인(또는 게스트) → 48개 태그 fresh/GOOD 확인 → 종료·정리.
- 실행 절차를 `docs/architecture/COMPOSE_RUN.md`에 쓴다. README 링크는 Claude가 추가한다.

### 완료 기준
- 깨끗한 체크아웃에서 `docker compose up -d` → `scripts/check-compose.py` PASS → `docker compose down -v`로 정리되는 것을 로컬에서 실행하고, 출력 요약을 보고서에 남긴다.

---

## CX-08. 조회 전용 운영 챗봇 (보류)

**사용자가 LLM 공급자와 비용을 승인하기 전에는 착수하지 않는다.** 현재 문서는 "추가 유료 서비스는 만들지 않았다"고 되어 있다.

승인되면 따를 경계:
- 서버 측 도구 호출만 사용한다. 도구는 CX-02·04·05, 스냅샷, 알람 **조회 API만** 쓴다. 명령·작업지시·알람 조치·정지 사유 같은 쓰기 도구는 만들지 않는다.
- 호출자의 세션 권한(read)으로만 조회한다.
- 답변에는 근거 데이터의 조회 시각과 잠정·불명 여부를 포함한다. 불명을 정상이라고 답하지 않는다.
- 키가 없으면 기능이 비활성이다. 키는 환경변수로만 받고, 프롬프트·응답 원문을 로그에 남기지 않는다.

---

## 4. 로컬 검증 명령

작업마다 다음을 실행하고 결과(통과 수, 소요 시간, 실패 원문)를 보고서에 붙인다.

```sh
./scripts/check-python.sh lint
.venv/bin/python -m pytest <변경한 시험 파일> -q
./scripts/check-local.sh                       # 통합 직전 1회 (약 18분, Docker 필요)
.venv/bin/python scripts/check-operable-system.py \
  --chromium /opt/pw-browsers/chromium-1194/chrome-linux/chrome   # 수집·명령·정산 경로에 영향이 있을 때
```

- 미실행·실패·차단을 통과로 적지 않는다. Docker가 없는 환경이면 "차단: Docker 없음"으로 적는다.
- 웹(`apps/web`) 검사는 Claude가 실행한다.

## 5. 완료 보고 형식

작업마다 `docs/work-orders/reports/CX-xx.md`를 남기고 `docs/DEVELOPMENT_STATUS.md`의 `### Codex 백엔드 보강` 절에 한 줄 요약을 추가한다.

```markdown
# CX-xx 결과
- 커밋: <sha>
- 상태: 해당 범위 검증 통과 | 일부 통과 | 차단 | 진행 중
- 변경 파일: …
- 계약 차이: 없음 | 있음(§6 절차로 합의한 내용)
- 실행한 검증: 명령 / 결과 / 소요 시간
- high 검토: 대상 / 결과 (해당 시)
- 기준 문서 영향: 신규 범위 여부, 바뀐 기본값과 이유
- 남은 범위·제한: …
```

## 6. 계약 변경 절차

위 JSON은 화면이 그대로 사용한다. 필드 이름·의미·단위를 바꿔야 하면:

1. 구현 전에 `docs/work-orders/reports/CX-xx.md`의 "계약 차이"에 제안을 적고 사용자에게 알린다.
2. Claude가 UI 영향을 검토해 이 문서를 개정한다(개정 번호 v1.1 등).
3. 합의 전에는 원래 계약대로 구현한다.

필드를 **추가**하는 것은 합의 없이 해도 된다. 단, 보고서에 적는다.

## 7. 참고: Claude(UI/UX) 쪽 작업

Codex가 수정할 필요는 없으며, 연결 지점을 이해하기 위한 목록이다.

| ID | 화면 | 사용하는 API |
|---|---|---|
| UX-01 | 앱 골격: 사이드 내비게이션, 페이지 분리, 디자인 토큰, 빈·오류·데이터 없음 상태 | 기존 |
| UX-02 | 대시보드: KPI 카드, 시간대별 생산, 신선도 배지 | CX-05 |
| UX-03 | 라인 모니터링 정리: 공정도, 태그 표, 제어 패널, ID 축약·복사 | 기존 |
| UX-04 | 트렌드: 다중 태그, 단위별 축, 누락 구간 끊김 표시 | CX-01 |
| UX-05 | 보고서: 작업별 실적, 일일 보고, 상태 타임라인, CSV 다운로드, 정지 사유 입력 | CX-02, CX-03 |
| UX-06 | SPC: X̄-R 관리도, 위반 표시, 공정능력, 추세 경고 | CX-04 |
| UX-07 | 로그인: 게스트 둘러보기 버튼 | CX-06 |
| UX-08 | README 재구성, 스크린샷·GIF | — |

UI는 계약 JSON으로 만든 고정 데이터로 먼저 개발한다. 실제 API에 결합해 검증하기 전에는 "UI 구현, 실제 API 결합 미검증"으로 기록한다.
