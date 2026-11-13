/* NovaMarket NM-14 operational workspace.
 *
 * Every displayed value comes from the live, authenticated market API. There
 * are no sample prices, balances or performance figures in this file: a
 * missing value renders as an explicit empty/unavailable state.
 */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  // Market data is refreshed every fifteen minutes, not per second.
  const REFRESH_MS = 15 * 60 * 1000;
  const SECTIONS = {
    overview: "Overview",
    market: "Market Intelligence",
    events: "Market Events",
    discovery: "Asset Discovery",
    forecasts: "Forecasts",
    portfolios: "Portfolios",
    risk: "Risk",
    strategies: "Strategies",
    paper: "Paper Trading",
    backtests: "Backtesting",
    shadow: "Shadow Mode",
    learning: "Adaptive Learning",
    wallet: "Wallet & Ledger",
    activity: "Activity & Audit",
    health: "System Health",
    settings: "Settings",
  };
  const SECTION_COMPONENT = {
    market: "market_data",
    events: "radar",
    discovery: "discovery",
    forecasts: "forecasting",
    portfolios: "portfolio",
    risk: "risk",
    strategies: "strategy",
    paper: "execution",
    backtests: "backtesting",
    shadow: "shadow",
    learning: "learning",
    wallet: "wallet",
  };
  const SECTION_RECENT = {
    events: "events",
    discovery: "candidates",
    forecasts: "forecasts",
    portfolios: "portfolios",
    risk: "risk_decisions",
    strategies: "strategies",
    paper: "paper_orders",
    backtests: "backtests",
    shadow: "shadow_sessions",
    learning: "learning_experiments",
    wallet: "wallets",
  };
  const ROW_KEYS = [
    "id", "wallet_id", "name", "title", "status", "lifecycle", "kind", "type",
    "instrument_id", "strategy_id", "portfolio_id", "order_id", "session_id",
    "experiment_id", "horizon", "created_at", "observed_at", "amount",
    "return_pct", "realized_pnl", "available", "total", "score",
  ];

  const state = {
    overview: null,
    active: "overview",
    loading: false,
    lastLoaded: 0,
    timer: null,
  };

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function clear(node) {
    while (node && node.firstChild) node.removeChild(node.firstChild);
  }

  function token() {
    return $("tokenInput").value.trim();
  }

  function headers() {
    return token() ? { Authorization: `Bearer ${token()}` } : {};
  }

  async function request(path, options = {}) {
    const response = await fetch(path, {
      ...options,
      headers: { "Content-Type": "application/json", ...headers(), ...(options.headers || {}) },
    });
    const text = await response.text();
    let body = {};
    try { body = text ? JSON.parse(text) : {}; } catch (_) { body = { detail: text }; }
    if (!response.ok) {
      const error = new Error(body.detail || body.message || `${response.status} ${response.statusText}`);
      error.status = response.status;
      throw error;
    }
    return body;
  }

  function notify(text, kind = "info", sticky = false) {
    const node = $("message");
    node.className = `message show ${kind}`;
    node.textContent = text;
    if (!sticky) window.setTimeout(() => node.classList.remove("show"), 7000);
  }

  function statusClass(value) {
    const text = String(value || "").toLowerCase();
    if (["ok", "running", "ready", "completed", "approved", "active", "fresh", "valid"].includes(text)) return "ok";
    if (["degraded", "warning", "warn", "pending", "paused", "stale"].includes(text)) return "warn";
    if (["error", "failed", "disabled", "rejected", "blocked", "critical"].includes(text)) return "bad";
    return "muted";
  }

  function setBadge(id, text, value) {
    const node = $(id);
    node.textContent = text;
    node.className = `badge ${statusClass(value)}`;
  }

  function metric(label, value, hint) {
    const card = el("div", "metric");
    card.appendChild(el("div", "label", label));
    card.appendChild(el("div", "value", value === undefined || value === null || value === "" ? "—" : value));
    if (hint) card.appendChild(el("div", "hint", hint));
    return card;
  }

  function summaryCards(overview) {
    const summary = overview.summary || {};
    const cards = $("summaryCards");
    clear(cards);
    cards.appendChild(metric("Wallet balance", formatMoney(summary.wallet_balance_inr), `${summary.wallets || 0} wallets · ${summary.ledger_entries || 0} ledger entries`));
    cards.appendChild(metric("Paper orders", summary.paper_orders ?? 0, `${summary.paper_fills || 0} simulated fills`));
    cards.appendChild(metric("Risk decisions", summary.risk_decisions ?? 0, "independent NM-08 engine"));
    cards.appendChild(metric("Backtests", summary.backtests ?? 0, "isolated NM-11 runs"));
    cards.appendChild(metric("Shadow sessions", summary.shadow_sessions ?? 0, "non-executing NM-12"));
    cards.appendChild(metric("Learning", summary.learning_initialised ? "Ready" : "Not open", "approval-gated NM-13"));
  }

  function formatMoney(value) {
    if (value === undefined || value === null || value === "") return "—";
    return `₹${value} / ${value} NC`;
  }

  function safetyList(overview) {
    const safety = overview.safety || {};
    const list = $("safetyList");
    clear(list);
    const rows = [
      ["Real-money trading", safety.real_money_trading ? "ENABLED" : "Disabled", safety.real_money_trading ? "bad" : "ok"],
      ["Paper execution", safety.paper_only ? "Simulated only" : "Unknown", "ok"],
      ["Broker connection", safety.broker_connected ? "Connected" : "None", safety.broker_connected ? "bad" : "ok"],
      ["Live execution routes", String(safety.live_execution_routes ?? 0), "ok"],
      ["Shadow execution", safety.shadow_executes ? "Enabled" : "Never", safety.shadow_executes ? "bad" : "ok"],
      ["Learning execution", safety.learning_executes ? "Enabled" : "Never", safety.learning_executes ? "bad" : "ok"],
    ];
    for (const [label, value, cls] of rows) {
      const pill = el("span", `pill ${cls}`, `${label}: ${value}`);
      list.appendChild(pill);
    }
  }

  function componentGrid(status) {
    const grid = $("componentGrid");
    clear(grid);
    const components = [
      ["Market data", status.market_data],
      ["Events", status.radar],
      ["Discovery", status.discovery],
      ["Forecasts", status.forecasting],
      ["Portfolios", status.portfolio],
      ["Risk", status.risk],
      ["Strategies", status.strategy],
      ["Paper execution", status.execution],
      ["Backtesting", status.backtesting],
      ["Shadow", status.shadow],
      ["Learning", status.learning],
      ["Wallet / ledger", status.wallet],
    ];
    for (const [name, body] of components) {
      const row = el("div", "component");
      row.appendChild(el("span", "component-name", name));
      const value = !body || body.initialised === false
        ? (body && body.detail ? "not open" : "unavailable")
        : (body.health && (body.health.state || body.health)) || body.lifecycle
          || (body.initialised === true ? "initialised" : body.phase || "reported");
      row.appendChild(el("span", "component-state", value));
      grid.appendChild(row);
    }
  }

  function renderRows(container, rows, options = {}) {
    clear(container);
    if (!rows || !rows.length) {
      container.appendChild(el("div", "empty", options.empty || "No records are stored yet."));
      return;
    }
    for (const row of rows.slice(0, options.limit || 8)) {
      const item = el("div", "row");
      const header = el("div", "row-title");
      const title = row.name || row.title || row.instrument_id || row.strategy_id || row.id || row.wallet_id || row.order_id || row.session_id || row.experiment_id || "Record";
      header.appendChild(el("span", "", title));
      const badgeValue = row.status || row.lifecycle || row.state || row.kind || row.type || row.phase;
      if (badgeValue) header.appendChild(el("span", `badge ${statusClass(badgeValue)}`, badgeValue));
      item.appendChild(header);
      const meta = [];
      for (const key of ROW_KEYS) {
        if (["id", "name", "title", "status", "lifecycle", "state", "kind", "type", "phase"].includes(key)) continue;
        const value = row[key];
        if (value === undefined || value === null || value === "" || typeof value === "object") continue;
        meta.push(`${key}=${value}`);
      }
      if (meta.length) item.appendChild(el("div", "row-meta", meta.slice(0, 8).join(" · ")));
      if (row.last_error || row.error || row.detail) {
        item.appendChild(el("div", "row-meta", `error: ${row.last_error || row.error || row.detail}`));
      }
      container.appendChild(item);
    }
  }

  function extractRows(body) {
    if (!body) return [];
    if (Array.isArray(body)) return body;
    if (body.ok === false || body.available === false) return [];
    for (const key of ["items", "rows", "events", "wallets", "candidates", "forecasts", "portfolios", "decisions", "strategies", "orders", "backtests", "sessions", "experiments", "evidence", "alerts"]) {
      if (Array.isArray(body[key])) return body[key];
    }
    return [];
  }

  function renderActivity(container, events, empty) {
    clear(container);
    if (!events || !events.length) {
      container.appendChild(el("div", "empty", empty || "No market activity has been published yet."));
      return;
    }
    for (const event of events.slice(0, 8)) {
      const row = el("div", "row");
      const header = el("div", "row-title");
      header.appendChild(el("span", "", event.type || "market event"));
      if (event.at) header.appendChild(el("span", "badge muted", String(event.at).slice(11, 19)));
      row.appendChild(header);
      const payload = event.payload ? Object.entries(event.payload).slice(0, 4).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(" · ") : "";
      if (payload) row.appendChild(el("div", "row-meta", payload));
      container.appendChild(row);
    }
  }

  function renderAudit(container, audit) {
    clear(container);
    if (!audit || audit.available === false) {
      container.appendChild(el("div", "empty", (audit && audit.detail) || "The application audit trail is unavailable."));
      return;
    }
    if (!audit.entries || !audit.entries.length) {
      container.appendChild(el("div", "empty", "No NovaMarket audit records have been written yet."));
      return;
    }
    for (const entry of audit.entries.slice(0, 8)) {
      const row = el("div", "row");
      const header = el("div", "row-title");
      header.appendChild(el("span", "", entry.intent || entry.user_request || "market operation"));
      if (entry.outcome) header.appendChild(el("span", `badge ${statusClass(entry.outcome)}`, entry.outcome));
      row.appendChild(header);
      row.appendChild(el("div", "row-meta", `${entry.timestamp || entry.at || ""} · ${entry.route || "/market"}`));
      container.appendChild(row);
    }
  }

  function renderChrome() {
    if (!state.overview) return;
    const overview = state.overview;
    const status = overview.status || {};
    const health = status.health || {};
    $("phaseBadge").textContent = overview.phase || "NM-14";
    setBadge("lifecycleBadge", status.lifecycle || "unknown", status.lifecycle);
    setBadge("healthBadge", `health ${health.state || "unknown"}`, health.state);
    setBadge("phaseBadge", overview.phase || "NM-14", "ok");
    $("enableButton").disabled = ["running", "ready", "degraded"].includes(status.lifecycle);
    $("disableButton").disabled = ["disabled", "shutting_down"].includes(status.lifecycle);
  }

  function renderOverview() {
    if (!state.overview) return;
    const overview = state.overview;
    renderChrome();
    summaryCards(overview);
    safetyList(overview);
    componentGrid(overview.status || {});
    renderActivity($("overviewActivity"), overview.activity, "No market activity has been published yet.");
    renderAudit($("overviewAudit"), overview.audit);
  }

  function renderSection() {
    const section = state.active;
    if (section === "overview") { renderOverview(); return; }
    if (section === "activity") {
      const overview = state.overview || {};
      renderActivity($("activityContent"), overview.activity, "No market activity has been published yet.");
      renderAudit($("auditContent"), overview.audit);
      return;
    }
    if (section === "health") {
      renderHealth();
      return;
    }
    if (section === "settings") {
      loadSettings();
      return;
    }
    const status = (state.overview && state.overview.status) || {};
    const component = status[SECTION_COMPONENT[section]];
    const target = $(`${section}Content`);
    renderComponent(target, component, section);
    const recent = (state.overview && state.overview.recent) || {};
    const body = recent[SECTION_RECENT[section]];
    const rows = extractRows(body);
    if (body && body.ok === false) {
      target.appendChild(el("div", "empty", body.detail || "This subsystem is not available."));
    } else if (!rows.length) {
      target.appendChild(el("div", "empty", "No records are stored yet. This is an empty state, not a placeholder."));
    } else {
      const card = el("div", "card");
      card.appendChild(el("h3", "", "Recent records"));
      const list = el("div", "rows");
      renderRows(list, rows, { limit: 5 });
      card.appendChild(list);
      target.appendChild(card);
    }
  }

  function renderComponent(target, body, section) {
    clear(target);
    if (!body) {
      target.appendChild(el("div", "empty", "The subsystem has not reported state."));
      return;
    }
    const card = el("div", "card");
    const line = el("div", "status-line");
    const stateValue = body.initialised === false ? "not open" : (body.health?.state || body.lifecycle || "reported");
    line.appendChild(el("span", `status-dot ${statusClass(stateValue)}`));
    line.appendChild(el("span", "", body.detail || body.last_error || `State: ${stateValue}`));
    card.appendChild(line);
    if (body.phase) card.appendChild(el("div", "row-meta", `phase ${body.phase}`));
    const dl = el("dl", "kv");
    const flat = Object.entries(body).filter(([, value]) => value === null || ["string", "number", "boolean"].includes(typeof value)).slice(0, 18);
    for (const [key, value] of flat) {
      dl.appendChild(el("dt", "", key));
      dl.appendChild(el("dd", "", value === null ? "—" : String(value)));
    }
    if (dl.children.length) card.appendChild(dl);
    if (Array.isArray(body.guarantees) && body.guarantees.length) {
      card.appendChild(el("h3", "", "Guarantees"));
      const list = el("ul", "list");
      for (const item of body.guarantees) list.appendChild(el("li", "", item));
      card.appendChild(list);
    }
    target.appendChild(card);
    if (section === "market" && body.status) {
      const sub = el("div", "card");
      sub.appendChild(el("h3", "", "Market data state"));
      const pre = el("pre", "", JSON.stringify(body.status, null, 2));
      sub.appendChild(pre);
      target.appendChild(sub);
    }
  }

  function renderHealth() {
    const target = $("healthContent");
    clear(target);
    const status = (state.overview && state.overview.status) || {};
    const health = status.health || {};
    const card = el("div", "card");
    card.appendChild(el("h3", "", `Health: ${health.state || "unknown"}`));
    if (Array.isArray(health.reasons) && health.reasons.length) {
      const list = el("ul", "list");
      for (const reason of health.reasons) list.appendChild(el("li", "", reason));
      card.appendChild(list);
    } else {
      card.appendChild(el("div", "empty", "No health problems are currently reported."));
    }
    target.appendChild(card);
    const deps = el("div", "card");
    deps.appendChild(el("h3", "", "Dependencies"));
    const grid = el("div", "component-grid");
    for (const dep of status.dependencies || []) {
      const row = el("div", "component");
      row.appendChild(el("span", "component-name", dep.name || "dependency"));
      row.appendChild(el("span", "component-state", dep.available ? "available" : (dep.detail || "unavailable")));
      grid.appendChild(row);
    }
    deps.appendChild(grid);
    target.appendChild(deps);
    const caps = el("div", "card");
    caps.appendChild(el("h3", "", "Capabilities"));
    const rows = el("div", "rows");
    renderRows(rows, (status.capabilities || []).map((cap) => ({ ...cap, name: cap.name || cap.capability_id, status: cap.available ? "available" : "unavailable" })), { limit: 12 });
    caps.appendChild(rows);
    target.appendChild(caps);
  }

  async function loadSettings() {
    const target = $("settingsContent");
    clear(target);
    target.appendChild(el("div", "empty", "Loading resolved configuration…"));
    try {
      const body = await request("/market/config");
      clear(target);
      const card = el("div", "card");
      card.appendChild(el("h3", "", body.valid ? "Configuration is valid" : "Configuration error"));
      if (body.error) card.appendChild(el("div", "empty", body.error));
      const pre = el("pre", "", JSON.stringify(body.config || {}, null, 2));
      card.appendChild(pre);
      target.appendChild(card);
      if (body.schema) {
        const schema = el("div", "card");
        schema.appendChild(el("h3", "", "Editable fields"));
        const list = el("ul", "list");
        for (const field of body.schema) list.appendChild(el("li", "", field));
        schema.appendChild(list);
        target.appendChild(schema);
      }
    } catch (error) {
      clear(target);
      target.appendChild(el("div", "empty", `Configuration unavailable: ${error.message}`));
    }
  }

  function parseToken() {
    // Kept for a single validation opportunity; the value stays in the input
    // element and is never written to storage or a log.
    return token();
  }

  async function load() {
    if (state.loading) return;
    state.loading = true;
    const button = $("refreshButton");
    button.disabled = true;
    try {
      const overview = await request("/market/overview");
      state.overview = overview;
      state.lastLoaded = Date.now();
      renderChrome();
      $("lastUpdated").textContent = `Live state generated ${overview.generated_at || "just now"}`;
      if (state.active === "settings") await loadSettings();
      else renderSection();
      if (overview.audit && overview.audit.available === false) {
        notify(`Audit unavailable: ${overview.audit.detail || "not attached"}`, "warn");
      }
    } catch (error) {
      if (error.status === 401) {
        notify("Authentication required. Paste the API token in the left rail, then refresh.", "warn", true);
      } else {
        notify(`Live state unavailable: ${error.message}`, "error", true);
      }
      $("lastUpdated").textContent = "No live state — see the message above.";
    } finally {
      state.loading = false;
      button.disabled = false;
    }
  }

  async function lifecycle(action) {
    const path = action === "enable" ? "/market/enable" : "/market/disable";
    notify(`${action === "enable" ? "Enabling" : "Disabling"} NovaMarket…`, "info", true);
    try {
      await request(path, { method: "POST", body: JSON.stringify({ reason: "workspace operator action" }) });
      notify(`NovaMarket ${action}d.`, "info");
      await load();
    } catch (error) {
      notify(`${action} failed: ${error.message}`, "error", true);
    }
  }

  function selectSection(section) {
    if (!SECTIONS[section]) return;
    state.active = section;
    for (const button of document.querySelectorAll(".nav-item")) {
      button.classList.toggle("is-active", button.dataset.section === section);
    }
    for (const panel of document.querySelectorAll(".panel")) {
      panel.classList.toggle("is-active", panel.id === `panel-${section}`);
    }
    $("sectionTitle").textContent = SECTIONS[section];
    renderChrome();
    renderSection();
    if (window.location.hash !== `#${section}`) history.replaceState(null, "", `#${section}`);
  }

  function init() {
    // The token is held in this tab's memory only. Persisting it in
    // localStorage would turn one XSS into a durable credential leak.
    $("tokenInput").value = "";
    $("tokenInput").addEventListener("change", () => parseToken());
    $("refreshButton").addEventListener("click", load);
    $("enableButton").addEventListener("click", () => lifecycle("enable"));
    $("disableButton").addEventListener("click", () => lifecycle("disable"));
    for (const button of document.querySelectorAll(".nav-item")) {
      button.addEventListener("click", () => selectSection(button.dataset.section));
    }
    const start = (window.location.hash || "").replace("#", "");
    selectSection(SECTIONS[start] ? start : "overview");
    load();
    state.timer = window.setInterval(() => {
      if (!document.hidden) load();
    }, REFRESH_MS);
    window.addEventListener("focus", () => {
      if (Date.now() - state.lastLoaded > REFRESH_MS) load();
    });
    window.addEventListener("beforeunload", () => window.clearInterval(state.timer));
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
