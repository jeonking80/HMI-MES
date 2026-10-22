# 배포된 화면에 가상 설비 연결

Vercel의 웹·API와 Neon DB는 유지한다. 별도 설비 앱과 게이트웨이는 두 프로세스로 실행하며 암호화 OPC UA로 통신한다. 게이트웨이는 HTTPS로 Vercel 수집 API에 전송하고 PostgreSQL이나 설비 SQLite를 직접 열지 않는다.

## 현재 검증 경계

공개 앱의 DB 준비 검사와 사용자 로그인·암호 변경은 사용자 결과로 확인했다. 별도 임시 DB·실제 CLI·브라우저·OPC UA의 작업 등록/배포/연결/예열/시작/생산/일시정지/종료/SETTLED 대조는 `scripts/check-operable-system.py`로 통과했다. 공개 앱의 실제 설비 수집·명령과 영속 호스팅은 이와 별도로 확인해야 한다.

## 연결 파일 생성과 수동 적용

전용 런타임 `deploy/plant_runtime.py`가 사용하는 ID는 아래와 같다. 기존 preview 저장소는 사용하지 않는다.

| 항목 | 값 |
| --- | --- |
| Source | `demo-packaging-source-v1` |
| Stream | `events-main` |
| Gateway | `demo-packaging-gateway-v1` |
| Line | `packaging-line-1` |
| 초기 capture generation | 1 |

토큰은 아래 생성기로 새 보호 디렉터리에 한 번 생성한다. 기존 파일을 덮어쓰거나 기존 큐를 지우지 않는다. 실제 simulator/gateway 저장 identity와 일치하는 값이어야 한다.

```sh
.venv/bin/python -m deploy.generate_gateway_setup \
  --source-id demo-packaging-source-v1 --stream-id events-main \
  --gateway-id demo-packaging-gateway-v1 --line-id packaging-line-1 \
  --key-id demo-plant-key-1 --capture-generation 1 \
  --expires-at 2027-01-20T00:00:00+00:00 \
  --output-directory .local/commissioning/connection
```

관리자가 수행할 실제 적용은 두 가지다.

1. Neon SQL Editor에서 DB `hmi_mes_demo`, 역할 `neondb_owner`로 `prepare-source.sql` 전체 실행. 기존 초기화 SQL이나 계정 seed를 다시 실행하지 않는다. 같은 binding 재시도는 기존 커서·복구 상태를 보존하며 다른 binding은 거절한다.
2. Vercel 프로젝트의 환경변수에 `HMI_GATEWAY_CREDENTIALS_JSON`을 **Production** 범위로 추가하고 `vercel-gateway-credentials.json` 전체를 Value에 입력한다. 환경변수 저장 후 Production을 **Redeploy**한다. 기존 DSN·Origin은 변경하지 않는다.

`token.secret` 원문은 게이트웨이에만 전달한다. Vercel JSON에는 해시만 들어 있다. 토큰이나 앱/DB 비밀번호를 채팅·Git에 복사하지 않는다. 제공한 날짜 이후에는 운영자가 명시적으로 토큰을 교체해야 한다.

## 이 클라우드에서 연결 확인

준비된 `.local/commissioning/runtime`은 영속 파일을 보존하지만 **클라우드 환경이 중단되면 프로세스도 중단될 수 있다**. 이를 상시 호스팅으로 간주하지 않는다. 외부 접속 정책에 `hmi-mes.vercel.app` 허용도 필요하다.

```sh
.venv/bin/python deploy/plant_runtime.py --directory .local/commissioning/runtime prepare
.venv/bin/python deploy/plant_runtime.py --directory .local/commissioning/runtime serve \
  --token-file "$PWD/.local/commissioning/connection/token.secret"
```

새 저장소에 한해서 `prepare --initialize`를 명시한다. 일반 재실행은 `prepare`만 사용한다. 기동 후에는 설비가 복구 필요 상태로 멈춘다. 정지 출력·물품 위치·저장소·미결 명령을 실제 확인한 운영자가 별도 `commission` 명령으로 복구·REMOTE/AUTO를 설정한다. 이 명령은 START나 작업을 만들지 않는다. 현재 암호 파일을 지정하며 최초 암호 변경 후에는 `console-password`를 사용한다.

```sh
.venv/bin/python deploy/plant_runtime.py --directory .local/commissioning/runtime commission \
  --password-file .local/commissioning/runtime/console-initial-password \
  --new-password-file .local/commissioning/runtime/console-password \
  --outputs-stopped --positions-verified --storage-reconciled \
  --commands-reconciled --operator-confirmed
```

## 상시 호스팅 배포안

저장소 루트 `render.yaml`은 Render의 **유료 Starter 웹 서비스와 1GB 영속 디스크**를 사용하는 검토용 배포 구성이다. 아직 서비스나 유료 리소스를 생성하지 않았다. 생성 전 Render 계정 소유자가 현재 요금·디스크 요금·결제 조건을 확인해야 한다. Render 계정 접근 권한은 이 작업 환경에 없다.

계정 소유자가 [Render Blueprint 가져오기](https://render.com/deploy?repo=https://github.com/jeonking80/HMI-MES)를 열고 저장소·구성을 검토한다. `HMI_GATEWAY_TOKEN`에는 위 `token.secret` 원문을 비밀값으로 입력한다. 토큰은 보호 디스크 파일로 저장되며 자식 프로세스의 환경에서 제거된다. 다른 토큰으로 자동 덮어쓰지 않는다.

이미지는 `deploy/plant-service.Dockerfile`로 빌드한다. 웹에 노출되는 것은 `/health/live`의 프로세스 생존 여부뿐이며, 설비 콘솔과 OPC 포트는 컨테이너 loopback에 둔다. 초기 디스크 구성이 성공한 뒤 `HMI_PLANT_ALLOW_INITIALIZE=false`로 변경하여 저장소 유실 때 새로 초기화하는 것을 금지한다. 디스크와 원본 큐·사건을 유지하고 한 소유 프로세스만 실행한다. 같은 source를 이 클라우드와 Render에서 동시에 실행하지 않는다. 기동 전 기존 소유 프로세스를 종료하고 저장소·미전송 큐·원본 사건을 보존하여 이전해야 한다.

기동 후 운영자는 Render Shell에서 동일한 `commission` 명령을 `/var/data/runtime`과 `/app/.venv/bin/python` 경로로 실행한다. 이 보조 명령은 실행·재공·출력이 없는 최초 준비에 한정한다. 기존 작업이 남은 재기동에서는 자동 복구하거나 초기화하지 않으며 기존 설비 콘솔의 명령·위치·수량 대조 절차가 필요하다. 이 배포안의 콘솔은 loopback이므로 관리자는 서버 내부에서 인증된 콘솔 API를 사용해야 한다. 공개 앱 B 콘솔 배포까지 구현된 것으로 간주하지 않는다. 앱 A에서 값이 갱신되는 것을 확인한 후 [작업자 사용 순서](../docs/OPERATOR_GUIDE.md)를 따른다.

동봉된 개발 OPC 인증서는 30일 유효하다. 장기 운영에는 중단·백업·양측 인증서 교체·검증을 포함하는 별도 인증서 관리가 필요하다. 이 배포안만으로 전체 기획의 운영·장시간 수락이 완료되는 것은 아니다.
