# 서비스 전용 배치 수신 HTTP 경계

`apps/api/internal.py`는 게이트웨이의 `GatewayBatch`를 PostgreSQL 수집 서비스에 전달하는 FastAPI factory다. 브라우저 사용자 로그인·운전 명령·실행 등록 API와 분리된다. DB migration·기준 정보·source/stream binding·원래 execution 등록은 호출자가 명시적으로 먼저 완료해야 한다. 시작 시 테이블이나 기본 계정·토큰을 만들지 않는다.

## 구성과 인증

`create_internal_app(ingestion_service, credential_bindings, config=...)`에 `IngestionService(engine)`와 1~128개의 `ServiceCredential`을 전달한다. DB engine은 [DB 구성](DATABASE_BOOTSTRAP.md)의 PostgreSQL 설정을 사용한다. `ServiceCredential.from_secret_file()`의 필수 입력은 `key_id`, `gateway_id`, `source_id`, `stream_id`, `secret_file`, timezone이 있는 `expires_at`이다. 기본 principal·secret 경로·암호는 없다. 같은 key ID 또는 token digest를 중복 구성하면 실패한다.

운영자가 암호학적 난수 32~64바이트를 padding 없는 base64url로 인코딩한 token을 준비한다. Linux secret 파일은 프로세스 소유의 일반 파일이며 group/other 권한이 없어야 한다(예: `0600`). symlink·디렉터리·잘못된 크기/인코딩은 거부한다. 끝의 개행은 허용한다. 비밀값은 저장소·문서·명령줄·로그에 기록하지 않는다. 서버는 파일에서 읽은 값의 SHA-256 digest만 credential 객체에 보관하고 repr에서 숨긴다. 형식 검사는 난수의 실제 엔트로피를 측정하지 않으므로 난수 생성은 운영자의 책임이다.

요청은 정확히 하나의 `Authorization: Bearer <token>` 헤더를 사용한다. 서버가 구성한 digest를 고정 길이 상수 시간 비교하고 만료/폐기 여부를 확인한다. 쿠키·브라우저 역할·사용자가 보낸 gateway 헤더는 서비스 권한이 되지 않는다. 인증된 credential의 `(gateway_id, source_id, stream_id)`와 배치 본문이 정확히 같아야 하며 DB의 source binding도 별도로 검증한다. `revoked=True`나 `expires_at` 변경은 새 app 구성에 적용되며 실행 중 secret 파일 자동 reload/폐기 API는 제공하지 않는다. 회전 시 새 key ID와 새 token을 명시적으로 구성하고 필요한 겹침 기간 후 이전 credential을 제거/폐기한다.

기본 `InternalConfig()`는 HTTPS를 요구한다. 로컬 개발에서만 `allow_insecure_loopback=True`를 명시할 수 있으며 HTTP URL host와 실제 peer가 모두 loopback이어야 한다. 서버도 `127.0.0.1`/`::1`에 bind한다. 원격 HTTP는 이 옵션으로 허용되지 않는다. 원격 실행은 검증 가능한 서버 인증서와 TLS listener를 구성하고 게이트웨이는 인증서 검증을 유지해야 한다. ASGI server에서 proxy header를 무조건 신뢰하면 scheme/peer 판단이 바뀔 수 있으므로 직접 TLS listener를 사용하거나 검토한 신뢰 proxy만 허용한다. 여기서는 TLS 인증서 발급·배포를 자동화하지 않는다.

## HTTP 계약

| 경로 | 결과 |
|---|---|
| `GET /health/live` | 인증/DB 연결 없이 `200 {"status":"alive"}`. 이 경로만 HTTP 허용 |
| `GET /health/ready` | TLS·서비스 인증 후 PostgreSQL 연결과 지원하는 단일 revision(중앙 `SUPPORTED_INGESTION_REVISIONS` 목록) 확인. 성공 `200 {"status":"ready"}`, DB/스키마 문제 `503` |
| `POST /internal/v1/batches` | TLS·서비스 인증·범위 검사 후 배치 ingest. 실제 commit 후에만 `200 DurableAck` 반환 |

요청 JSON은 [전송 계약](../../contracts/transport.py)의 `GatewayBatch`와 SHA-256 일치 검증을 따른다. `InternalConfig.max_body_bytes`는 기본 4 MiB이며 1~4,194,304바이트로만 설정할 수 있다. `Content-Length`를 신뢰하지 않고 실제 stream에서 누적 크기를 제한한다. Swagger/OpenAPI 문서 경로와 브라우저 CORS 권한은 제공하지 않는다. 실행을 등록하는 `/internal/v1/executions`도 없다.

