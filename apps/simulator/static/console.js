"use strict";

// App B only. Credentials remain in HttpOnly cookies; CSRF exists only in memory.
const $ = (id) => document.getElementById(id);
const forms = {
  login: $("login-form"), password: $("password-form"), command: $("command-form"),
  mode: $("mode-form"), inputs: $("inputs-form"), recovery: $("recovery-form"),
  scrap: $("scrap-form"), lookup: $("lookup-form"), fieldFault: $("field-fault-form"),
};
const field = (form, name) => form.elements.namedItem(name);
const model = {
  principal: null, csrf: null, snapshot: null, summary: null, receivedAt: 0,
  connected: false, busy: false, commands: new Map(), polling: false,
  inputVersion: null, scrapSignature: null, passwordOpen: false,
  sessionEpoch: 0,
  commandSignature: null, fieldRevision: null, fieldUncertain: false,
};
const UINT64_MAX = 18446744073709551615n;
const TERMINAL = new Set(["SUCCEEDED", "FAILED", "REJECTED", "EXPIRED"]);
const STATE_LABELS = {
  STOPPED: "정지", READY: "운전 준비", RUNNING: "운전 중", PAUSED: "일시정지",
  STOPPING: "정지 절차 중", FAULT: "고장", ESTOP: "비상정지", RECOVERY_REQUIRED: "복구 확인 필요",
};
const KIND_LABELS = { LOAD_JOB: "작업 연결", START: "시작 / 재개", PAUSE: "일시정지", END_RUN: "실행 종료", SET_PARAMETERS: "설정 변경", RESET_FAULT: "고장 리셋" };
const RESULT_LABELS = {
  REQUESTED: "요청됨", DELIVERED: "전달됨", ACCEPTED: "접수 · 실행 대기",
  SUCCEEDED: "성공 결과 수신", FAILED: "실행 실패", REJECTED: "접수 거절",
  EXPIRED: "미전달 만료", OUTCOME_UNKNOWN: "결과 확인 필요", COMMAND_NOT_FOUND: "원래 ID 대조 필요",
};
const STATIONS = ["투입기", "실링기", "배출·검사기"];
const RECOVERY_KEYS = ["outputs_stopped", "positions_verified", "storage_reconciled", "commands_reconciled", "operator_confirmed"];

function uintString(value) {
  if (typeof value !== "string" || !/^(0|[1-9][0-9]*)$/.test(value) || value.length > 20 || BigInt(value) > UINT64_MAX) {
    throw new Error("UInt64 관측 형식이 올바르지 않습니다. 조작을 중단합니다.");
  }
  return value;
}
function quantity(value) { return BigInt(uintString(value)).toLocaleString("ko-KR"); }
function text(id, value) { $(id).textContent = value; }
function element(tag, className, content) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (content !== undefined) node.textContent = content;
  return node;
}
function badge(id, label, tone = "neutral") { text(id, label); $(id).className = `badge ${tone}`; }
function notice(message, tone = "neutral") {
  $("notice").textContent = message;
  $("notice").className = `notice ${tone}`;
  $("notice").hidden = false;
}
function fresh() { return model.connected && model.snapshot && performance.now() - model.receivedAt < 3000; }
function operator() { return model.principal?.role === "simulator_operator" && !model.principal.must_change_password && Boolean(model.csrf); }
function unresolved() { return [...model.commands.values()].some((record) => !TERMINAL.has(record.result?.status)); }

class RequestError extends Error {
  constructor(status) {
    const messages = {
      400: "콘솔 출처 또는 요청이 허용되지 않았습니다.",
      401: "로그인 정보가 올바르지 않거나 세션이 만료되었습니다. 다시 로그인하세요.",
      403: "권한·암호 변경·Origin·CSRF 조건을 확인하세요. 서버가 조작을 거절했습니다.",
      409: "현재 설비 조건과 충돌하거나 명시적 복구가 필요합니다. 최신 상태를 확인하세요.",
      413: "요청이 너무 큽니다.", 422: "입력 형식을 확인하세요. 서버가 요청을 거절했습니다.",
      429: "요청 횟수 제한에 도달했습니다. 잠시 후 직접 다시 요청하세요.",
      503: "시뮬레이터 저장소 또는 실행 상태를 사용할 수 없습니다.",
    };
    super(messages[status] || `서버가 요청을 처리하지 못했습니다. (HTTP ${status})`);
    this.status = status;
  }
}

