/** Render the portfolio from real captured screens and recorded results; does not contact the application, the database or external AI. */
import {readFile,writeFile,mkdir} from 'node:fs/promises';
import {resolve,dirname} from 'node:path';
import {fileURLToPath} from 'node:url';
import {createRequire} from 'node:module';
import {execFileSync} from 'node:child_process';
const require=createRequire(import.meta.url);
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const sourceCommit='8134cb8656a83709e669dfe05866825d842105ec';
const capturedAt='2026-10-29';
// 면접관에게 보여 줄 문서만 담은 포트폴리오 저장소. 코드 저장소 링크는 넣지 않는다.
const DOCS='https://github.com/jeonking80/portfolio/tree/main/HMI-MES';
const DOC=name=>`https://github.com/jeonking80/portfolio/blob/main/HMI-MES/docs/${name}.md`;
const root=resolve(dirname(fileURLToPath(import.meta.url)),'..');
const output=resolve(root,'apps/web/public/portfolio');await mkdir(output,{recursive:true});
// 수치는 기록 파일에서만 읽는다. 파일이 없으면 생성하지 않는다.
const facts=JSON.parse(await readFile(resolve(root,'docs/assets/portfolio/facts.json'),'utf8'));
const tests=JSON.parse(await readFile(resolve(root,'docs/assets/portfolio/test-result.json'),'utf8'));
const asset=async name=>'data:image/png;base64,'+(await readFile(resolve(root,'docs/assets/portfolio',name+'.png'))).toString('base64');
const fig=async(name,caption,cls='')=>`<figure class="${cls}"><div class="shot"><img src="${await asset(name)}" alt="${caption}"></div><figcaption>${caption}</figcaption></figure>`;
const p=(title,text)=>`<div class="point"><h3>${title}</h3><p>${text}</p></div>`;
const tag=s=>`<span class="tag">${s}</span>`;
const n=v=>Number(v).toLocaleString('ko-KR');
const esc=s=>s.replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;');
const pages=[];
function page(kicker,title,sub,body,cls=''){pages.push(`<section class="page ${cls}"><header><div class="eyebrow">HMI · MES <span>/ ${kicker}</span></div><h1>${title}</h1><p class="subtitle">${sub}</p></header><div class="page-body">${body}</div><footer><span>전우석 · HMI-MES 설비 운영 시스템 · 2026.11</span><span>__PAGE_NUMBER__</span></footer></section>`)}

// 1. 표지
page('포트폴리오','HMI · MES 설비 운영 시스템','가상 포장라인을 실제 암호화 OPC UA로 연결해, 설비 감시·제어부터 작업지시·실적 정산까지 잇는 웹 HMI·MES',`
<div class="cover-grid"><div><div class="cover-name">HMI<br><span>MES.</span></div><p class="lead">버튼을 누른 것과<br>설비가 실제로 움직인 것을<br>끝까지 구분하는 시스템.</p><div class="tags">${['Python · FastAPI','PostgreSQL','OPC UA · asyncua','React · TypeScript','Vercel · Neon'].map(tag).join('')}</div><p><strong>작성자 전우석</strong></p><p class="muted">개발 기간 ${facts.period} · 커밋 ${n(facts.commits)}개<br>기획 방향·운영 규칙 결정, 화면 검수, 개발 조율<br>Codex · Claude Code를 활용한 구현과 검증</p></div>${await fig('11-monitor-running',`공개 배포 hmi-mes.vercel.app · ${capturedAt} 실제 운전 중인 라인 모니터링 화면`)}</div>`,'cover');

// 2. 프로젝트 개요
page('프로젝트 개요','설비가 실제로 한 일만 실적이 됩니다','투입 컨베이어 → 실링기 → 검사 배출기로 이어지는 포장라인 1개를 감시·제어하고, 작업지시별 생산 실적을 정산합니다.',`
<div class="overview"><div><h2 class="section-h">무엇을 하는 시스템인가</h2><p class="body-text">설비 시뮬레이터(앱 B)가 세 설비와 가상 PLC를 돌리고, 운영 시스템(앱 A)이 OPC UA로 값을 수집해 웹 화면으로 보여 줍니다. 작업자는 화면에서 작업지시를 불러와 운전하고, 시스템은 생산 사건을 원래 작업 실행에 귀속해 실적을 정산합니다. 두 앱은 DB와 메모리를 공유하지 않습니다.</p><h2 class="section-h">핵심 규칙 3가지</h2><ol class="rules"><li><b>판정은 PLC가 합니다</b><span>화면은 요청만 보냅니다. 응답이 끊기면 같은 명령 ID로 결과만 조회하고, 새 ID로 다시 실행하지 않습니다.</span></li><li><b>모르는 것은 모른다고 표시합니다</b><span>통신 단절·품질 불명·누락을 0이나 정상값, 정산 완료로 바꾸지 않습니다.</span></li><li><b>정산이 끝나야 다음 작업입니다</b><span>생산 사건은 고유 ID로 한 번만 반영하고, 설비 최종 수량과 대조한 뒤에만 다음 작업을 연결합니다.</span></li></ol></div><div><h2 class="section-h">규모 (저장소 실측)</h2><div class="metric-grid"><div><strong>${facts.tables}</strong><span>DB 테이블</span></div><div><strong>${facts.immutable_tables}</strong><span>트리거로 수정·삭제를 막는 테이블</span></div><div><strong>${facts.db_functions}</strong><span>DB 함수 (트리거 ${facts.triggers}개)</span></div><div><strong>${facts.migrations}</strong><span>마이그레이션</span></div><div><strong>${facts.screens}</strong><span>메뉴 화면 (로그인·설비 상세 창 별도)</span></div><div><strong>${n(tests.python_passed+tests.python_failed+tests.python_errors)}</strong><span>Python 자동화 시험 (+ 화면 ${tests.web_passed})</span></div></div><p class="caption">테이블·함수·트리거 수는 로컬 PostgreSQL에 마이그레이션 ${facts.migration_head}까지 적용해 센 값입니다. 저장 프로시저 대신 트랜잭션은 Python 코드에서 제어하고, DB 함수는 불변 이력 보호와 보관 정리에 씁니다. 시험 결과는 12쪽에 있습니다.</p></div></div>`);

