/* ── Phase 39: Portfolio Manager for Multiple AI-Run Ventures ───────────── */

let portfolioSelectedId = "";
let portfolioLastDashboard = null;

function portfolioLabel(value) {
  return String(value || "").replace(/_/g, " ").toLowerCase().replace(/\b\w/g, (c) => c.toUpperCase());
}

function portfolioValue(id) {
  return byId(id) ? byId(id).value.trim() : "";
}

function portfolioNumber(id, fallback = 0) {
  const raw = portfolioValue(id);
  if (!raw) return fallback;
  const value = Number(raw);
  if (!Number.isFinite(value)) throw new Error(`${id} must be a number`);
  return value;
}

function portfolioMoney(value, currency) {
  if (value === null || value === undefined) return "NOT CALCULABLE";
  return `${currency || ""} ${Number(value).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`.trim();
}

function portfolioMetric(block, currency) {
  if (!block || typeof block !== "object") return "not recorded";
  if (!block.available) return `NOT CALCULABLE — ${block.reason || "inputs unavailable"}`;
  if (typeof block.value === "string") return block.value;
  const value = Number(block.value);
  if (!Number.isFinite(value)) return "available";
  const suffix = block.kind && block.kind !== "actual" ? ` (${portfolioLabel(block.kind)})` : "";
  return `${currency ? `${currency} ` : ""}${value.toLocaleString(undefined, { maximumFractionDigits: 2 })}${suffix}`;
}

function portfolioCard(target, title, note) {
  const card = el("article", "info-card");
  card.appendChild(el("h3", "", title));
  if (note) card.appendChild(el("p", "muted", note));
  target.appendChild(card);
  return card;
}

function portfolioRow(target, title, status, lines) {
  const row = el("article", "info-card");
  const head = el("div", "surface-head-row");
  head.appendChild(el("h4", "", title || "Record"));
  if (status) head.appendChild(el("span", "pill", portfolioLabel(status)));
  row.appendChild(head);
  (lines || []).filter(Boolean).forEach((line) => row.appendChild(el("p", "muted", String(line))));
  target.appendChild(row);
  return row;
}

async function refreshPortfolioStatus() {
  const data = await requestJson("/portfolio/status");
  byId("portfolioStatus").textContent =
    `${data.portfolios ?? 0} portfolio(s) · ${data.ventures ?? 0} venture reference(s) · ` +
    `${data.open_alerts ?? 0} open alert(s) · ${data.pending_decisions ?? 0} pending decision(s) · ` +
    `Phase 38 reader ${data.configured?.finance_reader ? "connected" : "unavailable"} · ` +
    `funds transfer ${data.boundaries?.funds_transfer ? "enabled" : "disabled"}`;
  return data;
}

async function refreshPortfolios() {
  const data = await requestJson("/portfolios?limit=100");
  if (byId("portfolioList")) render("portfolioList", data, "portfolioList");
  return data;
}

async function loadPortfolio(id) {
  if (!id) return null;
  portfolioSelectedId = id;
  const detail = await requestJson(`/portfolios/${encodeURIComponent(id)}`);
  render("portfolioDetail", detail, "portfolioDetail");
  const dashboard = await requestJson(`/portfolios/${encodeURIComponent(id)}/dashboard`);
  portfolioLastDashboard = dashboard;
  render("portfolioDashboard", dashboard, "portfolioDashboard");
  return { detail, dashboard };
}

function portfolioActivity(lines) {
  if (!byId("portfolioActivity")) return;
  const target = byId("portfolioActivity");
  clearNode(target);
  (lines || []).forEach((line) => target.appendChild(el("p", "muted", String(line))));
}

function portfolioPath(suffix) {
  return `/portfolios/${encodeURIComponent(portfolioSelectedId)}${suffix || ""}`;
}

function portfolioAction(buttonId, loadingText, task) {
  if (!portfolioSelectedId) {
    showToast("Select a portfolio first");
    return;
  }
  run("portfolioDashboard", "portfolioDashboard", task, buttonId || null, { loadingText });
}

