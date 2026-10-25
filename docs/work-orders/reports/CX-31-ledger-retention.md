# CX-31-ledger-retention — 15차 C안 확정 원장 정리

- 기준 main `b8954cd`, 작업 브랜치 `codex/kuls-backend-v3`.
- 사용자 승인 범위: 7일 이상된 서버 확정 ACK 원장의 연속 앞부분 삭제·체크포인트·시작 검증 변경. 서버/API·유실 근거 삭제·한도 변경·운영 적용은 범위 밖이다.
- 구현 및 로컬 시험 준비됨. 최초 Ruff 실패를 수정했으나 1회 제한에 따라 재검사하지 않아 **전체 검사 통과로 표시하지 않는다.**

## 규칙과 구현

BufferLimits.ledger_retention 기본값은 7일이다. `prune_acknowledged(now=..., limit=1000)`는 checkpoint 다음 capture부터 연속된 ACKNOWLEDGED 행만 선택한다. 전체 DurableAck COMMITTED 계약·batch/gateway/source/stream·내용 해시·metadata accounting을 검증하며 ACK committed_at에서 보존 기간이 지나야 한다. outbound·missing_ranges.batch_id·loss_intervals.report_batch_id 참조가 있거나 미확정/EVICTED/보존 기간 안의 행이면 거기서 멈춘다. 뒤의 확정 행을 건너뛰지 않는다.

schema v2의 ledger_checkpoint는 generation, last_capture_seq, 누적 SHA-256, 삭제 건수, updated_at, checkpoint checksum 및 metadata_bytes를 보관한다. 누적 해시는 gateway·이전 hash·각 행의 ID/순번/내용 및 envelope 해시/source/stream/정규화된 전체 ACK를 포함한다. 표현 압축 차이는 해시에 영향을 주지 않는다. 체크포인트 갱신·원장 삭제·논리 metadata 감소가 한 트랜잭션이며 오류면 모두 롤백한다.

완전한 기존 v1 원장부터 검증한 뒤 빈 checkpoint와 v2 marker를 원자적으로 만든다. v2의 checkpoint 누락을 새 checkpoint로 덮지 않는다. 시작 시 남은 원장은 checkpoint+1부터 마지막 capture까지 연속이어야 하며, 전부 삭제됐다면 checkpoint가 저장된 capture head와 같아야 한다. malformed checkpoint도 BufferCorruption으로 write gate를 걸며 증거를 재구성하지 않는다.

worker의 별도 coroutine이 monotonic 기준 1시간마다 최대 1,000행 정리를 요청한다. UTC는 나이 판정에만 쓴다. SQLite 소유 스레드에서 직렬 실행하고 삭제 후보·첫 잔여 순번만 검증한다. 기존 전체 시작 검증은 유지한다. metadata SUM와 참조 조회는 표 크기에 영향을 받아 고정 지연은 보장하지 않는다.

enqueue는 예상 새 원장/초기 source_progress 예산 또는90% 압력에서 정리를 먼저 시도한다. 그 정리는 별도 커밋되어 후속 enqueue 실패가 되돌리지 않는다. ACK 경로에서는 삭제하지 않는다. 예산 부족은 기존 METADATA_FULL로 차단한다. 30분·100MB·50MB 한도는 그대로다.

`GATEWAY_BUFFER.md`에 승인된 보존 종료, 원래 ACK의 로컬 재조회 상실·서버의 과거 ID 판정, v1→v2 이관/롤백 영향, 해시 신뢰 범위, 물리 파일 크기와 논리 bytes 차이, UTC 의존성과 아래 용량 한계를 기록했다. 개발 상태의 과거 무기한 원장 보존 문구는 승인된 C안으로 대체했음을 명시했다. 서버 동작 변경은 필요하지 않았으며 기존 PostgreSQL 중복/충돌 규칙을 그대로 사용한다.

## 검사 결과

브라우저용 빌드(1회): `VITE_SCREEN_TRANSPORT=polling npm --prefix apps/web run build` 통과(타입 검사 포함). 생성 dist만 사용했고 UI 소스는 수정하지 않았다.

최초 시험(1회):

```sh
.venv/bin/python -m pytest tests/unit tests/integration/test_gateway_buffer.py tests/integration/test_gateway_setup.py tests/integration/test_gateway_recovery_bounds.py tests/integration/test_gateway_pipeline.py tests/integration/test_gateway_opcua.py tests/integration/test_gateway_fifo_recovery.py tests/integration/test_gateway_command_launcher.py tests/integration/test_gateway_retention_server.py tests/integration/test_ingestion.py --junitxml=/tmp/hmi-v15-tests.xml -q
```

