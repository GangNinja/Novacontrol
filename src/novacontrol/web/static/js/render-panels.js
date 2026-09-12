/* ── Chat Rendering ────────────────────────────────────────── */

function appendMessage(role, text, payload) {
  const stream = byId("chatStream");
  const message = el("article", `message ${role}`);
  message.appendChild(el("strong", "", role === "user" ? "You" : "NovaControl"));
  const body = el("div", "message-body");
  body.appendChild(renderMd(text)); // shared markdown renderer (dom.js)
  message.appendChild(body);
  if (payload) {
    const details = el("div", "pill-row");
    details.appendChild(el("span", "pill", payload.route || payload.decision?.intent || "response"));
    message.appendChild(details);
  }
  stream.appendChild(message);
  stream.scrollTop = stream.scrollHeight;
}

function renderChatResult(data) {
  const stream = byId("chatStream");
  clearNode(stream);
  stream.classList.remove("empty-state");
  // A running conversation: keep the user's question visible above the answer
  // instead of replacing the whole thread with just the latest answer page.
  if (state.lastQuery) appendMessage("user", state.lastQuery);

  // /ask answers travel in one flat envelope {route, intent, summary, data}:
  // `data` is the handler payload. Chat is a CONVERSATION, not an Explore page:
  // research answers render as a message bubble with the sourced answer text
  // and their source chips — the full report page (sections, sources rail,
  // follow-up chips) stays in the Explore panel where it belongs.
  const payload = data.data || data;

  if ((data.route === "desktop_automation" || payload.workflow?.actions) && payload.approval) {
    renderCommand(stream, payload);
    return;
  }

  if (looksLikeResearch(payload)) {
    renderResearchBubble(stream, payload);
    return;
  }

  const summary = cleanSummary(data); // shared extractor owns field precedence
  renderGeneralAiPage(stream, {
    query: state.lastQuery || "Ask NovaControl",
    summary,
    route: data.route || data.intent || "answer",
    payload,
  });
}

// Research answers INSIDE the conversation: the answer text as a markdown
// bubble (the shared extractor already prefers `answer` over `overview`) —
// the template builders structure it as an intro line, "**What it is:**" /
// "**Key points:**" headings and bullets, and renderMd renders those live.
// Under it: the source chips so every claim stays clickable, plus an
// "explore" hand-off chip that jumps to the Explore panel with the same
// topic pre-filled — the full report page (sections, sources rail, "Ask
// Next" chips) lives THERE, never duplicated inside the chat.
function renderResearchBubble(stream, report) {
  const answer = extractAnswerText(report, "");
  appendMessage(
    "assistant",
    answer || "I researched the topic but the sources returned nothing usable — try Explore for the full report.",
    { route: "explore" },
  );
  const bubble = stream.querySelector(".message.assistant:last-of-type");
  if (!bubble) return;
  if (report.source_chips?.length) bubble.appendChild(sourceChips(report.source_chips));
  const handoff = el("div", "pill-row");
  const chip = el("button", "explore-handoff", "explore");
  chip.type = "button";
  chip.title = "Open the full research report in Explore";
  chip.addEventListener("click", () => {
    // Hand off the ALREADY-FETCHED report: render it into the Explore output
    // directly (no second research round-trip), then switch to the panel.
    const output = byId("exploreOutput");
    if (output) {
      clearNode(output);
      output.classList.remove("empty-state");
      renderExplore(output, report);
      output.scrollIntoView({ behavior: "smooth", block: "start" });
    }
    const nav = document.querySelector('.nav-item[data-panel="explorePanel"]');
    if (nav) nav.click();
  });
  handoff.appendChild(chip);
  bubble.appendChild(handoff);
}

