/* ── Phase 35: Business Opportunity & Validation workspace ──── */

function businessStageClass(stage) {
  if (stage === "validated" || stage === "conditionally_validated") return "ok";
  if (["not_viable", "rejected", "requires_further_research", "insufficient_evidence"].includes(stage)) return "warn";
  return "";
}

function businessNumber(value, digits = 2) {
  const number = Number(value);
  return Number.isFinite(number) ? number.toFixed(digits) : "Unavailable";
}

function businessActionButton(labelText, action, opportunityId, extra = {}) {
  const button = el("button", "cyber-btn small", labelText);
  button.type = "button";
  button.addEventListener("click", () => {
    if (action.indexOf("experiment-") === 0 && window.businessExperiment) {
      window.businessExperiment(action, opportunityId, extra);
    } else if (window.businessAction) {
      window.businessAction(action, opportunityId, extra);
    }
  });
  return button;
}

function renderBusinessList(target, data) {
  const rows = Array.isArray(data.opportunities) ? data.opportunities : [];
  const total = data.total ?? rows.length;
  target.appendChild(el("p", "summary", `${total} opportunit${total === 1 ? "y" : "ies"} recorded. Showing ${rows.length}.`));
  if (!rows.length) {
    target.appendChild(el("p", "muted", "No opportunity matches this filter. Record a sourced signal or brief above."));
    return;
  }
  rows.forEach((row) => {
    const card = el("article", "info-card");
    const head = el("div", "surface-head-row");
    head.appendChild(el("h4", "", row.title || "Opportunity"));
    head.appendChild(el("span", `pill ${businessStageClass(row.stage)}`, String(row.stage || "discovered").replace(/_/g, " ").toUpperCase()));
    card.appendChild(head);
    card.appendChild(el("p", "", row.customer_problem || ""));
    card.appendChild(el("p", "muted", `${row.industry || "industry not set"} · ${row.target_audience || "audience not set"} · ${row.evidence_count ?? 0} evidence record(s), ${row.source_count ?? 0} source(s)`));
    if (row.score) {
      card.appendChild(el("p", "muted", `Score ${businessNumber(row.score.weighted_score)} · coverage ${Math.round(Number(row.score.evidence_coverage || 0) * 100)}% · ${row.score.eligible ? "eligible" : "not eligible"}`));
    }
    if (row.verdict) card.appendChild(el("p", "muted", `Verdict: ${String(row.verdict).replace(/_/g, " ")}`));
    const open = el("button", "cyber-btn small", "Open workspace");
    open.type = "button";
    open.addEventListener("click", () => {
      if (window.loadBusinessOpportunity) window.loadBusinessOpportunity(row.id);
    });
    card.appendChild(open);
    target.appendChild(card);
  });
}

function renderBusinessScan(target, data) {
  const rows = Array.isArray(data.candidates) ? data.candidates : [];
  target.appendChild(el("p", "summary", `${rows.length} source-backed candidate(s). No opportunity was created automatically.`));
  (data.warnings || []).forEach((row) => target.appendChild(el("p", "muted", row)));
  if (!rows.length) {
    target.appendChild(el("p", "muted", data.note || "No usable sourced candidate was found."));
    return;
  }
  rows.forEach((row) => {
    const card = el("article", "info-card");
    card.appendChild(el("h4", "", row.title || row.source_url));
    card.appendChild(el("p", "muted", `${row.excerpt || "no excerpt"} · retrieved ${row.retrieved_at || "unknown"}`));
    const use = el("button", "cyber-btn small", "Use this source in the brief");
    use.type = "button";
    use.addEventListener("click", () => {
      if (byId("businessTitle")) byId("businessTitle").value = row.title || "";
      if (byId("businessSourceTitle")) byId("businessSourceTitle").value = row.title || "";
      if (byId("businessSourceUrl")) byId("businessSourceUrl").value = row.source_url || "";
      if (byId("businessSignal")) byId("businessSignal").value = row.signal_type || "market_trend";
      if (byId("businessProblem")) byId("businessProblem").focus();
    });
    card.appendChild(use);
    target.appendChild(card);
  });
  target.appendChild(el("p", "muted", data.note || ""));
}