- **783 통과 / 기존 v1 fixture 2 실패 / 경고5**,453.83초. 기존 legacy-envelope 시험은 새 v2 DB에서 envelope 열만 제거하고 v2/checkpoint를 남겨 실제 v1을 재현하지 못했다.
- fixture에서 checkpoint를 제거하고 user_version=1을 명시한 뒤 실패한 같은 매개변수2개만 재실행해 **2 통과,0.44초**. 제품 동작은 추가 변경하지 않았다. 전체785개를 한 번에 통과했다고 표시하지 않는다.
- 13차에서 막혔던 실제 브라우저 FIFO 시험은 폴링 빌드에서 통과(212.768초).
- 실제 PostgreSQL 통합에서 로컬 원장 삭제 후 같은 wire 배치는 서버의 원래 ACK로 응답하고, 같은 ID의 새 capture/다른 내용은 서버 IngestConflict이며 새 배치는 로컬 대기열에 보존됨을 확인했다. 서버 원본 ACK와 행은 유지됐다.
- 정리/젊은·미확정·EVICTED·유실 reporter 참조 보존, 중간 장애 롤백, v1 이관·v2 체크포인트 손상/누락, 재오픈, 압력 정리의 독립 커밋, worker 주기·동일 스레드·ACK 분리도 포함했다.
- Ruff check **1회 실패2건**: buffer.py SQL literal101자(E501), worker.py import 구간 공백(I001). 문자열 분리·공백만 수동 수정했다. 1회 제한에 따라 재검사하지 않았고 통과로 표시하지 않는다.
- format --check는 당시 변경 Python6개 파일 **1회 통과**. 이후 위 Ruff 서식 수정과 v1 fixture2줄 수정 뒤에는 재검사하지 않았다.
- 로그 `/tmp/hmi-v15-tests.log`, 최초 JUnit `/tmp/hmi-v15-tests.xml`. gpt-6.1-sol high 읽기 전용 최종 검토에서 원자성·귀속·checkpoint 연속성의 차단 문제 없음. 구현·시험 분담은 medium을 사용했다.

## 86,400건 가속 적재 실측

실제 temp SQLite public enqueue와 ACK를 각각86,400회 수행했다. 논리 시간은1Hz24시간이지만 실제 경과시간은 **46.764초**다. 실제24시간 운전이나 기존 제외된8시간 운전을 수행하지 않았다.

시험 설정은 보존10초·metadata300,000B이고100 captures마다 최대1,000행을 정리했다.87시점에서 측정했다.

| 시점 | metadata_bytes | 남은 원장 |
|---|---:|---:|
| 1,000건 | 12,232 | 10 |
| 86,400건 | 12,293 | 10 |
| 관측 최소/최대 | 12,232 / 12,293 | 최대10 |

spread61B로 일정하게 유지됐고 최종 capture 위치86,400·verified 커서86,400 및 재오픈 검증이 통과했다. 전체 시계열: `evidence/CX-31-ledger-retention-86400.json`.

## 운영 한계와 남은 것

**기본7일과50MB만으로 장기 고정 용량은 보장하지 않는다.**13차 실측938B/초라면7일 약567MB라 첫 삭제 가능 시점 이전에50MB가 찰 수 있다. 현재와 같은 EVICTED/유실 참조 원장이 앞을 막으면 그 뒤는 나이가 지나도 삭제할 수 없다. 이를 해소하기 위해 보존 기간·유실 근거·한도를 임의 변경하지 않았다. 짧은 시험 설정의 일정 용량을 기본 운영 설정의 증명으로 쓰지 않는다.

호스트 UTC가 크게 앞으로 틀어지면 committed_at과의 나이 계산에 영향이 있으며 독립 시각 인증은 추가하지 않았다. metadata SUM·참조 조회 비용과 큰 ACK의 headroom도 무한 보장이 아니다. 로컬 삭제 뒤 원래 ACK와 ID 중복 근거는 복구할 수 없고 서버 증거에 의존한다. 과거 offline compaction 도구의 semantic snapshot은 기존 표 중심이며 새 checkpoint의 보존은 buffer 자체 검증·이번 회귀 시험으로 확인했다.

운영 SQLite를 열어 이관/삭제하지 않았고, 공개 DB·source/generation·runtime·commission·생산 조작·배포는 수행하지 않았다. 운영 적용과 PR·병합·공개 확인은 Claude 담당이다. 최초 Ruff 실패와 검증 범위를 보고하며 추가 검사·14차 착수는 하지 않는다.
