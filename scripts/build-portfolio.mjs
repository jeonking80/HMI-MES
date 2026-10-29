/** Render the portfolio from real captured screens; does not contact the application, the database or external AI. */
import {readFile,writeFile,mkdir} from 'node:fs/promises';
import {resolve,dirname} from 'node:path';
import {fileURLToPath} from 'node:url';
import {createRequire} from 'node:module';
import {execFileSync} from 'node:child_process';
const require=createRequire(import.meta.url);
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const sourceCommit='59a1064c0ce855532f2abc91aafa63733891327f';
const capturedAt='2026-10-29';
const root=resolve(dirname(fileURLToPath(import.meta.url)),'..');
const output=resolve(root,'apps/web/public/portfolio');await mkdir(output,{recursive:true});
const asset=async name=>'data:image/png;base64,'+(await readFile(resolve(root,'docs/assets/portfolio',name+'.png'))).toString('base64');
const fig=async(name,caption,cls='')=>`<figure class="${cls}"><div class="shot"><img src="${await asset(name)}" alt="${caption}"></div><figcaption>${caption}</figcaption></figure>`;
const p=(title,text)=>`<div class="point"><h3>${title}</h3><p>${text}</p></div>`;
const tag=s=>`<span class="tag">${s}</span>`;
const pages=[];
function page(kicker,title,sub,body,cls=''){pages.push(`<section class="page ${cls}"><header><div class="eyebrow">HMI · MES <span>/ ${kicker}</span></div><h1>${title}</h1><p class="subtitle">${sub}</p></header><div class="page-body">${body}</div><footer><span>전우석 · HMI-MES 설비 운영 시스템 · ${capturedAt.replaceAll('-','.')}</span><span>__PAGE_NUMBER__</span></footer></section>`)}

page('프로젝트 개요','설비가 실제로 한 일만 실적이 됩니다','가상 포장라인 · 실제 암호화 OPC UA · 웹 HMI · MES 작업지시와 정산',`
<div class="cover-grid"><div><div class="cover-name">HMI<br><span>MES.</span></div><p class="lead">가상 PLC가 판정하고,<br>OPC UA로 수집하고,<br>정산이 끝나야 다음 작업.</p><div class="tags">${['1인 프로젝트','AI 코딩 에이전트 활용','공개 데모'].map(tag).join('')}</div><p><strong>작성자 전우석 · 개발 기간 9일</strong></p><p class="muted">2026.10.20 ~ 10.28 · 커밋 264개<br>기획 방향·운영 규칙 결정, 화면 검수와 수정 요구, 개발 조율<br>Codex · Claude Code를 활용한 구현과 검증</p></div>${await fig('11-monitor-running',`공개 배포 hmi-mes.vercel.app · ${capturedAt} 실제 운전 중 라인 모니터링. 값은 설비 앱이 OPC UA로 보낸 수집값입니다.`)}</div>`,'cover');

page('문제 정의와 구현 범위','“버튼을 눌렀다”와 “설비가 움직였다”는 다릅니다','설비 현장에서 화면이 거짓말하기 쉬운 지점을 처리 규칙으로 정했습니다.',`
<div class="split"><aside>${p('명령의 결과를 모르는 순간','요청 접수와 실행 결과를 분리하고, 응답이 끊기면 같은 명령 ID로 조회만 합니다.')}${p('통신이 끊긴 동안의 값','측정값마다 품질과 시각을 보관합니다. 불명을 0이나 정상으로 바꾸지 않습니다.')}${p('재전송과 재시작','생산 사건마다 고유 ID와 원래 실행 ID를 붙여 실적이 두 번 잡히지 않게 합니다.')}${p('작업 전환','재공을 정리하고 설비 최종 수량과 대조한 뒤에만 다음 작업을 연결합니다.')}<div class="note">KULS 공개 HMI 구성예시 기준 · 현장설비 → Master PLC → OPC 서버 → HMI·MES 서버 → 모니터링의 각 계층을 직접 구현</div></aside>${await fig('12-zone-sealer','실링기 상세 창. 운전 중 바꾼 온도 155 ℃ · 공압 5.8 bar · 속도 11 m/min이 설비 수집값으로 돌아온 화면입니다.')}</div>`);