function renderGeneralAiPage(target, { query, summary, route, payload }) {
  const layout = el("article", "ai-answer-layout");
  const main = el("div", "answer-main");
  main.appendChild(el("div", "query-chip", query));

  // Markdown-aware lead so scratch-brain answers render **bold** cleanly.
  const lead = el("div", "answer-lead");
  lead.appendChild(renderMd(summary));
  main.appendChild(lead);

  const section = el("section", "answer-section");
  section.appendChild(el("h3", "", label(route)));

  // The answer text already rendered in the lead above via the shared extractor
  // (summary) — never re-read payload.message/content here; only structure.
  if (payload.suggestions?.length) {
    const chips = el("div", "suggestion-row");
    payload.suggestions.forEach((suggestion) => {
      const button = el("button", "suggestion-chip", suggestion);
      button.type = "button";
      button.addEventListener("click", () => {
        byId("chatInput").value = suggestion;
        submitChat();
      });
      chips.appendChild(button);
    });
    section.appendChild(chips);
  } else if (payload.plan?.steps) {
    const list = el("ul", "");
    payload.plan.steps.forEach((step) => list.appendChild(richBullet(step.description || step.name || step.status)));
    section.appendChild(list);
  } else if (payload.sections?.length) {
    payload.sections.forEach((ps) => {
      const block = el("div", "reasoning-block");
      block.appendChild(el("h4", "", ps.title || "Reasoning"));
      const list = el("ul", "");
      (ps.items || []).forEach((item) => list.appendChild(richBullet(item)));
      block.appendChild(list);
      section.appendChild(block);
    });
  } else if (payload.actions) {
    const list = el("ul", "");
    payload.actions.forEach((action) => list.appendChild(richBullet(action.description || action.title)));
    section.appendChild(list);
  } else {
    section.appendChild(el("p", "", "I prepared the result in the local runtime."));
  }
  main.appendChild(section);

  if (payload.next_actions?.length) {
    const next = el("section", "answer-section");
    next.appendChild(el("h3", "", "Next Actions"));
    const list = el("ul", "");
    payload.next_actions.forEach((action) => list.appendChild(richBullet(action)));
    next.appendChild(list);
    main.appendChild(next);
  }

  const rail = el("aside", "source-rail");
  rail.appendChild(el("h3", "", "Context"));
  const card = el("div", "rail-card");
  card.appendChild(el("div", "rail-meta", "NovaControl"));
  card.appendChild(el("p", "", `Brain mode: ${payload.brain_mode || payload.provider || "local runtime"}.`));
  rail.appendChild(card);

  layout.appendChild(main);
  layout.appendChild(rail);
  target.appendChild(layout);
}

/* ── Approval Banner (shared by command + workflow) ────────── */

function approvalBanner(approval, doneTitle, doneText) {
  const banner = el("div", `approval-banner ${approval.approved ? "approved" : "pending"}`);
  banner.appendChild(el("strong", "", approval.approved ? doneTitle : "Approval needed"));
  // Once executed, drop the pre-approval caution text — it reads as if nothing ran yet.
  banner.appendChild(el("span", "", approval.approved ? doneText : (approval.message || "")));
  return banner;
}

/* ── Command Rendering ──────────────────────────────────────── */