| HTTP 상태 | 의미 |
|---|---|
| `401` | token 누락·오류·중복 authorization·만료·폐기 |
| `403` | 허용되지 않은 transport 또는 credential/DB source 범위 불일치 |
| `409` | 이미 저장한 배치/사건과 내용 충돌 |
| `413` | 실제 요청 body 한도 초과 |
| `422` | 잘못된 JSON·자료형·hash·추가 필드 등 배치 계약 오류 |
| `503` | DB 연결·schema·수집 transaction/commit 불가 |

실패 응답에는 ACK 또는 `COMMITTED` 판정을 넣지 않으며 입력 token·원문·SQL·DB 연결 문자열·드라이버 오류를 되돌리지 않는다. 성공 응답은 `Cache-Control: no-store`를 포함한다. 수집 서비스가 transaction을 commit한 뒤 반환한 ACK만 직렬화하므로 deferred constraint trigger에서 실제 commit이 실패하면 `503`이고 receipt·inbox·생산 사건·집계는 rollback된다. ACK가 유실된 경우 게이트웨이는 같은 배치 ID와 내용을 재전송하며 저장된 같은 ACK를 받고 실적은 중복 반영되지 않는다. HTTP 성공만으로 판단하지 않고 게이트웨이의 배치별 `validate_ack()` 검증 후 spool ACK 처리를 진행해야 한다.

## 검증 범위

저장소 루트에서 다음 검사를 실행한다. 실제 PostgreSQL 시험의 로컬 Docker·digest 고정 이미지·폐기 가능한 인증 DB 준비/정리 방식은 [DB 검증](DATABASE_BOOTSTRAP.md)을 따른다.

```sh
.venv/bin/ruff check apps/api/internal.py tests/integration/test_internal_api.py
.venv/bin/ruff format --check apps/api/internal.py tests/integration/test_internal_api.py
.venv/bin/python -m pytest -q tests/integration/test_internal_api.py
```

2026-10-20 검증: 위 Ruff lint/format 통과, targeted suite **32개 통과(9.86초)**. 시험은 ASGI HTTP 경계와 실제 PostgreSQL을 결합해 독립 connection에서 commit/ACK 가시성, ACK 유실 후 중복 재시도, 세 식별자 범위·쿠키 권한 거부, 만료/폐기·비밀 파일 검증, body 상한, malformed/hash 오류, 내용 충돌, readiness와 연결 실패를 확인했다. 실제 PostgreSQL deferred trigger로 commit 실패와 전체 rollback도 검증했다. 이 suite 자체는 실제 TCP/TLS listener·게이트웨이 프로세스·OPC UA까지의 통합 시험을 뜻하지 않는다. 해당 통합 검증과 인증서/secret 운영·폐기 배포·부하/장시간 복구 검증은 별도 기록해야 한다.

## 명령 전달·결과 보고

동일한 서비스 credential의 gateway/source/stream 범위로 아래 경로를 제공한다. 사용자 쿠키로 명령을 가져가거나 결과를 쓰지 못한다. command dispatch migration을 명시적으로 적용해야 한다.

- `POST /internal/v1/commands/claim`의 본문은 빈 JSON 객체다. 한 번만 `SUBMIT` 요청을 반환하며, 영속 예약 이후에는 원래 ID의 `QUERY`만 반환한다. 커밋 실패 시 티켓을 반환하지 않는다.
- `POST /internal/v1/commands/submission`은 PLC `CommandSubmission` 그대로 받는다.
- `POST /internal/v1/commands/result`는 PLC `CommandResult` 또는 `CommandNotFound` 그대로 받는다.

보고 ACK는 트랜잭션 커밋 뒤 `{committed:true,command_id:...}`이며 입력 ID와 일치한다. 본문은 최대 64KiB와 서비스 설정 상한 중 작은 값으로 제한한다. 충돌은 409, 범위 위반은 403이며 오류에 토큰·입력 본문·SQL 오류를 포함하지 않는다. 명령 ID 내용 충돌은 원래 명령의 실행 실패가 아니다. [명령 원장](COMMANDS.md)의 충돌 latch와 결과 불명 정책을 따른다.
