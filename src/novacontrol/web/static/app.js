/* ── NovaControl Web UI — Orchestrator ────────────────────────
 *
 *  This file is the entry point.  All logic lives in the modules
 *  loaded before it via <script> tags in index.html:
 *
 *    js/dom.js          — DOM utilities (byId, el, clearNode, …)
 *    js/state.js        — App state, API helpers, textForSpeech
 *    js/render-utils.js — Loading, error, run, richBullet, …
 *    js/render-explore.js — AI answer page, source rail, citations
 *    js/render-panels.js  — Chat, command, build, workflow, …
 *    js/voice.js        — Speech recognition & synthesis
 *    js/effects.js      — 3D background & card tilt
 *    app.js             — This file: event binding & init
 * ──────────────────────────────────────────────────────────── */

/* ── Chat Submit ──────────────────────────────────────────── */

function submitChat(options = {}) {
  if (byId("askButton").classList.contains("loading")) return; // ignore rapid double-sends (input is cleared below)
  const request = byId("chatInput").value.trim();
  if (!request) { showToast("Type a message first"); return; }
  state.lastQuery = request;
  state.pendingSpeak = Boolean(options.speak);
  byId("chatInput").value = "";
  run("chatStream", "chat", () => requestJson("/ask", { request }), "askButton", {
    loadingText: "Thinking through your question...",
    successToast: "Response ready",
    onSuccess: (data) => {
      recordChatTurn(request, "user");
      recordChatTurn(chatTextForHistory(data), "assistant", data.route || data.intent || "");
    },
    onError: () => {
      state.pendingSpeak = false;
      if (state.voiceMode === "conversation") setVoiceStatus("Voice ready");
    },
  });
}

/* ── Tab Navigation ───────────────────────────────────────── */

function setupTabs() {
  document.querySelectorAll(".nav-item").forEach((button) => {
    button.addEventListener("click", () => {
      document.querySelectorAll(".nav-item").forEach((item) => item.classList.remove("active"));
      document.querySelectorAll(".panel").forEach((panel) => panel.classList.remove("active"));
      button.classList.add("active");
      byId(button.dataset.panel).classList.add("active");
      // Panel choice survives reloads; restored on init by restoreActivePanel().
      try { localStorage.setItem("novacontrol.activePanel", button.dataset.panel); } catch (_) { /* storage unavailable */ }
    });
  });
}

function restoreActivePanel() {
  let saved = "";
  try { saved = localStorage.getItem("novacontrol.activePanel") || ""; } catch (_) { /* storage unavailable */ }
  const target = saved && byId(saved) ? saved : "commandPanel";
  const nav = document.querySelector(`.nav-item[data-panel="${target}"]`);
  if (nav) nav.click();
}

/* ── Action Button Bindings ────────────────────────────────── */