function renderBusinessEvidence(target, evidence) {
  const rows = Array.isArray(evidence) ? evidence : [];
  target.appendChild(el("h3", "", `Evidence (${rows.length})`));
  target.appendChild(el("p", "muted", "Primary, secondary, estimate, synthesis and assumption are kept distinct. A stale source is labelled and cannot currently validate demand."));
  if (!rows.length) {
    target.appendChild(el("p", "muted", "No evidence has been recorded yet."));
    return;
  }
  rows.forEach((row) => {
    const card = el("article", "info-card");
    const head = el("div", "surface-head-row");
    head.appendChild(el("h4", "", `${String(row.kind || "other").replace(/_/g, " ")} — ${String(row.quality || "unknown").toUpperCase()}`));
    head.appendChild(el("span", `pill ${row.stale ? "warn" : row.quality === "primary" ? "ok" : ""}`, row.stale ? "STALE" : String(row.quality || "").toUpperCase()));
    card.appendChild(head);
    card.appendChild(el("p", "", row.claim || ""));
    const source = row.source || null;
    if (source) {
      card.appendChild(el("p", "muted", `${source.title || "source"} · ${source.source_type || "source"} · retrieved ${source.retrieved_at || "unknown"}`));
      if (source.url) {
        const link = el("a", "source-link", source.url);
        link.href = source.url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        card.appendChild(link);
      }
    } else {
      card.appendChild(el("p", "muted", "No source attached — this cannot count as measured evidence."));
    }
    card.appendChild(el("p", "muted", `Supplied by ${row.supplied_by || "not named"} · confidence ${businessNumber(row.confidence)}${row.notes ? ` · ${row.notes}` : ""}`));
    if (row.value && Object.keys(row.value).length) {
      card.appendChild(el("p", "muted", `Value: ${JSON.stringify(row.value)}`));
    }
    target.appendChild(card);
  });
}

function renderBusinessMarket(target, data) {
  const market = data.market;
  const customer = data.customer;
  const competition = data.competition;
  if (!market && !customer && !competition) return;
  target.appendChild(el("h3", "", "Market, customer and competition"));
  if (market) {
    const size = market.market_size || {};
    const card = el("article", "info-card");
    card.appendChild(el("h4", "", "Market research"));
    if (size.status === "unavailable" || size.value === null || size.value === undefined) {
      card.appendChild(el("p", "", "Market size: unavailable — no sourced figure was supplied."));
    } else {
      card.appendChild(el("p", "", `Market size: ${size.currency || ""} ${Number(size.value).toLocaleString()} · ${size.status}${size.year ? ` · ${size.year}` : ""}`));
    }
    (market.demand_indicators || []).slice(0, 6).forEach((row) => card.appendChild(el("p", "muted", `• ${row.text} (${row.quality || "unlabelled"}${row.stale ? ", stale" : ""})`)));
    (market.trends || []).slice(0, 4).forEach((row) => card.appendChild(el("p", "muted", `Trend: ${row.text} (${row.quality || "unlabelled"})`)));
    if (market.geography) card.appendChild(el("p", "muted", `Geography: ${market.geography}`));
    if (market.maturity) card.appendChild(el("p", "muted", `Maturity: ${market.maturity}`));
    card.appendChild(el("p", "muted", market.note || ""));
    target.appendChild(card);
  }
  if (customer) {
    const card = el("article", "info-card");
    const primaryLabel = customer.has_current_primary
      ? "current primary evidence"
      : customer.has_primary
        ? "stale primary evidence only"
        : "no primary evidence";
    card.appendChild(el("h4", "", `Customer validation — ${primaryLabel}`));
    card.appendChild(el("p", "", `${customer.interview_count || 0} interview(s), ${customer.feedback_count || 0} feedback record(s). ${customer.note || ""}`));
    (customer.severity || []).slice(0, 6).forEach((row) => card.appendChild(el("p", "muted", `• ${row.text} (${row.quality || "unlabelled"})`)));
    (customer.frequency || []).slice(0, 4).forEach((row) => card.appendChild(el("p", "muted", `• ${row.text} (${row.quality || "unlabelled"})`)));
    (customer.alternatives || []).slice(0, 4).forEach((row) => card.appendChild(el("p", "muted", `• ${row.text} (${row.quality || "unlabelled"})`)));
    (customer.willingness_to_pay || []).slice(0, 4).forEach((row) => card.appendChild(el("p", "muted", `WTP: ${row.text} (${row.quality || "unlabelled"})`)));
    (customer.unmet_demand || []).slice(0, 4).forEach((row) => card.appendChild(el("p", "muted", `Unmet demand: ${row.text} (${row.quality || "unlabelled"})`)));
    if (customer.gap) card.appendChild(el("p", "muted", customer.gap));
    target.appendChild(card);
  }
  if (competition) {
    const card = el("article", "info-card");
    card.appendChild(el("h4", "", `Competitor landscape — ${competition.search_complete ? "search recorded" : "INCOMPLETE"}`));
    card.appendChild(el("p", "muted", competition.note || ""));
    (competition.competitors || []).slice(0, 8).forEach((row) => {
      card.appendChild(el("p", "", `${row.name} — ${row.offering || ""} · pricing ${row.pricing || "unavailable"}${row.source_url ? " · sourced" : " · no URL"}${row.stale ? " · stale" : ""}`));
    });
    (competition.gaps || []).forEach((gap) => card.appendChild(el("p", "muted", gap)));
    target.appendChild(card);
  }
}