const architecture=`<div class="architecture"><div class="node">앱 B 설비 시뮬레이터<br><small>투입 · 실링 · 배출 + 가상 Master PLC + OPC UA 서버</small></div><div class="link">↕ OPC UA · Basic256Sha256 서명·암호화 · 인증서 검증</div><div class="node">I/O 게이트웨이<br><small>SQLite 버퍼 · DB 커밋 ACK 후에만 삭제</small></div><div class="link">↕ HTTPS · 수집 배치 / 명령 전달·결과 보고</div><div class="arch-row"><div class="node top">앱 A API · Python<br><small>세션·역할·CSRF → 명령 원장 · 수집 · 정산</small></div><div class="link side">⟷<br>HTTPS</div><div class="node">브라우저<br><small>웹 HMI · MES 화면</small></div></div><div class="link">↕ SQL</div><div class="node db">PostgreSQL<br><small>작업지시 · 실행 · 생산 사건 · 명령 · 알람</small></div></div>`;
page('기술 구성','두 앱은 DB를 공유하지 않고 OPC UA로만 연결합니다','앱 A 운영 시스템 · 앱 B 설비 시뮬레이터 · 게이트웨이 · PostgreSQL',`
<div class="split equal"><div>${architecture}<p class="caption">설명용 구조도. 게이트웨이와 브라우저는 DB에 직접 붙지 않고 API만 거칩니다. 운전 허용 여부와 실행 결과는 앱 B의 가상 PLC가 최종 판정합니다.</p></div><div class="stack">${await fig('02-control-power','운전 제어의 설비 전원 단계. 원격 실행 환경의 설비 앱을 켜고 끕니다.','short')}<table><tr><th>계층</th><th>선택과 목적</th></tr><tr><td>설비</td><td>asyncua OPC UA 서버 · 가상 PLC · SQLite 원자 저장</td></tr><tr><td>연동</td><td>OPC UA 클라이언트 · 디스크 버퍼 · 재전송</td></tr><tr><td>운영</td><td>Python API · PostgreSQL · Alembic</td></tr><tr><td>화면</td><td>React · TypeScript · Vite</td></tr><tr><td>데모</td><td>Vercel + Neon 무료 요금제 · 설비는 임시 클라우드 실행</td></tr></table></div></div>`);

page('운전 흐름','전원부터 정산까지 다섯 단계로 안내합니다','작업자는 지금 단계에서 할 일만 봅니다. 다음 단계로 넘어갈 조건은 서버와 PLC가 판정합니다.',`
<div class="split"><aside><ol class="steps"><li><b>설비 전원</b><span>설비 앱·게이트웨이 기동, 통신 정상 확인</span></li><li><b>작업 준비</b><span>배포된 작업지시를 한 번에 불러오기</span></li><li><b>예열</b><span>실링 온도 도달을 PLC가 판정, 자동 시작 선택</span></li><li><b>생산</b><span>양품 / 목표, 일시정지·재개, 운전 중 설정 변경</span></li><li><b>종료·정산</b><span>재공 정리 → 최종 수량 대조 → 다음 작업</span></li></ol><div class="note">촬영 시나리오: 작업지시 등록·배포 → 불러오기 → 예열 → 생산 → 일시정지·재개 → 종료 → 정산 → 기존 작업지시 재실행</div></aside>${await fig('09-control-running','운전 중인 운전 제어 화면. 단계 바, 현재 단계 패널, 라인 조건이 함께 보입니다.')}</div>`);