function setupPortfolioWorkspace() {
  if (!byId("portfolioRefreshButton")) return;
  byId("portfolioRefreshButton").addEventListener("click", () =>
    run("portfolioDashboard", "portfolioDashboard", async () => {
      await refreshPortfolioStatus();
      await refreshPortfolios();
      if (portfolioSelectedId) await loadPortfolio(portfolioSelectedId);
      return portfolioLastDashboard || { summary: "Select a portfolio" };
    }, "portfolioRefreshButton")
  );
  byId("portfolioCreateButton").addEventListener("click", () => {
    const payload = {
      name: portfolioValue("portfolioName"),
      base_currency: portfolioValue("portfolioCurrency") || "USD",
      description: portfolioValue("portfolioDescription"),
      objectives: portfolioValue("portfolioObjectives").split(/\r?\n/).map((row) => row.trim()).filter(Boolean),
    };
    run("portfolioDashboard", "portfolioDashboard", async () => {
      const created = await requestJson("/portfolios", payload);
      portfolioSelectedId = created.id;
      await refreshPortfolioStatus();
      await refreshPortfolios();
      const result = await loadPortfolio(created.id);
      return result.dashboard;
    }, "portfolioCreateButton", { loadingText: "Creating an isolated portfolio…" });
  });
  byId("portfolioVentureAddButton").addEventListener("click", () =>
    portfolioAction("portfolioVentureAddButton", "Checking Phase 35–38 lifecycle evidence…", async () => {
      const lifecycle = portfolioValue("portfolioLifecycle") || "OPPORTUNITY_DISCOVERY";
      const payload = {
        venture_id: portfolioValue("portfolioVentureId"),
        product_id: portfolioValue("portfolioProductId"),
        opportunity_id: portfolioValue("portfolioOpportunityId"),
        lifecycle_status: lifecycle,
        reason: portfolioValue("portfolioLifecycleReason") || `registered as ${lifecycle}`,
        approved: byId("portfolioLifecycleApproved").checked,
      };
      const added = await requestJson(portfolioPath("/ventures"), payload);
      await loadPortfolio(portfolioSelectedId);
      portfolioActivity([
        `Registered ${added.name} at ${portfolioLabel(added.lifecycle_status)}.`,
        "The venture remains an independent Phase 38 ledger; no financial record was copied.",
      ]);
      return portfolioLastDashboard;
    })
  );
  byId("portfolioEvaluateButton").addEventListener("click", () =>
    portfolioAction("portfolioEvaluateButton", "Evaluating evidence coverage and ranking ventures…", async () => {
      const result = await requestJson(portfolioPath("/evaluations"), { policy: "balanced" });
      await loadPortfolio(portfolioSelectedId);
      portfolioActivity([
        `${result.ranking.length} venture(s) evaluated under the ${result.policy} policy.`,
        "A venture below the coverage floor is shown without a rank; missing evidence is never scored as zero.",
      ]);
      return portfolioLastDashboard;
    })
  );
  byId("portfolioRiskButton").addEventListener("click", () =>
    portfolioAction("portfolioRiskButton", "Assessing portfolio and venture risks…", async () => {
      const result = await requestJson(portfolioPath("/risks/assess"));
      await loadPortfolio(portfolioSelectedId);
      portfolioActivity([
        `${result.risks.length} risk(s) assessed; ${result.correlated_risks.length} shared exposure(s).`,
        "No statistical diversification is claimed without a defensible model.",
      ]);
      return portfolioLastDashboard;
    })
  );
  byId("portfolioAllocationButton").addEventListener("click", () =>
    portfolioAction("portfolioAllocationButton", "Comparing approved budgets and actual evidence…", async () => {
      const result = await requestJson(portfolioPath("/allocations"));
      await loadPortfolio(portfolioSelectedId);
      portfolioActivity([
        `${result.items.length} allocation recommendation(s) generated.`,
        "Nothing was funded: a Phase 38 budget approval is still required for any budget change.",
      ]);
      return portfolioLastDashboard;
    })
  );
  byId("portfolioAlertsButton").addEventListener("click", () =>
    portfolioAction("portfolioAlertsButton", "Refreshing stale-aware portfolio alerts…", async () => {
      const result = await requestJson(portfolioPath("/alerts/refresh"));
      await loadPortfolio(portfolioSelectedId);
      portfolioActivity([
        `${result.open} alert(s) open; ${result.created} created and ${result.updated} updated.`,
        `${result.suppressed_stale_ventures} venture(s) suppressed because the source data was stale.`,
      ]);
      return portfolioLastDashboard;
    })
  );
  byId("portfolioRecommendButton").addEventListener("click", () =>
    portfolioAction("portfolioRecommendButton", "Preparing evidence-backed strategic advice…", async () => {
      const result = await requestJson(portfolioPath("/recommendations"));
      await loadPortfolio(portfolioSelectedId);
      portfolioActivity([
        `${result.recommendations.length} advisory recommendation(s) generated.`,
        "Advice is not a decision; a named approver must decide and no action is executed automatically.",
      ]);
      return portfolioLastDashboard;
    })
  );
  byId("portfolioScenarioButton").addEventListener("click", () =>
    portfolioAction("portfolioScenarioButton", "Simulating a portfolio scenario…", async () => {
      const type = portfolioValue("portfolioScenarioType") || "revenue_shortfall";
      const value = portfolioNumber("portfolioScenarioValue", 20);
      const assumptions = {};
      if (type === "revenue_shortfall") assumptions.shortfall_pct = value;
      else if (type === "scale_venture") assumptions.revenue_growth_pct = value;
      else if (type === "launch_delay") assumptions.delay_months = Math.max(1, value);
      else if (type === "development_spend") assumptions.additional_monthly_cost = value;
      else if (type === "reduce_opex") assumptions.reduction_pct = value;
      else if (type === "marketing_budget") assumptions.marketing_delta = value;
      else if (type === "pause_venture") assumptions.pause_months = Math.max(1, value);
      else if (type === "infrastructure_cost") assumptions.additional_monthly_infrastructure = value;
      const result = await requestJson(portfolioPath("/scenarios"), {
        type,
        venture_id: portfolioValue("portfolioScenarioTarget"),
        horizon_months: portfolioNumber("portfolioScenarioHorizon", 12),
        assumptions,
      });
      await loadPortfolio(portfolioSelectedId);
      portfolioActivity([
        result.available
          ? `Simulated ${portfolioLabel(result.scenario_type)} over ${result.horizon_months} months: projected ending cash ${portfolioMoney(result.projected?.ending_cash, result.currency)}.`
          : `Scenario is unavailable: ${(result.missing || []).join("; ") || "insufficient data"}.`,
        "Simulated outcomes are not actual performance and are not guarantees.",
      ]);
      return portfolioLastDashboard;
    })
  );
  byId("portfolioReportButton").addEventListener("click", () =>
    portfolioAction("portfolioReportButton", "Freezing a 12-section portfolio report snapshot…", async () => {
      const result = await requestJson(portfolioPath("/reports"), {
        period: portfolioValue("portfolioReportPeriod") || "monthly",
      });
      await loadPortfolio(portfolioSelectedId);
      portfolioActivity([
        `Generated “${result.title}” with ${result.sections.length} sections.`,
        `Snapshot hash ${String(result.snapshot_hash || "").slice(0, 12)}…; later source changes do not rewrite it.`,
      ]);
      return portfolioLastDashboard;
    })
  );
}