// 3. 시스템 아키텍처
const arch=`<div class="arch3">
<div class="zone cloud"><div class="zone-label">공개 클라우드 · 무료 요금제</div>
<div class="a-node user">브라우저 · 웹 HMI / MES<small>React · TypeScript · 화면 10종 · 휴대폰 폭 대응</small></div>
<div class="a-link">↕ HTTPS · 세션 쿠키 · CSRF · Origin 검사</div>
<div class="a-row"><div class="a-node api">Vercel 서버리스 함수 (FastAPI)<small>화면·제어 API · 수집 API · 분석 API 3개로 분리</small></div><div class="a-link side">⟷<br>SQL</div><div class="a-node db">Neon PostgreSQL<small>테이블 ${facts.tables}개 · 트리거 ${facts.triggers}개</small></div></div></div>
<div class="a-link big">↕ HTTPS · 게이트웨이 서비스 토큰 · 수집 배치 / 명령 전달 · 결과 보고</div>
<div class="zone plant"><div class="zone-label">설비 쪽 · 임시 클라우드 컨테이너</div>
<div class="a-row two"><div class="a-node">I/O 게이트웨이<small>SQLite 디스크 버퍼 · DB 커밋 ACK 후에만 삭제</small></div><div class="a-node">런타임 에이전트<small>화면의 전원 요청으로 설비 기동 · 60분 뒤 자동 정지</small></div></div>
<div class="a-link">↕ OPC UA · Basic256Sha256 서명·암호화 · 인증서 검증</div>
<div class="a-node plantapp">앱 B 설비 시뮬레이터<small>투입 · 실링 · 배출 → 가상 Master PLC → OPC UA 서버 · 물품·수량·사건 SQLite 원자 저장</small></div></div></div>`;
page('시스템 아키텍처','두 앱은 OPC UA로만 만나고, 화면과 게이트웨이는 API만 거칩니다','KULS 공개 HMI 구성예시(현장설비 → Master PLC → OPC 서버 → HMI·MES 서버 → 모니터링)의 각 계층을 직접 구현',`
<div class="split arch-split"><div>${arch}</div><aside>${p('앱 A · 운영 시스템','인증, 명령 원장, 수집, 작업지시, 정산, 분석을 맡습니다. 분석 조회는 별도 함수로 분리해 무거운 조회가 제어 API를 막지 않게 했습니다.')}${p('앱 B · 설비 시뮬레이터','운전 조건 판단과 실행 결과의 최종 판정을 맡습니다. 운영 시스템이 멈춰도 자체 규칙으로 동작합니다.')}${p('게이트웨이','OPC UA 값을 읽어 묶음으로 보내고, 명령을 PLC에 전달합니다. 응답을 받기 전에는 버퍼를 지우지 않습니다.')}<div class="note">게이트웨이와 브라우저는 DB에 직접 붙지 않습니다. 설비 쪽은 상시 실행이 아니며, 화면의 전원 요청이 있을 때만 켜집니다.</div></aside></div>`);