page('운전 중 설정 변경','속도·온도·공압을 바꾸되, 허용 여부는 PLC가 정합니다','화면은 요청만 보냅니다. 범위 밖 요청은 서버가 먼저 막고, 최종 판정은 PLC가 합니다.',`
<div class="split"><aside>${p('레시피 기준 허용 폭','라인 속도 ±20% · 실링 온도 ±10 °C · 실링 공압 ±0.5 bar. 기준은 작업에 연결된 공정 설정 버전입니다.')}${p('PLC 최종 판정','실링 공압 4.5~6 bar 범위 밖이면 PLC가 거절합니다. 화면은 거절 사유를 그대로 표시합니다.')}${p('컨베이어는 하나의 라인 속도','투입·실링·배출이 같은 라인 속도를 공유합니다. 모니터링 화면에서도 같은 값을 조절합니다.')}${p('결과까지 추적','변경 요청도 일반 명령과 같이 원래 명령 ID로 접수·결과를 확인하고, 실행별 변경 기록을 남깁니다.')}</aside>${await fig('10-control-setpoint','공압이 들어간 공정 설정 v2로 운전 중. 바꿀 값과 허용 범위, 적용 전 변경 내용을 함께 보여 줍니다.')}</div>`);

page('라인 모니터링','세 설비의 상태와 데이터 품질을 한 화면에서 봅니다','공정도 중심 화면 · 설비별 상세 창 · 휴대폰 폭 대응',`
<div class="wide-narrow">${await fig('11b-monitor-speed','라인 속도 카드와 설비 상태. 세 번째 실행(WO-20261029-002) 중 데스크톱 1440px 폭에서 촬영했습니다.')}${await fig('20-m-monitor','같은 기능의 휴대폰 폭(390px) 화면. 첫 번째 실행(WO-20261029-001) 중에 촬영했습니다.')}</div>`);

page('작업지시와 공정 설정','작업지시는 한 번 만들고, 여러 번 실행합니다','작업지시는 계획이고, 실행은 회차입니다. 실적은 회차마다 원래 실행 ID에 귀속합니다.',`
<div class="split"><aside>${p('등록 · 배포 · 수정 · 취소','건별로 관리하고 누가 언제 무엇을 했는지 진행 기록에 남깁니다.')}${p('재실행','같은 작업지시를 다시 불러오면 새 실행 ID를 발급합니다. 이전 회차의 실적은 그대로 보존됩니다.')}${p('공정 설정 버전','온도·속도·공압을 버전으로 고정합니다. 배포 후에는 수정하지 않고 새 버전을 만듭니다.')}<div class="note">이번 촬영: 기존 작업지시 WO-002의 3회째 실행과, 공압 5.5 bar를 넣은 공정 설정 v2로 새 작업지시 실행</div></aside>${await fig('16-order-history','기존 작업지시를 다시 실행한 뒤의 진행 기록. 실행 회차와 회차별 결과가 쌓입니다.')}</div>`);

page('명령 접수와 결과 불명','응답이 끊겨도 새 ID로 다시 실행하지 않습니다','접수 · 실행 · 결과 불명을 구분합니다. 확인되지 않은 명령은 원래 ID로만 조회합니다.',`
<div class="split"><aside>${p('접수와 결과 분리','API는 명령을 원장에 예약하고, 게이트웨이가 PLC에 전달한 뒤 결과를 따로 조회해 보고합니다.')}${p('결과 불명은 불명으로','만료나 통신 단절로 결과를 모르면 OUTCOME_UNKNOWN으로 남기고, 같은 명령 ID의 조회만 허용합니다.')}<div class="note"><b>촬영 중 실제 사례 2건</b><br>① 종료 요청(WO-001): 응답 직후 결과 불명 → 같은 원래 ID(78b67cf6…)를 다시 조회해 ‘실행 완료’를 확인했고, 정산 수량과 일치했습니다(9쪽 화면).<br>② 설정 변경(WO-002): 화면에는 결과 불명으로 남았습니다. 설비 수집값은 바뀐 값을 보였지만 원래 ID 조회 결과는 확인하지 않았으므로 성공으로 기록하지 않습니다.<br><b>작업자 조치</b> ‘지난 요청 결과 찾기’에서 원래 ID로 조회 → 설비 현재값 확인 → 같은 변경을 새 ID로 다시 보내지 않습니다. 응답이 끊긴 원인은 아직 조사하지 않았습니다.</div></aside>${await fig('14-control-paused','일시정지 명령이 성공한 직후. 명령 결과와 라인 상태가 함께 표시됩니다.')}</div>`);

