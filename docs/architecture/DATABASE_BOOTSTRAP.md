# 초기 PostgreSQL 기준 정보 (DEV-04b)

구현 범위는 PostgreSQL 마이그레이션 1개와 **1개 현장·1개 라인·3개 설비·48개 태그**의 반복 가능한 등록이다. [DB·인증 설계](DATA_AUTH.md) 전체의 구현 완료가 아니며, 운전 명령·생산실적·인증 기능과 수락 시험 T01~T12를 제공하지 않는다. 기준 태그는 [태그 계약](../../contracts/tags/README.md)과 `contracts/tags/tags.v1.json`이다.

## 저장 모델과 계약 경계

`0001_reference_data`는 `sites`, `lines`, `assets`, `tag_definitions`를 만든다. Alembic은 별도로 `alembic_version`을 관리한다. ORM 모델을 시작 시 `create_all()`로 적용하지 않으며 migration과 bootstrap은 명시적인 별도 작업이다.

| 테이블 | 제약·초기 데이터 |
|---|---|
| sites | ID PK·code UNIQUE. `site-local` 1개 |
| lines | site FK RESTRICT·`(site_id,code)` UNIQUE·site 조회 인덱스. `packaging-line-1` 1개 |
| assets | line FK RESTRICT·`(line_id,code)` UNIQUE·line 조회 인덱스. Infeed·Sealer·Outfeed 각 1개 |
| tag_definitions | logical_name UNIQUE·`(line_id,namespace_uri,node_identifier)` UNIQUE. line FK, `(asset_id,line_id,tag_group)`→asset `(id,line_id,code)` FK RESTRICT. line/asset 조회 인덱스 |

Line 그룹의 18개 태그는 line을 참조하고 asset_id는 NULL이다. Infeed 8개·Sealer 12개·Outfeed 10개는 **동일 라인의 해당 code 설비**를 참조한다. 별도 Line 설비를 만들어 세 설비 기준을 늘리지 않는다. CHECK는 그룹과 소유자 관계, 허용 OPC 자료형, String identifier, 읽기 전용 access, 양수 수집주기와 비어 있지 않은 주소/식별을 검사한다. 전체 48개·9개 아날로그·정확한 자료형/주소/1000ms는 계약 검증과 bootstrap의 일치 검사로 보장한다. DB는 다른 개수의 임의 관리자 INSERT까지 48개로 제한하지 않는다.

각 tag 행은 schema_version·namespace URI·원래 tag metadata·enums·quality 정책의 JSONB snapshot과 정규 JSON SHA-256을 보관한다. bootstrap은 hash뿐 아니라 모든 구조화 필드와 snapshot을 비교한다. 같은 logical ID의 단위·자료형·주소·정책 등이 달라지거나 DB 행이 수정되면 자동 덮어쓰지 않는다. 검토한 계약 변경과 후속 migration/명시적인 설정 변경 작업이 필요하다. namespace index는 저장하지 않는다.

초기 주소 UNIQUE는 **line 기준**이다. 아직 source/stream 등록 테이블이나 실제 연결 mapping이 없으므로 DATA_AUTH 설계의 source 기준 UNIQUE는 후속 source 모델에 추가해야 한다. 이 단계는 UInt64 관측값/카운터를 저장하지 않고 `UInt64`라는 태그 자료형 metadata만 저장한다.

## 로컬 실행

고정 의존성 설치(`./scripts/bootstrap-python.sh`), 실제 PostgreSQL 17, 대상 DB 생성과 migration DDL·bootstrap DML 권한이 필요하다. 앱 A 운영 DB를 SQLite로 대체할 수 없다. `HMI_DATABASE_DSN`은 이 명시적 도구의 연결 입력이며 `postgresql://` 또는 `postgresql+psycopg://` 형식으로 host·user·database를 지정해야 한다. 이 입력은 향후 API의 `HMI_DB_*`/secret-file 설정 파서 구현을 뜻하지 않는다.

DSN은 세션/비밀 관리 도구에서 환경변수로 주입한다. 비밀번호가 있는 URL을 명령줄·셸 이력·문서·저장소·로그에 붙여 넣지 않는다. URL의 예약 문자는 인코딩해야 하며 psycopg가 지원하는 보호된 pgpass 입력도 사용할 수 있다. 소스·alembic.ini에 연결 문자열 기본값은 없다. 누락된 DSN은 실패하며 DB를 자동 선택하거나 생성하지 않는다.

저장소 루트에서 DSN이 주입된 환경으로 실행한다.

```sh
.venv/bin/python -m alembic -c alembic.ini upgrade head
.venv/bin/python -m apps.api.bootstrap
.venv/bin/python -m apps.api.bootstrap
```