function renderCommand(target, data) {
  // Any freshly previewed action becomes the stored approval for the sidebar's
  // "Approve And Run"; executed results (approval.approved) are skipped.
  rememberApproval(data);
  target.appendChild(el("p", "summary", data.summary || "Command prepared."));

  // Settings → Auto-approve & run: a freshly planned, tokened command executes
  // immediately through the exact same approve flow (one click saved). Only
  // fires once per render of a waiting_for_approval plan with a token.
  if (
    state.autoApproveRun &&
    data &&
    data.status === "waiting_for_approval" &&
    data.approval && data.approval.token && !data.approval.approved &&
    typeof window.runAutoApproved === "function"
  ) {
    const command = data.command;
    if (command && typeof window.runAutoApproved[command] === "function") {
      const executeIt = window.runAutoApproved[command];
      delete window.runAutoApproved[command]; // single-shot
      // Defer so this render completes before the execution re-renders.
      setTimeout(executeIt, 0);
    }
  }

  // Phone bridge not paired: the plan is prepared but nothing can run until a
  // device reports available, so execution is replaced by pairing guidance.
  // Distinct from the approval banner: approving is not the missing step.
  const bridgeBlocked = data.status === "waiting_for_phone_bridge";

  // Show multi-step plan
  if (data.steps && data.steps.length > 0) {
    const stepsCard = el("article", "info-card");
    stepsCard.appendChild(el("h4", "", "Action Plan"));
    const ol = el("ol", "action-steps");
    data.steps.forEach((step, i) => {
      const li = el("li", "", `${step}`);
      li.style.padding = "4px 0";
      li.style.color = "var(--text-secondary)";
      ol.appendChild(li);
    });
    stepsCard.appendChild(ol);
    target.appendChild(stepsCard);
  }

  if (bridgeBlocked) {
    target.appendChild(bridgeBlockedCard(data.bridge));
  } else if (data.approval) {
    target.appendChild(approvalBanner(data.approval, "Approved & Executed", "All planned actions completed."));
  }

  // No inline Approve And Run while the bridge is blocked: the plan carries no
  // execution token then anyway, and the button would just re-plan forever.
  if (!bridgeBlocked && data.approval && !data.approval.approved && data.approval.token && data.command) {
    const row = el("div", "button-row action-row");
    const approve = el("button", "cyber-btn primary", "Approve And Run");
    approve.type = "button";
    approve.addEventListener("click", () => {
      // /command/execute re-dispatches on the intent, so this single button approves
      // desktop plans AND browser plans (which must reach execute_browser_command).
      // The correlation id keeps THIS execution's progress rows out of any
      // concurrent same-family run's panel (and vice versa).
      const correlationId = newCorrelationId();
      run(target.id, "command", () =>
        requestJson("/command/execute", { command: data.command, approval_token: data.approval.token, correlation_id: correlationId })
      , null, { correlationId, onSuccess: () => clearApproval(data.command) });
    });
    row.appendChild(approve);
    target.appendChild(row);
  }

  // Show execution results
  if (data.execution_results && data.execution_results.length > 0) {
    const resultsCard = el("article", "info-card");
    resultsCard.appendChild(el("h4", "", "Execution Results"));
    data.execution_results.forEach((r) => {
      const resultEl = el("div", "result-item");
      const statusClass = r.status === "completed" ? "ok" : r.status === "denied" ? "warn" : "err";
      resultEl.appendChild(el("span", `pill ${statusClass}`, label(r.status)));
      resultEl.appendChild(el("p", "", r.error || "Action completed successfully."));
      if (r.output && Object.keys(r.output).length > 0) {
        const details = el("pre", "result-details");
        details.textContent = JSON.stringify(r.output, null, 2);
        resultEl.appendChild(details);
      }
      resultsCard.appendChild(resultEl);
    });
    target.appendChild(resultsCard);
  }

  const grid = el("div", "command-grid");
  const action = el("article", "info-card");
  action.appendChild(el("h4", "", data.target || "Desktop action"));
  action.appendChild(el("p", "", data.command || "No command text"));
  action.appendChild(el("span", "pill", label(data.status || "planned")));
  grid.appendChild(action);

  if (data.bridge && !bridgeBlocked) {
    const bridge = el("article", "info-card");
    bridge.appendChild(el("h4", "", "Phone Bridge"));
    bridge.appendChild(el("p", "", `${label(data.bridge.state || "unknown")} via ${data.bridge.adapter || "adapter"}`));
    bridge.appendChild(el("span", `pill ${data.bridge.available ? "ok" : "warn"}`, data.bridge.available ? "Device ready" : "Pair needed"));
    (data.bridge.next_steps || []).slice(0, 2).forEach((step) => bridge.appendChild(el("p", "", step)));
    grid.appendChild(bridge);
  }

  (data.workflow?.actions || []).forEach((item) => {
    const card = el("article", "info-card");
    card.appendChild(el("h4", "", item.description || item.type));
    card.appendChild(el("p", "", item.target || ""));
    card.appendChild(el("span", "pill", label(item.type || "action")));
    grid.appendChild(card);
  });

  (data.results || []).forEach((result) => {
    const card = el("article", "info-card");
    card.appendChild(el("h4", "", label(result.status || "result")));
    card.appendChild(el("p", "", result.error || result.output?.target || "Action sent."));
    card.appendChild(el("span", `pill ${result.status === "completed" ? "ok" : "warn"}`, label(result.status || "result")));
    grid.appendChild(card);
  });
  target.appendChild(grid);
}