page('수집 정합성과 정산','같은 사건이 두 번 와도 실적은 한 번만 반영합니다','생산 사건 · 중복 제거 · 처리 커서를 한 트랜잭션에 저장하고, 정산으로 설비 수량과 대조합니다.',`
<div class="split"><aside>${p('사건 단위 멱등','생산 사건마다 고유 ID와 원래 실행 ID를 붙입니다. 이미 받은 사건은 다시 반영하지 않습니다.')}${p('커밋 ACK 후 버퍼 삭제','게이트웨이는 DB 커밋 응답을 받은 사건만 버퍼에서 지웁니다. 응답이 끊기면 같은 배치를 다시 보냅니다.')}${p('누락은 누락으로','수신 순번에 빈 구간이 생기면 정산 완료로 바꾸지 않고 확인 필요로 남깁니다.')}${p('정산 후 다음 작업','종료 요청 → 신규 투입 중단 → 재공 정리 → 최종 요약 대조가 끝나야 다음 작업을 불러옵니다.')}</aside>${await fig('15-control-settled','종료 후 정산 결과. 직전 작업의 양품·불량이 확정된 뒤 작업 준비 단계로 돌아갑니다.')}</div>`);

page('대시보드와 보고서','하루 생산과 실행별 실적을 같은 근거로 집계합니다','가동·정지·불명 시간을 나눠 보여 줍니다. 근거가 없는 시간은 가동으로 계산하지 않습니다.',`
<div class="split equal"><div>${await fig('17-dashboard','촬영 당일 대시보드 상단. 설비가 꺼져 있던 시간은 ‘불명’으로 남겨 가동 비율이 1.1%로 보입니다.','short')}${p('불명은 가동으로 세지 않음','하루 중 설비 데이터가 없는 98.4%를 가동·정지로 추정하지 않습니다. 그래서 가동률이 낮게 보이며, 화면 상단에 그 이유를 적습니다.')}${p('회차별 실적','같은 작업지시 WO-002의 1·2·3회째가 따로 집계됩니다. 정산 상태와 수율도 회차마다 다릅니다.')}</div>${await fig('18b-report-detail','실행별 보고서. 작업별 실적 표와 WO-20261029-002 실행 상세(가동 3분 5초 · 정지 0초 · 불명 0초)입니다.')}</div>`);

page('트렌드와 SPC','공정값의 흐름과 관리 상태를 확인합니다','태그별 기간 조회와 실링 온도·공압 관리도. 데이터가 부족하면 계산하지 않고 그렇게 표시합니다.',`
${await fig('13-trends','실링 현재·설정 온도 트렌드(1시간). 작업지시가 돈 구간을 배경으로, 설비가 꺼진 시간은 ‘데이터 없음’으로 표시합니다.','chart-wide')}<div class="chart-below"><div>${p('설정 변경이 그대로 보임','운전 중 설정을 150 → 155 ℃로 바꾼 계단이 오른쪽 끝에 있습니다. 설비가 꺼진 시간은 선을 잇지 않습니다.')}${p('데이터 부족 처리 사례','SPC 관리한계는 실행 처음 20분의 부분군으로 계산합니다. 이번 실행은 몇 분짜리라 관리도를 그리지 않고 이유를 표시했습니다. 근거가 부족할 때 수치를 만들지 않는 처리입니다.')}</div>${await fig('19-spc','SPC 관리도 화면. 실행이 짧아 관리도 대신 이유를 표시했습니다.','spc-small')}</div>`);