// 4. 기술 스택과 선택 이유
page('기술 스택과 선택 이유','계층마다 “실패했을 때 무엇이 남는가”를 기준으로 골랐습니다','선택 이유와 함께, 그 선택으로 감수한 점을 같이 적었습니다.',`
<table class="stack-table"><tr><th>계층</th><th>선택</th><th>고른 이유</th><th>감수한 점</th></tr>
<tr><td>설비 연동</td><td>OPC UA (asyncua)<br>Basic256Sha256</td><td>실제 산업 현장 표준이고, 서명·암호화와 인증서 검증을 프로토콜 수준에서 제공합니다. 모의 HTTP가 아니라 실제 프로토콜로 연결했습니다.</td><td>세션·인증서 관리가 복잡해 연결 실패 경로의 시험이 많아졌습니다.</td></tr>
<tr><td>설비 앱 저장</td><td>SQLite</td><td>물품·수량·사건·명령을 한 파일, 한 트랜잭션에 원자적으로 저장합니다. 프로세스가 중간에 죽어도 일부만 저장되지 않습니다.</td><td>단일 프로세스 쓰기 전제입니다.</td></tr>
<tr><td>게이트웨이 버퍼</td><td>SQLite 디스크 큐</td><td>통신이 끊겨도 디스크에 쌓아 두고, DB 커밋 응답을 받은 묶음만 지웁니다. 30분·100MB 한도와 누락 구간을 기록합니다.</td><td>한도를 넘는 단절에서는 오래된 관측값을 잃고, 그 사실을 누락으로 남깁니다.</td></tr>
<tr><td>API</td><td>Python · FastAPI<br>Pydantic · SQLAlchemy Core</td><td>계약 모델(Pydantic)로 경계에서 형식을 다시 검증하고, ORM 자동 동작 없이 트랜잭션과 잠금 순서를 직접 제어합니다.</td><td>쿼리를 직접 쓰는 만큼 코드량이 늘었습니다.</td></tr>
<tr><td>운영 DB</td><td>PostgreSQL · Alembic</td><td>권고 잠금(advisory lock), 행 잠금, 부분 고유 인덱스, 트리거로 불변 이력을 DB가 직접 지킵니다. 스키마는 마이그레이션 ${facts.migrations}개로 관리합니다.</td><td>무료 Neon 저장 한도 안에서 보관 기간과 압축을 별도로 설계해야 했습니다.</td></tr>
<tr><td>화면</td><td>React · TypeScript · Vite</td><td>서버 계약을 타입으로 맞추고, 화면 갱신을 웹소켓과 폴링 중에서 배포 환경에 맞게 고릅니다.</td><td>서버리스 배포에서는 폴링을 써서 갱신 간격만큼 늦게 보입니다.</td></tr>
<tr><td>시험</td><td>pytest · Docker PostgreSQL<br>Playwright</td><td>실제 PostgreSQL과 실제 브라우저로 경로 전체를 시험합니다. CI 대신 저장소의 로컬 검사로 검증합니다.</td><td>전체 검사에 20분 이상 걸립니다.</td></tr>
<tr><td>배포</td><td>Vercel · Neon (무료)</td><td>비용 0으로 공개 데모를 운영합니다. 함수를 화면·수집·분석 3개로 나눠 각각의 시간 제한 안에 둡니다.</td><td>DB 사용량 한도가 있고, 설비 쪽은 상시 연결이 아닙니다.</td></tr></table>`);

// 5. 데이터 모델
const tbl=(name,ko,ref='')=>`<li><b>${name}</b><span>${ko}</span>${ref?`<em>→ ${ref}</em>`:''}</li>`;
page('데이터 모델','테이블을 수명에 따라 네 묶음으로 나눴습니다','기준정보는 버전으로, 현재 상태는 잠금으로, 이력은 트리거로, 운영 기록은 보관 정책으로 관리합니다.',`
<div class="model-grid">
<div class="m-col base"><h3>기준정보</h3><p>바꾸지 않고 새 버전을 만듭니다</p><ul>${tbl('sites · lines','공장 · 라인','')}${tbl('assets','설비 3대','lines')}${tbl('tag_definitions','태그 48개','assets')}${tbl('item_master','품목','users')}${tbl('recipe_versions','공정 설정 버전','item_master')}${tbl('users · roles','계정 · 역할 4종','')}</ul></div>
<div class="m-col state"><h3>현재 상태</h3><p>행 잠금 아래에서만 바뀝니다</p><ul>${tbl('work_orders','작업지시','recipe_versions · lines')}${tbl('work_order_execution_links','작업지시 ↔ 실행 회차','work_orders · production_executions')}${tbl('production_executions','작업 실행','source_streams')}${tbl('execution_totals','실행별 집계','production_executions')}${tbl('command_requests','명령 원장','production_executions')}${tbl('line_queue_entries','작업 대기열','work_orders')}${tbl('stream_progress','처리 커서','source_streams')}</ul></div>
<div class="m-col ledger"><h3>지울 수 없는 이력</h3><p>트리거가 수정·삭제를 거절합니다</p><ul>${tbl('item_audit','품목 변경 기록','item_master')}${tbl('registration_receipts','등록 영수증','recipe_versions · work_orders')}${tbl('work_order_cancellations','작업지시 취소','work_orders')}${tbl('line_queue_audit','대기열 변경','lines')}${tbl('alarm_actions','알람 확인·조치','alarm_instances')}${tbl('downtime_reason_history','정지 사유 이력','users')}${tbl('capture_clock_evidence','수집 시각 근거','ingest_batches')}</ul></div>
<div class="m-col ops"><h3>운영 기록</h3><p>보관 기간이 지나면 압축·정리합니다</p><ul>${tbl('event_inbox','생산 사건 원본','source_streams')}${tbl('production_events','실적 반영 사건','event_inbox · production_executions')}${tbl('ingest_batches','수집 묶음 원문','source_streams')}${tbl('stream_gaps','누락 구간','source_streams')}${tbl('analog_history_raw · minutes','1초 · 1분 트렌드','source_streams')}${tbl('auth_sessions · auth_audit','세션 · 인증 기록','users')}</ul></div></div>
<p class="caption">전체 ${facts.tables}개 테이블 중 업무 흐름의 중심 26개입니다. → 는 외래키 참조 대상입니다. 트리거로 수정·삭제를 막는 테이블은 모두 ${facts.immutable_tables}개이며, 생산 사건 원본(event_inbox)은 트리거 대신 사건 ID 기본키와 애플리케이션 규칙으로 한 번만 쓰입니다.</p>`);