/* ── Phone Bridge Status (JARVIS Phone panel) ───────────────── */

// The waiting_for_phone_bridge banner: the plan IS prepared, but nothing can
// run until a device reports available — so approval is not the missing step
// and the Approve And Run button is suppressed. Shows ALL pairing next-steps
// (not the truncated two of the ready-bridge card).
function bridgeBlockedCard(bridge) {
  const card = el("article", "info-card bridge-blocked");
  card.appendChild(el("h4", "", "Phone bridge needed"));
  card.appendChild(el("span", "pill warn", label((bridge && bridge.state) || "not_configured")));
  card.appendChild(el("p", "", "The action is prepared, but no phone is paired yet — Approve And Run unlocks once a device reports available."));
  const steps = el("ul", "bridge-steps");
  ((bridge && bridge.next_steps) || []).forEach((step) => steps.appendChild(richBullet(step)));
  card.appendChild(steps);
  return card;
}

// GET /phone/status returns a PhoneBridgeState, not a plan/execution shape.
// Mirrors the bridge card renderCommand already draws so pairing guidance
// (next_steps) is shown exactly once, with the same labels.
function renderPhoneStatus(target, data) {
  const card = el("article", "info-card");
  card.appendChild(el("h4", "", "Phone Bridge"));
  card.appendChild(el("p", "", `${label(data.state || "unknown")} via ${data.adapter || "adapter"}`));
  card.appendChild(el("span", `pill ${data.available ? "ok" : "warn"}`, data.available ? "Device ready" : "Pair needed"));
  (data.next_steps || []).forEach((step) => card.appendChild(el("p", "", step)));
  target.appendChild(card);
}

/* ── Build / Improvement Rendering ──────────────────────────── */

function renderBuild(target, data) {
  // /plan returns a flat {plan} / {actions} body — no envelope, no payload key.
  if (data.plan?.steps) {
    // Plan payloads identify themselves with `goal` (Plan.to_dict); the old
    // `name` read never matched, so the summary was always the fallback.
    target.appendChild(el("p", "summary", data.plan.name || data.plan.goal || "Plan created."));
    renderActionCards(target, data.plan.steps, "description");
  } else if (data.actions) {
    target.appendChild(el("p", "summary", `Found ${data.actions.length} improvement actions.`));
    renderActionCards(target, data.actions, "description");
    renderFindings(target, data.findings || []);
  } else {
    renderGeneric(target, data);
  }
}

/* ── Workflow Rendering ─────────────────────────────────────── */

function renderWorkflow(target, data) {
  target.appendChild(el("p", "summary", data.summary || "Improvement workflow ready."));
  // Bind a freshly generated preview's id to its goal so Approve And Apply can
  // reuse it (rememberPreview skips approve responses that carry it back applied).
  rememberPreview(data);

  if (data.approval) {
    target.appendChild(approvalBanner(data.approval, "Approved", "All planned changes completed."));
  }

  if (data.preview?.changes?.length) {
    renderPreviewPanel(target, data.preview);
  }

  if (data.apply_results?.length) {
    renderApplyResults(target, data.apply_results);
  }

  const tracker = el("div", "workflow");
  renderStepGroup(tracker, "Done", data.completed || [], "done");
  renderStepGroup(tracker, "Current", data.current ? [data.current] : [], "current");
  renderStepGroup(tracker, "Remaining", data.remaining || [], "remaining");
  target.appendChild(tracker);

  if (data.findings?.length) renderFindings(target, data.findings);
}

