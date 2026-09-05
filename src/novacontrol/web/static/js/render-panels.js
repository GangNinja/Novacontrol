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

  // /ask can return a full research report (Explore intent) — the report
  // renderer owns that shape, so delegate instead of re-deriving it here.
  if (tryRenderResearch(stream, data)) return;

  // /ask envelopes the handler payload under `payload`; flat bodies are the payload.
  const payload = data.payload || data;

  if ((data.route === "desktop_automation" || payload.workflow?.actions) && payload.approval) {
    renderCommand(stream, payload);
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

  if (data.approval) {
    target.appendChild(approvalBanner(data.approval, "Approved & Executed", "All planned actions completed."));
  }

  if (data.approval && !data.approval.approved && data.approval.token && data.command) {
    const row = el("div", "button-row action-row");
    const approve = el("button", "", "Approve And Run");
    approve.type = "button";
    approve.addEventListener("click", () => {
      // /command/execute re-dispatches on the intent, so this single button approves
      // desktop plans AND browser plans (which must reach execute_browser_command).
      run(target.id, "command", () =>
        requestJson("/command/execute", { command: data.command, approval_token: data.approval.token })
      , null, { onSuccess: () => { clearApproval(data.command); recordActivity("command", "Command executed", data.command); } });
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

  if (data.bridge) {
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

/* ── Build / Improvement Rendering ──────────────────────────── */

function renderBuild(target, data) {
  const payload = data.payload || data;
  if (payload.plan?.steps) {
    target.appendChild(el("p", "summary", payload.plan.name || "Plan created."));
    renderActionCards(target, payload.plan.steps, "description");
  } else if (payload.actions) {
    target.appendChild(el("p", "summary", `Found ${payload.actions.length} improvement actions.`));
    renderActionCards(target, payload.actions, "description");
    renderFindings(target, payload.findings || []);
  } else {
    renderGeneric(target, data);
  }
}

/* ── Workflow Rendering ─────────────────────────────────────── */

function renderWorkflow(target, data) {
  target.appendChild(el("p", "summary", data.summary || "Improvement workflow ready."));
  if (data.preview?.id) state.lastPreviewId = data.preview.id;

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