// 6. 데이터 저장 흐름
const step=(no,title,text,cls='')=>`<div class="f-step ${cls}"><span>${no}</span><div><b>${title}</b><p>${text}</p></div></div>`;
page('데이터 저장 흐름','수집 요청 하나는 하나의 트랜잭션 안에서 끝납니다','IngestionService.ingest — 게이트웨이가 보낸 수집 묶음 한 건의 처리 순서. 중간에 실패하면 전부 되돌리고 ACK를 보내지 않습니다.',`
<div class="flow-layout"><div class="flow">
${step(1,'인증 범위 확인','토큰으로 확인한 게이트웨이·수집원·스트림이 묶음 본문과 같은지 봅니다. 다르면 거절합니다.')}
${step(2,'트랜잭션 시작 · 잠금','게이트웨이 권고 잠금 → 라인·스트림 행 잠금 → 사건 ID별 권고 잠금(정렬 순서, 교착 방지).')}
${step(3,'같은 묶음인지 판별','같은 묶음 ID가 이미 있으면 내용 해시를 비교합니다.')}
<div class="f-branch"><div><b>내용까지 같음</b>저장해 둔 원래 ACK를 그대로 돌려줍니다. 다시 반영하지 않습니다.</div><div class="bad"><b>내용이 다름</b>충돌 증거를 저장하고 스트림을 막습니다. ACK는 주지 않습니다.</div></div>
${step(4,'사건별 수신','이미 받은 사건 ID면 원래 영수증만 돌려주고, 처음이면 원본을 먼저 저장한 뒤 실적에 반영합니다.')}
${step(5,'처리 커서 · 누락 구간','순번이 이어지면 커서를 전진시키고, 빈 순번은 누락 구간으로 기록합니다.')}
${step(6,'ACK 생성 · 묶음 원문 저장','ACK를 만들어 스스로 검증한 뒤 묶음 원문과 함께 저장합니다.')}
${step(7,'시각 근거 · 알람 · 트렌드','같은 트랜잭션 안에서 파생 기록을 만듭니다.')}
${step(8,'커밋 → ACK 반환','커밋이 끝난 뒤에만 ACK를 보냅니다. 게이트웨이는 이 ACK를 받아야 버퍼를 지웁니다.','last')}
</div><aside>${p('왜 한 트랜잭션인가','중복 제거, 실적, 처리 커서가 따로 저장되면 재시작 때 실적이 두 번 잡히거나 커서만 앞서 나갑니다. 셋을 같이 커밋하거나 같이 버립니다.')}${p('재전송은 안전하다','응답이 끊겨 게이트웨이가 같은 묶음을 다시 보내도 3단계에서 원래 ACK를 돌려받습니다.')}${p('충돌은 숨기지 않는다','같은 ID에 다른 내용이 오면 덮어쓰지 않고 증거를 남긴 채 그 스트림을 멈춥니다.')}</aside></div>`);