function renderBusinessFeasibility(target, feasibility) {
  if (!feasibility) return;
  const card = el("article", "info-card");
  card.appendChild(el("h4", "", `Feasibility — ${feasibility.complete ? "complete" : "incomplete"}`));
  card.appendChild(el("p", "muted", `Overall ${businessNumber(feasibility.overall_score)} · measured resources: ${feasibility.resource_snapshot?.available_ram_gb ?? "unavailable"} GB RAM, ${feasibility.resource_snapshot?.cpu_percent ?? "unavailable"}% CPU`));
  (feasibility.dimensions || []).forEach((row) => {
    card.appendChild(el("p", row.score === null || row.score === undefined ? "muted" : "", `${String(row.name).replace(/_/g, " ")}: ${row.score === null || row.score === undefined ? `unavailable — ${row.missing_reason || "no input"}` : businessNumber(row.score)}${row.rationale ? ` · ${row.rationale}` : ""}`));
  });
  (feasibility.blockers || []).forEach((row) => card.appendChild(el("p", "muted", `Blocker: ${row}`)));
  target.appendChild(card);
}

function renderBusinessFinancials(target, financials) {
  if (!financials) return;
  const card = el("article", "info-card");
  card.appendChild(el("h4", "", `Financial estimates (${financials.currency || "USD"}) — MODELLED`));
  (financials.scenarios || []).forEach((row) => {
    const line = el("p", row.calculated ? "" : "muted");
    if (!row.calculated) {
      line.textContent = `${row.name}: not calculated — missing ${(row.missing_inputs || []).join(", ") || "inputs"}.`;
    } else {
      line.textContent = `${row.name}: revenue ${businessNumber(row.monthly_revenue)} / month · gross ${businessNumber(row.monthly_gross_profit)} · net ${businessNumber(row.monthly_net_profit)} · margin ${row.contribution_margin_pct === null ? "unavailable" : `${Math.round(Number(row.contribution_margin_pct) * 100)}%`} · break-even ${businessNumber(row.break_even_units)} units · cash ${businessNumber(row.cash_required)}`;
    }
    card.appendChild(line);
    if (row.assumptions && Object.keys(row.assumptions).length) {
      card.appendChild(el("p", "muted", `Assumptions: ${JSON.stringify(row.assumptions)}`));
    }
  });
  card.appendChild(el("p", "muted", financials.assumptions_note || "Estimates, not actual revenue."));
  (financials.methodology || []).slice(0, 8).forEach((row) => card.appendChild(el("p", "muted", `Calculation: ${row}`)));
  target.appendChild(card);
}