page('검증 결과와 한계','통과한 범위와 남은 기준을 구분했습니다','시험 개수 · 공개 배포 운전 · 수락 시험 · 장시간 성능을 하나의 완료 주장으로 합치지 않습니다.',`
<div class="split equal"><div><div class="metrics"><div><strong>2,354</strong><span>Python 시험 · 2026-10-23 전체 로컬 검사 · 커밋 c8a24c1</span></div><div><strong>28</strong><span>DB 마이그레이션</span></div></div><table><tr><th>검증</th><th>결과와 범위</th></tr><tr><td>전체 로컬 검사</td><td>2026-10-23 · 커밋 c8a24c1 기준 통과 · Python 2,354 · 웹 91 · 타입·린트·빌드</td></tr><tr><td>30분 단절 복구</td><td>과거 로컬 구성(소스 fc6296e): 1,800초 단절 후 208.3초 만에 회복 · 통과. 앞선 두 번은 실패, 단절 중 태그 관측 240개 손실은 별도 기록</td></tr><tr><td>7일 이력 조회</td><td>합성 7일 이력 · 로컬 RAM 디스크: 사용자 10명 동시 p95 1.34~1.40초 · 통과. 공개 Neon 성능 아님</td></tr><tr><td>공개 배포 운전</td><td>${capturedAt} 이 문서의 촬영 실행 3건 · 모두 정산 완료</td></tr></table><p class="caption">시험 개수는 자동화 시험의 실행 수이며 최신 버전 전체 통과를 뜻하지 않습니다. 10-24 이후 변경은 변경 범위 시험만 실행했습니다.</p></div><div><div class="warning"><b>최신 버전에서 다시 확인하지 않은 것</b><p>과거 로컬 구성에서 30분 단절 복구와 합성 7일 이력 조회 성능을 검증했습니다. 최신 공개 배포에서는 같은 기준을 재검증하지 않았습니다. 수락 시험 T01~T12는 17개로 나눠 통과했지만 한 번에 묶은 실행은 하지 않았고, 8시간 연속 운전은 범위에서 뺐습니다.</p></div><div class="warning"><b>공개 데모의 제약</b><p>공개 배포에는 알람 생성이 연결되지 않았습니다(설치된 규칙 0개). 무료 DB 한도를 넘겨 새 프로젝트로 이전했고, 시험 데이터 삭제가 수집 순번 검증을 깨뜨려 수집 스트림을 초기화했습니다. 작업지시·계정·공정 설정은 보존했습니다.</p></div><p class="muted">설비는 소프트웨어 시뮬레이터이며 실물 PLC·현장 설비 검증이 아닙니다. 상용 현장 운영 성과로 표시하지 않습니다.</p></div></div>`);

page('개발 방식과 사용자 피드백','화면을 직접 써 보며 누락을 찾고, 규칙을 정했습니다','기준 문서 → 작은 작업 단위 → 로컬 검증 → 공개 배포 확인. 미실행·실패는 통과로 기록하지 않았습니다.',`
<div class="decision-grid"><div class="decision"><span class="case-label">직접 발견 · 방향 결정</span><h2>① 작업할 때마다 작업지시를 새로 만들어야 했다</h2><p><b>발견</b> 한 번 실행한 작업지시는 다시 불러올 수 없어, 같은 제품을 또 만들려면 지시서를 새로 등록해야 했습니다.</p><p><b>검토한 대안</b> (가) 지금처럼 실행마다 새 작업지시 등록 (나) 같은 작업지시를 회차로 다시 실행</p><p><b>선택과 이유</b> (나). 작업지시는 만든 뒤 유지하는 계획이어야 하고, 실적이 섞이지 않도록 회차마다 새 실행 ID를 받게 했습니다.</p><p><b>확인한 결과</b> 공개 배포에서 WO-20261028-002를 세 번 실행했고, 진행 기록과 보고서에 1·2·3회째 실적이 따로 남았습니다(7·10쪽).</p></div><div class="decision"><span class="case-label">직접 발견 · 수정 요구</span><h2>② 제어 화면인데 온도·공압을 못 바꿨다</h2><p><b>발견</b> 운전 중 바꿀 수 있는 값이 속도뿐이었고, 공압은 공정 설정에도 없었습니다.</p><p><b>판단</b> 화면에서 바꿀 수 있게 하되, 허용 폭은 공정 설정 기준으로 서버가 먼저 막고 PLC가 최종 판정하도록 정했습니다.</p><p><b>구현·검증</b> 공정 설정에 공압 항목을 추가하고, PLC 공압 범위와 운전 중 변경 허용 폭을 시험으로 고정했습니다. 화면 검사만으로는 다른 경로의 요청을 막지 못하므로 판정은 설비 쪽에 남겼습니다.</p></div></div><div class="ownership"><b>본인 기여:</b> 기획 방향과 운영 규칙 결정, 화면 검수와 문제 발견, 위 두 사례의 방향 선택, 개발 조율. <b>AI 지원:</b> 코드 작성·시험 생성·문서화(Codex · Claude Code). 모든 코드를 수작업 작성했다고 표현하지 않습니다.<br><span>근거: AGENTS.md 작업 규칙 · docs/DEVELOPMENT_STATUS.md · 마이그레이션 0027(공압) · 0028(작업지시 재사용)</span></div>`);

