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
    if (state.autoApproveRun) {
      window.runAutoApproved = window.runAutoApproved || {};
      window.runAutoApproved[command] = () => byId("commandRunButton").click();
    }
    run("commandOutput", "command", () => requestJson("/command/plan", { command }), "commandPlanButton");
  });

  // ── J.A.R.V.I.S panel: one tab, Desktop / Phone device switch ─
  // Same plan → approve → execute lifecycle as the Command panel, pinned to the
  // dedicated device endpoints so a phone phrase can never land on the desktop
  // controller (and vice versa) regardless of intent classification. The active
  // device chooses the endpoints, chips, and visibility of phone-only controls;
  // it persists across reloads like the sidebar tab does.
  // Approve And Run mirrors the Command panel: reuse the token from the last
  // preview of THIS command, re-plan once when it expired. It stays disabled
  // while the phone bridge is unpaired (the plan then carries no token and
  // execute would just bounce back the pairing plan); any token-bearing plan,
  // executed result, or available bridge report re-enables it.
  let jarvisDevice = "desktop";
  try { jarvisDevice = localStorage.getItem("novacontrol.jarvisDevice") === "phone" ? "phone" : "desktop"; } catch (_) { /* storage unavailable */ }

  const jarvisPlanEndpoint = () => (jarvisDevice === "phone" ? "/phone/plan" : "/desktop/plan");
  const jarvisExecuteEndpoint = () => (jarvisDevice === "phone" ? "/phone/execute" : "/desktop/execute");

  const setJarvisRunAvailability = (data) => {
    const button = byId("jarvisRunButton");
    if (!button) return;
    const blocked = !!data && data.status === "waiting_for_phone_bridge";
    button.disabled = blocked;
    button.title = blocked ? "Pair a phone bridge first — see the next steps in the output" : "";
  };

  const jarvisPlan = () => {
    const command = byId("jarvisInput").value.trim();
    if (!command) { showToast("Type a command first"); return; }
    state.lastQuery = command;
    recordJarvisHistory(command, "planned", jarvisDevice);
    // Auto-approve & run: register the execute flow so renderCommand can fire it
    // the moment the planned (tokened) result renders — one click saved.
    if (state.autoApproveRun && jarvisDevice === "desktop") {
      window.runAutoApproved = window.runAutoApproved || {};
      window.runAutoApproved[command] = jarvisRun;
    }
    run("jarvisOutput", "command", () => requestJson(jarvisPlanEndpoint(), { command }), "jarvisPlanButton", {
      onSuccess: (data) => setJarvisRunAvailability(data),
    });
  };
  const jarvisRun = () => {
    const command = byId("jarvisInput").value.trim();
    if (!command) { showToast("Type a command first"); return; }
    state.lastQuery = command;
    const execute = (token) => requestJson(jarvisExecuteEndpoint(), { command, approval_token: token });
    run("jarvisOutput", "command", async () => {
      const stored = approvalFor(command);
      if (stored) {
        try { return await execute(stored); }
        catch (error) {
          if (!/token|expired|unknown/i.test(String(error.message || ""))) throw error;
          clearApproval(command); // consumed or expired — fall through and re-plan once
        }
      }
      const plan = await requestJson(jarvisPlanEndpoint(), { command });
      const fresh = plan?.approval?.token;
      if (!fresh) return plan; // not an executable action — show the planned result
      return execute(fresh);
    }, "jarvisRunButton", {
      onSuccess: (data) => {
        setJarvisRunAvailability(data); // executed results re-enable
        if (data && data.status === "executed") {
          clearApproval(command);
          recordJarvisHistory(command, "executed", jarvisDevice);
        }
      },
    });
  };

  function setJarvisDevice(device) {
    jarvisDevice = device === "phone" ? "phone" : "desktop";
    try { localStorage.setItem("novacontrol.jarvisDevice", jarvisDevice); } catch (_) { /* storage unavailable */ }
    const phone = jarvisDevice === "phone";
    byId("jarvisDeviceDesktop").classList.toggle("active", !phone);
    byId("jarvisDeviceDesktop").setAttribute("aria-selected", String(!phone));
    byId("jarvisDevicePhone").classList.toggle("active", phone);
    byId("jarvisDevicePhone").setAttribute("aria-selected", String(phone));
    document.querySelectorAll(".phone-only").forEach((node) => { node.hidden = !phone; });
    byId("jarvisInput").placeholder = phone
      ? "e.g. open whatsapp on my phone, or text mom saying running late"
      : "e.g. open notepad and type hello world";
    byId("jarvisHint").textContent = phone
      ? "To connect: install Android platform-tools, enable USB debugging on the phone (Settings → About → tap Build number 7× → Developer options → USB debugging), plug it in over USB, then press Connect Phone and accept the prompt on the phone."
      : "Drive this PC: open apps/folders, chain steps (\"open steam and go to library and launch gta v\"), dictate into apps (\"open notepad and type hello\"), or say \"stop that\". Commands are planned first — nothing runs until you approve it (or enable auto-run in Settings).";
    byId("jarvisOutput").className = "friendly-output empty-state";
    byId("jarvisOutput").innerHTML =
      '<div class="empty-hint"><span class="empty-hint-icon">&#9654;</span> Preview an action to see exactly what will run before you approve it</div>';
    setJarvisRunAvailability(null);
  }

  byId("jarvisDeviceDesktop").addEventListener("click", () => setJarvisDevice("desktop"));
  byId("jarvisDevicePhone").addEventListener("click", () => setJarvisDevice("phone"));
  byId("jarvisPlanButton").addEventListener("click", jarvisPlan);
  byId("jarvisRunButton").addEventListener("click", jarvisRun);
  byId("jarvisInput").addEventListener("keydown", (event) => {
    if (event.key === "Enter") jarvisPlan();
  });

  // Connect Phone: runs the bridge pairing flow (starts ADB and probes for
  // devices so the phone shows its authorization prompt). Its result has the
  // bridge-status shape, so it renders through the same dedicated renderer.
  byId("jarvisPhoneConnectButton").addEventListener("click", () =>
    run("jarvisOutput", "phoneStatus", () => requestJson("/phone/connect", {}), "jarvisPhoneConnectButton", {
      successToast: "Pairing flow run — accept the prompt on your phone if it appeared",
    }));
  // Bridge Status: GET /phone/status has a neither-plan-nor-execution shape, so
  // it renders through the dedicated phone-status renderer, not renderCommand.
  byId("jarvisPhoneStatusButton").addEventListener("click", () =>
    run("jarvisOutput", "phoneStatus", () => requestJson("/phone/status"), "jarvisPhoneStatusButton", {
      onSuccess: (data) => setJarvisRunAvailability(data),
    }));

  // Quick chips: fill the panel input and plan — same contract as typing it.
  document.querySelectorAll("[data-jarvis-desktop]").forEach((chip) => {
    chip.addEventListener("click", () => {
      byId("jarvisInput").value = chip.dataset.jarvisDesktop;
      jarvisPlan();
    });
  });
  document.querySelectorAll("[data-jarvis-phone]").forEach((chip) => {
    chip.addEventListener("click", () => {
      byId("jarvisInput").value = chip.dataset.jarvisPhone;
      jarvisPlan();
    });
  });

  byId("jarvisHistoryClearButton").addEventListener("click", clearJarvisHistory);
  byId("jarvisHistoryList").addEventListener("click", (event) => {
    const target = event.target.closest("[data-delete-jarvis]");
    if (target) deleteJarvisHistoryEntry(target.dataset.deleteJarvis);
  });

  setJarvisDevice(jarvisDevice); // restore the persisted device on load
  renderJarvisHistory();
  setupJarvisVoice(jarvisPlan); // shares this scope's plan flow (input + endpoints)

  byId("commandRunButton").addEventListener("click", () => {
    const command = byId("commandInput").value.trim();
    if (!command) { showToast("Type a command first"); return; }
    // Approve And Run: reuse the token from the last preview of THIS command and
    // only re-plan (which mints a fresh token) when none is stored. A consumed or
    // expired token gets one automatic re-plan so a stale approval can't dead-end.
    const correlationId = newCorrelationId();
    const execute = (token) => requestJson("/command/execute", { command, approval_token: token, correlation_id: correlationId });
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
      correlationId,
      onSuccess: (data) => {
        if (data && data.status === "executed") {
          clearApproval(command);
        }
      },
    });
  });

  byId("askButton").addEventListener("click", () => submitChat());
  byId("speakButton").addEventListener("click", speakLast);

  // Brain mode switch (Chat panel): hot-swaps local scratch ↔ local LLM on the
  // server; no restart. The switch reflects the persisted server mode.
  byId("brainModeSwitch").addEventListener("change", async () => {
    const mode = byId("brainModeSwitch").value;
    try {
      const result = await requestJson("/brain/mode", { mode });
      showToast(brainModeToast(result));
      await refreshStatus();
      // Selecting Cloud before configuring one shows the way to Settings.
      if (mode === "cloud" && !result.cloud?.configured) {
        document.querySelector('[data-panel="settingsPanel"]').click();
      }
    } catch (error) {
      showToast(String(error.message || error));
      await syncBrainSwitch();
    }
  });

  // Clear Chat: wipe the visible thread + saved history AND the server-side
  // conversation memory, so the LLM loses the old context too.
  byId("clearChatButton").addEventListener("click", async () => {
    try {
      localStorage.removeItem(CHAT_KEY);
    } catch (_) { /* storage unavailable */ }
    const stream = byId("chatStream");
    clearNode(stream);
    stream.classList.add("empty-state");
    stream.appendChild(el("div", "empty-hint", "Chat cleared. Ask something to start a new conversation."));
    try { await requestJson("/chat/clear", {}); } catch (_) { /* local-only fallback */ }
    showToast("Chat history cleared");
  });

  byId("exploreButton").addEventListener("click", () => {
    const topic = byId("exploreInput").value.trim();
    if (!topic) { showToast("Enter a topic to research"); return; }
    state.lastQuery = topic;
    byId("exploreOutput").scrollIntoView({ behavior: "smooth", block: "start" });
    // Same run() lifecycle as every other action: shared loading/error/toast/button
    // state. Live research steps arrive over the single /events/stream activity
    // channel (run() shows them under the scan-line) while this POST completes
    // with the finished report.
    const correlationId = newCorrelationId();
    run("exploreOutput", "explore", async () => {
      const body = { topic, include_videos: true, correlation_id: correlationId };
      if (state.lastExploredTopic) body.last_topic = state.lastExploredTopic;
      return requestJson("/explore", body);
    }, "exploreButton", {
      correlationId,
      loadingText: "Researching — live progress appears below...",
      successToast: "Research complete",
      onSuccess: (report) => {
        trackExploredTopic(topic);
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
    // Reuse the preview generated for THIS goal (rememberPreview bound it); an
    // empty id means no reusable preview, so the server derives a fresh one.
    run("buildOutput", "workflow", () =>
      requestJson("/improve/approve", { goal, preview_id: previewFor(goal) }), "approveButton", {
      onSuccess: (data) => {
        if (data && data.status === "approved_and_applied") clearPreview(goal);
      },
    });
  });

  byId("learnButton").addEventListener("click", () => {
    const goal = byId("learnGoal").value.trim() || "improve NovaControl";
    const feedback = byId("learnFeedback").value.trim();
    run("learnOutput", "learn", () => requestJson("/learn", { goal, feedback }), "learnButton");
  });

  byId("trainButton").addEventListener("click", () => {
    const goal = byId("learnGoal").value.trim() || "improve NovaControl";
    const feedback = byId("learnFeedback").value.trim();
    run("learnOutput", "learn", () => requestJson("/train", { goal, feedback, iterations: 3 }), "trainButton");
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

  // Cloud LLM card: populate the provider picker, wire connect/remove.
  setupCloudLlmCard();
  byId("cloudConnectButton").addEventListener("click", () => connectCloudLlm());
  byId("cloudClearButton").addEventListener("click", () => removeCloudLlm());

  byId("clearTasksButton").addEventListener("click", () => clearAllTasks());
}

/* ── Recent Activity (Command Center timeline) ────────────── */

// The timeline lives in js/activity.js and is SERVER-fed: /activity seeds it,
// completion events on /events/stream append live. The old localStorage copy
// (ACTIVITY_KEY + recordActivity) is gone — the server journal is the source,
// so actions from every client (CLI, GUI, any tab) appear and nothing is
// duplicated per-browser.

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

/* ── J.A.R.V.I.S command history ─────────────────────────── */

const JARVIS_HISTORY_KEY = "novacontrol.jarvisHistory";
const JARVIS_HISTORY_LIMIT = 30;

function loadJarvisHistory() {
  try {
    const parsed = JSON.parse(localStorage.getItem(JARVIS_HISTORY_KEY) || "[]");
    return Array.isArray(parsed) ? parsed : [];
  } catch (_) {
    return [];
  }
}

// Record one planned/executed JARVIS command. A repeat of the immediately
// preceding command updates that entry's status instead of duplicating it
// (plan → approve of the same command is one history row, promoted to executed).
function recordJarvisHistory(command, status, device) {
  const clean = String(command || "").trim();
  if (!clean) return;
  const entries = loadJarvisHistory();
  const last = entries[entries.length - 1];
  if (last && last.command === clean && last.device === device) {
    last.status = status;
    last.at = Date.now();
  } else {
    entries.push({ id: `jh${Date.now()}${Math.random().toString(36).slice(2, 7)}`, command: clean.slice(0, 200), status, device: device || "desktop", at: Date.now() });
  }
  try {
    localStorage.setItem(JARVIS_HISTORY_KEY, JSON.stringify(entries.slice(-JARVIS_HISTORY_LIMIT)));
  } catch (_) { /* storage unavailable — history lives for this session */ }
  renderJarvisHistory();
}

function deleteJarvisHistoryEntry(id) {
  const entries = loadJarvisHistory().filter((entry) => entry.id !== id);
  try { localStorage.setItem(JARVIS_HISTORY_KEY, JSON.stringify(entries)); } catch (_) { /* ignore */ }
  renderJarvisHistory();
}

function clearJarvisHistory() {
  try { localStorage.removeItem(JARVIS_HISTORY_KEY); } catch (_) { /* ignore */ }
  renderJarvisHistory();
  showToast("JARVIS history cleared");
}

function renderJarvisHistory() {
  const list = byId("jarvisHistoryList");
  if (!list) return;
  const entries = loadJarvisHistory();
  byId("jarvisHistory").hidden = entries.length === 0;
  clearNode(list);
  [...entries].reverse().forEach((entry) => {
    const row = el("div", "jarvis-history-row");
    row.title = "Click to reuse this command";
    const badge = el("span", `jarvis-history-device ${entry.device === "phone" ? "is-phone" : "is-desktop"}`, entry.device === "phone" ? "Phone" : "Desktop");
    const text = el("span", "jarvis-history-command", entry.command);
    const status = el("span", `jarvis-history-status is-${entry.status}`, entry.status);
    const remove = el("button", "jarvis-history-delete", "Delete");
    remove.type = "button";
    remove.setAttribute("aria-label", `Delete history entry: ${entry.command}`);
    remove.dataset.deleteJarvis = entry.id;
    row.append(badge, text, status, remove);
    row.addEventListener("click", (event) => {
      if (event.target === remove) return;
      byId("jarvisInput").value = entry.command;
      byId("jarvisInput").focus();
    });
    list.appendChild(row);
  });
}

// JARVIS voice mode: speak a command, it fills the input and immediately runs
// the same plan → preview the typed flow uses. Independent from the chat voice
// so the two mics never fight over the same recognizer instance.
function setupJarvisVoice(jarvisPlan) {
  const button = byId("jarvisVoiceButton");
  if (!button) return;
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!Recognition) {
    button.disabled = true;
    button.title = "Voice is not available in this browser";
    return;
  }
  const recognition = new Recognition();
  recognition.continuous = false;
  recognition.interimResults = false;
  recognition.lang = "en-US";
  let listening = false;
  const resetButton = () => {
    listening = false;
    button.classList.remove("listening");
    button.textContent = "Voice";
  };
  recognition.onstart = () => {
    listening = true;
    button.classList.add("listening");
    button.textContent = "Listening...";
  };
  recognition.onend = resetButton;
  recognition.onerror = (event) => {
    resetButton();
    const code = event.error || "unknown";
    if (code === "aborted" || code === "no-speech") return;
    if (code === "not-allowed" || code === "service-not-allowed") {
      showToast("Microphone blocked — allow mic access for this site, then retry");
    } else {
      showToast(`Voice input failed (${code}). Try again.`);
    }
  };
  recognition.onresult = (event) => {
    let transcript = "";
    for (let index = event.resultIndex; index < event.results.length; index += 1) {
      if (event.results[index].isFinal) transcript += event.results[index][0].transcript;
    }
    transcript = transcript.trim();
    if (!transcript) return;
    byId("jarvisInput").value = transcript;
    jarvisPlan(); // spoken commands follow the exact same plan → approve path
  };
  button.addEventListener("click", () => {
    if (listening) { recognition.abort(); return; }
    try { recognition.start(); } catch (_) { /* already started */ }
  });
}

/* ── Status & Settings ────────────────────────────────────── */

async function refreshStatus() {
  try {
    const status = await requestJson("/status");
    setStatus(`Ready | ${status.app.modules.length} modules`);
    updateMetrics(status);
    renderBrainStatus(status); // always-visible LLM provider/model card on the System panel
    render("homeResult", status, "generic");
    renderActivityFeed();
    renderTaskList(status.app.tasks || []);
    await syncBrainSwitch();
    await loadSettings();
  } catch (error) {
    setStatus(String(error.message || error));
  }
}

/* ── Brain mode switch (Chat panel) ───────────────────────── */

// Keep the select in sync with the persisted server mode (survives reloads).
async function syncBrainSwitch() {
  try {
    const brain = await requestJson("/brain/mode");
    const select = byId("brainModeSwitch");
    if (select && brain.mode) select.value = brain.mode;
  } catch (_) { /* switch just keeps its last value */ }
}

// Short, human confirmation for the mode change based on the server's answer.
function brainModeToast(result) {
  const mode = result?.mode || "";
  if (mode === "scratch") return "Scratch brain active — answering locally";
  if (mode === "cloud") {
    return result.cloud?.configured
      ? `Cloud LLM active — ${result.cloud.label || result.cloud.provider}`
      : "Cloud LLM selected — configure a provider in Settings first";
  }
  if (mode === "llm") {
    return result.model
      ? `Local LLM active — ${result.model}`
      : "LLM mode saved — start Ollama to activate a model";
  }
  return "Auto mode — LLM when available, scratch otherwise";
}

/* ── Cloud LLM card (Settings panel) ──────────────────────── */

// Populate the provider picker from the server's preset registry (no secrets
// travel here), then reflect the configured cloud LLM in the status line.
let cloudPresets = [];

async function setupCloudLlmCard() {
  try {
    const { presets } = await requestJson("/brain/cloud/presets");
    cloudPresets = presets || [];
    const picker = byId("cloudProvider");
    clearNode(picker);
    for (const preset of cloudPresets) {
      const option = el("option", "", preset.label);
      option.value = preset.id;
      picker.appendChild(option);
    }
    // Switching providers updates the model placeholder to that provider's default.
    picker.addEventListener("change", () => {
      const preset = cloudPresets.find((row) => row.id === picker.value);
      if (preset) byId("cloudModel").placeholder = preset.default_model;
    });
    await refreshCloudStatus();
  } catch (_) {
    byId("cloudStatus").textContent = "Provider list unavailable";
  }
}

async function refreshCloudStatus() {
  try {
    const brain = await requestJson("/brain/mode");
    const cloud = brain.cloud || {};
    const status = byId("cloudStatus");
    if (cloud.configured) {
      status.textContent = `Active: ${cloud.label || cloud.provider}`
        + (cloud.api_key_hint ? ` — key ${cloud.api_key_hint}` : "");
    } else {
      status.textContent = "No cloud LLM configured";
    }
  } catch (_) { /* keep the last status line */ }
}

async function connectCloudLlm() {
  const provider = byId("cloudProvider").value;
  const apiKey = byId("cloudApiKey").value.trim();
  const model = byId("cloudModel").value.trim();
  if (!provider) { showToast("Pick a cloud provider first"); return; }
  if (!apiKey) { showToast("Paste the provider's API key first"); return; }
  const button = byId("cloudConnectButton");
  button.disabled = true;
  try {
    const result = await requestJson("/brain/cloud", { provider, api_key: apiKey, model });
    byId("cloudApiKey").value = ""; // never linger in the DOM after install
    showToast(brainModeToast(result));
    await refreshCloudStatus();
    await refreshStatus(); // brain switch + AI Brain card follow the cloud mode
  } catch (error) {
    showToast(String(error.message || error));
  } finally {
    button.disabled = false;
  }
}

async function removeCloudLlm() {
  const button = byId("cloudClearButton");
  button.disabled = true;
  try {
    await requestJson("/brain/cloud/clear", {});
    byId("cloudApiKey").value = "";
    showToast("Cloud LLM removed — local brain active");
    await refreshCloudStatus();
    await refreshStatus();
  } catch (error) {
    showToast(String(error.message || error));
  } finally {
    button.disabled = false;
  }
}

async function loadSettings() {
  const settings = await requestJson("/settings");
  byId("approvalMode").value = settings.approval_mode || "ask";
  byId("detailedExplanations").checked = Boolean(settings.detailed_explanations);
  byId("includeVideos").checked = Boolean(settings.include_videos_in_explore);
  byId("autoApproveRun").checked = Boolean(settings.auto_approve_run);
  state.autoApproveRun = Boolean(settings.auto_approve_run);
}

async function saveSettings() {
  await run("settingsOutput", "settings", () =>
    requestJson("/settings", {
      approval_mode: byId("approvalMode").value,
      detailed_explanations: byId("detailedExplanations").checked,
      include_videos_in_explore: byId("includeVideos").checked,
      auto_approve_run: byId("autoApproveRun").checked,
    })
  , "saveSettingsButton");
}

/* ── Vision panel ────────────────────────────────────────── */

function setupVision() {
  const describe = () =>
    run("visionDescribeOutput", "generic", () => requestJson("/vision/describe", {}), "visionDescribeButton");
  byId("visionDescribeButton").addEventListener("click", describe);

  byId("visionClickButton").addEventListener("click", () => {
    const label = byId("visionClickInput").value.trim();
    if (!label) { showToast("Type what to click first"); return; }
    run("visionDescribeOutput", "visionClick", () => requestJson("/vision/click", { label }), "visionClickButton", {
      successToast: "Vision click executed",
    });
  });

  // ── Vision model card (semantic element location) ───────────────
  // Same key-storage shape as the chat cloud LLM: provider+key persist in the
  // gitignored data dir, /vision/status carries only a redacted tail.
  const renderVisionModelCard = (status) => {
    const configured = status && status.configured;
    byId("visionModelStatus").textContent = configured
      ? `configured: ${status.label} · ${status.model}${status.api_key_hint ? ` (key ${status.api_key_hint})` : ""}`
      : "not configured — OCR + landmarks";
    // Ollama needs no API key; hide the key field for it.
    byId("visionModelKeyField").hidden = byId("visionModelProvider").value === "ollama";
  };

  const refreshVisionModelCard = async () => {
    try { renderVisionModelCard((await requestJson("/vision/status")).vision_llm); }
    catch (_) { byId("visionModelStatus").textContent = "status unavailable"; }
  };

  byId("visionModelProvider").addEventListener("change", () => {
    byId("visionModelKeyField").hidden = byId("visionModelProvider").value === "ollama";
  });

  byId("visionModelConnectButton").addEventListener("click", async () => {
    const provider = byId("visionModelProvider").value;
    const body = { provider, model: byId("visionModelName").value.trim() };
    if (provider !== "ollama") body.api_key = byId("visionModelKey").value.trim();
    if (provider !== "ollama" && !body.api_key) { showToast("Paste the provider's API key first"); return; }
    try {
      const status = await requestJson("/vision/model", body);
      renderVisionModelCard(status);
      showToast(`Vision model connected: ${status.model}`);
    } catch (error) {
      showToast(String(error.message || error));
    }
  });

  byId("visionModelClearButton").addEventListener("click", async () => {
    try {
      const status = await requestJson("/vision/model/clear", {});
      renderVisionModelCard(status);
      byId("visionModelKey").value = "";
      showToast("Vision model removed — element location is OCR-only again");
    } catch (error) {
      showToast(String(error.message || error));
    }
  });

  refreshVisionModelCard();

  byId("visionStatusButton").addEventListener("click", async () => {
    try {
      const status = await requestJson("/vision/status");
      showToast(status.vision_model
        ? "Vision model wired — screen understanding is live"
        : "No vision model — using window probes and OCR landmarks (configure one in the Vision Model card)");
    } catch (error) {
      showToast(String(error.message || error));
    }
  });

  const renderBugs = (container, data) => {
    container.classList.remove("empty-state");
    container.innerHTML = "";
    const bugs = (data && data.bugs) || [];
    byId("bugOpenCount").textContent = bugs.length ? `(${data.open_count} open)` : "";
    if (!bugs.length) {
      container.innerHTML = '<div class="empty-hint"><span class="empty-hint-icon">&#10003;</span> No bugs recorded</div>';
      return;
    }
    bugs.forEach((bug) => {
      const row = el("div", "jarvis-history-row" + (bug.status === "fixed" ? " fixed" : ""));
      const main = el("div", "jarvis-history-main");
      const proof = bug.status === "fixed" && bug.details && bug.details.auto_resolved
        ? ` · auto-resolved ${String(bug.details.auto_resolved.resolved_at || "")}` +
          (bug.details.auto_resolved.changed_pct != null ? ` (screen changed ${bug.details.auto_resolved.changed_pct}%)` : "")
        : "";
      main.appendChild(el("span", "jarvis-history-text", bug.what));
      main.appendChild(el("span", "jarvis-history-meta", `${bug.where} · ${bug.when}${bug.status === "fixed" ? " · fixed" : ""}${proof}`));
      row.appendChild(main);
      if (bug.status !== "fixed") {
        const fixBtn = el("button", "jarvis-history-delete", "Mark fixed");
        fixBtn.type = "button";
        fixBtn.setAttribute("aria-label", `Mark bug fixed: ${bug.what}`);
        fixBtn.addEventListener("click", async () => {
          try {
            await requestJson(`/bugs/${bug.id}/fix`, {});
            showToast("Bug marked fixed");
            refreshBugs();
          } catch (error) { showToast(String(error.message || error)); }
        });
        row.appendChild(fixBtn);
      }
      container.appendChild(row);
    });
  };

  const refreshBugs = () =>
    run("bugList", "generic", () => requestJson("/bugs"), "bugRefreshButton", {
      loadingText: "Loading bugs…",
    });
  // render() would use renderGeneric; render the bug rows ourselves.
  byId("bugRefreshButton").addEventListener("click", async () => {
    try {
      renderBugs(byId("bugList"), await requestJson("/bugs"));
    } catch (error) {
      showToast(String(error.message || error));
    }
  });
  byId("bugClearFixedButton").addEventListener("click", async () => {
    try {
      const result = await requestJson("/bugs/clear-fixed", {});
      showToast(`Removed ${result.removed} fixed bug(s)`);
      refreshBugs();
    } catch (error) {
      showToast(String(error.message || error));
    }
  });
  refreshBugs();
}

/* ── Initialize ────────────────────────────────────────────── */

setupTabs();
setupActions();
setupVision();
setupVoice();
restoreActivePanel(); // must run after setupTabs binds the nav clicks
renderChatHistory();
connectActivitySource(); // one live activity channel for the whole page
loadActivityFromServer(); // seed the timeline from the server journal
refreshStatus();