// 7. 핵심 코드
const ingestionSource=execFileSync('git',['show',sourceCommit+':apps/api/ingestion.py'],{cwd:root,encoding:'utf8'}).split('\n');
const at=(text,from=0)=>{const i=ingestionSource.findIndex((l,k)=>k>=from&&l.includes(text));if(i<0)throw Error('Excerpt anchor missing: '+text);return i};
const lockStart=at('with self._transaction() as connection:',at('    def ingest('));
const lockEnd=at('self._advisory(connection, "event", event_id)',lockStart);
const recvStart=at('    def _receive_event(');
const recvEnd=at('return EventReceipt(',recvStart);
const block=(a,b)=>ingestionSource.slice(a,b+1);
const lockLines=block(lockStart,lockEnd).filter(l=>!/check_retention_expired|capture_generation|raise SourceBindingError\("Capture generation/.test(l));
const recvLines=block(recvStart+3,recvEnd).filter(l=>!/^\s*(source_id|stream_id|seq|kind|body|content_sha256|stored_at|reason)=/.test(l)&&!/Insert the raw inbox first/.test(l));
const dedent=ls=>{const k=Math.min(...ls.filter(l=>l.trim()).map(l=>l.match(/^ */)[0].length));return ls.map(l=>l.slice(k))};
const excerpt=['# ingest(): 트랜잭션과 잠금 순서',...dedent(lockLines),'','# _receive_event(): 사건 단위 중복 제거',...dedent(recvLines)].join('\n');
page('핵심 코드','같은 사건은 원래 영수증으로 답하고, 처음 온 사건만 반영합니다','실제 코드 발췌 · apps/api/ingestion.py · 고정 커밋 '+sourceCommit.slice(0,7),`
<div class="code-layout"><div><div class="code-label">L${lockStart+1}~L${lockEnd+1}, L${recvStart+4}~L${recvEnd+1} 발췌 · 보관 기간·세대 검사와 저장 열 일부 생략</div><pre class="source-code"><code>${esc(excerpt)}</code></pre><p class="caption"><a href="${DOC('ingest-explainer')}">핵심 코드 해설 ↗</a> · <a href="${DOC('protocol')}">명령·수집 계약 ↗</a></p></div><div class="code-steps">${p('① 잠금 순서를 고정','게이트웨이 → 스트림 행 → 사건 ID 순으로 잠급니다. 사건 ID는 정렬해서 잠가, 두 묶음이 같은 사건을 반대 순서로 잡는 교착을 막습니다.')}${p('② 이미 받은 사건은 영수증만','사건 ID로 원본을 찾으면 다시 반영하지 않고 그때의 결과를 그대로 돌려줍니다.')}${p('③ 원본 먼저, 반영은 그다음','원본을 “반영 대기”로 저장한 뒤 실적에 반영합니다. 반영이 거절돼도 원본과 사유는 남습니다.')}${p('④ 실패는 격리로 기록','반영할 수 없는 사건은 지우지 않고 격리(QUARANTINED)로 표시해, 나중에 원래 실행으로 다시 처리할 수 있게 합니다.')}</div></div>`);

// 8. 보안·권한 구조
const yes='<td class="y">●</td>',no='<td></td>';
const perm=(name,ko,cells)=>`<tr><td><b>${ko}</b><small>${name}</small></td>${cells.map(c=>c?yes:no).join('')}</tr>`;
page('보안 · 권한 구조','권한은 첫 제어 경로부터, 화면이 아니라 서버가 검사합니다','역할 4종 × 권한 9종 · apps/api/auth.py의 PERMISSIONS 정의 그대로',`
<div class="split equal"><div><table class="perm-table"><tr><th>권한</th><th>조회자</th><th>작업자</th><th>생산 담당자</th><th>설비 관리자</th></tr>
${perm('read','조회',[1,1,1,1])}${perm('operate','시작·일시정지 등 운전',[0,1,0,0])}${perm('load_job','작업 불러오기',[0,1,1,0])}${perm('end_run','작업 종료',[0,1,1,0])}${perm('alarm_action','알람 확인·조치',[0,1,0,0])}${perm('downtime_annotate','정지 사유 입력',[0,1,1,0])}${perm('production_manage','작업지시 등록·배포',[0,0,1,0])}${perm('parameters_manage','공정 설정 관리',[0,0,1,0])}${perm('equipment_manage','설비 관리',[0,0,0,1])}</table><p class="caption">로그인 계정 하나에 여러 역할을 줄 수 있습니다. 게스트 둘러보기는 조회자 권한만 있고, 쓰기 요청은 403으로 거절됩니다.</p></div><div>${p('세션','비밀번호는 scrypt(N=2¹⁵)로 저장합니다. 세션은 30분 동안 쓰지 않으면, 또는 로그인 후 8시간이 지나면 끝납니다. 초기 비밀번호는 바꾸기 전까지 다른 작업을 막습니다.')}${p('변경 요청','모든 쓰기 요청에 CSRF 토큰과 정확한 Origin 일치를 요구합니다. 로그인·인증 결과는 auth_audit에 남깁니다.')}${p('설비 연동','게이트웨이는 별도 서비스 토큰으로 수집 API에 들어오고, 인증된 게이트웨이·수집원과 묶음 본문이 다르면 거절됩니다. OPC UA는 서명·암호화와 인증서 검증을 거칩니다.')}${p('비밀값','DB 접속정보·토큰·비밀번호는 소스·문서·로그에 쓰지 않고 배포 환경 변수로만 넣습니다.')}</div></div>`);

// 9. 동시성과 정합성
page('동시성과 정합성','동시에 들어와도, 다시 들어와도, 중간에 죽어도 숫자가 맞아야 합니다','문제 상황마다 어떤 장치로 막았는지 정리했습니다.',`
<table class="stack-table conc"><tr><th>상황</th><th>막는 장치</th><th>결과</th></tr>
<tr><td>같은 수집 묶음이 두 번 도착</td><td>묶음 ID + 내용 해시 비교, 사건 ID 기본키</td><td>원래 ACK·영수증을 돌려주고 실적은 한 번만 반영</td></tr>
<tr><td>두 묶음이 같은 사건을 동시에 처리</td><td>사건 ID별 트랜잭션 권고 잠금, 정렬된 잠금 순서</td><td>한쪽이 끝날 때까지 기다림, 교착 없음</td></tr>
<tr><td>명령 응답이 중간에 끊김</td><td>명령 원장의 원래 command_id, 만료 시 결과 불명 예약</td><td>새 ID로 재실행하지 않고 원래 ID로 결과만 조회</td></tr>
<tr><td>같은 작업지시를 두 번 대기열에 넣음</td><td>대기 중(WAITING)인 항목에만 거는 부분 고유 인덱스</td><td>대기 중 중복은 DB가 거절, 끝난 회차는 다시 넣을 수 있음</td></tr>
<tr><td>설비 앱이 배출 확정 중에 죽음</td><td>물품·수량·사건을 SQLite 한 트랜잭션에 저장</td><td>전부 남거나 전부 없음, 반쯤 저장된 수량 없음</td></tr>
<tr><td>이력 행을 실수로 고치거나 지움</td><td>UPDATE·DELETE를 거절하는 트리거 (${facts.immutable_tables}개 테이블)</td><td>DB 수준에서 거절, 애플리케이션 버그로도 바꿀 수 없음</td></tr>
<tr><td>마이그레이션이 두 곳에서 동시에 실행</td><td>스키마 전용 권고 잠금</td><td>한 번에 하나만 적용</td></tr>
<tr><td>작업 전환 때 이전 수량이 섞임</td><td>재공 0 확인과 설비 최종 요약 대조 후에만 다음 작업 연결</td><td>실행마다 수량이 분리, 불일치는 정산 보류</td></tr></table>`);

// 10. 자동화와 프론트엔드
page('자동화와 프론트엔드','사람이 반복하던 확인을 시스템이 대신합니다','설비 쪽 자동화와 화면 쪽 동작 방식을 함께 정리했습니다.',`
<div class="split auto-split"><aside><h2 class="section-h">자동화</h2>${p('원격 전원','화면의 전원 요청을 런타임 에이전트가 받아 설비 앱·게이트웨이를 켜고, 60분 뒤 스스로 끕니다. 비용과 DB 사용량을 묶어 두기 위해서입니다.')}${p('자동 시작 · 자동 종료','예열이 끝나면 시작, 목표 수량에 도달하면 종료를 선택할 수 있습니다. 둘 다 PLC 판정을 거칩니다.')}${p('재전송','게이트웨이는 ACK를 못 받은 묶음을 자동으로 다시 보냅니다. 6쪽의 멱등 처리 덕분에 안전합니다.')}${p('입력 자동화','품목을 고르면 공정 설정 값을, 공정 설정을 고르면 작업지시 번호·품목을 채웁니다.')}<h2 class="section-h">프론트엔드</h2>${p('갱신 방식','배포 환경에 따라 웹소켓 또는 폴링을 고릅니다. 탭이 보이지 않으면 폴링을 멈춰 DB 사용량을 줄입니다.')}${p('상태 표시','값마다 품질을 함께 받아 “불명”을 정상과 다른 색으로 표시합니다. 명령은 요청 저장 → PLC 접수 → 실행 완료의 단계로 보여 줍니다.')}</aside><div class="two-col-fig">${await fig('10-control-setpoint','운전 중 설정 변경. 바꿀 값·허용 범위·변경 내용을 적용 전에 보여 줍니다.')}${await fig('21-m-control','휴대폰 폭(390px) 운전 제어 화면')}</div></div>`);

// 11. 업무 흐름과 주요 화면
const thumb=async(name,caption)=>`<figure class="thumb"><div class="shot"><img src="${await asset(name)}" alt="${caption}"></div><figcaption>${caption}</figcaption></figure>`;
page('업무 흐름과 주요 화면','작업지시 등록부터 정산 · 다음 작업까지 한 흐름으로 이어집니다',`${capturedAt} 공개 배포를 실제로 운전하며 촬영 · 작업 실행 3건 모두 정산 완료`,`
<ol class="biz-flow"><li><b>등록·배포</b>작업지시</li><li><b>전원</b>설비 기동</li><li><b>불러오기</b>설비에 연결</li><li><b>예열</b>PLC 판정</li><li><b>생산</b>설정 변경</li><li><b>종료</b>재공 정리</li><li><b>정산</b>수량 대조</li><li><b>다음 작업</b>재실행 가능</li></ol>
<div class="thumbs">${await thumb('16-order-history','작업지시 · 같은 지시서를 3회 실행한 기록')}${await thumb('09-control-running','운전 제어 · 단계 바와 현재 단계')}${await thumb('11b-monitor-speed','라인 모니터링 · 세 설비와 라인 속도')}${await thumb('15-control-settled','정산 · 직전 작업 양품 8 · 불량 0')}${await thumb('18b-report-detail','보고서 · 회차별 실적과 가동 시간')}${await thumb('13-trends','트렌드 · 설정 150 → 155 ℃ 변화')}</div>`);

// 12. 테스트 전략과 결과
const total=tests.python_passed+tests.python_failed+tests.python_errors;
page('테스트 전략과 결과','단위에서 실제 경로까지, 실제 DB와 실제 브라우저로 시험했습니다',`커밋 ${tests.commit} 기준 로컬 전체 검사 (${tests.date})`,`
<div class="split equal"><div><table class="stack-table"><tr><th>층</th><th>무엇을 확인하나</th><th>도구</th></tr><tr><td>단위</td><td>PLC 판정, 상태 전이, 정산 계산, 권한 표</td><td>pytest</td></tr><tr><td>계약</td><td>태그 48개, 명령·사건·전송 형식, 직렬화</td><td>pytest · Pydantic</td></tr><tr><td>통합</td><td>실제 PostgreSQL에서 수집·중복·잠금·트리거·마이그레이션</td><td>Docker PostgreSQL</td></tr><tr><td>종단</td><td>설비 앱 → OPC UA → 게이트웨이 → API → DB → 브라우저</td><td>Playwright · Chromium</td></tr><tr><td>화면</td><td>화면 상태 계산, 입력 검증</td><td>Node 시험</td></tr><tr><td>장시간·성능</td><td>30분 단절 복구, 7일 이력 조회</td><td>전용 실행기 (과거 로컬)</td></tr></table>${p('검증 원칙','미실행·실패·차단을 통과로 적지 않습니다. 시험 개수는 자동화 시험의 실행 수이며 작성량이 아닙니다.')}</div><div><div class="metrics three"><div><strong>${n(tests.python_passed)}</strong><span>Python 통과 / ${n(total)}</span></div><div class="${tests.python_failed+tests.python_errors?'warn':''}"><strong>${n(tests.python_failed+tests.python_errors)}</strong><span>실패 · 오류</span></div><div><strong>${tests.web_passed}</strong><span>화면 시험 통과</span></div></div><div class="note"><b>${tests.note_title}</b><br>${tests.failure_note}</div><table class="stack-table small"><tr><th>과거 기록</th><th>결과</th></tr><tr><td>30분 통신 단절 복구 (CX-19)</td><td>과거 로컬 구성에서 1,800초 단절 후 208.3초 만에 회복, 통과. 앞선 두 번은 실패</td></tr><tr><td>7일 이력 조회 (CX-20)</td><td>합성 7일 이력·RAM 디스크에서 사용자 10명 p95 1.34~1.40초, 통과</td></tr></table><p class="caption">과거 기록 두 건은 최신 공개 배포에서 다시 검증하지 않았습니다.</p></div></div>`);

// 13. AI 활용 개발 방식
page('AI 활용 개발 방식','구현은 AI에게 맡기고, 결정과 검증의 기준은 사람이 쥐었습니다','개발 세션과 검증 세션을 나누고, 공개 배포는 사용자의 지시가 있을 때만 했습니다.',`
<div class="ai-flow"><div><b>사용자</b><span>기획 방향 · 운영 규칙 결정</span></div><i>→</i><div><b>Claude Code</b><span>작업지시서 ${facts.codex_orders}건 작성 · 계약 조정</span></div><i>→</i><div><b>Codex</b><span>서버 구현 · 작업 보고서 ${facts.work_reports}건</span></div><i>→</i><div><b>Claude Code</b><span>독립 검증 · 화면 · 통합</span></div><i>→</i><div class="human"><b>사용자</b><span>화면 사용 · 배포 지시</span></div><i>→</i><div><b>main 병합</b><span>Vercel 자동 배포</span></div></div>
<div class="split equal ai-split"><div><h2 class="section-h">직접 써 보며 찾은 결함</h2><table class="stack-table"><tr><th>사용자가 발견한 것</th><th>바뀐 것</th></tr><tr><td>작업할 때마다 작업지시를 새로 만들어야 했다</td><td>작업지시를 유지하고 회차로 재실행 (마이그레이션 0028)</td></tr><tr><td>제어 화면인데 온도·공압을 못 바꿨다</td><td>공정 설정에 공압 추가, 운전 중 허용 폭 검사 (0027)</td></tr><tr><td>알람 보기 버튼이 빈 목록으로 갔다</td><td>해당 설비 창·운전 제어로 연결</td></tr><tr><td>불러오기를 눌렀는데 버튼이 또 있었다</td><td>불러오기를 한 번 클릭으로 정리</td></tr><tr><td>트렌드가 어느 설비의 값인지 몰랐다</td><td>설비·태그 이름과 단위별 차트로 분리</td></tr></table></div><div><h2 class="section-h">사용자가 정한 것</h2>${p('비용과 데이터','유료 요금제를 쓰지 않는다. 공개 DB를 쓰기 전에는 반드시 확인을 받는다.')}${p('범위','8시간 연속 운전은 이번 범위에서 뺀다. 같은 작업지시는 유지하고 다시 실행한다.')}${p('복구 판단','시험 데이터 정리로 수집 순번이 깨졌을 때, 원본을 지어내지 않고 스트림을 초기화하는 쪽을 승인했다.')}<div class="ownership"><b>본인 기여:</b> 기획 방향·운영 규칙 결정, 화면 검수와 결함 발견, 범위와 복구 방식 결정, 개발 조율. <b>AI 지원:</b> 코드 작성, 시험 생성, 문서화. 모든 코드를 직접 작성했다고 표현하지 않습니다.</div></div></div>`);

// 14. 한계와 자료
page('한계와 자료','하지 않은 것을 먼저 밝힙니다','시뮬레이터 기반 프로토타입이며, 상용 현장 운영 성과로 표시하지 않습니다.',`
<div class="split equal"><div><h2 class="section-h">하지 않은 것</h2><table class="stack-table"><tr><th>항목</th><th>현재 상태</th></tr><tr><td>실물 장비 연동</td><td>실물 PLC·센서 없음. 설비와 PLC는 소프트웨어 시뮬레이터입니다.</td></tr><tr><td>공개 배포 성능 측정</td><td>단절 복구·이력 조회 성능은 과거 로컬 구성에서만 측정했습니다.</td></tr><tr><td>장시간 운전</td><td>8시간 연속 운전 시험은 하지 않았습니다.</td></tr><tr><td>수락 시험 묶음</td><td>T01~T12를 17개로 나눠 통과했지만 한 번에 묶은 실행은 하지 않았습니다.</td></tr><tr><td>공개 배포 알람</td><td>알람 생성이 공개 배포에는 연결되지 않았습니다(규칙 0개).</td></tr><tr><td>결과 불명 1건</td><td>촬영 중 설정 변경 1건이 결과 불명으로 남았고 원인은 조사하지 않았습니다.</td></tr><tr><td>공개 데모 운영</td><td>무료 DB 한도로 프로젝트를 한 번 옮겼고, 수집 스트림을 한 번 초기화했습니다.</td></tr></table></div><div><h2 class="section-h">자료</h2><div class="links big"><a href="${DOCS}">포트폴리오 저장소 ↗</a><a href="https://hmi-mes.vercel.app">공개 데모 ↗</a><a href="${DOC('system-architecture')}">시스템 구조 ↗</a><a href="${DOC('design-decisions')}">핵심 설계 결정 ↗</a><a href="${DOC('data-model')}">데이터 모델 ↗</a><a href="${DOC('protocol')}">명령·수집 계약 ↗</a><a href="${DOC('testing')}">테스트 ↗</a><a href="${DOC('user-guide')}">사용 안내 ↗</a></div><div class="note"><b>자료 범위</b><br>화면: 공개 배포 · ${capturedAt} 실제 운전<br>수치: 로컬 PostgreSQL 마이그레이션 적용 결과와 ${tests.date} 커밋 ${tests.commit} 로컬 전체 검사<br>코드: 고정 커밋 ${sourceCommit.slice(0,7)}</div><p class="muted">작성자 전우석 · 개발 기간 ${facts.period}<br>계정 비밀번호·DB 접속정보·인증서는 포함하지 않았습니다.</p></div></div>`);

const css=await readFile(resolve(root,'scripts/portfolio.css'),'utf8');
const numbered=pages.map((html,i)=>html.replace('__PAGE_NUMBER__',String(i+1).padStart(2,'0')));
const html=`<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>HMI-MES 포트폴리오</title><style>${css}</style></head><body>${numbered.join('\n')}</body></html>`;
if(/github\.com\/jeonking80\/HMI-MES/i.test(html))throw Error('Code repository link found in portfolio');
await writeFile(resolve(output,'hmi-mes-portfolio.html'),html);
const browser=await chromium.launch({executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH||undefined,headless:true,args:['--no-sandbox']});
try{
 const page=await browser.newPage({viewport:{width:1400,height:1000}});await page.setContent(html,{waitUntil:'load'});await page.evaluate(()=>document.fonts.ready);await page.emulateMedia({media:'print'});
 const layout=await page.evaluate(()=>[...document.querySelectorAll('.page')].map((p,i)=>{const limit=p.querySelector('footer').getBoundingClientRect().top-4;const right=p.getBoundingClientRect().right-2;const over=[...p.querySelectorAll('.page-body *')].filter(el=>{const r=el.getBoundingClientRect();return r.height>0&&(r.bottom>limit||r.right>right)}).map(el=>el.tagName+'.'+el.className);return {page:i+1,title:p.querySelector('h1').textContent,overflow:over.length>0,offenders:over.slice(0,3),images:[...p.querySelectorAll('img')].every(im=>im.complete&&im.naturalWidth>0)}}));
 const codeFits=await page.locator('.source-code').evaluate(el=>el.scrollWidth<=el.clientWidth);
 if(!codeFits)throw Error('Code excerpt overflows');
 if(layout.some(p=>p.overflow||!p.images)){console.log(JSON.stringify(layout.filter(p=>p.overflow||!p.images),null,2));throw Error('Portfolio layout or image validation failed')}
 await page.pdf({path:resolve(output,'hmi-mes-portfolio.pdf'),format:'A4',landscape:true,printBackground:true,preferCSSPageSize:true});
 await writeFile(resolve(root,'docs/assets/portfolio/pdf-validation.json'),JSON.stringify({page_count:pages.length,layout:layout.map(({offenders,...rest})=>rest),source_commit:sourceCommit,captured_at:capturedAt},null,2)+'\n');
 console.log(JSON.stringify({pages:pages.length,pdf:'apps/web/public/portfolio/hmi-mes-portfolio.pdf',layout:'passed'}));
}finally{await browser.close()}