const commandSource=execFileSync('git',['show',sourceCommit+':apps/gateway/commands.py'],{cwd:root,encoding:'utf8'});
const lines=commandSource.split('\n');
const startLine=lines.findIndex(l=>l.includes('command_id = work.request.command_id'));
const endLine=lines.findIndex((l,i)=>i>startLine&&l.includes('return CommandWorkResult("uncertain", command_id, submitted, False)'));
if(startLine<0||endLine<0)throw Error('Command excerpt not found');
// 판단에 필요한 줄만 남긴다: 만료 시 미전달, 제출 실패 시 재전송 없음, 원래 ID로 결과 조회, 확인 못 하면 uncertain.
const keep=[[0,0],[2,2],[6,8],[12,13],'…',[20,29],'…',[34,36]];
const body=lines.slice(startLine,endLine+1);
if(!body[6].includes('expires_at')||!body[20].includes('except _UNCERTAIN')||!body[34].includes('"reported"'))throw Error('Command excerpt anchors moved');
const kept=[];
keep.forEach((range,i)=>{if(range==='…'){const next=body[keep[i+1][0]];kept.push(next.match(/^ */)[0]+'# … 보고 응답 검증 생략');return}kept.push(...body.slice(range[0],range[1]+1))});
const indent=Math.min(...kept.filter(l=>l.trim()).map(l=>l.match(/^ */)[0].length));
const excerpt=kept.map(l=>l.slice(indent)).join('\n');
const escapedExcerpt=excerpt.replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;');
page('코드로 설명하는 명령 처리','결과를 모르면 다시 보내지 않고, 원래 ID로 묻습니다','실제 코드 발췌 · CommandWorker.run_once · apps/gateway/commands.py',`
<div class="code-layout"><div><div class="code-label">고정 소스 L${startLine+1}~L${endLine+1} 중 판단에 필요한 줄만 발췌 · 생략 구간 표시</div><pre class="source-code"><code>${escapedExcerpt}</code></pre><p class="caption"><a href="https://github.com/jeonking80/HMI-MES/blob/${sourceCommit}/apps/gateway/commands.py#L${startLine+1}">게이트웨이 명령 처리 코드 ↗</a> · <a href="https://github.com/jeonking80/HMI-MES/blob/${sourceCommit}/contracts/PROTOCOL.md">명령·사건 프로토콜 ↗</a></p></div><div class="code-steps">${p('① 만료된 명령은 전달하지 않음','API가 이미 결과 불명으로 예약한 명령입니다. 늦게 PLC에 보내면 작업자가 모르는 동작이 됩니다.')}${p('② 전달 중 끊겨도 재전송 없음','PLC 접수 응답이나 보고 응답이 사라져도 같은 명령을 다시 제출하지 않습니다.')}${p('③ 결과는 항상 원래 ID로 조회','제출 여부와 관계없이 같은 command_id로 PLC 결과를 묻고, 다른 명령의 응답이면 거절합니다.')}${p('④ 확인 못 하면 uncertain','보고까지 확인되지 않으면 성공으로 기록하지 않고, 다음 주기에 같은 ID로 다시 조회합니다.')}</div></div><div class="code-takeaway"><b>설계 의도:</b> 같은 명령이 두 번 실행되는 것보다 결과를 잠시 모르는 편이 안전합니다. 판정은 PLC 원장이 하고, 운영 시스템은 그 결과를 원래 ID로 맞춰 봅니다.</div>`);

