# KULS v3 중단 보고서

2026-10-23 UTC. 브랜치 `codex/kuls-backend-v3`. **사용자의 “이제 또 실패하면 보고서만 작성하고 멈춰” 지시에 따라 추가 실패 확인 후 구현·재시도·후속 시험을 중단했다. 릴리스·프로젝트 완료가 아니다.**

## 마지막 실패

- 기준 커밋: `31c8e1416d4d671912c9439ee8cf1d2225759962`. 해당 커밋은 원격 브랜치에 푸시했다. 마지막 집중 실행에는 아직 커밋하지 않은 시험 파일2개의 수정본을 사용했고 source hash와 dirty=true를 기록했다.
- 진행 중이던 집중 묶음: **T05 실패 / T06 OPC 통과**, 총37.621초. JUnit 시작14:03:26.180516, 마지막 envelope 종료14:04:03.789479 UTC.
- T05는 두 번째 `RESET_FAULT`의 **실제 UI POST가 HTTP409**를 반환해 실패했다. 응답 body/reason을 수집하지 못해 **거절의 근본 원인은 미확정**이다. JSON의 `RESET_BLOCKED`는 앞선 첫 RESET의 정상 PLC 거절이며 이번409의 원인이 아니다.
- T06 OPC는 마지막 fresh capture와 실제 cut 시점에 맞춘2.000835초 GOOD /3.600762초 DELAYED /5.800934초 ALARM과 정상 복귀를 통과했다. 이 한 사례의 성공을 전체 수락 통과로 확대하지 않는다.
- 중단 지시를 받은 에이전트가 첫 실패 출력을 확인했을 때 이미 시작되어 있던2개 묶음은 종료된 상태였다. **지시 수신 후 새 시험 시작0회**이며 이후 수정·재시도·전체9개 브라우저 시험·다음 릴리스 실행은 하지 않았다.

[원본 JSON·JUnit 색인](evidence/CX-22-final-stop/index.json), [실행·중단·동결 소스 근거](evidence/CX-22-final-stop/provenance.json), [당시 미커밋 시험 수정 patch](evidence/CX-22-final-stop/uncommitted-fixture.patch.gz)를 보존했다. 실패 XML은 원문 bytes를 바꾸지 않은 gzip과 원본 SHA256으로 남겼다. 현재 시험 파일2개는 추가 편집·커밋하지 않고 미완료 상태로 유지한다. 보고서와 증거만 별도로 저장한다.

## 현재 판정과 남은 항목

| 항목 | 현재 판정 |
|---|---|
| OPS-02 공개0018·원격 시간 근거 | 관리 연결이 없어 실제 이관·권한 실측·공개 실연 미실행. 지시서 §6의 [순서별 인계](OPS-02.md) 제공 |
| CX-17 T01~T12 | 과거 분할17개 통과. 최종 커밋의 전체 수락은 아직 통과하지 못함 |
| CX-18 전체 백업·복원 | 관련153개와 실제 backup/restore/resume CLI 검증 근거 유지. 분할 결과와 중복 합산하지 않음 |
| CX-19 실제30분 단절 | 실제1,800.401859초 단절 후208.339389초 회복·원래 실행 정산 통과. 한도로 삭제된 태그 관측240개 손실은 기록 유지 |
| CX-20 7일 이력 성능 | 전체5,443,200원시/90,720집계 검산,10명 p95 cold1.398692초/warm1.343585초 통과. PG buffers cold/OS tmpfs cache warm 조건 |
| CX-21 실제8시간 | 사용자 지시로 제외·미실행 |
| CX-22 clean release | 새 no-cache build·전체 구성 운전·데이터 보존 재시작·독립 실행은 통과. 마지막 전체 묶음은17 passed/2 failed. 이후 집중 묶음도1 failed/1 passed여서 **릴리스 미통과·중단** |

원래 실패를 지우거나 기준을 완화하지 않았다. 전체 묶음의 [release5 manifest](evidence/CX-22-release-attempt5.json), [원본23개 archive](evidence/CX-22-release5-original-evidence.tar.gz), [원본 SHA 색인](evidence/CX-22-release5-original-evidence.index.json)을 보존했다. 공개 적용·최종 단일 수락·운영 Compose CLI 후속 검증은 미완료로 남긴다. 자동 재개하지 않는다.

실행 중이던 시험 프로세스·임시 PG·웹 추출 컨테이너·임시 clone은 소유 범위에서 정리했다. 보호된 preview PostgreSQL과 기존 실습 runtime은 유지한다. 비밀값·DB dump는 보고서에 포함하지 않았다.
