# Neon·Vercel 수동 배포

웹 화면과 사용자·수집 API는 Vercel, 운영 PostgreSQL은 Neon을 사용한다. 설비 시뮬레이터와 OPC 게이트웨이는 별도 장기 실행 프로세스다. Vercel 함수 안에서 설비 공정이나 OPC 수집 루프를 실행하지 않는다.

## 현재 적용 상태

사용자 콘솔 결과로 Neon 프로젝트 `hmi-mes-demo`, production 브랜치의 DB `hmi_mes_demo`에 초기 스키마 `0014_mixed_alarm_runtime`, 공장 1·라인 1·설비 3·태그 48·역할 4개가 저장된 것을 확인했다. `demo`·`viewer` 앱 계정도 생성됐으며 최초 로그인 시 비밀번호 변경이 필요하다. 사용자가 DB 역할 `hmi_app`의 LOGIN과 비밀번호 설정을 확인했다. **이 DB에 초기화·계정 SQL을 다시 실행하지 않는다.**

사용자 Vercel 콘솔에서 main `57aef27`의 Production 배포 Ready와 실제 도메인 `hmi-mes.vercel.app`을 확인했다. 사용자 브라우저의 `/api/ready` 응답은 `{"ready":false}`이므로 실제 Neon 런타임 연결은 아직 확인되지 않았다. 로그인과 별도 게이트웨이 연결도 후속 검증한다. Vercel 배포 Ready와 DB 준비 성공을 구분한다.

## 1. Neon 연결 문자열

Neon **Connect**에서 다음을 선택한다.

- Branch: `production`
- Database: `hmi_mes_demo`
- Role: `hmi_app`
- Connection pooling: ON

연결 문자열의 사용자·DB가 위 값인지 확인한다. SQL로 만든 역할에 초기 비밀번호가 없으면 콘솔 Reset password가 실패할 수 있다. 이 경우 소유자로 `ALTER ROLE hmi_app WITH LOGIN PASSWORD '직접_생성한_비밀번호';`를 실행해 최초 비밀번호를 설정한다. 이미 설정했다면 반복하지 않는다.

연결 문자열과 암호는 채팅·Git·문서에 붙여 넣지 않는다. Vercel 환경변수 화면에 직접 입력한다. API는 `hmi_app`만 허용하며 TLS 인증서·호스트 검증을 강제한다. 소유자·마이그레이션 역할 연결 문자열을 런타임에 사용하지 않는다.

## 2. Vercel 프로젝트 가져오기

GitHub에 배포 파일이 반영된 브랜치를 확인한 뒤 **Create New → Project**에서 `jeonking80/HMI-MES`를 가져온다. 실제 코드가 있는 브랜치를 Production Branch로 선택한다. README만 있는 브랜치는 배포할 수 없다.

| 설정 | 값 |
| --- | --- |
| Project Name | `hmi-mes-demo` (사용 가능 여부에 따라 변경) |
| Framework Preset | Other |
| Root Directory | 저장소 루트 (`.`) |
| Build / Output | 저장소 `vercel.json` 설정 사용 |
| Node.js | 22 LTS 또는 24 LTS |
| Python | `.python-version`의 3.12 |

`vercel.json`이 웹 빌드와 `api/index.py` Python 함수를 구성한다. 화면은 `VITE_SCREEN_TRANSPORT=polling`으로 빌드된다. 정적 파일과 API는 같은 도메인을 사용한다.

## 3. 환경변수

Production 환경에 다음 값을 입력한다. Preview를 쓰려면 해당 도메인·환경변수도 별도로 맞춘다.

| 이름 | 값 |
| --- | --- |
| `HMI_DATABASE_DSN` | 위 Neon `hmi_app` 풀링 연결 문자열 전체 |
| `HMI_PUBLIC_ORIGIN` | 실제 Production 주소. 예: `https://hmi-mes-demo.vercel.app` |

Origin에는 끝 `/`나 경로를 넣지 않는다. 프로젝트 이름과 실제 할당 도메인이 다를 수 있으므로 배포 후 **Domains**의 실제 주소를 확인한다. 주소가 달라지면 환경변수를 수정하고 **Redeploy**한다. 로그인·변경 요청은 정확한 HTTPS origin을 검사한다.

`HMI_GATEWAY_CREDENTIALS_JSON`은 게이트웨이를 연결할 때 설정한다. 미설정 시 내부 수집은 503으로 닫히며 사용자 로그인·조회 API와 구분된다. 임의 토큰이나 가짜 설비 데이터를 넣지 않는다. Neon·Vercel API 키는 이 수동 배포에 필요하지 않다.

## 4. 배포 후 확인

1. 실제 Production URL의 `/api/health`에서 `{"alive":true}`를 확인한다.
2. `/api/ready`가 HTTP 200, `{"ready":true}`인지 확인한다. 503이면 고정 `stage`·`reason`으로 연결·인증·TLS·권한·스키마·태그 기준 정보 중 실패 지점을 확인한다. 응답에 원본 오류나 연결 문자열을 포함하지 않는다. 비밀값을 로그에 출력하지 않는다.
3. 루트 화면에서 보호 파일 `.local/neon-demo/LOGIN.md`의 초기 앱 계정으로 로그인하고 비밀번호를 변경한다. 이 파일은 로컬 생성 파일이며 Git에 포함되지 않는다. DB 역할 암호와 앱 로그인 암호는 서로 별개다.
4. `viewer`는 조회만 허용되는지 확인한다. 실제 브라우저의 HTTPS·쿠키·Origin 동작은 이 공개 배포에서 검증한다.
5. 설비 원천을 연결하기 전에는 미수신·불명 표시가 정상이다. 로그인 성공만으로 설비 연동·제어·전체 수락 완료를 표시하지 않는다.

별도 설비·게이트웨이 설정은 [설비 앱 실행](../docs/architecture/SIMULATOR_RUN.md)과 [수집·명령 실행](../docs/architecture/M2_RUN.md)을 기준으로 연결한다. Vercel 환경에는 로컬 개발 컨테이너의 DB 주소를 사용하지 않는다.

## 새 DB를 처음 준비할 때만

PostgreSQL 17의 빈 DB `hmi_mes_demo`에서 CREATEROLE 권한이 있는 소유자로 `neon-init.sql` 전체를 실행한다. 기존 자료나 역할이 있으면 중단하도록 만들어졌으며 자동 업그레이드용 파일이 아니다. 신규 설치 SQL의 재생성·일치 검사는 다음 명령을 사용한다.

```sh
.venv/bin/python deploy/generate_neon_init.py --check
.venv/bin/python deploy/generate_neon_demo.py --help
```

계정 생성기는 임시 PostgreSQL에서 비밀번호 해시를 만들고 보호된 로컬 파일에만 저장한다. 생성된 seed와 LOGIN 파일을 공개 저장소에 추가하지 않는다.