function renderBusinessScore(target, score) {
  if (!score) return;
  const card = el("article", "info-card");
  card.appendChild(el("h4", "", `Opportunity score ${businessNumber(score.weighted_score)} / threshold ${businessNumber(score.threshold)}`));
  card.appendChild(el("p", "", `Evidence coverage ${Math.round(Number(score.evidence_coverage || 0) * 100)}% · ${score.eligible ? "eligible for a validated verdict" : "not eligible for a validated verdict"}.`));
  (score.dimensions || []).forEach((row) => {
    card.appendChild(el("p", row.score === null || row.score === undefined ? "muted" : "", `${String(row.name).replace(/_/g, " ")} (weight ${businessNumber(row.weight)}): ${row.score === null || row.score === undefined ? `unavailable — ${row.missing_reason || "no evidence"}` : businessNumber(row.score)}`));
  });
  (score.disqualifiers || []).forEach((row) => card.appendChild(el("p", "muted", `Disqualifier: ${row}`)));
  card.appendChild(el("p", "muted", score.methodology || ""));
  target.appendChild(card);
}

function renderBusinessExperiments(target, experiments) {
  if (!Array.isArray(experiments) || !experiments.length) return;
  target.appendChild(el("h3", "", "Validation experiments"));
  experiments.forEach((row) => {
    const card = el("article", "info-card");
    const head = el("div", "surface-head-row");
    head.appendChild(el("h4", "", row.title || "Experiment"));
    head.appendChild(el("span", `pill ${row.status === "approved" ? "ok" : row.status === "rejected" ? "warn" : ""}`, String(row.status || "proposed").toUpperCase()));
    card.appendChild(head);
    card.appendChild(el("p", "", row.hypothesis || ""));
    card.appendChild(el("p", "muted", `${row.objective || ""} · audience ${row.target_audience || "not set"} · ${row.duration_days ?? "?"} day(s) · cost ${row.estimated_cost === null ? "not estimated" : `${row.currency || ""} ${row.estimated_cost}`}`));
    if (row.required_resources && row.required_resources.length) card.appendChild(el("p", "muted", `Resources: ${row.required_resources.join("; ")}`));
    card.appendChild(el("p", "muted", `Success: ${(row.success_criteria || []).join("; ")}`));
    card.appendChild(el("p", "muted", `Failure: ${(row.failure_criteria || []).join("; ")}`));
    card.appendChild(el("p", "muted", `Measure: ${row.measurement_method || "not stated"} · approval ${row.requires_approval ? "required" : "not required"}`));
    if (row.expected_evidence && row.expected_evidence.length) card.appendChild(el("p", "muted", `Expected evidence: ${row.expected_evidence.join("; ")}`));
    if (row.outcome) card.appendChild(el("p", "muted", `Recorded outcome: ${row.outcome}`));
    const actions = el("div", "button-row");
    const opportunityId = target.dataset.opportunityId || "";
    if (row.status === "proposed" && row.requires_approval) {
      actions.appendChild(businessActionButton("Approve experiment", "experiment-approve", opportunityId, { experimentId: row.id, approved: true }));
      actions.appendChild(businessActionButton("Deny experiment", "experiment-deny", opportunityId, { experimentId: row.id, approved: false }));
    } else if (row.status === "proposed") {
      actions.appendChild(businessActionButton("Record outcome", "experiment-outcome", opportunityId, { experimentId: row.id }));
    }
    if (row.status === "approved") {
      actions.appendChild(businessActionButton("Record outcome", "experiment-outcome", opportunityId, { experimentId: row.id }));
    }
    if (actions.childNodes.length) card.appendChild(actions);
    card.appendChild(el("p", "muted", "No external action is executed by this control; it records an operator decision only."));
    target.appendChild(card);
  });
}

function renderBusinessReport(target, report) {
  if (!report) return;
  target.appendChild(el("h3", "", `Validation report — ${String(report.verdict || "").replace(/_/g, " ")}`));
  target.appendChild(el("p", "muted", `${report.rationale || ""} Sources ${report.source_count ?? 0}, primary ${report.primary_count ?? 0}, stale ${report.stale_count ?? 0}; version ${report.version ?? 1}.`));
  (report.sections || []).forEach((section) => {
    const card = el("article", "info-card");
    card.appendChild(el("h4", "", section.title || section.key));
    card.appendChild(el("p", businessSectionClass(section.status), section.body || ""));
    if (section.status && section.status !== "reported") card.appendChild(el("p", "muted", `Status: ${section.status}`));
    target.appendChild(card);
  });
  const limits = el("article", "info-card");
  limits.appendChild(el("h4", "", "Limitations"));
  (report.limitations || []).forEach((row) => limits.appendChild(el("p", "muted", row)));
  target.appendChild(limits);
}

