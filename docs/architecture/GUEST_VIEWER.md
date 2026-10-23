# 게스트 조회 세션

CX-06은 기본 비활성이다. 기존 `demo`와 일반 viewer 로그인·비밀번호 변경 정책은 그대로 적용한다. 공개 배포의 게스트 활성화는 사용자 승인 후 별도 운영 작업이며, 이번 변경에서 공개 환경 설정이나 `cloud_plant.json`은 수정하지 않았다.

## 명시적 설정

새 기능에는 Alembic `0017_guest_viewer`가 필요하다. 기존 0014에서도 게스트를 끈 기존 인증 경로는 새 테이블을 조회하지 않는다. 서버 시작은 계정이나 스키마를 만들지 않는다.

운영자가 명시적으로 다음을 실행한다. DB 연결은 기존 보호된 `HMI_DATABASE_DSN` 설정을 사용한다.

```sh
python -m apps.api bootstrap-guest --origin http://127.0.0.1:8002
python -m apps.api serve-user --origin http://127.0.0.1:8002 --enable-guest-viewer
```

`bootstrap-guest`는 `guest-viewer` 계정을 새로 만들고 `guest_viewer_accounts`에 전용 계정임을 기록한다. 이미 같은 이름의 계정이 있으면 거절하며 일반 계정을 전환하지 않는다. 임의의 비밀번호 해시를 만들지만 비밀번호를 보관·출력하지 않으며 해당 계정의 비밀번호 로그인과 변경을 항상 거절한다. 일반 `bootstrap-user`에서도 이 이름을 예약한다.

Vercel의 명시적 환경 설정은 `HMI_GUEST_VIEWER_ENABLED=true`이다. 생략 또는 `false`는 비활성이고 다른 값은 구성 오류다. 고정 패키지 정책에 활성 값을 추가하지 않았다. 활성 앱 시작은 전용 계정 표식, 활성 상태, `must_change_password=false`, 정확한 `{viewer}` 역할을 검증한다. 잘못된 구성이면 앱 생성을 거절한다.

코드 조합은 `UserAuth(..., guest_viewer=True)` 및 `UserHTTPConfig(..., guest_viewer=True)`를 함께 사용한다. 별도 provisioning 함수는 `apps.api.guest.bootstrap_guest(auth)`이다.

## HTTP 계약

- `GET /auth/config`: `{"guest_viewer": false}` 또는 `true`.
- `POST /auth/guest`: 정확한 허용 Origin과 `Content-Type: application/json`, 빈 객체 `{}`가 필요하다. 쿠키·CSRF·만료 응답은 기존 로그인과 같은 형태다. 비활성은 404, 잘못된 Origin은 403, 본문 오류는 415/422다.
- 쿠키는 기존과 같은 HttpOnly·SameSite=Strict·HTTPS Secure 정책을 사용하고 Max-Age는 1800초다. DB 절대 만료도 30분이며 일반 유휴 갱신으로 늘어나지 않는다.
- 게스트는 기존 세션 쿠키로 조회하며 기존 권한 검사에 의해 명령·작업지시·알람·정지 사유 등 쓰기는 403이다. 비밀번호 변경도 403이다. 전용 계정 역할 교체는 관리자에게도 거절한다.
- 비활성 인스턴스는 기존 게스트 세션도 401로 거절한다. 계정 활성·역할·표식은 매 세션 검사에서 다시 확인한다.

## 서버리스 동시성 및 감사

모든 게스트 발급은 PostgreSQL의 전용 사용자 행 잠금 아래 실행한다. 같은 DB를 사용하는 여러 프로세스와 트랜잭션 풀에서도 활성 세션 50개 상한과 IP당 분당 5회 제한이 원자적으로 적용된다. 폐기·절대 만료·유휴 만료 세션은 동시 세션 수에서 제외한다. 거절된 입장 시도도 IP 카운터에 반영한다.

IP는 ASGI 연결 상대를 정규화한 뒤 SHA-256으로 해시하고 `guest_rate_windows`에 저장한다. 요청의 forwarded 헤더는 신뢰하지 않는다. 프록시에서 개별 IP가 전달되지 않으면 같은 상대 IP의 보수적 제한을 공유한다. 오래된 1분 창은 다음 요청에서 지우며 활성 키는 최대 2048개다. 꽉 차면 새 키는 429다. 프로세스 메모리만으로 제한하지 않는다.

발급 시 `auth_audit`에 actor=`GUEST`, target_login=`guest-viewer`, action=`GUEST`를 같은 트랜잭션으로 기록한다. 비밀번호·토큰·원시 IP는 감사 기록에 남기지 않는다. 마이그레이션 다운그레이드는 GUEST 감사 증거가 있으면 CHECK 복원에서 거절한다. 운영 롤백에서 증거를 자동 삭제하지 않는다.
