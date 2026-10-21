# 설비 앱 B 현장 콘솔 UI

`apps/simulator/static/index.html`, `console.js`, `console.css`는 [앱 B HTTP 콘솔](SIMULATOR_CONSOLE.md)의 같은 출처 정적 화면이다. `/`에서 제공하며 Node 빌드나 외부 CDN·폰트·JavaScript 라이브러리를 요구하지 않는다. 앱 A의 HMI·MES 화면과 계정·세션을 공유하지 않는다. 화면은 시뮬레이션 시험 환경임을 명시한다.

## 인증과 최신 관측

로그인은 `POST /auth/challenge`의 pre-auth CSRF와 HttpOnly cookie를 사용한 뒤 `POST /auth/login`을 수행한다. 세션 token은 JavaScript 응답에 없고 브라우저가 같은 출처 cookie로만 보낸다. CSRF는 메모리에만 두며 `localStorage`, `sessionStorage`, URL에 쓰지 않는다. 초기 암호 변경·일반 암호 변경·로그아웃 UI를 제공하며 암호를 로그나 저장소에 기록하지 않는다. 제출한 암호 입력칸은 지운다.

새로 고침 뒤 기존 cookie로 조회는 가능하지만 메모리 CSRF가 없으므로 조작하려면 다시 로그인한다. 조회 역할·초기 암호·재로그인 필요·관측 지연 상태에서는 변경 fieldset을 비활성화한다. 서버도 매 요청에 실제 권한·Origin·CSRF를 재검사한다. 화면의 비활성화가 권한 검사를 대신하지 않는다.

인증 중에는 1초마다 실제 `/snapshot`을 읽고 PLC 상태·제어/운전 모드·복구 게이트·가상 센서·수량·물품 위치와 48개 태그를 갱신한다. 브라우저가 숨겨져 있으면 polling을 쉬고 다시 보이면 새 관측을 받는다. 최근 성공 관측으로부터 3초가 지나거나 요청이 실패하면 값은 마지막 관측으로 흐리게 표시하고 조작을 차단한다. 통신 실패를 수량 0이나 정상 상태로 바꾸지 않는다. 로그아웃·인증 만료 시 개인 세션과 화면 관측을 지우며 이전 세션의 늦은 응답도 새 세션을 덮어쓰지 못한다.

## 명령과 완료 근거

여섯 명령은 `/commands`로 제출한다. 사용자 의도를 확인한 뒤 제출 직전 `/snapshot`의 새 제어 버전을 사용하며, 명령 ID를 `crypto.randomUUID()`로 한 번 생성한다. 요청 시각·5초 deadline은 해당 새 서버 관측 시각을 기준으로 생성하여 브라우저 시계 차이를 줄인다. UInt64 제어 버전·사건 순번·누적 수량·물품 ID는 정규 십진 문자열로 전달하고 필요한 표시만 `BigInt`로 계산한다. JSON UInt64를 `Number`로 변환하지 않는다.

접수와 실행 성공을 구분한다. 원래 명령 ID를 화면에 표시하고 `/commands/{id}`로 조회한다. 요청 응답이 유실되거나 ID를 찾지 못하면 결과 확인 필요로 남기고 새 ID로 자동 제출하지 않는다. 미확정 명령을 추적하는 동안 새 명령 제출은 막는다. 재시작 시 서버의 `pending_commands`도 원래 ID로 추적한다. 새로 고침 뒤 사용자가 ID를 직접 입력하여 조회할 수 있다. 명령 요청 원본과 추적 목록은 브라우저 메모리에만 있으므로 durable 재조회 근거는 서버 원장이다.

성공 응답만으로 공정 상태를 만들어 내지 않는다. 완료 evidence·적용 제어 버전과 최신 실제 PLC 피드백을 대조하고, START는 RUNNING·구동 피드백, PAUSE는 정지·PAUSED, 설정은 적용 버전·값, END_RUN은 `/runs/{id}`의 최종 요약·재공 0을 추가 확인한다. 현재 제어 버전이 완료 시점과 다르면 저장된 완료 근거와 현재 상태를 별도로 대조하도록 표시한다. 원본 요청을 모르는 복원된 명령의 인자까지 임의로 추정하지 않는다.

