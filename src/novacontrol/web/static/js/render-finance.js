/* ── Phase 38: Business Operations & Finance workspace ───────── */

let financeSelectedId = "";
let financeLastDashboard = null;

function financeLabel(value) {
  return String(value || "").replace(/_/g, " ").toLowerCase().replace(/\b\w/g, (c) => c.toUpperCase());
}

function financeValue(id) {
  return byId(id) ? byId(id).value.trim() : "";
}

function financeNumber(id, fallback = 0) {
  const raw = financeValue(id);
  if (!raw) return fallback;
  const value = Number(raw);
  if (!Number.isFinite(value)) throw new Error(`${id} must be a number`);
  return value;
}

function financeJson(id, label) {
  const raw = financeValue(id);
  if (!raw) return null;
  try {
    const value = JSON.parse(raw);
    if (value === null || typeof value !== "object") throw new Error(`${label} must be JSON`);
    return value;
  } catch (error) {
    throw new Error(`${label}: ${error.message || error}`);
  }
}

function financeMoney(value, currency) {
  const amount = Number(value || 0);
  return `${currency || ""} ${amount.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`.trim();
}

function financeMetric(block, currency) {
  if (!block || typeof block !== "object") return "not recorded";
  if (block.available) {
    const value = Number(block.value);
    if (!Number.isFinite(value)) return "available";
    return block.kind === "estimate" ? `${value.toLocaleString(undefined, { maximumFractionDigits: 2 })} (estimate)` : value.toLocaleString(undefined, { maximumFractionDigits: 4 });
  }
  return `NOT CALCULABLE — ${block.reason || "inputs unavailable"}`;
}

function financeCard(target, title, note) {
  const card = el("article", "info-card");
  card.appendChild(el("h3", "", title));
  if (note) card.appendChild(el("p", "muted", note));
  target.appendChild(card);
  return card;
}

function financeRow(title, status, lines) {
  const row = el("article", "info-card");
  const head = el("div", "surface-head-row");
  head.appendChild(el("h4", "", title || "Record"));
  if (status) head.appendChild(el("span", "pill", financeLabel(status)));
  row.appendChild(head);
  (lines || []).filter(Boolean).forEach((line) => row.appendChild(el("p", "muted", String(line))));
  return row;
}

async function refreshFinanceStatus() {
  const data = await requestJson("/finance/status");
  byId("financeStatus").textContent =
    `${data.ventures ?? 0} venture(s) · ${data.journal_entries ?? 0} journal entr${data.journal_entries === 1 ? "y" : "ies"} · ` +
    `${data.revenue_records ?? 0} revenue record(s) · ${data.expense_records ?? 0} expense record(s) · ${data.open_alerts ?? 0} open alert(s) · ` +
    `Phase 37 reader ${data.configured?.customer_reader ? "connected" : "unavailable"} · payments ${data.boundaries?.payments ? "enabled" : "disabled"}`;
  return data;
}

async function refreshFinanceVentures() {
  const data = await requestJson("/finance/ventures?limit=100");
  if (byId("financeVentureList")) render("financeVentureList", data, "financeList");
  return data;
}

async function loadFinanceVenture(id) {
  if (!id) return null;
  financeSelectedId = id;
  const detail = await requestJson(`/finance/ventures/${encodeURIComponent(id)}`);
  render("financeDetail", detail, "financeDetail");
  const dashboard = await requestJson(`/finance/ventures/${encodeURIComponent(id)}/dashboard`);
  financeLastDashboard = dashboard;
  render("financeDashboard", dashboard, "financeDashboard");
  renderFinanceRecordLists(detail, dashboard);
  return { detail, dashboard };
}