function setupActions() {
  byId("refreshButton").addEventListener("click", refreshStatus);

  byId("homeHealthButton").addEventListener("click", () => {
    document.querySelector('[data-panel="systemPanel"]').click();
    byId("systemHealthButton").click();
  });

  byId("commandPlanButton").addEventListener("click", () => {
    const command = byId("commandInput").value.trim();
    if (!command) { showToast("Type a command first"); return; }
    run("commandOutput", "command", () => requestJson("/command/plan", { command }), "commandPlanButton");
  });

  byId("commandRunButton").addEventListener("click", () => {
    const command = byId("commandInput").value.trim();
    if (!command) { showToast("Type a command first"); return; }
    // Approve And Run: reuse the token from the last preview of THIS command and
    // only re-plan (which mints a fresh token) when none is stored. A consumed or
    // expired token gets one automatic re-plan so a stale approval can't dead-end.
    const execute = (token) => requestJson("/command/execute", { command, approval_token: token });
    run("commandOutput", "command", async () => {
      const stored = approvalFor(command);
      if (stored) {
        try { return await execute(stored); }
        catch (error) {
          if (!/token|expired|unknown/i.test(String(error.message || ""))) throw error;
          clearApproval(command); // consumed or expired — fall through and re-plan once
        }
      }
      const plan = await requestJson("/command/plan", { command });
      const fresh = plan?.approval?.token;
      if (!fresh) return plan; // not an executable desktop action — show the planned result
      return execute(fresh);
    }, "commandRunButton", {
      onSuccess: (data) => {
        if (data && data.status === "executed") {
          clearApproval(command);
          recordActivity("command", "Command executed", command);
        }
      },
    });
  });

  byId("askButton").addEventListener("click", () => submitChat());
  byId("speakButton").addEventListener("click", speakLast);

  byId("exploreButton").addEventListener("click", () => {
    const topic = byId("exploreInput").value.trim();
    if (!topic) { showToast("Enter a topic to research"); return; }
    state.lastQuery = topic;
    byId("exploreOutput").scrollIntoView({ behavior: "smooth", block: "start" });
    // Same run() lifecycle as every other action: shared loading/error/toast/button
    // state. Live research steps arrive over the single /events/stream activity
    // channel (run() shows them under the scan-line) while this POST completes
    // with the finished report.
    run("exploreOutput", "explore", async () => {
      const body = { topic, include_videos: true };
      if (state.lastExploredTopic) body.last_topic = state.lastExploredTopic;
      return requestJson("/explore", body);
    }, "exploreButton", {
      loadingText: "Researching — live progress appears below...",
      successToast: "Research complete",
      onSuccess: (report) => {
        trackExploredTopic(topic);
        recordActivity("research", "Research complete", topic);
      },
    });
  });

  byId("planButton").addEventListener("click", () => {
    const goal = byId("planInput").value.trim();
    if (!goal) { showToast("Describe a goal to plan"); return; }
    run("buildOutput", "build", () => requestJson("/plan", { goal }), "planButton");
  });

  byId("improveButton").addEventListener("click", () => {
    const goal = byId("improveInput").value.trim() || "make NovaControl code itself and improve";
    state.lastBuildGoal = goal;
    run("buildOutput", "workflow", () => requestJson("/improve/workflow", { goal }), "improveButton");
  });

  byId("previewButton").addEventListener("click", () => {
    const goal = byId("improveInput").value.trim() || state.lastBuildGoal || "improve NovaControl safely";
    state.lastBuildGoal = goal;
    run("buildOutput", "workflow", () => requestJson("/improve/preview", { goal }), "previewButton");
  });

  byId("approveButton").addEventListener("click", () => {
    const goal = byId("improveInput").value.trim() || state.lastBuildGoal || "improve NovaControl safely";
    state.lastBuildGoal = goal;
    run("buildOutput", "workflow", () => requestJson("/improve/approve", { goal, preview_id: state.lastPreviewId }), "approveButton");
  });

  byId("learnButton").addEventListener("click", () => {
    const goal = byId("learnGoal").value.trim() || "improve NovaControl";
    const feedback = byId("learnFeedback").value.trim();
    run("learnOutput", "learn", () => requestJson("/learn", { goal, feedback }), "learnButton", {
      onSuccess: () => recordActivity("learn", "Learning cycle", goal),
    });
  });

  byId("trainButton").addEventListener("click", () => {
    const goal = byId("learnGoal").value.trim() || "improve NovaControl";
    const feedback = byId("learnFeedback").value.trim();
    run("learnOutput", "learn", () => requestJson("/train", { goal, feedback, iterations: 3 }), "trainButton", {
      onSuccess: () => recordActivity("learn", "Training run", goal),
    });
  });

  byId("systemHealthButton").addEventListener("click", () =>
    run("systemOutput", "health", () => requestJson("/system/health"), "systemHealthButton")
  );
  byId("hardenButton").addEventListener("click", () =>
    run("systemOutput", "health", () => requestJson("/system/harden"), "hardenButton")
  );
  byId("packageButton").addEventListener("click", () =>
    run("systemOutput", "generic", () => requestJson("/system/package"), "packageButton")
  );

  byId("saveSettingsButton").addEventListener("click", saveSettings);
  byId("clearCliButton").addEventListener("click", () => { byId("cliOutput").textContent = ""; });
}

/* ── Recent Activity (Command Center timeline) ────────────── */

const ACTIVITY_KEY = "novacontrol.activity";
const ACTIVITY_LIMIT = 25;
const ACTIVITY_PILL = { command: "ok", research: "", learn: "warn" };

function loadActivity() {
  try {
    const raw = JSON.parse(localStorage.getItem(ACTIVITY_KEY) || "[]");
    return Array.isArray(raw) ? raw : [];
  } catch {
    return [];
  }
}