page('시연과 자료','작업지시에서 정산까지, 공개 화면으로 따라갈 수 있습니다','시연 순서와 근거 문서를 함께 제공합니다.',`
<div class="split equal"><div><ol class="demo"><li><b>로그인과 설비 전원</b>역할별 권한, 원격 실행 설비 켜기</li><li><b>작업지시</b>공정 설정 선택 → 등록·배포</li><li><b>운전</b>불러오기 → 예열 → 생산 → 운전 중 설정 변경</li><li><b>종료·정산</b>일시정지·재개 → 종료 → 정산 → 같은 작업지시 재실행</li><li><b>분석</b>대시보드 · 보고서 · 트렌드 · SPC와 남은 검증 기준</li></ol><div class="links"><a href="https://github.com/jeonking80/HMI-MES">GitHub 저장소 ↗</a><a href="https://hmi-mes.vercel.app">공개 데모 ↗</a><a href="https://github.com/jeonking80/HMI-MES/blob/main/docs/USER_MANUAL.md">사용자 매뉴얼 ↗</a><a href="https://github.com/jeonking80/HMI-MES/blob/main/docs/DEVELOPMENT_STATUS.md">개발 진행 상태 ↗</a></div></div><div>${await fig('21-m-control','휴대폰 폭의 운전 제어 화면. 운전 중 실제 상태입니다.','short')}<div class="note"><b>자료 범위</b><br>화면: 공개 배포 · ${capturedAt} 실제 운전 · 소스 ${sourceCommit.slice(0,7)}<br>검증 수치: 저장소의 진행 기록과 수락 시험 대응표 기준<br>공개 화면의 설비는 임시 실행 환경이라 상시 켜져 있지 않습니다.</div><p class="muted">작성자: 전우석 · 개발 기간: 9일.<br>계정 비밀번호·DB 접속정보·인증서는 포함하지 않았습니다.</p></div></div>`);

const css=(await readFile(resolve(root,'scripts/portfolio.css'),'utf8'));
const numbered=pages.map((html,i)=>html.replace('__PAGE_NUMBER__',String(i+1).padStart(2,'0')));
const html=`<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>HMI-MES 포트폴리오</title><style>${css}</style></head><body>${numbered.join('\n')}</body></html>`;
await writeFile(resolve(output,'hmi-mes-portfolio.html'),html);
const browser=await chromium.launch({executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH||undefined,headless:true,args:['--no-sandbox']});
try{
 const page=await browser.newPage({viewport:{width:1400,height:1000}});await page.setContent(html,{waitUntil:'load'});await page.evaluate(()=>document.fonts.ready);await page.emulateMedia({media:'print'});
 const layout=await page.evaluate(()=>[...document.querySelectorAll('.page')].map((p,i)=>({page:i+1,title:p.querySelector('h1').textContent,overflow:[...p.querySelectorAll('.page-body>*,.page-body figure,.page-body .note,.page-body p,.page-body h2,.page-body h3,.page-body figcaption,.page-body .point,.page-body table,.page-body pre,.page-body .code-takeaway,.page-body .warning')].some(el=>el.getBoundingClientRect().bottom>p.querySelector('footer').getBoundingClientRect().top-4),images:[...p.querySelectorAll('img')].every(im=>im.complete&&im.naturalWidth>0)})));
 const codeFits=await page.locator('.source-code').evaluate(el=>el.scrollWidth<=el.clientWidth);
 if(!codeFits)throw Error('Code excerpt overflows');
 if(layout.some(p=>p.overflow||!p.images)){console.log(JSON.stringify(layout,null,2));throw Error('Portfolio layout or image validation failed')}
 await page.pdf({path:resolve(output,'hmi-mes-portfolio.pdf'),format:'A4',landscape:true,printBackground:true,preferCSSPageSize:true});
 await writeFile(resolve(root,'docs/assets/portfolio/pdf-validation.json'),JSON.stringify({page_count:pages.length,layout,source_commit:sourceCommit,captured_at:capturedAt},null,2)+'\n');
 console.log(JSON.stringify({pages:pages.length,pdf:'apps/web/public/portfolio/hmi-mes-portfolio.pdf',html:'apps/web/public/portfolio/hmi-mes-portfolio.html',layout:'passed'}));
}finally{await browser.close()}