function businessSectionClass(status) {
  if (["assumption", "incomplete", "unavailable"].includes(status)) return "muted";
  return "";
}

function renderBusinessDetail(target, data) {
  target.dataset.opportunityId = data.id || "";
  const head = el("div", "surface-head-row");
  head.appendChild(el("h3", "", data.title || "Opportunity"));
  head.appendChild(el("span", `pill ${businessStageClass(data.stage)}`, String(data.stage || "discovered").replace(/_/g, " ").toUpperCase()));
  target.appendChild(head);
  target.appendChild(el("p", "muted", `${data.industry || "industry not set"} · ${data.category || "category not set"} · discovered ${data.discovered_at || ""} · ${data.source_count ?? 0} source(s) · ${data.primary_evidence_count ?? 0} primary record(s)`));
  target.appendChild(el("p", "", data.description || data.proposed_solution || ""));
  target.appendChild(el("p", "muted", `Customer problem: ${data.customer_problem || "not stated"}`));
  target.appendChild(el("p", "muted", `Target audience: ${data.target_audience || "not stated"}`));

  if (data.verdict) {
    const verdict = el("article", "info-card");
    verdict.appendChild(el("h4", "", `Verdict: ${String(data.verdict).replace(/_/g, " ")}`));
    verdict.appendChild(el("p", "", data.verdict_rationale || ""));
    target.appendChild(verdict);
  } else {
    target.appendChild(el("p", "muted", "No validation verdict has been derived yet. A score alone cannot validate this opportunity."));
  }

  const transitions = Array.isArray(data.available_transitions) ? data.available_transitions : [];
  const locked = ["validated", "not_viable", "rejected", "archived"].includes(data.stage);
  const paused = data.stage === "paused";
  const canAssess = !paused && !locked && data.stage !== "validation";
  const canEvaluate = !paused && !locked;
  const actions = el("div", "button-row");
  if (canAssess) {
    actions.appendChild(businessActionButton("Research with Explore", "research", data.id));
    actions.appendChild(businessActionButton("Run assessment", "assess", data.id));
    actions.appendChild(businessActionButton("Plan experiments", "plan", data.id));
  }
  if (canEvaluate) actions.appendChild(businessActionButton("Evaluate & report", "evaluate", data.id));
  if (paused) actions.appendChild(businessActionButton("Resume", "resume", data.id));
  else if (!locked) actions.appendChild(businessActionButton("Pause", "pause", data.id));
  if (transitions.includes("rejected")) actions.appendChild(businessActionButton("Reject", "reject", data.id));
  if (transitions.includes("archived")) actions.appendChild(businessActionButton("Archive", "archive", data.id));
  if (actions.childNodes.length) target.appendChild(actions);
  target.appendChild(el("p", "muted", "External experiments, customer contact and spending are never executed automatically; approvals are recorded and the operator runs them separately."));

  renderBusinessMarket(target, data);
  renderBusinessFeasibility(target, data.feasibility);
  renderBusinessFinancials(target, data.financials);
  renderBusinessScore(target, data.score);
  renderBusinessExperiments(target, data.experiments);
  renderBusinessReport(target, data.report);
  renderBusinessEvidence(target, data.evidence);

  if (Array.isArray(data.history) && data.history.length) {
    target.appendChild(el("h3", "", "Activity history"));
    const list = el("div", "health-list");
    data.history.forEach((row) => {
      const item = el("div", "list-item");
      item.appendChild(el("strong", "", `${row.from_stage || "start"} → ${row.to_stage || "?"}`));
      item.appendChild(el("p", "muted", `${row.at || ""} · ${row.actor || "operator"}${row.reason ? ` · ${row.reason}` : ""}`));
      list.appendChild(item);
    });
    target.appendChild(list);
  }
}

function renderBusinessOpportunities(target, data) {
  if (!data) {
    target.appendChild(el("p", "muted", "No result was returned."));
    return;
  }
  if (Array.isArray(data.candidates)) {
    renderBusinessScan(target, data);
    return;
  }
  if (data.id && (Array.isArray(data.evidence) || data.customer_problem)) {
    renderBusinessDetail(target, data);
    return;
  }
  renderBusinessList(target, data);
}