function renderPreviewPanel(target, preview) {
  const panel = el("section", "preview-panel");
  panel.appendChild(el("h3", "", "Temporary Code Preview"));
  const meta = el("div", "pill-row");
  meta.appendChild(el("span", "pill", `Preview ${preview.id.slice(0, 8)}`));
  meta.appendChild(el("span", `pill ${preview.verification?.ok ? "ok" : "error"}`, preview.verification?.ok ? "Syntax OK" : "Syntax Error"));
  panel.appendChild(meta);
  preview.changes.forEach((change) => {
    const card = el("article", "preview-card");
    card.appendChild(el("strong", "", change.relative_path));
    card.appendChild(el("p", "", change.description));
    if (change.content_preview?.length) {
      card.appendChild(el("pre", "", change.content_preview.join("\n")));
    }
    panel.appendChild(card);
  });
  target.appendChild(panel);
}

function renderApplyResults(target, results) {
  const section = el("section", "preview-panel");
  section.appendChild(el("h3", "", "Apply Results"));
  results.forEach((result) => {
    const item = el("div", "list-item");
    item.appendChild(el("strong", "", result.path));
    item.appendChild(el("span", `pill ${result.status === "applied" ? "ok" : "error"}`, label(result.status)));
    item.appendChild(el("p", "", result.message));
    section.appendChild(item);
  });
  target.appendChild(section);
}

function renderStepGroup(target, title, steps, status) {
  if (!steps.length) return;
  const group = el("section", "step-group");
  group.appendChild(el("h3", "", title));
  steps.forEach((step) => {
    const item = el("article", `step-item ${status}`);
    item.appendChild(el("span", `step-dot ${status}`, status === "done" ? "Done" : status === "current" ? "Now" : "Next"));
    const body = el("div", "");
    body.appendChild(el("h4", "", step.title));
    body.appendChild(el("p", "", step.detail || ""));
    if (step.verification?.length) {
      const row = el("div", "pill-row");
      step.verification.forEach((t) => row.appendChild(el("span", "pill", t)));
      body.appendChild(row);
    }
    item.appendChild(body);
    group.appendChild(item);
  });
  target.appendChild(group);
}

/* ── Learning Rendering ─────────────────────────────────────── */

function renderLearning(target, data) {
  target.appendChild(el("p", "summary", data.message || data.training_scope || "Learning cycle complete."));
  if (data.iterations) {
    const grid = el("div", "card-grid");
    data.iterations.forEach((item) => {
      const card = el("article", "info-card");
      card.appendChild(el("h4", "", `Iteration ${item.iteration}`));
      card.appendChild(el("p", "", `${item.action_count} actions, ${item.finding_count} findings`));
      card.appendChild(el("span", "pill ok", item.memory_key));
      grid.appendChild(card);
    });
    target.appendChild(grid);
  }
  if (data.plan?.actions) renderActionCards(target, data.plan.actions, "description");
}

/* ── Health Rendering ───────────────────────────────────────── */

function renderHealth(target, data) {
  const checks = data.checks || data.health?.checks || data.hardening?.checks || [];
  const ready = data.ready ?? data.ok ?? data.health?.ok ?? data.hardening?.ready;
  const pendingCount = checks.filter((check) => !(check.ok ?? check.level === "ok")).length;
  const hasError = checks.some((check) => check.level === "error" || check.ok === false);
  let summary = "System is ready.";
  if (ready === false || hasError) summary = "Needs attention.";
  else if (pendingCount) summary = `System is ready — ${pendingCount} item${pendingCount === 1 ? "" : "s"} to review.`;
  target.appendChild(el("p", "summary", summary));
  const list = el("div", "health-list");
  checks.forEach((check) => {
    const item = el("div", "list-item");
    const ok = check.ok ?? check.level === "ok";
    item.appendChild(el("strong", "", check.name));
    item.appendChild(el("span", `pill ${ok ? "ok" : "warn"}`, ok ? "OK" : "Review"));
    item.appendChild(el("p", "", check.detail || check.message || ""));
    list.appendChild(item);
  });
  target.appendChild(list);
}