// Record a completed user action into the home timeline. Kept in localStorage so
// history survives reloads; capped so the feed stays cheap.
function recordActivity(type, title, detail) {
  const entries = loadActivity();
  entries.unshift({ type, title, detail: detail || "", at: Date.now() });
  try {
    localStorage.setItem(ACTIVITY_KEY, JSON.stringify(entries.slice(0, ACTIVITY_LIMIT)));
  } catch (_) { /* storage unavailable — feed still lives for this session */ }
  renderActivityFeed();
}

function timeAgo(at) {
  const seconds = Math.max(0, Math.round((Date.now() - at) / 1000));
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return new Date(at).toLocaleDateString();
}

function renderActivityFeed() {
  const feed = byId("homeActivity");
  if (!feed) return;
  const entries = loadActivity();
  clearNode(feed);
  feed.classList.toggle("empty-state", entries.length === 0);
  if (!entries.length) {
    const hint = el("div", "empty-hint");
    hint.appendChild(el("span", "empty-hint-icon", "\u26A1"));
    hint.appendChild(document.createTextNode(" Executed commands, research, and learning cycles will appear here"));
    feed.appendChild(hint);
    return;
  }
  entries.slice(0, ACTIVITY_LIMIT).forEach((entry) => {
    const row = el("div", "activity-item");
    row.appendChild(el("span", `pill activity-type ${ACTIVITY_PILL[entry.type] || ""}`.trim(), label(entry.type || "activity")));
    const body = el("div", "activity-body");
    body.appendChild(el("strong", "", entry.title || label(entry.type)));
    if (entry.detail) body.appendChild(el("span", "", entry.detail));
    row.appendChild(body);
    row.appendChild(el("time", "activity-time", timeAgo(entry.at)));
    feed.appendChild(row);
  });
}

/* ── Chat History (survives reloads) ──────────────────────── */

const CHAT_KEY = "novacontrol.chatHistory";
const CHAT_LIMIT = 30;
const CHAT_TEXT_LIMIT = 1500;

function loadChatHistory() {
  try {
    const raw = JSON.parse(localStorage.getItem(CHAT_KEY) || "[]");
    return Array.isArray(raw) ? raw : [];
  } catch {
    return [];
  }
}

// Extract the assistant's plain-text answer from any response shape.
function chatTextForHistory(data) {
  return extractAnswerText(data).slice(0, CHAT_TEXT_LIMIT);
}

// Append one turn to the persisted conversation (newest last, capped).
function recordChatTurn(text, role, route) {
  const clean = String(text || "").trim();
  if (!clean) return;
  const entries = loadChatHistory();
  entries.push({ role, text: clean.slice(0, CHAT_TEXT_LIMIT), route: route || "", at: Date.now() });
  try {
    localStorage.setItem(CHAT_KEY, JSON.stringify(entries.slice(-CHAT_LIMIT)));
  } catch (_) { /* storage unavailable — conversation lives for this session */ }
}

// Re-render the saved conversation into the chat panel on load.
function renderChatHistory() {
  const stream = byId("chatStream");
  const entries = loadChatHistory();
  if (!stream || !entries.length) return;
  stream.classList.remove("empty-state");
  entries.forEach((entry) =>
    appendMessage(entry.role === "user" ? "user" : "assistant", entry.text, entry.route ? { route: entry.route } : undefined)
  );
}

/* ── Status & Settings ────────────────────────────────────── */

async function refreshStatus() {
  try {
    const status = await requestJson("/status");
    setStatus(`Ready | ${status.app.modules.length} modules`);
    updateMetrics(status);
    render("homeResult", status, "generic");
    renderActivityFeed();
    await loadSettings();
  } catch (error) {
    setStatus(String(error.message || error));
  }
}

async function loadSettings() {
  const settings = await requestJson("/settings");
  byId("approvalMode").value = settings.approval_mode || "ask";
  byId("detailedExplanations").checked = Boolean(settings.detailed_explanations);
  byId("includeVideos").checked = Boolean(settings.include_videos_in_explore);
}

async function saveSettings() {
  await run("settingsOutput", "settings", () =>
    requestJson("/settings", {
      approval_mode: byId("approvalMode").value,
      detailed_explanations: byId("detailedExplanations").checked,
      include_videos_in_explore: byId("includeVideos").checked,
    })
  , "saveSettingsButton");
}

/* ── Initialize ────────────────────────────────────────────── */

setupTabs();
setupActions();
setupVoice();
restoreActivePanel(); // must run after setupTabs binds the nav clicks
renderChatHistory();
connectActivitySource(); // one live activity channel for the whole page
refreshStatus();