function renderFinanceRecordLists(detail, dashboard) {
  const venture = detail?.venture || {};
  if (byId("financeRevenueList")) {
    const target = byId("financeRevenueList");
    clearNode(target);
    const rows = venture.revenue_records || [];
    target.appendChild(el("p", "muted", `${rows.length} revenue record(s). Booked, invoiced, recognized and collected are separate observations.`));
    rows.slice(-12).reverse().forEach((row) => {
      target.appendChild(financeRow(
        `${financeMoney(row.reporting_net, row.currency)} · ${financeLabel(row.stage)}`,
        row.stage,
        [`Product ${row.product_ref || "unassigned"} · customer ${row.customer_ref || "unassigned"}`, `Booked ${row.booked_date || "—"} · invoiced ${row.invoice_date || "—"} · recognized ${row.recognition_date || "—"} · collected ${row.collection_date || "—"}`, row.source_ref ? `Source ${row.source} / ${row.source_ref}` : `Source ${row.source}`],
      ));
    });
  }
  if (byId("financeExpenseList")) {
    const target = byId("financeExpenseList");
    clearNode(target);
    const rows = venture.expense_records || [];
    target.appendChild(el("p", "muted", `${rows.length} expense record(s). Only approved actuals become actuals in the ledger.`));
    rows.slice(-12).reverse().forEach((row) => {
      target.appendChild(financeRow(
        `${financeMoney(row.reporting_amount, venture.reporting_currency)} · ${financeLabel(row.category)}`,
        `${row.status} / ${row.approval_status}`,
        [`${row.vendor || "vendor unrecorded"} · payment ${row.payment_status} · ${row.transaction_date}`, row.supporting_reference ? `Support: ${row.supporting_reference}` : "No supporting reference recorded"],
      ));
    });
  }
  if (byId("financeJournalList")) {
    const target = byId("financeJournalList");
    clearNode(target);
    const entries = (venture.journal_entries || []).slice(-10).reverse();
    target.appendChild(el("p", "muted", `${(venture.journal_entries || []).length} append-only journal entr${(venture.journal_entries || []).length === 1 ? "y" : "ies"}. Reversals are new entries.`));
    entries.forEach((entry) => {
      const lines = (entry.lines || []).map((line) => `${line.account_code} Dr ${line.debit} Cr ${line.credit}`).join(" · ");
      target.appendChild(financeRow(entry.memo || entry.id, entry.reverses ? "reversal" : "posted", [`${entry.date} · ${entry.id} · ${lines}`, entry.reverses ? `Reverses ${entry.reverses}` : `Source ${entry.source}`]));
    });
  }
  if (byId("financeBudgetList")) {
    const target = byId("financeBudgetList");
    clearNode(target);
    (dashboard?.budgets || []).forEach((budget) => {
      target.appendChild(financeRow(
        `${budget.name || budget.budget_id} · ${financeMoney(budget.total_actual)} / ${financeMoney(budget.total_budget)}`,
        budget.status,
        [`${budget.period?.start || ""} → ${budget.period?.end || ""} · variance ${financeMoney(budget.total_variance)}`, budget.available ? "Approved budget" : "Draft/rejected: comparison only, not authorized spending"],
      ));
    });
  }
  if (byId("financeForecast")) {
    const target = byId("financeForecast");
    clearNode(target);
    const forecast = dashboard?.forecast || {};
    if (!forecast.id) {
      target.appendChild(el("p", "muted", "No forecast has been generated."));
    } else {
      target.appendChild(el("p", "", `${financeLabel(forecast.scenario)} · ${forecast.horizon_months} months · ${forecast.available ? "calculated" : "NOT CALCULABLE"}`));
      target.appendChild(el("p", "muted", `Confidence ${forecast.confidence || "not stated"} · ${forecast.confidence_note || ""}`));
      target.appendChild(el("p", "muted", `${(forecast.assumptions?.starting_cash !== undefined) ? `Starting cash ${financeMoney(forecast.assumptions.starting_cash)}. ` : ""}${(forecast.limitations || []).join("; ")}`));
      if (forecast.totals) target.appendChild(el("p", "", `Forecast revenue ${financeMoney(forecast.totals.forecast_revenue)} · expenses ${financeMoney(forecast.totals.forecast_expenses)} · ending cash ${financeMoney(forecast.totals.ending_cash)} · runway ${forecast.totals.cash_runway_months ?? "no exhaustion in horizon"} month(s)`));
    }
  }
  if (byId("financeAlerts")) {
    const target = byId("financeAlerts");
    clearNode(target);
    const alerts = dashboard?.open_alerts || [];
    target.appendChild(el("p", "muted", `${alerts.length} open business-health alert(s).`));
    alerts.forEach((alert) => {
      target.appendChild(financeRow(`${financeLabel(alert.metric)} · ${alert.severity}`, alert.status, [`Observed ${JSON.stringify(alert.observed_value)} · threshold ${JSON.stringify(alert.threshold)}`, ...(alert.evidence || []), `Investigate: ${alert.recommended_investigation || ""}`, `At ${alert.timestamp}`]));
    });
  }
  if (byId("financeReports")) {
    const target = byId("financeReports");
    clearNode(target);
    const reports = (detail?.venture?.management_reports || []).slice(-5).reverse();
    target.appendChild(el("p", "muted", `${(detail?.venture?.management_reports || []).length} management report(s).`));
    reports.forEach((report) => target.appendChild(financeRow(report.title, "report", [`${report.period?.start} → ${report.period?.end}`, report.executive_summary])));
  }
}