/* ── Settings Rendering ─────────────────────────────────────── */

function renderSettingsResult(target, data) {
  target.appendChild(el("p", "summary", "Settings saved."));
  const grid = el("div", "card-grid");
  Object.entries(data).forEach(([key, value]) => {
    const card = el("article", "info-card");
    card.appendChild(el("h4", "", label(key)));
    card.appendChild(el("p", "", String(value)));
    grid.appendChild(card);
  });
  target.appendChild(grid);
}

/* ── Generic Fallback ───────────────────────────────────────── */

function renderGeneric(target, data) {
  if (data.commands) {
    const grid = el("div", "card-grid");
    Object.entries(data.commands).forEach(([name, command]) => {
      const card = el("article", "info-card");
      card.appendChild(el("h4", "", label(name)));
      card.appendChild(el("p", "", command));
      grid.appendChild(card);
    });
    target.appendChild(grid);
    return;
  }
  const card = el("article", "info-card");
  card.appendChild(el("h4", "", "Result"));
  card.appendChild(el("p", "", textForSpeech(data)));
  target.appendChild(card);
}

/* ── Metrics Update ─────────────────────────────────────────── */

// Always-visible AI brain status for the System panel: mode + provider + model
// from /status (status.app.brain), so which brain is answering is visible at a
// glance without reading JSON. Unknown/missing brain renders a neutral state.
function renderBrainStatus(status) {
  const container = byId("brainStatus");
  if (!container) return;
  const brain = status?.app?.brain || {};
  const active = Boolean(brain.model_configured);
  const effective = brain.effective_mode || (active ? "llm" : "scratch");
  const card = el("article", "info-card brain-card");
  card.appendChild(el("h4", "", "AI Brain"));
  const row = el("p", "brain-line");
  const modeText = effective === "scratch" ? "Local scratch brain" : effective === "llm" ? "Local LLM" : "Auto";
  row.appendChild(el("span", `pill ${active ? "ok" : "warn"}`, modeText));
  const provider = label(brain.provider || "unknown");
  row.appendChild(el("strong", "", provider + (brain.model ? ` · ${brain.model}` : "")));
  card.appendChild(row);
  card.appendChild(
    el(
      "p",
      "",
      effective === "scratch"
        ? "Answering locally with the built-in scratch brain. Switch back to Auto or Local LLM in the Chat panel to use Ollama."
        : active
          ? "Chat and Explore synthesis use this local model."
          : "Auto mode: answering locally until the Ollama server is detected."
    )
  );
  clearNode(container);
  container.appendChild(card);
}

/* ── Tracked Tasks (System panel) ───────────────────────────── */

function renderTaskList(tasks) {
  const container = byId("taskList");
  if (!container) return;
  clearNode(container);
  if (!Array.isArray(tasks) || !tasks.length) {
    container.appendChild(el("p", "task-empty", "No tracked tasks yet — every request you make creates one."));
    return;
  }
  tasks.slice(0, 30).forEach((task) => {
    const row = el("div", "task-row");
    const body = el("div", "task-row-body");
    body.appendChild(el("span", "task-row-title", task.title || "(untitled task)"));
    const when = task.created_at ? new Date(task.created_at).toLocaleString() : "";
    body.appendChild(el("span", "task-row-meta", `${label(task.status || "pending")} · ${label(task.kind || "general")} · ${when}`));
    row.appendChild(body);
    row.appendChild(el("span", `pill ${task.status === "completed" ? "ok" : task.status === "failed" ? "err" : "warn"}`, label(task.status || "pending")));
    const del = el("button", "cyber-btn danger small task-delete", "Delete");
    del.type = "button";
    del.addEventListener("click", () => deleteTask(task.id));
    row.appendChild(del);
    container.appendChild(row);
  });
}