async function api(path, { method = "GET", body, csrf = model.csrf, challenge = false } = {}) {
  const headers = { Accept: "application/json" };
  if (method !== "GET") headers["Content-Type"] = "application/json";
  if (csrf) headers["X-CSRF-Token"] = csrf;
  if (challenge) headers["X-Simulator-Request"] = "1";
  const abort = new AbortController();
  const timeout = setTimeout(() => abort.abort(), 8000);
  try {
    const response = await fetch(path, { method, headers, credentials: "same-origin", cache: "no-store", signal: abort.signal, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
    if (!response.ok) throw new RequestError(response.status);
    return await response.json();
  } finally { clearTimeout(timeout); }
}

function renderAccess() {
  const signed = Boolean(model.principal);
  text("identity", signed ? `${model.principal.login} · ${model.principal.role === "simulator_operator" ? "조작자" : "조회자"}` : "로그인 필요");
  $("login-panel").hidden = signed && Boolean(model.csrf);
  $("logout").hidden = !signed || !model.csrf;
  $("show-password").hidden = !signed || !model.csrf;
  $("password-panel").hidden = !signed || !model.csrf || !(model.principal.must_change_password || model.passwordOpen);
  if (signed && !model.csrf) {
    text("login-help", "기존 세션으로 조회 중입니다. 새로 고침 후 조작·암호 변경·로그아웃을 사용하려면 다시 로그인하세요.");
    field(forms.login, "login").value = model.principal.login;
  }
  const allowed = operator() && fresh() && !model.busy;
  document.querySelectorAll("fieldset[data-operator]").forEach((set) => { set.disabled = !allowed; });
  forms.password.querySelector("button").disabled = model.busy;
  forms.login.querySelector("button").disabled = model.busy;
  $("logout").disabled = model.busy;
  const commandBlocked = unresolved();
  forms.command.querySelector("button[type=submit]").disabled = !allowed || commandBlocked;
  const reason = !signed ? "로그인 후 조회·조작" : !model.csrf ? "재로그인 후 조작 가능" : model.principal.must_change_password ? "초기 암호 변경 필요" : model.principal.role !== "simulator_operator" ? "조회 전용 · 조작 불가" : !fresh() ? "최신 관측 필요" : commandBlocked ? "원래 명령 결과 먼저 확인" : "조작자 · 서버에서 재검사";
  badge("permission", reason, allowed && !commandBlocked ? "good" : "neutral");
  $("live-values").classList.toggle("unavailable", !fresh());
  $("stale-banner").hidden = !signed || !model.snapshot || fresh();
  if (!signed) badge("connection", "인증 대기");
  else if (fresh()) badge("connection", "연결됨 · 1초 관측", "good");
  else badge("connection", model.snapshot ? "관측 지연 / 연결 끊김" : "관측 대기", "warning");
  const settle = $("settle-run");
  if (settle) settle.disabled = !allowed;
}

function clearSession() {
  model.fieldRevision = null; model.fieldUncertain = false; $("field-faults").hidden = true;
  model.sessionEpoch += 1;
  model.principal = null; model.csrf = null; model.connected = false;
  model.snapshot = null; model.summary = null; model.receivedAt = 0;
  model.commands.clear(); model.inputVersion = null; model.scrapSignature = null;
  field(forms.login, "password").value = "";
  forms.password.reset();
  for (const id of ["line-state", "control-version", "good-total", "reject-total", "scrap-total", "wip-count", "target-quantity", "temperature", "temperature-sp", "run-feedback", "actual-speed", "pressure", "fault-code", "event-seq"]) text(id, "—");
  text("state-description", "로그인 후 새 관측을 확인하세요.");
  text("execution-label", "연결 실행 —"); text("observed-at", "인증한 관측 대기");
  badge("control-mode", "제어 모드 —"); badge("operation-mode", "운전 모드 —");
  badge("recovery-badge", "확인 전");
  $("pipeline").replaceChildren(...STATIONS.map((name) => { const station = element("div", "station"); station.append(element("h4", "", name), element("p", "", "인증한 관측 대기")); return station; }));
  $("scrap-items").replaceChildren(element("p", "empty", "로그인 후 물품 위치를 확인하세요."));
  $("tag-rows").replaceChildren();
  if ($("settlement")) $("settlement").hidden = true;
  resetRecoveryChecks(); renderCommands(); renderAccess();
}

function validateSnapshot(snapshot) {
  if (!snapshot?.state || !snapshot.tags || !Number.isFinite(Date.parse(snapshot.observed_at))) throw new Error("관측 응답을 검증할 수 없습니다.");
  uintString(snapshot.state.control_version); uintString(snapshot.state.last_event_seq);
  if (uintString(snapshot.tags["Line.ControlVersion"]) !== snapshot.state.control_version) throw new Error("PLC와 태그의 제어 버전이 일치하지 않습니다.");
  for (const key of ["Line.GoodTotal", "Line.RejectTotal", "Line.ScrapTotal", "Line.EventSeq", "Line.Heartbeat"]) uintString(snapshot.tags[key]);
  for (const item of snapshot.process?.slots || []) if (item) uintString(item.item_id);
  return snapshot;
}

async function refreshSnapshot() {
  const epoch = model.sessionEpoch;
  try {
    const snapshot = validateSnapshot(await api("/snapshot"));
    if (epoch !== model.sessionEpoch || !model.principal) return null;
    model.snapshot = snapshot; model.receivedAt = performance.now(); model.connected = true;
    for (const pending of snapshot.pending_commands || []) {
      const existing = model.commands.get(pending.command_id);
      model.commands.set(pending.command_id, { ...existing, id: pending.command_id, kind: pending.kind, result: pending.result, received: true });
    }
    if (snapshot.state.pending_command_id && !model.commands.has(snapshot.state.pending_command_id)) {
      const id = snapshot.state.pending_command_id;
      model.commands.set(id, { id, kind: snapshot.state.pending_kind, result: { status: "OUTCOME_UNKNOWN", reason: "재시작 후 원래 ID의 결과를 대조하세요." } });
    }
    renderSnapshot(); renderCommands(); renderAccess();
    return snapshot;
  } catch (error) {
    if (epoch !== model.sessionEpoch) return null;
    model.connected = false;
    if (error.status === 401) clearSession();
    renderAccess();
    throw error;
  }
}

function renderSnapshot() {
  const { state, tags, process, observed_at } = model.snapshot;
  text("line-state", STATE_LABELS[state.state] || state.state);
  text("state-description", state.recovery_required ? "저장 상태 대조 후 명시적 복구가 필요합니다." : state.state === "RUNNING" ? "가상 PLC의 운전 상태와 구동 피드백을 확인하세요." : "새 운전은 별도 명령과 PLC 허용 검사를 거칩니다.");
  badge("control-mode", `${state.control_mode} · ${state.control_mode === "LOCAL" ? "현장" : "원격"}`);
  badge("operation-mode", state.operation_mode);
  text("control-version", state.control_version);
  text("observed-at", `관측 ${new Date(observed_at).toLocaleTimeString("ko-KR", { hour12: false })}`);
  text("good-total", quantity(tags["Line.GoodTotal"])); text("reject-total", quantity(tags["Line.RejectTotal"])); text("scrap-total", quantity(tags["Line.ScrapTotal"]));
  text("wip-count", tags["Line.WipCount"]); text("target-quantity", tags["Line.TargetQuantity"]);
  text("temperature", Number(tags["Sealer.TemperaturePV"]).toFixed(1)); text("temperature-sp", Number(tags["Sealer.TemperatureSP"]).toFixed(1));
  text("temperature-base", Number.isFinite(process?.temperature_c) ? Number(process.temperature_c).toFixed(1) : "실행 없음");
  const temperatureOffset = Number(state.inputs.temperature_offset_c);
  text("temperature-offset", temperatureOffset.toFixed(1));
  badge("temperature-offset-status", temperatureOffset === 0 ? "시험 센서 편차 없음" : "시험 센서 편차 적용 중", temperatureOffset === 0 ? "neutral" : "warning");
  text("execution-label", `연결 실행 ${state.execution_id || "없음"}`);
  text("run-feedback", tags["Infeed.RunFeedback"] ? "운전" : "정지");
  text("actual-speed", Number(tags["Infeed.ActualSpeed"]).toFixed(1)); text("pressure", Number(tags["Sealer.AirPressure"]).toFixed(1));
  text("fault-code", tags["Line.FaultCode"]); text("event-seq", tags["Line.EventSeq"]);
  badge("recovery-badge", state.recovery_required ? "복구 확인 필요" : "복구 게이트 해제", state.recovery_required ? "warning" : "good");
  const stations = STATIONS.map((name, index) => {
    const item = process?.slots?.[index];
    const station = element("div", `station${item ? " occupied" : ""}`);
    station.append(element("span", "station-number", `0${index + 1}`), element("h4", "", name), element("p", "mono", item ? `물품 #${item.item_id}` : "재공 없음"));
    if (item) station.append(element("p", "", `실행 ${item.execution_id}`));
    return station;
  });
  $("pipeline").replaceChildren(...stations);
  const rows = Object.entries(tags).map(([key, value]) => {
    const row = element("tr");
    row.append(element("td", "", key), element("td", "mono", typeof value === "boolean" ? String(value) : value));
    return row;
  });
  $("tag-rows").replaceChildren(...rows);
  // Do not replace a field the operator is currently editing.
  if (model.inputVersion === null) populateInputs();
  renderFieldFaults();
  renderScrap(process);
}

const FIELD_MODES = { NORMAL: "정상", FROZEN: "Good 동결", BAD: "Bad", UNCERTAIN: "Uncertain", READ_FAILURE: "설비 읽기 실패", LINK_DOWN: "가상 필드 단절", DELAY: "읽기 완료 지연" };
function populateFieldFaultIntent() {
  const group = field(forms.fieldFault, "group").value;
  const status = model.snapshot?.field_channels?.find(item => item.configured.group_id === group);
  if (!status) return;
  model.fieldRevision = uintString(status.configured.revision);
  field(forms.fieldFault, "mode").value = status.configured.mode;
  field(forms.fieldFault, "delay_ms").value = String(status.configured.delay_ms ?? 1000);
  syncFieldDelayInput();
  text("field-fault-intent", `${group} · 조회한 설정 버전 ${model.fieldRevision} 기준`);
}
function syncFieldDelayInput() {
  const delay = field(forms.fieldFault, "mode").value === "DELAY";
  $("field-delay-input").hidden = !delay;
  field(forms.fieldFault, "delay_ms").disabled = !delay;
}
function renderFieldFaults() {
  const statuses = model.snapshot?.field_channels;
  $("field-faults").hidden = !statuses;
  if (!statuses) return;
  const rows = statuses.map(status => {
    const current = status.configured, published = status.published;
    const runtime = status.publication_runtime;
    const progress = !runtime ? "미발행 / 읽기 진행 불명" : [
      `${FIELD_MODES[runtime.published_mode]} / ${runtime.published_revision}`,
      runtime.waiting_for_first_completion ? "첫 읽기 완료 대기" : "첫 읽기 완료 확인",
      runtime.read_in_flight ? "읽기 진행 중" : "진행 중 읽기 없음",
    ].join(" · ");
    const duration = current.mode === "DELAY" ? ` · ${current.delay_ms}ms` : "";
    const heartbeat = published?.fields.find(item => item.tag_id === `${current.group_id}.LinkHeartbeat`);
    const codes = [...new Set(published?.fields.map(item => item.status_code) || [])];
    const originalCodes = [...new Set(current.normal_checkpoint?.fields.map(item => item.status_code) || [])];
    const originalQuality = originalCodes.length ? ` · 원본 품질 ${originalCodes.map(code => `0x${code.toString(16)}`).join(", ")}` : "";
    const quality = codes.map(code => `${Math.floor(code / 1073741824) === 0 ? "Good" : Math.floor(code / 1073741824) === 1 ? "Uncertain" : "Bad"} / 0x${code.toString(16)}`).join(", ") || "미발행";
    const row = element("tr");
    for (const value of [current.group_id, `${FIELD_MODES[current.mode]} / ${current.revision}${duration}`, published ? `${FIELD_MODES[published.mode]} / ${published.revision}${status.pending ? " · 새 설정 발행 대기" : ""}` : "미발행 / 불명", progress, quality + originalQuality, heartbeat ? `${heartbeat.reading.value} / ${heartbeat.source_timestamp}` : "미발행"]) row.append(element("td", "", value));
    row.dataset.fieldGroup = current.group_id;
    return row;
  });
  $("field-fault-rows").replaceChildren(...rows);
  if (model.fieldRevision === null) populateFieldFaultIntent();
  $("field-fault-apply").disabled = model.fieldUncertain;
}

function populateInputs() {
  const { state } = model.snapshot;
  for (const [name, value] of Object.entries(state.inputs)) {
    const input = field(forms.inputs, name);
    if (!input) continue;
    if (input.type === "checkbox") input.checked = value;
    else input.value = String(value);
  }
  field(forms.mode, "control_mode").value = state.control_mode;
  field(forms.mode, "operation_mode").value = state.operation_mode;
  if (!field(forms.command, "execution_id").value) field(forms.command, "execution_id").value = state.execution_id || "";
  model.inputVersion = state.control_version;
}

function renderScrap(process) {
  const signature = JSON.stringify(process?.slots?.map((item) => item?.item_id || null) || []);
  if (signature === model.scrapSignature) return;
  model.scrapSignature = signature;
  const rows = [];
  (process?.slots || []).forEach((item, index) => {
    if (!item) return;
    const label = element("label", "check");
    const checkbox = element("input"); checkbox.type = "checkbox"; checkbox.name = "item_id"; checkbox.value = item.item_id;
    label.append(checkbox, element("span", "", `${STATIONS[index]} · 물품 #${item.item_id} · ${item.execution_id}`));
    rows.push(label);
  });
  $("scrap-items").replaceChildren(...(rows.length ? rows : [element("p", "empty", "현재 관측에 폐기할 재공이 없습니다.")]));
  field(forms.scrap, "positions_verified").checked = false;
}

function feedback(record) {
  if (record.result?.status !== "SUCCEEDED") return "";
  if (!fresh()) return "성공 결과를 받았지만 최신 피드백은 확인되지 않았습니다.";
  const s = model.snapshot.state, result = record.result, request = record.request;
  if (!result.evidence || uintString(s.control_version) !== uintString(result.applied_control_version)) return "현재 제어 버전이 완료 시점과 다릅니다. 저장된 완료 근거와 현재 피드백을 대조하세요.";
  let agrees = s.state === result.evidence.state && s.execution_id === result.evidence.execution_id;
  if (record.kind === "START") agrees &&= s.state === "RUNNING" && s.inputs.run_feedback;
  if (record.kind === "PAUSE") agrees &&= s.state === "PAUSED" && !s.inputs.run_feedback && s.inputs.speed_m_per_min === 0;
  if (record.kind === "LOAD_JOB") agrees &&= s.recipe_version === request?.command.args.recipe_version && s.execution_id === request?.execution_id;
  if (record.kind === "SET_PARAMETERS") agrees &&= s.recipe_version === request?.command.args.recipe_version && s.parameters?.temperature_c === request?.command.args.parameters.temperature_c && s.parameters?.speed_m_per_min === request?.command.args.parameters.speed_m_per_min;
  if (record.kind === "RESET_FAULT") agrees &&= !s.latched_fault_code && !s.inputs.fault_code && !s.inputs.run_feedback;
  if (record.kind === "END_RUN") agrees &&= model.summary?.execution_id === s.execution_id && model.summary.final && model.summary.wip_quantity === 0;
  return agrees ? "성공 결과와 현재 PLC 피드백이 일치합니다." : "성공 결과 수신 · 현재 피드백 대조가 필요합니다.";
}

function renderCommands() {
  const records = [...model.commands.values()].reverse();
  const signature = JSON.stringify(records.map((record) => [record.id, record.kind, record.result, feedback(record)]));
  if (signature === model.commandSignature) return;
  model.commandSignature = signature;
  if (!records.length) { $("command-list").replaceChildren(element("p", "empty", "이 브라우저에서 추적하는 명령이 없습니다.")); return; }
  const nodes = records.map((record) => {
    const status = record.result?.status || "OUTCOME_UNKNOWN";
    const card = element("article", "command-record");
    const header = element("header");
    header.append(element("strong", "", KIND_LABELS[record.kind] || "원래 명령 조회"), element("span", `badge ${status === "SUCCEEDED" ? "good" : ["FAILED", "REJECTED", "EXPIRED"].includes(status) ? "bad" : "warning"}`, RESULT_LABELS[status] || status));
    card.append(header, element("code", "", record.id));
    if (record.result?.reason_code) card.append(element("p", "", record.result.reason_code));
    if (record.result?.reason) card.append(element("p", "", record.result.reason));
    if (status === "ACCEPTED") card.append(element("p", "", "접수는 실행 성공을 뜻하지 않습니다. 같은 ID의 결과를 확인 중입니다."));
    if (status === "OUTCOME_UNKNOWN" || status === "COMMAND_NOT_FOUND") card.append(element("p", "", "새 ID로 자동 재실행하지 않습니다. 원래 ID와 실행·물품 상태를 대조하세요."));
    const matched = feedback(record); if (matched) card.append(element("p", "", matched));
    if (record.result?.applied_control_version) card.append(element("p", "mono", `완료 제어 버전 ${uintString(record.result.applied_control_version)}`));
    const query = element("button", "button text", "같은 ID 다시 조회"); query.type = "button";
    query.addEventListener("click", () => runAction(async () => { await queryResult(record.id); }));
    card.append(query);
    return card;
  });
  $("command-list").replaceChildren(...nodes);
}

async function queryResult(id) {
  const epoch = model.sessionEpoch;
  const result = await api(`/command-record?${new URLSearchParams({ command_id: id })}`);
  if (epoch !== model.sessionEpoch || !model.principal) return null;
  const existing = model.commands.get(id) || { id };
  if (result.code === "COMMAND_NOT_FOUND") {
    model.commands.set(id, { ...existing, result: { status: "COMMAND_NOT_FOUND", reason: "저장된 ID를 찾지 못했습니다. 전달 가능성이 있다면 실행 실패로 단정할 수 없습니다." } });
  } else {
    if (result.command_id !== id) throw new Error("조회 결과의 명령 ID가 일치하지 않습니다.");
    if (result.applied_control_version !== null && result.applied_control_version !== undefined) uintString(result.applied_control_version);
    model.commands.set(id, { ...existing, result });
  }
  renderCommands(); renderAccess();
  return result;
}

async function refreshSummary() {
  const epoch = model.sessionEpoch;
  const id = model.snapshot?.state.execution_id;
  if (!id) { model.summary = null; renderSettlement(); return; }
  const summary = await api(`/run-summary?${new URLSearchParams({ execution_id: id })}`);
  if (epoch !== model.sessionEpoch || !model.principal) return;
  model.summary = summary.code === "RUN_NOT_FOUND" ? null : summary;
  if (model.summary) for (const name of ["input_quantity", "good_quantity", "reject_quantity", "scrap_quantity", "last_event_seq"]) uintString(model.summary[name]);
  renderSettlement(); renderCommands();
}

function renderSettlement() {
  let block = $("settlement");
  if (!block) { block = element("div", "reconcile-block"); block.id = "settlement"; $("pipeline").parentElement.append(block); }
  if (!model.summary?.final) { block.hidden = true; return; }
  block.hidden = false;
  const summary = model.summary;
  const message = element("p", "muted", `실행 최종 경계 확정 · ${summary.execution_id} · 투입 ${quantity(summary.input_quantity)} = 양품 ${quantity(summary.good_quantity)} + 불량 ${quantity(summary.reject_quantity)} + 폐기 ${quantity(summary.scrap_quantity)} + 재공 ${summary.wip_quantity}`);
  const note = element("p", "form-help", "이 콘솔의 대조는 시뮬레이터 현장 확인입니다. MES 사건 정산 완료를 대신하지 않습니다.");
  block.replaceChildren(message, note);
  if (!model.snapshot.state.previous_execution_settled) {
    const button = element("button", "button secondary", "최종 수량 대조 확인"); button.id = "settle-run"; button.type = "button";
    button.addEventListener("click", () => runAction(async () => {
      requireOperator();
      if (!window.confirm(`실행 ${summary.execution_id}의 최종 수량과 사건 경계를 대조했습니까? 다음 작업 연결을 위한 현장 대조로 기록합니다.`)) return;
      await api(`/run-settlement?${new URLSearchParams({ execution_id: summary.execution_id })}`, { method: "POST", body: { operator_confirmed: true } });
      await refreshSnapshot(); await refreshSummary(); notice("현재 실행의 현장 대조 확인을 기록했습니다. 다음 작업 연결은 별도 명령입니다.", "good");
    }));
    block.append(button);
  } else block.append(element("span", "badge good", "시뮬레이터 현장 대조 확인됨"));
  renderAccess();
}

function requireOperator() {
  if (!operator() || !fresh()) throw new Error("로그인한 조작자와 3초 이내 최신 관측이 필요합니다.");
}
function idValue(input, required = true) {
  const value = input.value.trim();
  if ((!value && required) || value.length > 128 || /\s/.test(value)) throw new Error("ID는 공백 없는 1~128자로 입력하세요.");
  return value || null;
}
function numberValue(input, { integer = false, min = 0, max = Number.MAX_VALUE } = {}) {
  const value = Number(input.value);
  if (input.value === "" || !Number.isFinite(value) || value < min || value > max || (integer && !Number.isInteger(value))) throw new Error("수치 입력 범위를 확인하세요.");
  return value;
}
function recoveryChecks() {
  const checks = Object.fromEntries(RECOVERY_KEYS.map((name) => [name, field(forms.recovery, name).checked]));
  if (!Object.values(checks).every(Boolean)) throw new Error("복구 대조 항목 5개를 직접 확인하세요.");
  return checks;
}
function resetRecoveryChecks() { for (const name of RECOVERY_KEYS) field(forms.recovery, name).checked = false; }

async function runAction(action) {
  if (model.busy) return;
  model.busy = true; renderAccess();
  try { await action(); }
  catch (error) {
    if (error.status === 401) clearSession();
    if (!(error instanceof RequestError)) model.connected = fresh();
    notice(error.message || "요청 결과를 확인할 수 없습니다. 자동으로 다시 보내지 않습니다.", "bad");
  } finally { model.busy = false; renderAccess(); }
}

forms.login.addEventListener("submit", (event) => {
  event.preventDefault();
  runAction(async () => {
    const login = field(forms.login, "login").value;
    const password = field(forms.login, "password").value;
    field(forms.login, "password").value = "";
    const challenge = await api("/auth/challenge", { method: "POST", body: {}, csrf: null, challenge: true });
    const session = await api("/auth/login", { method: "POST", body: { login, password }, csrf: challenge.csrf_token });
    model.sessionEpoch += 1;
    model.principal = session; model.csrf = session.csrf_token; model.passwordOpen = false;
    renderAccess(); await refreshSnapshot(); await refreshSummary();
    notice(session.must_change_password ? "로그인했습니다. 초기 암호를 변경한 뒤 조작하세요." : "로그인했습니다. 최신 PLC 관측을 표시합니다.", "good");
  });
});
forms.password.addEventListener("submit", (event) => {
  event.preventDefault();
  runAction(async () => {
    const password = field(forms.password, "password").value;
    if (password !== field(forms.password, "confirm").value) throw new Error("새 비밀번호 확인이 일치하지 않습니다.");
    const passwordBytes = new TextEncoder().encode(password).length;
    if (passwordBytes < 12 || passwordBytes > 1024 || password.includes("\0")) throw new Error("새 암호는 UTF-8 기준 12~1024 bytes이며 NUL을 포함할 수 없습니다.");
    forms.password.reset();
    const session = await api("/auth/password", { method: "POST", body: { new_password: password } });
    model.principal = session; model.csrf = session.csrf_token; model.passwordOpen = false;
    await refreshSnapshot(); notice("암호를 변경하고 이전 세션을 취소했습니다.", "good");
  });
});
$("show-password").addEventListener("click", () => { model.passwordOpen = !model.passwordOpen; renderAccess(); });
$("logout").addEventListener("click", () => runAction(async () => {
  await api("/auth/logout", { method: "POST", body: {} });
  clearSession(); notice("로그아웃했습니다.");
}));
$("refresh").addEventListener("click", () => runAction(async () => { await refreshSnapshot(); await refreshSummary(); notice("최신 관측을 확인했습니다.", "good"); }));

function commandFields() {
  const kind = field(forms.command, "kind").value;
  $("job-fields").hidden = kind !== "LOAD_JOB";
  $("parameter-fields").hidden = !["LOAD_JOB", "SET_PARAMETERS"].includes(kind);
  field(forms.command, "execution_id").placeholder = kind === "LOAD_JOB" ? "새 실행 ID를 직접 지정하세요" : "현재 실행 ID · 리셋은 비워둘 수 있음";
  for (const name of ["work_order_id", "target_quantity"]) field(forms.command, name).required = kind === "LOAD_JOB";
  for (const name of ["recipe_version", "temperature_c", "speed_m_per_min"]) field(forms.command, name).required = ["LOAD_JOB", "SET_PARAMETERS"].includes(kind);
  field(forms.command, "execution_id").required = ["LOAD_JOB", "START", "PAUSE", "END_RUN"].includes(kind);
}
field(forms.command, "kind").addEventListener("change", commandFields);
forms.command.addEventListener("submit", (event) => {
  event.preventDefault();
  runAction(async () => {
    requireOperator(); if (unresolved()) throw new Error("미확정 원래 명령의 결과를 먼저 대조하세요.");
    const kind = field(forms.command, "kind").value;
    const execution = idValue(field(forms.command, "execution_id"), ["LOAD_JOB", "START", "PAUSE", "END_RUN"].includes(kind));
    const args = {};
    if (["LOAD_JOB", "SET_PARAMETERS"].includes(kind)) {
      args.recipe_version = idValue(field(forms.command, "recipe_version"));
      args.parameters = {
        temperature_c: numberValue(field(forms.command, "temperature_c")),
        speed_m_per_min: numberValue(field(forms.command, "speed_m_per_min"), { min: Number.MIN_VALUE }),
      };
    }
    if (kind === "LOAD_JOB") {
      args.work_order_id = idValue(field(forms.command, "work_order_id"));
      args.target_quantity = numberValue(field(forms.command, "target_quantity"), { integer: true, min: 1, max: 4294967295 });
    }
    if (!window.confirm(`${KIND_LABELS[kind]} 명령을 요청합니까? 실행 ${execution || "없음"}. PLC가 실제 허용 조건과 완료를 판정합니다.`)) return;
    const snapshot = await refreshSnapshot();
    requireOperator(); if (unresolved()) throw new Error("새 관측에 미확정 명령이 있습니다. 원래 ID부터 확인하세요.");
    const requested = new Date(snapshot.observed_at);
    const id = `console-${crypto.randomUUID()}`;
    const request = {
      schema_version: "1.0", command_id: id, target_id: snapshot.state.target_id,
      execution_id: execution, command: { kind, args }, requested_by: model.principal.login,
      requested_at: requested.toISOString(), expires_at: new Date(requested.getTime() + 5000).toISOString(),
      expected_control_version: uintString(snapshot.tags["Line.ControlVersion"]),
    };
    const record = { id, kind, request, result: { status: "DELIVERED", reason: "요청 응답을 기다립니다." } };
    model.commands.set(id, record); renderCommands();
    try {
      const response = await api("/commands", { method: "POST", body: request });
      if (response.submission.command_id !== id) throw new Error("접수 응답의 명령 ID가 일치하지 않습니다.");
      record.result = response.result || { status: "REJECTED", reason_code: response.submission.reason_code, reason: response.submission.reason };
      model.commands.set(id, record);
      notice(response.submission.disposition === "ACCEPTED" ? `명령 ${id}를 접수했습니다. 실제 결과를 같은 ID로 확인합니다.` : `명령이 거절되었습니다: ${response.submission.reason_code}`, response.submission.disposition === "ACCEPTED" ? "neutral" : "warning");
    } catch (error) {
      record.result = { status: "OUTCOME_UNKNOWN", reason: "접수 응답을 확정할 수 없습니다. 원래 ID를 조회하며 명령을 다시 보내지 않습니다." };
      model.commands.set(id, record); renderCommands();
      throw error;
    }
    await refreshSnapshot(); await queryResult(id); await refreshSummary();
    renderCommands();
  });
});
forms.lookup.addEventListener("submit", (event) => { event.preventDefault(); runAction(async () => { const id = idValue(field(forms.lookup, "command_id")); await queryResult(id); await refreshSnapshot(); await refreshSummary(); }); });

forms.mode.addEventListener("submit", (event) => {
  event.preventDefault(); runAction(async () => {
    requireOperator();
    const control_mode = field(forms.mode, "control_mode").value, operation_mode = field(forms.mode, "operation_mode").value;
    const snapshot = await refreshSnapshot(); requireOperator();
    await api("/inputs", { method: "POST", body: { inputs: snapshot.state.inputs, control_mode, operation_mode } });
    await refreshSnapshot(); populateInputs(); notice("현장·운전 모드를 적용하고 PLC 피드백을 다시 관측했습니다.", "good");
  });
});
forms.inputs.addEventListener("submit", (event) => {
  event.preventDefault(); runAction(async () => {
    requireOperator();
    const changed = {};
    for (const name of ["estop", "guard_closed", "equipment_ready", "fault_resettable", "run_feedback"]) changed[name] = field(forms.inputs, name).checked;
    changed.temperature_offset_c = numberValue(field(forms.inputs, "temperature_offset_c"), { min: -50, max: 50 });
    changed.pressure_bar = numberValue(field(forms.inputs, "pressure_bar"));
    changed.fault_code = numberValue(field(forms.inputs, "fault_code"), { integer: true, max: 4294967295 });
    changed.speed_m_per_min = numberValue(field(forms.inputs, "speed_m_per_min"));
    if (!window.confirm("선택한 안전·고장·공압·온도 센서·피드백 시험 입력을 실제 가상 PLC에 적용합니까?")) return;
    const snapshot = await refreshSnapshot(); requireOperator();
    await api("/inputs", { method: "POST", body: { inputs: { ...snapshot.state.inputs, ...changed } } });
    await refreshSnapshot(); populateInputs(); resetRecoveryChecks(); notice("시험 입력을 적용했습니다. 고장 원인 해소 후에도 별도 리셋·재개가 필요할 수 있습니다.", "good");
  });
});
$("clear-temperature-offset").addEventListener("click", () => {
  field(forms.inputs, "temperature_offset_c").value = "0";
  notice("온도 센서 편차 입력값을 0으로 바꿨습니다. 시험 입력 적용을 눌러 저장하세요.");
});
field(forms.fieldFault, "group").addEventListener("change", populateFieldFaultIntent);
field(forms.fieldFault, "mode").addEventListener("change", syncFieldDelayInput);
$("field-fault-refresh").addEventListener("click", () => runAction(async () => {
  const states = await api("/field-faults");
  if (model.snapshot) model.snapshot.field_channels = states;
  model.fieldUncertain = false; populateFieldFaultIntent(); renderFieldFaults();
  text("field-fault-result", "현재 저장 설정과 실제 발행을 조회했습니다. 조회는 설정을 적용하지 않습니다.");
}));
forms.fieldFault.addEventListener("submit", event => {
  event.preventDefault(); runAction(async () => {
    requireOperator();
    if (model.fieldUncertain || model.fieldRevision === null) throw new Error("먼저 설정 다시 조회로 현재 버전을 대조하세요.");
    const group = field(forms.fieldFault, "group").value;
    const intent = { mode: field(forms.fieldFault, "mode").value, expected_revision: model.fieldRevision };
    if (intent.mode === "DELAY") {
      const value = field(forms.fieldFault, "delay_ms").value;
      if (!/^[0-9]+$/.test(value)) throw new Error("지연 시간은 100~10,000ms의 정수로 입력하세요.");
      const delay = Number(value);
      if (!Number.isInteger(delay) || delay < 100 || delay > 10000 || delay % 100 !== 0) throw new Error("지연 시간은 100~10,000ms, 100ms 단위로 입력하세요.");
      const current = model.snapshot?.field_channels?.find(item => item.configured.group_id === group)?.configured;
      if (current?.mode === "DELAY" && current.delay_ms !== delay) throw new Error("시간을 바꾸려면 먼저 NORMAL을 적용하고 실제 NORMAL 발행을 확인한 뒤 새 DELAY를 설정하세요.");
      intent.delay_ms = delay;
    }
    if (!window.confirm(`${group} 필드 시험 조건 ${intent.mode}${intent.delay_ms === undefined ? "" : ` ${intent.delay_ms}ms`}을 적용합니까? OPC 연결은 유지됩니다.`)) return;
    try {
      const committed = await api(`/field-faults/${group}`, { method: "POST", body: intent });
      model.fieldRevision = uintString(committed.revision);
      text("field-fault-intent", `${group} · 저장한 설정 버전 ${model.fieldRevision} 기준`);
      text("field-fault-result", "시험 설정을 저장했습니다. 실제 OPC 발행 버전은 새 관측에서 확인하세요.");
      await refreshSnapshot();
    } catch (error) {
      model.fieldUncertain = true;
      text("field-fault-result", "적용 결과를 확정하지 않았습니다. 자동 재요청하지 않습니다. 설정 다시 조회로 버전과 조건을 대조하세요.");
      throw error;
    } finally { $("field-fault-apply").disabled = model.fieldUncertain; }
  });
});
forms.recovery.addEventListener("submit", (event) => {
  event.preventDefault(); runAction(async () => {
    requireOperator(); const checks = recoveryChecks(); const checkedVersion = model.snapshot.state.control_version;
    if (!window.confirm("대조한 실행·물품·명령 상태를 근거로 복구 게이트를 해제합니까? START는 별도 요청입니다.")) return;
    const snapshot = await refreshSnapshot(); requireOperator();
    if (snapshot.state.control_version !== checkedVersion) { resetRecoveryChecks(); throw new Error("확인 중 PLC 제어 상태가 바뀌었습니다. 복구 항목을 다시 대조하세요."); }
    await api("/recover", { method: "POST", body: checks });
    resetRecoveryChecks(); await refreshSnapshot(); await refreshSummary(); notice("명시적 복구 결과를 관측했습니다. 자동 재가동은 수행하지 않습니다.", "good");
  });
});
$("reconcile").addEventListener("click", () => runAction(async () => {
  requireOperator(); const checks = recoveryChecks(); const id = idValue(field(forms.recovery, "interrupted_id"));
  const checkedVersion = model.snapshot.state.control_version;
  if (!window.confirm(`원래 명령 ${id}의 중단과 정지 위치를 대조했습니까? 중단 확정 결과를 기록하며 재실행하지 않습니다.`)) return;
  const snapshot = await refreshSnapshot(); requireOperator();
  if (snapshot.state.control_version !== checkedVersion) { resetRecoveryChecks(); throw new Error("확인 중 PLC 제어 상태가 바뀌었습니다. 원래 명령과 위치를 다시 대조하세요."); }
  const result = await api(`/command-reconciliation?${new URLSearchParams({ command_id: id })}`, { method: "POST", body: checks });
  model.commands.set(id, { ...(model.commands.get(id) || { id }), result });
  resetRecoveryChecks(); await refreshSnapshot(); renderCommands(); notice("대조한 원래 명령의 중단 결과를 기록했습니다. 다음으로 복구 항목을 다시 확인하세요.", "good");
}));
forms.scrap.addEventListener("submit", (event) => {
  event.preventDefault(); runAction(async () => {
    requireOperator();
    const ids = [...forms.scrap.querySelectorAll('input[name="item_id"]:checked')].map((input) => uintString(input.value));
    const positions = new Map((model.snapshot.process?.slots || []).map((item, index) => [item?.item_id, index]));
    const reason = field(forms.scrap, "reason").value.trim();
    if (!ids.length || !reason || !field(forms.scrap, "positions_verified").checked) throw new Error("폐기 물품·사유·정지 위치 확인이 모두 필요합니다.");
    if (!window.confirm(`물품 ${ids.join(", ")}를 사유 “${reason}”로 폐기합니까? 선택 물품의 수량과 사건이 함께 기록됩니다.`)) return;
    const snapshot = await refreshSnapshot(); requireOperator();
    const current = new Map((snapshot.process?.slots || []).map((item, index) => [item?.item_id, index]));
    if (!ids.every((id) => current.has(id) && current.get(id) === positions.get(id))) throw new Error("선택한 물품 위치가 바뀌었습니다. 새 관측에서 다시 확인하세요.");
    await api("/scrap", { method: "POST", body: { item_ids: ids, reason, positions_verified: true } });
    forms.scrap.reset(); model.scrapSignature = null; await refreshSnapshot(); await refreshSummary(); notice("선택 물품의 폐기 결과를 실제 공정 관측에서 확인했습니다.", "good");
  });
});

async function poll() {
  if (!model.principal || model.polling || model.busy || document.hidden) return;
  model.polling = true;
  const epoch = model.sessionEpoch;
  try {
    if (!await refreshSnapshot()) return;
    for (const record of model.commands.values()) if (!TERMINAL.has(record.result?.status)) await queryResult(record.id);
    await refreshSummary();
  } catch (error) {
    if (epoch !== model.sessionEpoch) return;
    if (error.status === 401) clearSession();
    model.connected = false; renderAccess();
  } finally { model.polling = false; }
}
async function boot() {
  commandFields(); renderAccess();
  try {
    model.principal = await api("/auth/me");
    renderAccess(); await refreshSnapshot(); await refreshSummary();
  } catch (error) {
    if (error.status !== 401) notice("콘솔에 연결할 수 없습니다. 서버 실행과 같은 출처 연결을 확인하세요.", "warning");
    renderAccess();
  }
  setInterval(() => { renderAccess(); void poll(); }, 1000);
}
void boot();