function renderFinanceList(target, data) {
  const rows = Array.isArray(data.ventures) ? data.ventures : [];
  target.appendChild(el("p", "summary", `${data.total ?? rows.length} isolated venture(s).`));
  if (!rows.length) {
    target.appendChild(el("p", "muted", "No venture exists. Create one to open its isolated ledger."));
    return;
  }
  rows.forEach((row) => {
    const card = el("article", "info-card");
    const head = el("div", "surface-head-row");
    head.appendChild(el("h4", "", row.name || row.id));
    head.appendChild(el("span", `pill ${row.open_alerts ? "warn" : "ok"}`, row.open_alerts ? `${row.open_alerts} alert(s)` : financeLabel(row.operating_status)));
    card.appendChild(head);
    card.appendChild(el("p", "muted", `${financeLabel(row.business_model)} · ${row.reporting_currency} · recognized ${financeMoney(row.recognized_revenue, row.reporting_currency)} · cash ${financeMoney(row.cash_balance, row.reporting_currency)}`));
    const open = el("button", "cyber-btn small", "Open venture");
    open.type = "button";
    open.addEventListener("click", () => loadFinanceVenture(row.id).catch((error) => showToast(String(error.message || error))));
    card.appendChild(open);
    target.appendChild(card);
  });
}

function renderFinanceDetail(target, data) {
  const venture = data.venture || {};
  const summary = data.summary || {};
  const operations = data.operations || {};
  const head = el("div", "surface-head-row");
  head.appendChild(el("h3", "", venture.name || summary.name || "Venture"));
  head.appendChild(el("span", "pill", `${venture.reporting_currency || ""} · ${financeLabel(venture.operating_status)}`));
  target.appendChild(head);
  target.appendChild(el("p", "muted", `${financeLabel(venture.business_model)} · ${venturesafe(venture.description)}`));
  target.appendChild(el("p", "", `Recognized revenue ${financeMoney(summary.recognized_revenue, venture.reporting_currency)} · cash ${financeMoney(summary.cash_balance, venture.reporting_currency)} · open alerts ${summary.open_alerts ?? 0}`));
  const ops = financeCard(target, "Operational performance", operations.note || "");
  ops.appendChild(el("p", "", `${operations.completed ?? 0}/${operations.tasks ?? 0} task(s) complete · ${operations.blocked ?? 0} blocked · ${operations.overdue ?? 0} overdue · ${operations.open_risks ?? 0} open risk(s)`));
  ops.appendChild(el("p", "muted", `Completion rate ${financeMetric(operations.completion_rate)} · delivery ${financeMetric(operations.average_delivery_days)} day(s) · utilization ${financeMetric(operations.average_resource_utilization)}`));
  ops.appendChild(el("p", "muted", `Operational cost ${financeMoney(operations.operational_cost, venture.reporting_currency)} · objectives ${operations.objectives ?? 0} · milestones ${operations.milestones ?? 0} · workflows ${operations.workflows ?? 0}`));
  const links = financeCard(target, "Read-only Phase 35/36/37 links", data.links?.note || "");
  (data.links?.products || []).forEach((row) => links.appendChild(el("p", "muted", `${row.product_id}: ${row.available ? `${row.name} (${financeLabel(row.state)})` : row.reason}`)));
  (data.links?.customer_references || []).forEach((row) => links.appendChild(el("p", "muted", `Customer reference ${row.customer_ref} · ${row.reader}`)));
  if (venture.authorization?.owner) {
    const members = financeCard(target, "Authorization", "Role checks are enforced by the service; the configured API principal is the acting identity.");
    members.appendChild(el("p", "muted", `Owner ${venture.authorization.owner} · ${(venture.authorization.members || []).length} member(s)`));
  }
  const notes = financeCard(target, "Financial boundaries", "");
  notes.appendChild(el("p", "muted", "Payments, purchases, bank changes and external financial commitments are disabled. Finalized journal entries are never edited; corrections are reversals."));
}