/* ── Renderers ──────────────────────────────────────────────────────────── */

function renderPortfolioList(target, data) {
  const portfolios = data?.portfolios || [];
  if (!portfolios.length) {
    target.appendChild(el("p", "muted", "No portfolio exists yet. Create one above, then register existing Phase 38 ventures."));
    return;
  }
  portfolios.forEach((row) => {
    const card = el("article", "info-card");
    const head = el("div", "surface-head-row");
    head.appendChild(el("h4", "", row.name || "Portfolio"));
    head.appendChild(el("span", "pill", portfolioLabel(row.status)));
    card.appendChild(head);
    card.appendChild(el("p", "muted", `${row.ventures} venture(s) · base ${row.base_currency} · ${row.open_alerts} open alert(s)`));
    if (row.description) card.appendChild(el("p", "muted", row.description));
    const button = el("button", "cyber-btn small", "Open Portfolio");
    button.type = "button";
    button.addEventListener("click", () => run("portfolioDashboard", "portfolioDashboard", async () => {
      const result = await loadPortfolio(row.id);
      return result.dashboard;
    }, null, { loadingText: "Loading portfolio intelligence…" }));
    card.appendChild(button);
    target.appendChild(card);
  });
}

function renderPortfolioDetail(target, data) {
  const portfolio = data?.portfolio || {};
  target.classList.remove("empty-state");
  target.appendChild(el("h3", "", portfolio.name || "Portfolio"));
  target.appendChild(el("p", "muted",
    `${portfolio.description || "No description"} · owner ${portfolio.owner || "unknown"} · base ${portfolio.base_currency} · role ${portfolio.role || data?.role || "viewer"}`));
  target.appendChild(el("p", "muted",
    `${data?.counts?.ventures ?? 0} venture reference(s), ${data?.restricted_ventures ?? 0} restricted from this view. Objectives: ${(portfolio.objectives || []).join("; ") || "not recorded"}.`));
  const list = el("div", "task-list");
  (data?.venture_summary || []).forEach((row) => {
    const card = el("article", "info-card");
    const head = el("div", "surface-head-row");
    head.appendChild(el("h4", "", row.name || row.id));
    head.appendChild(el("span", "pill", portfolioLabel(row.lifecycle_status)));
    card.appendChild(head);
    card.appendChild(el("p", "muted", `${row.reporting_currency} · priority ${row.priority} · Phase 35 ${row.opportunity_id || "not linked"} · Phase 36 ${row.product_id || "not linked"}`));
    const drill = el("button", "cyber-btn small", "Open Phase 38 Finance");
    drill.type = "button";
    drill.addEventListener("click", () => {
      const nav = document.querySelector('[data-panel="financePanel"]');
      if (nav) nav.click();
      if (typeof loadFinanceVenture === "function") {
        loadFinanceVenture(row.id).catch((error) => showToast(String(error.message || error)));
      }
    });
    card.appendChild(drill);
    list.appendChild(card);
  });
  target.appendChild(list);
}

