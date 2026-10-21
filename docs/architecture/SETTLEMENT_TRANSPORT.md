# 최종 실행 요약 전송과 정산 재검사

앱 A 내부 API는 원래 PostgreSQL 실행 등록을 기준으로 조회 대상을 제공한다. gateway는 해당 실행의 실제 OPC UA `GetRunSummary`만 조회하고, 실제 `final=true` 요약만 전송한다. 이 경로는 PLC 제어·종료 명령이나 최종 요약 생성 기능을 갖지 않는다.

## 내부 HTTP 계약

`mount_settlement_routes(app,engine,authenticate,clock)`를 기존 내부 서비스 앱에 연결한다. 부모 앱이 TLS/loopback 정책과 bearer credential을 검사하고, 각 route는 PostgreSQL에 등록된 gateway/source/stream 귀속을 추가 검사한다. 브라우저 세션은 이 경로의 자격증명이 아니다. JSON 요청은 각각 65,536바이트를 넘을 수 없고 추가 필드를 거절한다. 잘못된 payload는 422, scope 위반은 403, schema/DB 장애는 안전한 고정 메시지의 503이다.

| 경로 | 요청 | 응답 |
|---|---|---|
| `POST /internal/v1/runs/pending` | `{after_execution_id?: Identifier|null, limit?: 1..100}` | `{execution_ids:[...], next_cursor: Identifier|null}` |
| `POST /internal/v1/runs/summary` | 정확한 실제 `RunSummary`, `final=true` 필수 | `{committed:true, execution_id, status, reason_code}` |

pending의 기본 limit은 100이다. 인증된 source/stream에 등록된 원래 실행 중 `SETTLED`를 제외한 모든 실행을 조회하며, 최종 요약이 아직 없는 실행도 포함한다. 실행 ID의 C collation 순서로 cursor보다 큰 ID를 limit+1개 조회해 다음 페이지 유무를 판정한다. 다음 페이지가 있으면 마지막 반환 ID가 cursor이고, 마지막 페이지는 null이다. 각 정산/활성 실행 때문에 뒤의 실행이 굶지 않도록 gateway는 다음 cursor를 유지하고 마지막 페이지 뒤 처음부터 순환한다. 순회 도중 cursor보다 앞에 등록된 실행도 다음 순환에서 발견한다.

요약 route는 공유 schema 잠금을 잡은 `engine.begin()` 안에서 `record_final_summary`를 실행하고 **커밋 성공 후에만** ACK를 만든다. 이 ACK는 실제 최종 요약과 현재 정산 판정의 영속 수신을 뜻한다. `committed=true,status=PENDING`은 정산 완료가 아니다. 원래 사건·누락·검증 커서·설정·수량·미정 명령의 정산 조건을 완화하지 않으며 원본 수량을 수정하지 않는다.

같은 실제 최종 요약을 재전송하면 기존 사건 처리 상태를 기준으로 정산을 재검사한다. 예를 들어 사건 도착 전 `PENDING/EVENTS_PENDING`이었다가 원래 사건이 커밋된 뒤 같은 요약으로 `SETTLED/MATCHED`가 될 수 있다. 다른 최종 경계를 보내면 정산 서비스가 충돌 증거를 보존한다. ACK 유실은 새 실행·요약이나 성공 판정을 만들 근거가 아니다.

## Gateway 서비스

`RunSummaryTransport(IntakeConfig)`는 기존 서비스 token·TLS 구성으로 위 두 경로를 호출한다. redirect를 따르지 않으며 응답은 설정 상한과 64KiB 중 작은 값까지 읽는다. pending 목록의 순서·중복·페이지 크기·cursor를 검사하고 summary ACK의 엄격한 schema·실제 `committed=true`·원래 execution ID 일치를 검사한다. 잘못된/유실된 ACK와 일시 장애는 `DeliveryUncertain`, 명시적 서비스 거절은 `DeliveryRejected`다. 원문 응답·token을 오류에 넣지 않는다.

`RunSummaryWorker(opc,transport,*,source_id,stream_id,page_limit=100).run_once()`는 한 페이지를 처리한다. `.next_cursor`는 다음 호출의 페이지 위치다. OPC 연결 뒤 원래 execution ID로 `GetRunSummary`를 요청하고 ID·source·stream을 다시 검사한다. `RunNotFound`와 nonfinal은 정상적인 조회 보류로 세어 서버에 가짜 실패나 최종 요약을 만들지 않는다. 뒤의 실행이 계속 처리되도록 개별 실행의 조회/전송 오류가 있어도 페이지 cursor를 전진시킨다. 마지막 페이지 뒤 재순회하므로 ACK가 유실된 미정산 실행은 같은 원래 실행에서 다시 조회·전송한다. 이미 SETTLED로 커밋된 실행은 pending 목록에서 빠진다.

결과는 `RunSummaryWorkResult(state,queried,reported,skipped,uncertain)`다. `reported`는 커밋 ACK를 받은 수이며 PLC 성공이나 MES 정산 완료 수가 아니다. 작업자는 `SubmitCommand`, END_RUN, 다른 제어 메서드를 호출하지 않는다. cancellation은 전파하고, 이 worker의 실패가 원래 command ID를 바꾸거나 재실행하게 하지 않는다.

## 검증과 범위

```sh
.venv/bin/ruff check apps/api/settlement_http.py apps/gateway/settlement.py tests/integration/test_settlement_http.py tests/unit/test_gateway_settlement.py
.venv/bin/pytest -q tests/integration/test_settlement_http.py tests/unit/test_gateway_settlement.py
```

실제 일회용 PostgreSQL+ASGI 시험은 등록 scope 격리·정렬 페이지·엄격한 입력/본문 상한·실제 사건 도착 전 PENDING과 도착 후 SETTLED·동일 최종 요약 재전송·커밋 실패 때 ACK 없음과 rollback을 확인한다. gateway 단위 시험은 주입 OPC/HTTP로 원래 실행 재시도·활성/미존재 실행 보류·페이지 공정성·identity 위반·취소·엄격한 ACK/본문 상한을 확인한다. 이 gateway 단위 시험은 실제 OPC 전송 시험을 의미하지 않는다. 실행기 hook과 전체 OPC→HTTP→DB 정산 검증은 별도 통합 결과로 기록해야 한다.

게이트웨이 CLI는 `--enable-settlement`를 명시하면 수집·명령과 독립된 정산 루프를 실행한다. 종료 시 진행 중인 조회를 취소하고 연결을 회수한다. 실제 CLI 및 단위 결합 53개, 별도 실제 OPC·HTTP·PostgreSQL pipeline 5개 시험을 통과했다. 후자는 최종 요약 ACK 유실과 사건 도착 후 SETTLED를 확인하며 전체 M2/M4 완료를 대신하지 않는다.