function venturesafe(value) {
  return String(value || "").trim() || "No description recorded.";
}

function renderFinanceDashboard(target, data) {
  const venture = data.venture || {};
  const currency = venture.reporting_currency || data.revenue?.currency || "";
  const revenue = data.revenue || {};
  const expenses = data.expenses || {};
  const cash = data.cash_flow || {};
  const capital = data.working_capital || {};
  const profit = data.profitability || {};
  const health = data.health || {};
  target.appendChild(el("p", "muted", `${financeLabel(venture.business_model)} · ${currency} · ${financeLabel(venture.operating_status)} · actual, estimated, projected and forecast values are labelled separately.`));

  const healthCard = financeCard(target, "Business health", data.legend ? "Threshold crossings only; missing or stale data produces no alert." : "");
  healthCard.appendChild(el("p", "", `${(data.open_alerts || []).length} open alert(s). Latest health snapshot ${health.at || "not refreshed"}.`));
  if (health.data_fresh === false) healthCard.appendChild(el("p", "warn", health.freshness?.reason || "Source data is stale."));
  (health.metrics ? Object.entries(health.metrics) : []).forEach(([key, value]) => healthCard.appendChild(el("p", "muted", `${financeLabel(key)}: ${typeof value === "number" ? value.toLocaleString(undefined, { maximumFractionDigits: 4 }) : value}`)));

  const revenueCard = financeCard(target, "Revenue intelligence", revenue.note || "");
  revenueCard.appendChild(el("p", "", `Booked ${financeMoney(revenue.booked_revenue, currency)} · invoiced ${financeMoney(revenue.invoiced_revenue, currency)} · recognized ${financeMoney(revenue.recognized_revenue, currency)} · collected ${financeMoney(revenue.collected_cash, currency)}`));
  revenueCard.appendChild(el("p", "muted", `Deferred ${financeMoney(revenue.deferred_revenue, currency)} · outstanding invoices ${financeMoney(revenue.outstanding_invoices, currency)} · overdue ${financeMoney(revenue.overdue_receivables, currency)} · refunds ${financeMoney(revenue.refunds, currency)} · tax ${financeMoney(revenue.taxes, currency)}`));
  revenueCard.appendChild(el("p", "muted", `Recurring recognized ${financeMoney(revenue.recurring_recognized_revenue, currency)} · ${revenue.counts?.recognized ?? 0} recognized record(s)`));

  const expenseCard = financeCard(target, "Expenses", expenses.note || "");
  expenseCard.appendChild(el("p", "", `Actual ${financeMoney(expenses.actual, currency)} · unapproved actual ${financeMoney(expenses.unapproved_actual, currency)}`));
  expenseCard.appendChild(el("p", "muted", `Estimated ${financeMoney(expenses.estimated, currency)} · committed ${financeMoney(expenses.committed, currency)} · projected ${financeMoney(expenses.projected, currency)}`));
  (expenses.by_category || []).slice(0, 8).forEach((row) => expenseCard.appendChild(el("p", "muted", `${financeLabel(row.category)}: ${financeMoney(row.actual, currency)}`)));

  const cashCard = financeCard(target, "Cash flow, working capital and runway", capital.method_note || "");
  cashCard.appendChild(el("p", "", `Operating ${financeMoney(cash.operating, currency)} · investing ${financeMoney(cash.investing, currency)} · financing ${financeMoney(cash.financing, currency)} · net ${financeMoney(cash.net_change, currency)} · closing cash ${financeMoney(cash.closing_cash, currency)}`));
  cashCard.appendChild(el("p", "", `Current assets ${financeMoney(capital.current_assets, currency)} · current liabilities ${financeMoney(capital.current_liabilities, currency)} · working capital ${financeMoney(capital.working_capital, currency)}`));
  cashCard.appendChild(el("p", "muted", `Current ratio ${financeMetric(capital.current_ratio)} · quick ratio ${financeMetric(capital.quick_ratio)} · DSO ${financeMetric(capital.days_sales_outstanding)} · DPO ${financeMetric(capital.days_payable_outstanding)} · CCC ${financeMetric(capital.cash_conversion_cycle)}`));
  cashCard.appendChild(el("p", "muted", `Receivables turnover ${financeMetric(capital.receivables_turnover)} · payables turnover ${financeMetric(capital.payables_turnover)} · inventory turnover ${financeMetric(capital.inventory_turnover)} · runway ${financeMetric(capital.cash_runway_months)} month(s)`));

  const profitCard = financeCard(target, "Profitability and unit economics", profit.contribution_note || "");
  profitCard.appendChild(el("p", "", `Revenue ${financeMoney(profit.revenue, currency)} · gross ${financeMoney(profit.gross_profit, currency)} · operating ${financeMoney(profit.operating_profit, currency)} · net ${financeMoney(profit.net_profit, currency)}`));
  profitCard.appendChild(el("p", "muted", `Gross margin ${profit.gross_margin ?? "not calculable"} · operating margin ${profit.operating_margin ?? "not calculable"} · net margin ${profit.net_margin ?? "not calculable"}`));
  profitCard.appendChild(el("p", "muted", `CAC ${financeMetric(profit.customer_acquisition_cost, currency)} · LTV ${financeMetric(profit.customer_lifetime_value, currency)} · payback ${financeMetric(profit.customer_payback_months)} month(s) · ARPC ${financeMetric(profit.average_revenue_per_customer, currency)}`));
  (profit.revenue_per_product || []).slice(0, 8).forEach((row) => profitCard.appendChild(el("p", "muted", `${row.product_ref}: revenue ${financeMoney(row.recognized_revenue, currency)} · contribution ${financeMoney(row.contribution, currency)}`)));

  const statementCard = financeCard(target, "Financial statements", "Statements come from posted entries only; incomplete data is explicitly unavailable.");
  const income = data.income_statement || {};
  const balance = data.balance_sheet || {};
  statementCard.appendChild(el("p", "muted", `Income statement: ${income.available ? `revenue ${financeMoney(income.revenue, currency)} · net profit ${financeMoney(income.net_profit, currency)}` : income.reason || "unavailable"}`));
  statementCard.appendChild(el("p", "muted", `Balance sheet: ${balance.available ? `assets ${financeMoney(balance.total_assets, currency)} · liabilities ${financeMoney(balance.total_liabilities, currency)} · equity ${financeMoney(balance.total_equity, currency)} · balanced ${balance.balanced ? "yes" : "no"}` : balance.reason || "unavailable"}`));
  statementCard.appendChild(el("p", "muted", `Trial balance: ${data.trial_balance?.balanced ? "balanced" : "not available or unbalanced"} · difference ${financeMoney(data.trial_balance?.difference, currency)}`));

  const seriesCard = financeCard(target, "Six-month observations", "Months with no record are not filled with zero.");
  (data.monthly_series || []).forEach((row) => seriesCard.appendChild(el("p", "muted", `${row.month}: revenue ${row.revenue_observed ? financeMoney(row.recognized_revenue, currency) : "not observed"} · expenses ${row.expense_observed ? financeMoney(row.actual_expenses, currency) : "not observed"}`)));

  const budgetsCard = financeCard(target, "Budgets", "Approved budgets authorize comparison, not spending.");
  (data.budgets || []).forEach((budget) => budgetsCard.appendChild(el("p", "muted", `${budget.name || budget.budget_id} · ${budget.status} · actual ${financeMoney(budget.total_actual, currency)} / ${financeMoney(budget.total_budget, currency)} · variance ${financeMoney(budget.total_variance, currency)}`)));

  const legend = financeCard(target, "Actual / estimate / projection / forecast", "Values carry their kind in the API; this legend prevents visual conflation.");
  (data.legend ? Object.entries(data.legend) : []).forEach(([kind, meaning]) => legend.appendChild(el("p", "muted", `${financeLabel(kind)}: ${meaning}`)));
}

