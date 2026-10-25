# CX-37 품목 마스터

## 등록과 사용

품목은 ID·이름·단위·사용 여부를 갖는다. production_manage 계정이 POST /api/v1/items로 등록하고, read 계정(게스트 포함)이 GET /api/v1/items 또는 GET /api/v1/items/{id}로 조회한다. 변경 요청은 기존 session·Origin·CSRF 보호를 사용한다. 목록은 after_id·limit(1~100)·enabled 필터와 items/next_cursor 응답으로 페이지를 나눈다.

품목 ID가 등록 멱등 키다. 같은 ID와 같은 내용은 원래 결과를 반환하고 감사를 중복 추가하지 않으며, 다른 내용은409다. 새로운 등록은 item_master와 CREATE 감사가 한 트랜잭션이다. 이름은1~128자, 단위는1~32자이며 공백뿐인 값은 거절한다. enabled는 실제 boolean이고 기본true다.

schema21부터 새 공정 설정·작업지시는 실제 등록된 enabled 품목만 쓴다. 수동·서버 ID 자동 발급 모두 같은 도메인 검사를 통과한다. 이미 저장된 원래 ID/요청 영수증의 재조회는 먼저 처리한다. 알 수 없는 품목을 등록 시점에 몰래 생성하지 않는다. DB FK도 품목 존재를 보장한다. 이전 schema의 기존 기능은 유지하고 품목 API는 사용할 수 없다고 응답한다.

이번 범위는 목록·등록이다. 품목 정의와 단위는 불변이며 사용 여부도 등록할 때 지정한다. 수정·사용 전환·삭제 API는 추가하지 않았다. 기존 bind·운전·정산 조건도 변경하지 않았다. 불변 테이블이므로 가용성 조회에 UPDATE 권한을 요구하는 행 잠금은 사용하지 않는다.

## 0020 → 0021 이관

기존 recipe_versions와 work_orders의 서로 다른 item_id 합집합을 한 번만 등록한다. 이름=id, 단위=EA, enabled=true, origin=MIGRATION, created_by=null이다. 이 값들은 기존 문서에서 복원한 실제 이름·단위가 아니라 호환을 위한 명시적 기본값이다. 과거 생산 수량·등록 정의·시각·감사·멱등 영수증·ID counter는 변경하지 않는다. 실제 이름/단위를 수정하는 기능은 별도 계약이 필요하다.

그 뒤 두 등록 테이블의 item_id FK를 추가한다. master와 감사는 UPDATE/DELETE/TRUNCATE를 SQL trigger로 막고 runtime에는 SELECT/INSERT만 허용한다. 사용자 등록은 origin=USER와 실제 생성자, 이관 자료는 가상의 사용자 없이 origin=MIGRATION을 보존한다.

이관 전체는 한 트랜잭션이다. 품목/감사 또는 사용한 등록이 있으면 downgrade를 거절한다. 공개 적용 전 전체 백업과 행 수 대조가 필요하며, 사용 후 롤백은 근거를 삭제하는 downgrade 대신 백업 복원/전진 수정으로 계획한다. deploy/neon-http/upgrade-0020-0021.json과 grants-0021.json은 준비만 했고 공개 적용은 Claude가 한다.

## 배포 후속

Claude는 품목 선택/등록 UI와 공개 이관·grants를 반영한다. 자체 seed를 쓰는 UI 소유 브라우저 검사/시연 스크립트는 recipe/작업지시 전에 실제 품목을 명시적으로 등록해야 한다. 테스트용 임의 품목을 앱 시작 또는 모든 fixture에 자동 생성하는 우회는 두지 않는다. 운영 runtime·WO-T1·공개 DB는 이번 작업에서 변경하지 않는다.