function renderPortfolioDashboard(target, data) {
  const overview = data?.overview || {};
  const currency = data?.base_currency || "";
  target.classList.remove("empty-state");
  const summary = el("article", "surface");
  summary.appendChild(el("h3", "", `${data?.portfolio?.name || "Portfolio"} overview`));
  summary.appendChild(el("p", "muted",
    `${overview.total_ventures ?? 0} venture(s) · ${overview.active_ventures ?? 0} active · ${overview.under_review ?? 0} under review · ${overview.paused_ventures ?? 0} paused`));
  summary.appendChild(el("p", "muted",
    `Revenue ${portfolioMetric(overview.revenue, currency)} · Expenses ${portfolioMetric(overview.expenses, currency)} · Profitability ${portfolioMetric(overview.profitability, currency)} · Cash ${portfolioMetric(overview.cash, currency)}`));
  if (overview.consolidation_complete === false) {
    summary.appendChild(el("p", "muted", "Consolidated totals are incomplete because at least one visible venture lacks a usable explicit FX rate or source data."));
  }
  target.appendChild(summary);

  const lifecycle = el("article", "surface");
  lifecycle.appendChild(el("h3", "", "Venture lifecycle"));
  Object.entries(data?.lifecycle_distribution || {}).forEach(([key, value]) => {
    lifecycle.appendChild(el("p", "muted", `${portfolioLabel(key)}: ${value}`));
  });
  if (!Object.keys(data?.lifecycle_distribution || {}).length) lifecycle.appendChild(el("p", "muted", "No venture is registered."));
  target.appendChild(lifecycle);

  const comparison = el("article", "surface");
  comparison.appendChild(el("h3", "", "Venture comparison"));
  const table = el("div", "task-list");
  (data?.venture_comparison || []).forEach((row) => {
    const card = el("article", "info-card");
    const head = el("div", "surface-head-row");
    head.appendChild(el("h4", "", row.venture_name || row.venture_id));
    head.appendChild(el("span", "pill", portfolioLabel(row.lifecycle_status)));
    card.appendChild(head);
    card.appendChild(el("p", "muted",
      `Revenue ${portfolioMoney(row.revenue, row.currency)} · actual expenses ${portfolioMoney(row.actual_expenses, row.currency)} · net ${portfolioMoney(row.net_profit, row.currency)}`));
    card.appendChild(el("p", "muted",
      `Growth ${row.revenue_growth === null || row.revenue_growth === undefined ? "NOT CALCULABLE" : `${(row.revenue_growth * 100).toFixed(1)}%`} · margin ${row.operating_margin === null || row.operating_margin === undefined ? "NOT CALCULABLE" : `${(row.operating_margin * 100).toFixed(1)}%`} · runway ${row.runway_months ?? "NOT CALCULABLE"} · customers ${row.customers ?? "unavailable"}`));
    if (row.fresh === false) card.appendChild(el("p", "muted", "Source snapshot is stale; alerts are suppressed for this venture."));
    table.appendChild(card);
  });
  comparison.appendChild(table);
  target.appendChild(comparison);

  const capital = el("article", "surface");
  capital.appendChild(el("h3", "", "Capital and resources"));
  (data?.capital?.approved_budgets || []).forEach((row) => {
    capital.appendChild(el("p", "muted", `${row.venture_id}: approved budget ${portfolioMoney(row.approved_budget, row.currency)} · actual ${portfolioMoney(row.actual, row.currency)}`));
  });
  (data?.capital?.resources?.resources || []).forEach((row) => {
    capital.appendChild(el("p", "muted", `${row.name}: ${row.allocated}/${row.capacity} ${row.unit} utilized (${row.utilization === null ? "n/a" : `${(row.utilization * 100).toFixed(1)}%`})`));
  });
  if (!(data?.capital?.approved_budgets || []).length && !(data?.capital?.resources?.resources || []).length) {
    capital.appendChild(el("p", "muted", "No approved budget or shared resource is recorded. Recommendations do not fabricate one."));
  }
  target.appendChild(capital);

  const risk = el("article", "surface");
  risk.appendChild(el("h3", "", "Risk and alerts"));
  risk.appendChild(el("p", "muted", `${data?.alerts?.open ?? 0} open alert(s); ${data?.risks?.high_or_critical?.length ?? 0} high/critical risk(s); ${data?.risks?.correlated?.length ?? 0} correlated exposure(s).`));
  (data?.risks?.high_or_critical || []).slice(0, 8).forEach((row) => {
    risk.appendChild(el("p", "muted", `${portfolioLabel(row.severity)} · ${portfolioLabel(row.category)} · ${row.observed || ""} — ${row.mitigation || ""}`));
  });
  (data?.alerts?.alerts || []).filter((row) => row.status === "open").slice(0, 8).forEach((row) => {
    risk.appendChild(el("p", "muted", `Alert ${portfolioLabel(row.severity)} · ${row.metric}: ${row.observed} — ${row.recommended_action || ""}`));
  });
  target.appendChild(risk);

  const strategic = el("article", "surface");
  strategic.appendChild(el("h3", "", "Strategic intelligence"));
  const ranking = data?.strategic?.ranking?.ranking || [];
  ranking.slice(0, 10).forEach((row) => {
    strategic.appendChild(el("p", "muted", row.rank ? `#${row.rank} ${row.venture_name} — score ${row.score} · coverage ${(row.coverage * 100).toFixed(0)}% · ${row.confidence} confidence` : `${row.venture_name} — unranked: ${row.reason}`));
  });
  const recommendations = data?.strategic?.recommendations || [];
  recommendations.slice(0, 6).forEach((row) => {
    strategic.appendChild(el("p", "muted", `${portfolioLabel(row.kind)} · ${row.venture_name}: ${row.recommended_action} (${row.confidence} confidence, approval required)`));
  });
  (data?.strategic?.pending_decisions || []).slice(0, 6).forEach((row) => {
    strategic.appendChild(el("p", "muted", `Pending decision: ${row.title} — ${portfolioLabel(row.kind)}`));
  });
  (data?.strategic?.scenarios || []).slice(-2).forEach((row) => {
    strategic.appendChild(el("p", "muted", `Scenario ${portfolioLabel(row.scenario_type)} (${row.kind}): ending cash ${portfolioMoney(row.projected?.ending_cash, row.currency)}`));
  });
  (data?.strategic?.reports || []).slice(-4).forEach((row) => {
    strategic.appendChild(el("p", "muted", `Report: ${row.title} · ${row.period?.start} to ${row.period?.end}`));
  });
  if (!ranking.length && !recommendations.length && !(data?.strategic?.reports || []).length) {
    strategic.appendChild(el("p", "muted", "Run Evaluate, Assess Risks, Strategic Advice or Generate Report to populate this section."));
  }
  target.appendChild(strategic);

  const coordination = el("article", "surface");
  coordination.appendChild(el("h3", "", "Coordination and access"));
  const responsibilities = data?.coordination?.responsibilities || {};
  Object.entries(responsibilities).forEach(([role, agent]) => coordination.appendChild(el("p", "muted", `${portfolioLabel(role)}: ${agent || "unassigned"}`)));
  coordination.appendChild(el("p", "muted", `${(data?.coordination?.dependencies || []).length} cross-venture dependency record(s); ${(data?.coordination?.dependency_cycles || []).length} cycle(s).`));
  coordination.appendChild(el("p", "muted", "Restricted venture data is omitted, not replaced with invented values."));
  target.appendChild(coordination);
}

setupPortfolioWorkspace();
