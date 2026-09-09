/* ── Rendering Infrastructure ──────────────────────────────── */

function renderLoading(targetId, message = "Working") {
  resetProgressSteps(); // a new run starts a fresh paced stream
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
  else if (type === "visionClick" && data && data.action === "guided_click") renderVisionClickResult(target, data);
  else renderGeneric(target, data);
  state.lastText = textForSpeech(data);
}

async function run(targetId, type, task, buttonId, options = {}) {
  if (buttonId && byId(buttonId).classList.contains("loading")) return; // ignore rapid double-clicks
  renderLoading(targetId, options.loadingText);
  if (buttonId) setButtonLoading(buttonId, true);
  // While this operation is in flight, live events from the single activity
  // channel are shown under the loading scan-line (see appendLiveStep). The
  // run's correlation id routes its events here — and only here — so two
  // concurrent runs of the same family never mix rows. A caller-supplied
  // options.correlationId (the same id the request body carries) wins so the
  // echo matches exactly; voice submits mint one locally and rely on adoption.
  const family = activityFamily(type);
  const correlationId = options.correlationId || newCorrelationId();
  if (family) registerActivitySlot(correlationId, family, targetId);
  try {
    const data = await task();
    await drainProgressSteps(); // let fast runs still show each stage visibly
    render(targetId, data, type);
    showToast(options.successToast || "Action completed");
    if (options.onSuccess) options.onSuccess(data);
  } catch (error) {
    renderError(targetId, error);
    if (options.onError) options.onError(error);
  } finally {
    if (family) releaseActivitySlot(correlationId);
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

/* ── Run-scoped correlation ─────────────────────────────────

Every activity run mints a client-side correlation id and passes it to the
server; progress events echo it back, and onActivityEvent routes each frame to
THE run that owns it. Two concurrent runs of the same family (a research in
Explore while /ask researches from Chat, or two Approve And Runs in two tabs)
interleave in the timeline without mixing rows. Runs that cannot pass a body
(voice-driven submits) register their slot anyway and match the next
same-family frame carrying an unknown id — a best-effort adoption, never a
cross-family mix. */
const ACTIVITY_SLOTS = new Map(); // correlation_id -> { family, targetId }
const ACTIVITY_SLOTS_MAX = 12;

function newCorrelationId() {
  return "run-" + Math.random().toString(36).slice(2, 10) + Date.now().toString(36);
}

function registerActivitySlot(correlationId, family, targetId) {
  if (!correlationId) return;
  ACTIVITY_SLOTS.set(correlationId, { family, targetId });
  // Bounded: drop the OLDEST slot when over budget (Map preserves insertion order).
  while (ACTIVITY_SLOTS.size > ACTIVITY_SLOTS_MAX) {
    const oldest = ACTIVITY_SLOTS.keys().next().value;
    ACTIVITY_SLOTS.delete(oldest);
  }
}

function releaseActivitySlot(correlationId) {
  if (correlationId) ACTIVITY_SLOTS.delete(correlationId);
}

function activitySlotForEvent(family, correlationId) {
  if (correlationId) {
    const hit = ACTIVITY_SLOTS.get(correlationId);
    if (hit) return hit;
  }
  // Unclaimed frame (CLI/GUI-initiated run, or a voice submit without a body):
  // adopt it into an open slot — exact-family slots first, then chat slots,
  // whose answer can legitimately BE an explore/command stream.
  for (const slot of ACTIVITY_SLOTS.values()) {
    if (slot.family === family) return slot;
  }
  for (const slot of ACTIVITY_SLOTS.values()) {
    if (slotFamilyAccepts(slot.family, family)) return slot;
  }
  return null;
}

// A chat run (family "ask") accepts both explore and command frames, because
// its answer can BE a research report or an approved action execution.
function slotFamilyAccepts(slotFamily, eventFamily) {
  if (slotFamily === eventFamily) return true;
  return slotFamily === "ask" && (eventFamily === "explore" || eventFamily === "command");
}

// Open the single channel once per page. Native EventSource reconnects after
// drops; the server sends comment keep-alive frames so the connection stays warm.
function connectActivitySource() {
  if (activitySource || typeof EventSource === "undefined") return;
  try {
    activitySource = new EventSource(activitySourceUrl());
    activitySource.addEventListener("explore.progress", onActivityEvent);
    activitySource.addEventListener("command.progress", onActivityEvent);
    // Completion events feed the Recent Activity timeline (activity.js) —
    // every open tab appends every completion, whatever client started it.
    activitySource.addEventListener("command.completed", onCompletedActivity);
    activitySource.addEventListener("explore.completed", onCompletedActivity);
    activitySource.addEventListener("learn.completed", onCompletedActivity);
    // (Re)seed from the journal whenever the channel (re)opens — EventSource
    // auto-reconnect after a drop backfills anything missed while offline.
    activitySource.onopen = () => { if (typeof loadActivityFromServer === "function") loadActivityFromServer(); };
  } catch (_) {
    activitySource = null;
  }
}

function onCompletedActivity(event) {
  let data = {};
  try { data = JSON.parse(event.data); } catch (_) { return; }
  if (typeof recordActivityFromEvent === "function") recordActivityFromEvent(data);
}

function onActivityEvent(event) {
  let data = {};
  try { data = JSON.parse(event.data); } catch (_) { return; }
  const family = String(event.type || "").split(".")[0];
  const slot = activitySlotForEvent(family, data.correlation_id);
  if (!slot) return;
  appendLiveStep(slot.targetId, data.detail || data.step || "");
}

// Show one live line under run()'s loading scan-line while an action runs.
//
// Steps are PACED, not appended instantly: when the research pipeline finishes
// faster than a human can read, the completed run() would otherwise flash every
// stage and the final report into the DOM inside a single frame. appendLiveStep
// queues the step text; a drain timer reveals one queued step at a time with a
// minimum gap between reveals, and run() awaits drainProgressSteps() before
// rendering the final result — so even a cached, instant response shows each
// stage visibly.
const PROGRESS_STEP_GAP_MS = 350; // visible pacing while the run is in flight
const PROGRESS_STEP_FLUSH_MS = 60; // once the run finished, flush quickly but still staged
let progressQueue = [];
let progressTimer = null;
let progressLastReveal = 0;

function resetProgressSteps() {
  progressQueue = [];
  if (progressTimer) { clearTimeout(progressTimer); progressTimer = null; }
}

// Resolve once every queued step has been revealed (fast-flush mode).
function drainProgressSteps() {
  return new Promise((resolve) => {
    if (!progressQueue.length && !progressTimer) { resolve(); return; }
    progressQueueDrained = resolve;
  });
}

let progressQueueDrained = null;

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
  const lastShown = rows.length
    ? rows[rows.length - 1].textContent
    : (progressQueue.length ? progressQueue[progressQueue.length - 1].text : "");
  if (lastShown === text) return; // no repeats
  progressQueue.push({ targetId, text });
  scheduleProgressStep();
}

function scheduleProgressStep() {
  if (progressTimer) return;
  // Pace by RUN state, not queue depth: a just-queued first step in a still-
  // running run must wait the full visible gap. Only after run() finished
  // (loading panel gone) do we fast-flush so the report swap isn't stalled.
  const next = progressQueue[0];
  const target = next ? byId(next.targetId) : null;
  const runInFlight = !!target && !!target.querySelector(".loading-indicator");
  const gap = runInFlight ? PROGRESS_STEP_GAP_MS : PROGRESS_STEP_FLUSH_MS;
  const wait = Math.max(0, progressLastReveal + gap - Date.now());
  progressTimer = setTimeout(showNextProgressStep, wait);
}

function showNextProgressStep() {
  progressTimer = null;
  const item = progressQueue.shift();
  if (item) {
    const target = byId(item.targetId);
    // Reveal only while the run's loading area still exists; a replaced panel
    // (new run started) discards stale steps.
    if (target && target.querySelector(".loading-indicator")) {
      let box = target.querySelector(".explore-progress");
      if (!box) {
        box = el("div", "explore-progress");
        target.appendChild(box);
      }
      const rows = box.querySelectorAll(".progress-step");
      if (!rows.length || rows[rows.length - 1].textContent !== item.text) {
        box.appendChild(el("p", "progress-step", item.text));
      }
      progressLastReveal = Date.now();
    }
  }
  if (progressQueue.length) {
    scheduleProgressStep();
  } else if (progressQueueDrained) {
    const resolve = progressQueueDrained;
    progressQueueDrained = null;
    resolve();
  }
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