async function financeRefreshAfterMutation() {
  await refreshFinanceStatus();
  await refreshFinanceVentures();
  if (!financeSelectedId) return null;
  const result = await loadFinanceVenture(financeSelectedId);
  return result.dashboard;
}

function financeAction(buttonId, loadingText, task) {
  const selected = financeSelectedId;
  if (!selected) {
    showToast("Select a venture first");
    return;
  }
  run("financeDashboard", "financeDashboard", task, buttonId || null, { loadingText });
}

function financePath(suffix) {
  return `/finance/ventures/${encodeURIComponent(financeSelectedId)}${suffix || ""}`;
}

function setupFinanceWorkspace() {
  if (!byId("financeRefreshButton")) return;
  byId("financeRefreshButton").addEventListener("click", () =>
    run("financeDashboard", "financeDashboard", async () => {
      await refreshFinanceStatus();
      await refreshFinanceVentures();
      if (financeSelectedId) await loadFinanceVenture(financeSelectedId);
      return financeLastDashboard || { summary: "Select a venture" };
    }, "financeRefreshButton")
  );
  byId("financeVentureCreateButton").addEventListener("click", () => {
    const payload = {
      name: financeValue("financeVentureName"),
      business_model: financeValue("financeVentureModel") || "other",
      reporting_currency: financeValue("financeVentureCurrency") || "USD",
      description: financeValue("financeVentureDescription"),
      products_services: financeValue("financeVentureProducts").split(/\r?\n/).map((row) => row.trim()).filter(Boolean),
      linked_products: financeValue("financeVentureProductIds").split(/\r?\n/).map((row) => row.trim()).filter(Boolean),
      customer_refs: financeValue("financeVentureCustomerRefs").split(/\r?\n/).map((row) => row.trim()).filter(Boolean),
    };
    run("financeDashboard", "financeDashboard", async () => {
      const created = await requestJson("/finance/ventures", payload);
      financeSelectedId = created.id;
      await refreshFinanceStatus();
      await refreshFinanceVentures();
      const result = await loadFinanceVenture(created.id);
      return result.dashboard;
    }, "financeVentureCreateButton", { loadingText: "Creating an isolated venture and chart of accounts…" });
  });
  byId("financeRevenueButton").addEventListener("click", () => financeAction("financeRevenueButton", "Recording revenue at its explicit stage…", async () => {
    const payload = {
      gross_amount: financeNumber("financeRevenueAmount"),
      tax: financeNumber("financeRevenueTax", 0),
      currency: financeValue("financeRevenueCurrency"),
      stage: financeValue("financeRevenueStage") || "booked",
      transaction_date: financeValue("financeRevenueDate"),
      due_date: financeValue("financeRevenueDue"),
      product_ref: financeValue("financeRevenueProduct"),
      customer_ref: financeValue("financeRevenueCustomer"),
      invoice_number: financeValue("financeRevenueInvoice"),
      recurring: byId("financeRevenueRecurring").checked,
      source: "operator_entry",
    };
    const fx = financeValue("financeRevenueFx");
    if (fx) {
      payload.fx_rate_to_reporting = Number(fx);
      payload.fx_rate_source = financeValue("financeRevenueFxSource");
    }
    await requestJson(financePath("/revenue"), payload);
    return financeRefreshAfterMutation();
  }));
  byId("financeExpenseButton").addEventListener("click", () => financeAction("financeExpenseButton", "Recording an expense without posting an unapproved actual…", async () => {
    const status = financeValue("financeExpenseStatus") || "actual";
    await requestJson(financePath("/expenses"), {
      amount: financeNumber("financeExpenseAmount"),
      category: financeValue("financeExpenseCategory") || "other",
      status,
      payment_status: status === "actual" ? financeValue("financeExpensePayment") || "unpaid" : "not_applicable",
      transaction_date: financeValue("financeExpenseDate"),
      vendor: financeValue("financeExpenseVendor"),
      product_ref: financeValue("financeExpenseProduct"),
      supporting_reference: financeValue("financeExpenseSource"),
      source: "operator_entry",
    });
    return financeRefreshAfterMutation();
  }));
  byId("financeExpenseApproveButton").addEventListener("click", () => financeAction("financeExpenseApproveButton", "Approving an actual expense and posting its accrual…", async () => {
    const id = financeValue("financeExpenseApproveId");
    if (!id) throw new Error("Enter an expense ID to approve");
    await requestJson(financePath(`/expenses/${encodeURIComponent(id)}/approval`), { approved: true, note: "reviewed in Finance workspace" });
    return financeRefreshAfterMutation();
  }));
  byId("financeExpensePayButton").addEventListener("click", () => financeAction("financeExpensePayButton", "Recording a payment; no money is moved…", async () => {
    const id = financeValue("financeExpenseApproveId");
    if (!id) throw new Error("Enter an approved expense ID");
    await requestJson(financePath(`/expenses/${encodeURIComponent(id)}/payment`), {});
    return financeRefreshAfterMutation();
  }));
  byId("financeJournalButton").addEventListener("click", () => financeAction("financeJournalButton", "Validating and posting a balanced entry…", async () => {
    const lines = financeJson("financeJournalLines", "Journal lines");
    await requestJson(financePath("/journal"), {
      date: financeValue("financeJournalDate"),
      memo: financeValue("financeJournalMemo"),
      lines: lines || [],
      cash_flow_category: financeValue("financeJournalCashFlow") || "operating",
      source: "manual_journal",
    });
    return financeRefreshAfterMutation();
  }));
  byId("financeJournalReverseButton").addEventListener("click", () => {
    const id = financeValue("financeJournalReverseId");
    if (!id) { showToast("Enter a journal ID to reverse"); return; }
    const reason = window.prompt("Reason for the reversal (required):");
    if (!reason) return;
    financeAction("financeJournalReverseButton", "Posting a reversal; the original stays intact…", async () => {
      await requestJson(financePath(`/journal/${encodeURIComponent(id)}/reverse`), { reason });
      return financeRefreshAfterMutation();
    });
  });
  byId("financePeriodCloseButton").addEventListener("click", () => financeAction("financePeriodCloseButton", "Closing the period and snapshotting its statements…", async () => {
    await requestJson(financePath("/periods/close"), { start: financeValue("financePeriodStart"), end: financeValue("financePeriodEnd") });
    return financeRefreshAfterMutation();
  }));
  byId("financeBudgetButton").addEventListener("click", () => financeAction("financeBudgetButton", "Creating a draft budget…", async () => {
    const lines = financeJson("financeBudgetLines", "Budget lines");
    await requestJson(financePath("/budgets"), {
      name: financeValue("financeBudgetName"),
      period_start: financeValue("financeBudgetStart"),
      period_end: financeValue("financeBudgetEnd"),
      scope: "venture",
      lines: lines || [],
    });
    return financeRefreshAfterMutation();
  }));
  byId("financeBudgetApproveButton").addEventListener("click", () => {
    const id = financeValue("financeBudgetApproveId");
    if (!id) { showToast("Enter a budget ID"); return; }
    financeAction("financeBudgetApproveButton", "Approving a budget with a note…", async () => {
      await requestJson(financePath(`/budgets/${encodeURIComponent(id)}/approval`), { approved: true, note: "approved in Finance workspace" });
      return financeRefreshAfterMutation();
    });
  });
  byId("financeBudgetReportButton").addEventListener("click", () => {
    const id = financeValue("financeBudgetApproveId");
    if (!id) { showToast("Enter a budget ID"); return; }
    run("financeBudgetList", "financeList", async () => requestJson(financePath(`/budgets/${encodeURIComponent(id)}/report`)), "financeBudgetReportButton", { loadingText: "Comparing approved actuals with the budget…" });
  });
  byId("financeForecastButton").addEventListener("click", () => financeAction("financeForecastButton", "Building an assumption-explicit forecast…", async () => {
    const assumptions = financeJson("financeForecastAssumptions", "Forecast assumptions") || {};
    await requestJson(financePath("/forecasts"), {
      horizon_months: financeNumber("financeForecastHorizon", 6),
      scenario: financeValue("financeForecastScenario") || "base",
      assumptions,
    });
    return financeRefreshAfterMutation();
  }));
  byId("financeScenarioButton").addEventListener("click", () => financeAction("financeScenarioButton", "Comparing four scenarios without claiming accuracy…", async () => {
    const assumptions = financeJson("financeForecastAssumptions", "Scenario assumptions") || {};
    await requestJson(financePath("/scenarios"), {
      horizon_months: financeNumber("financeForecastHorizon", 6),
      assumptions,
      custom: assumptions,
    });
    return financeRefreshAfterMutation();
  }));
  byId("financeHealthButton").addEventListener("click", () => financeAction("financeHealthButton", "Checking configured thresholds against fresh observations…", async () => {
    await requestJson(financePath("/health/refresh"), {});
    return financeRefreshAfterMutation();
  }));
  byId("financeReportButton").addEventListener("click", () => financeAction("financeReportButton", "Generating a grounded management report…", async () => {
    await requestJson(financePath("/reports"), {
      title: financeValue("financeReportTitle"),
      period_start: financeValue("financePeriodStart"),
      period_end: financeValue("financePeriodEnd"),
    });
    return financeRefreshAfterMutation();
  }));

  refreshFinanceStatus().catch(() => {});
  refreshFinanceVentures().catch(() => {});
}

setupFinanceWorkspace();
