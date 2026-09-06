/* ── Rendering Infrastructure ──────────────────────────────── */

function renderLoading(targetId, message = "Working") {
  const target = byId(targetId);
  clearNode(target);
  target.classList.remove("empty-state");
  const wrapper = el("div", "loading-indicator scan-line");
  wrapper.appendChild(el("p", "summary", message));
  target.appendChild(wrapper);
}

function renderError(targetId, error) {
  const target = byId(targetId);
  clearNode(target);
  target.classList.remove("empty-state");
  const card = el("div", "error-card");
  card.appendChild(el("h4", "", "Something went wrong"));
  card.appendChild(el("p", "", String(error.message || error)));
  card.appendChild(el("p", "error-action", "Try again in a moment, or check the CLI panel for technical details."));
  target.appendChild(card);
  showToast("Action failed — check the result area for details");
}

function render(targetId, data, type = "generic") {
  const target = byId(targetId);
  if (type === "chat") {
    renderChatResult(data);
    state.lastText = textForSpeech(data);
    if (state.pendingSpeak) {
      state.pendingSpeak = false;
      speakLast();
    }
    return;
  }
  clearNode(target);
  target.classList.remove("empty-state");
  if (type === "explore") renderExplore(target, data);
  else if (type === "command") renderCommand(target, data);
  else if (type === "build") renderBuild(target, data);
  else if (type === "workflow") renderWorkflow(target, data);
  else if (type === "learn") renderLearning(target, data);
  else if (type === "health") renderHealth(target, data);
  else if (type === "phoneStatus") renderPhoneStatus(target, data);
  else if (type === "settings") renderSettingsResult(target, data);
  else renderGeneric(target, data);
  state.lastText = textForSpeech(data);
}

async function run(targetId, type, task, buttonId, options = {}) {
  if (buttonId && byId(buttonId).classList.contains("loading")) return; // ignore rapid double-clicks
  renderLoading(targetId, options.loadingText);
  if (buttonId) setButtonLoading(buttonId, true);
  // While this operation is in flight, live events from the single activity
  // channel are shown under the loading scan-line (see appendLiveStep).
  const family = activityFamily(type);
  if (family) state.activity = { family, targetId };
  try {
    const data = await task();
    render(targetId, data, type);
    showToast(options.successToast || "Action completed");
    if (options.onSuccess) options.onSuccess(data);
  } catch (error) {
    renderError(targetId, error);
    if (options.onError) options.onError(error);
  } finally {
    if (family && state.activity && state.activity.targetId === targetId) state.activity = null;
    if (buttonId) setButtonLoading(buttonId, false);
  }
}

/* ── Live activity stream (ONE SSE channel on the app EventBus) ── */

let activitySource = null;

// Which progress families a run type subscribes to while loading. "ask" (Chat)
// accepts research AND command steps, because a chat answer can be an Explore
// report or an approved action execution.
function activityFamily(type) {
  if (type === "explore") return "explore";
  if (type === "command") return "command";
  if (type === "chat") return "ask";
  return null;
}

function activitySourceUrl() {
  const token = byId("tokenInput").value.trim();
  return token ? `/events/stream?token=${encodeURIComponent(token)}` : "/events/stream";
}

// Open the single channel once per page. Native EventSource reconnects after
// drops; the server sends comment keep-alive frames so the connection stays warm.
function connectActivitySource() {
  if (activitySource || typeof EventSource === "undefined") return;
  try {
    activitySource = new EventSource(activitySourceUrl());
    activitySource.addEventListener("explore.progress", onActivityEvent);
    activitySource.addEventListener("command.progress", onActivityEvent);
  } catch (_) {
    activitySource = null;
  }
}

function onActivityEvent(event) {
  const act = state.activity;
  if (!act) return;
  const family = String(event.type || "").split(".")[0];
  if (!familyMatches(act.family, family)) return;
  let data = {};
  try { data = JSON.parse(event.data); } catch (_) { return; }
  appendLiveStep(act.targetId, data.detail || data.step || "");
}

function familyMatches(activityFamilyName, eventFamily) {
  if (activityFamilyName === eventFamily) return true;
  return activityFamilyName === "ask" && (eventFamily === "explore" || eventFamily === "command");
}

// Show one live line under run()'s loading scan-line while an action runs.
function appendLiveStep(targetId, text) {
  if (!text) return;
  const target = byId(targetId);
  if (!target || !target.querySelector(".loading-indicator")) return; // run finished
  let box = target.querySelector(".explore-progress");
  if (!box) {
    box = el("div", "explore-progress");
    target.appendChild(box);
  }
  const rows = box.querySelectorAll(".progress-step");
  if (rows.length && rows[rows.length - 1].textContent === text) return; // no repeats
  box.appendChild(el("p", "progress-step", text));
}

/* ── Shared text helpers ───────────────────────────────────── */

// Chat lead summary: the shared extractor with a friendly default when empty.
function cleanSummary(value) {
  return extractAnswerText(value, "I completed the request.");
}

function richBullet(text) {
  const item = el("li", "");
  const value = String(text);
  // Render the shared inline markdown renderer (dom.js _renderInline) so **bold**,
  // *emphasis*, and `code` never leak as raw markers from list items.
  const colon = value.indexOf(":");
  if (colon > 0 && colon < 42) {
    item.appendChild(el("strong", "", value.slice(0, colon + 1)));
    item.appendChild(document.createTextNode(" "));
    _renderInline(item, value.slice(colon + 1).trim());
  } else {
    _renderInline(item, value);
  }
  return item;
}

function renderActionCards(target, actions, descriptionKey) {
  if (!actions.length) return;
  const grid = el("div", "card-grid");
  actions.forEach((action, index) => {
    const card = el("article", "info-card");
    card.appendChild(el("h4", "", action.title || action.name || `Step ${index + 1}`));
    card.appendChild(el("p", "", action[descriptionKey] || action.status || "Ready"));
    if (action.verification?.length) {
      const row = el("div", "pill-row");
      action.verification.forEach((item) => row.appendChild(el("span", "pill", item)));
      card.appendChild(row);
    }
    grid.appendChild(card);
  });
  target.appendChild(grid);
}

function renderFindings(target, findings) {
  if (!findings.length) return;
  const wrapper = el("div", "link-list");
  wrapper.appendChild(el("h3", "", "Findings"));
  findings.slice(0, 8).forEach((finding) => {
    const item = el("div", "list-item");
    item.appendChild(el("strong", "", finding.path || "Project"));
    item.appendChild(el("span", `pill ${finding.severity === "error" ? "error" : "warn"}`, finding.severity));
    item.appendChild(el("p", "", finding.message));
    wrapper.appendChild(item);
  });
  target.appendChild(wrapper);
}