## 시험 입력과 명시적 정리

LOCAL/REMOTE·MANUAL/AUTO 변경, 비상정지·가드·설비 준비·고장·공압 시나리오를 제공한다. 정지 위치 대조용 구동 피드백·관측 속도 입력은 접힌 상세 영역에 두고 직접 시험 입력임을 표시한다. 입력은 편집 중 자동으로 덮어쓰지 않으며 제출 직전 현재 PLC 입력을 읽어 변경 항목과 합친다. 측정값을 생성하는 공정 tick은 서버에서만 수행한다.

복구는 정지·물품 위치·저장 상태·명령·조작자 확인의 다섯 항목을 사용자가 직접 선택한 뒤 확인창을 통과해야 한다. 중단된 원래 명령은 `/reconcile/{id}`에 같은 대조 항목을 전달하여 명시적으로 확정한다. 복구·중단 대조는 START를 수행하지 않고, 확인 항목은 작업 후 다시 해제한다.

폐기는 실제 snapshot의 슬롯에 있는 물품 ID만 선택하고 사유·정지 위치 확인·최종 확인창을 요구한다. 제출 직전 물품이 여전히 존재하는지 다시 확인하며 UInt64 물품 ID를 문자열로 보낸다. END_RUN이 재공을 자동 폐기하지 않는다. 최종 실행 요약 이후 현장 대조 버튼은 `/runs/{id}/settle`에 명시적 확인을 전달한다. 이는 다음 작업 연결을 위한 시뮬레이터 현장 대조이며 MES 사건 정산 완료를 의미하지 않는다.

## 검증과 남은 범위

```sh
node --check apps/simulator/static/console.js
.venv/bin/python -m pytest -q tests/integration/test_simulator_console.py
# Playwright Python 및 Chromium 설치 환경에서 별도 실제 브라우저 검증
.venv/bin/python scripts/check-simulator-browser.py
```

2026-10-20: JavaScript 문법 검사 통과. 실제 JavaScript UInt64 helper에서 2^53+1·2^63·최댓값의 무손실 보존과 숫자 token·선행 0·음수·초과 값 거절 7개 경계 검사를 수행했다. HTML의 ID 61개 중복 없음·JS 참조 일치·암호 form의 POST fallback도 확인했다. HTTP adapter의 정적 자원 검증은 `/`, JS, CSS의 정상 응답·올바른 MIME·CSP·no-store와 경로 이탈·허용되지 않은 Host 거절을 포함한다.

별도 Playwright/Chromium 154 시험에서 실제 `apps.simulator serve` subprocess·HTTP 서버·OPC listener·공정 ticker를 실행했다. 화면 클릭으로 로그인·초기 암호 변경·다섯 항목 복구·LOCAL/AUTO·LOAD_JOB·50°C 실제 가상 예열·START 생산·PAUSE 정지 피드백·동일 START ID 재요청의 `created=False` 및 수량/버전 불변·END_RUN 최종 수량 보존·현장 대조·작업 B 연결(수량 0, A 최종 요약 보존)·로그아웃 후 401을 확인했다. 잡히지 않은 JavaScript 예외가 없고 서버 subprocess도 정상 종료했다. `.local/qa/simulator-console.png`를 확인해 한글 렌더링과 화면 배치·비밀값 미노출을 점검했다. 이 브라우저 시험은 기본 pytest와 별도로 실행하며 Playwright/Chromium이 없으면 명시적으로 실패한다. 실제 장비 운전 검증이나 앱 A/MES 전체 T01~T12 통과를 의미하지 않는다.

화면에서 생성한 가상 생산 값이나 canned 성공 결과는 없다. 전체 T01~T12·앱 A HMI/MES·실제 장비·운영 복구/보관 완료를 주장하지 않는다. 장시간 세션·다중 브라우저 조작 경합·다양한 모바일/보조공학 환경·운영 HTTPS 배포와 전체 수락 검증은 후속 범위다. 정적 화면을 통한 조작도 PLC의 안전 조건·현재 버전·복구 제한을 그대로 통과해야 한다.