async function deleteTask(id) {
  if (!id) return;
  await requestJson("/tasks/delete", { id });
  showToast("Task deleted");
  await refreshStatus();
}

async function clearAllTasks() {
  const result = await requestJson("/tasks/clear", {});
  showToast(`Deleted ${result.deleted ?? 0} task(s)`);
  await refreshStatus();
}

function updateMetrics(status) {
  const app = status.app || {};
  const metrics = [
    ["Runtime", app.runtime_started ? "Running" : "Ready"],
    ["Modules", app.modules?.length || 0],
    ["Tasks", app.tracked_tasks || 0],
    ["Projects", app.projects || 0],
  ];
  const grid = byId("metricGrid");
  clearNode(grid);
  metrics.forEach(([name, value]) => {
    const card = el("article", "metric-card");
    card.appendChild(el("span", "", name));
    card.appendChild(el("strong", "", String(value)));
    grid.appendChild(card);
  });

  const capabilities = byId("capabilityList");
  clearNode(capabilities);
  const heading = el("div", "pill-row");
  heading.appendChild(el("span", "pill ok", "Modules"));
  heading.appendChild(el("span", "pill", "API actions update automatically"));
  capabilities.appendChild(heading);
  (app.modules || []).forEach((module) => {
    const item = el("div", "list-item");
    item.appendChild(el("strong", "", label(module)));
    item.appendChild(el("span", "", "Available"));
    capabilities.appendChild(item);
  });
  (status.api?.routes || []).forEach((route) => {
    const item = el("div", "list-item");
    item.appendChild(el("strong", "", route.path));
    item.appendChild(el("span", "pill", route.method));
    item.appendChild(el("p", "", route.description));
    capabilities.appendChild(item);
  });
}

/* ── Vision guided-click result card ───────────────────────── */

// A guided click renders its own card, not the generic JSON dump: action
// status, how the element was located, whether verification SAW the screen
// change ("Verified" vs "Unverified" vs the honest reason it could not),
// and any open bugs this verified click auto-resolved.
function renderVisionClickResult(target, data) {
  const verification = data.verification || {};
  const changed = verification.changed;
  const locatedBy = data.output && data.output.located_by ? String(data.output.located_by) : "ocr";
  const point = data.output && data.output.coordinates ? data.output.coordinates : null;

  const card = el("article", "info-card vision-result-card");
  card.appendChild(el("h4", "", `Guided click: ${String(data.label || "")}`));

  const statusRow = el("p", "pill-row");
  const statusOk = data.status === "completed";
  statusRow.appendChild(el("span", `pill ${statusOk ? "ok" : "error"}`, statusOk ? "Completed" : String(data.status || "failed")));
  if (changed === true) statusRow.appendChild(el("span", "pill ok", "Verified"));
  else if (changed === false) statusRow.appendChild(el("span", "pill error", "Unverified: no visible change"));
  else statusRow.appendChild(el("span", "pill warn", "Unverified"));
  statusRow.appendChild(el("span", "pill", `located by ${locatedBy}`));
  card.appendChild(statusRow);

  const lines = [];
  if (point) lines.push(`Clicked at (${point[0]}, ${point[1]})`);
  if (verification.changed_pct != null) lines.push(`Screen changed by ${verification.changed_pct}%`);
  if (verification.label_gone_near_click === true) lines.push("The label left the click area (button likely activated)");
  if (verification.note) lines.push(String(verification.note));
  if (typeof data.resolved_bugs === "number" && data.resolved_bugs > 0) {
    lines.push(`Auto-resolved ${data.resolved_bugs} open bug(s) for this flow — the log now records the passing evidence.`);
  }
  if (data.error) lines.push(`Error: ${String(data.error)}`);
  if (!lines.length) lines.push(textForSpeech(data) || "Click executed.");
  const list = el("ul", "vision-result-lines");
  lines.forEach((line) => list.appendChild(el("li", "", line)));
  card.appendChild(list);

  clearNode(target);
  target.appendChild(card);
}