첫 bootstrap은 sites=1, lines=1, assets=3, tag_definitions=48을 INSERT한다. 다음 실행은 모두 0 INSERT이며 기존 데이터는 보존한다. 이미 다른 기준 정보가 있는 DB에서도 알려진 baseline ID만 검증/추가하고 나머지를 지우지 않는다. 먼저 migration을 적용하지 않았거나 지원 revision과 다르면 bootstrap은 실패한다. CLI는 DSN·드라이버 오류 내용·SQL 매개변수를 출력하지 않으며 실패 시 DB 상태 확인 후 재시도를 요구한다.

두 작업은 동일한 transaction advisory lock을 사용한다. migration DDL과 버전 변경은 한 트랜잭션으로 수행하고, bootstrap은 모든 기준 정보 INSERT/일치 검사를 한 트랜잭션으로 수행한다. 충돌하면 이전 INSERT도 rollback한다. 동시에 두 bootstrap을 실행해도 하나가 완료될 때까지 기다린 뒤 기존 행을 검증한다. 이 잠금은 이 도구 사이의 경합을 제어하며 임의 DB client를 막는 권한 시스템을 대신하지 않는다.

`downgrade base`는 태그와 기준 정보 테이블 및 데이터를 삭제한다. 다음 명령은 **폐기 가능한 시험 DB에서만** 실행하며 운영 DB에서는 검토한 백업·변경 절차가 선행되어야 한다.

```sh
.venv/bin/python -m alembic -c alembic.ini downgrade base
```

## 실제 PostgreSQL 검증

시험은 로컬 Docker 소켓을 명시적으로 선택하고 인증된 임시 PostgreSQL 17 컨테이너·DB·난수 비밀번호를 만든다. 비밀번호는 임시 보호 파일/메모리로만 전달하고 종료 시 컨테이너·볼륨·파일을 정리한다. PostgreSQL은 loopback의 임시 포트만 공개한다. 기존 앱 DB·사용자 볼륨·실제 `.env`를 읽거나 초기화하지 않는다. Docker의 endpoint/context/TLS 선택자만 해당 subprocess에서 제거하며 proxy·CA·registry credential 설정은 보존한다.

이미지는 아래 digest로 고정하며 시험이 자동 pull하지 않는다. 로컬 daemon과 해당 이미지가 준비되지 않으면 시험은 명시적으로 **실패**한다. 실제 PostgreSQL 시험을 조용히 skip하거나 SQLite/모의 DB로 대체하지 않는다. 이미지 pull은 환경의 network/proxy/CA 정책에 따라 별도로 준비한다.

```text
postgres:17-bookworm@sha256:3645570cccdfa447589da9f57dd740faa29b30938e861289a5574b6ca6b03826
```

```sh
.venv/bin/ruff check apps/api tests/integration/test_database_bootstrap.py
.venv/bin/ruff format --check apps/api tests/integration/test_database_bootstrap.py
.venv/bin/python -m pytest -q tests/integration/test_database_bootstrap.py
```

2026-10-20 검증: 위 targeted suite **25개 통과**. 실제 DB에서 upgrade→downgrade→upgrade, ORM metadata와 migration 일치, bootstrap 반복/동시 실행, 계약의 태그 개수·타입·주소·소유자, logical-ID 계약 충돌 시 선행 INSERT rollback, 고유성·부모 FK·다른 라인/그룹 참조·읽기 전용 CHECK 거절을 확인했다. CLI는 `python -m apps.api.bootstrap`로 실제 두 번 실행해 48→0 INSERT를 확인하고 자격증명이 stdout/stderr에 노출되지 않음을 검사했다.

남은 DEV-04 범위는 recipe/alarm 설정 버전과 검증된 시험 범위, 사용자·역할·세션·서비스 인증·DB 최소 권한 분리, source/stream·수집 배치/커서/누락·관측/집계, 작업지시·실행·명령·사건·실적·알람·정산·감사 테이블 및 관련 migration이다. 게이트웨이 SQLite와 실측 복원·운영 통합은 미구현이다. 시뮬레이터 SQLite의 부분 구현 범위는 [DEV-07a](SIMULATOR_STORAGE.md)를 따른다. 장비가 없는 시험용 recipe·alarm 값이나 기본 계정을 이 단계에서 임의로 생성하지 않는다. 마이그레이션 checksum 관리와 schema 확장 시 지원 revision 갱신은 후속 migration 운영 범위다.

## 후속 스키마와 migration 작성

현재 `upgrade head`는 `0003_user_auth`까지 적용한다. `0002_ingestion`의 수집 모델은 [수집 설계](INGESTION.md), `0003_user_auth`의 사용자·역할·세션은 [앱 A 인증](API_AUTH.md)을 따른다. 위 초기 단계의 검증 범위를 이후 기능 전체의 완료로 해석하지 않는다.

일부 migration은 완전한 ORM metadata 없이 고정 SQL DDL로 작성했다. Alembic 자동 생성은 비활성화했다. 초기 기준 정보 metadata만으로 자동 생성하면 수집·인증 테이블을 삭제 대상으로 오인하기 때문이다. 새 migration은 명시적으로 작성하고 실제 PostgreSQL upgrade/downgrade 및 기존 데이터 보존 시험으로 검증한다.
