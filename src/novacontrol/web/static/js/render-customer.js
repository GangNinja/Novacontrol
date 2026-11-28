/* ── Phase 37: Customer & Growth workspace ─────────────────── */

let customerSelectedId = "";
let customerLastDetail = null;
let customerSelectedTicketId = "";

function customerLabel(value) {
  return String(value || "").replace(/_/g, " ").toLowerCase().replace(/\b\w/g, (c) => c.toUpperCase());
}

function customerPill(value) {
  const text = String(value || "").toUpperCase();
  if (["QUALIFIED", "SENT", "WON", "CLOSED_WON", "RESOLVED", "ACTIVE", "COMPLETE"].includes(text)) return "ok";
  if (["UNQUALIFIED", "LOST", "CLOSED_LOST", "FAILED", "BLOCKED", "CHURNED", "HIGH"].includes(text)) return "warn";
  return "";
}

function customerValue(id) {
  return byId(id) ? byId(id).value.trim() : "";
}

function customerNumber(id, fallback = 0) {
  const raw = customerValue(id);
  if (!raw) return fallback;
  const value = Number(raw);
  if (!Number.isFinite(value)) throw new Error(`${id} must be a number`);
  return value;
}

function customerLines(id) {
  return customerValue(id).split(/\r?\n/).map((row) => row.trim()).filter(Boolean);
}

function customerJson(id, label) {
  const raw = customerValue(id);
  if (!raw) return {};
  try {
    const value = JSON.parse(raw);
    if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error(`${label} must be a JSON object`);
    return value;
  } catch (error) {
    throw new Error(`${label}: ${error.message || error}`);
  }
}

async function refreshCustomerStatus() {
  const data = await requestJson("/customer/status");
  const configured = data.configured || {};
  byId("customerStatus").textContent =
    `${data.workspaces ?? 0} workspace(s) · ${data.leads ?? 0} lead(s) · ${data.customers ?? 0} customer(s) · ${data.tickets ?? 0} ticket(s) · ` +
    `Phase 36 reader ${configured.product_reader ? "connected" : "unavailable"} · sender ${configured.communication_sender ? "configured" : "not configured (sends stay blocked)"}`;
  return data;
}

async function refreshCustomerWorkspaces() {
  const data = await requestJson("/customer/workspaces?limit=100");
  render("customerWorkspaceList", data, "customerGrowth");
  return data;
}

async function loadCustomerWorkspace(id) {
  if (!id) return null;
  customerSelectedId = id;
  const data = await requestJson(`/customer/workspaces/${encodeURIComponent(id)}`);
  customerLastDetail = data;
  render("customerDetail", data, "customer");
  return data;
}

async function customerRefreshAfterMutation() {
  if (!customerSelectedId) return null;
  await refreshCustomerWorkspaces();
  return loadCustomerWorkspace(customerSelectedId);
}

function customerRequest(loadingText, path, payload, buttonId) {
  if (!customerSelectedId) { showToast("Select a customer workspace first"); return; }
  run("customerDetail", "customer", async () => {
    await requestJson(path, payload);
    return customerRefreshAfterMutation();
  }, buttonId || null, { loadingText });
}

function customerWorkspacePath(suffix) {
  return `/customer/workspaces/${encodeURIComponent(customerSelectedId)}${suffix || ""}`;
}

function customerCard(target, title, note) {
  const card = el("article", "info-card");
  card.appendChild(el("h3", "", title));
  if (note) card.appendChild(el("p", "muted", note));
  target.appendChild(card);
  return card;
}

function renderCustomerGrowth(target, data) {
  const rows = Array.isArray(data.workspaces) ? data.workspaces : [];
  target.appendChild(el("p", "summary", `${data.total ?? rows.length} customer workspace(s). Showing ${rows.length}.`));
  if (!rows.length) {
    target.appendChild(el("p", "muted", "No customer workspace exists yet. Create one from a product that Phase 36 has approved for launch."));
    return;
  }
  rows.forEach((row) => {
    const card = el("article", "info-card");
    const head = el("div", "surface-head-row");
    head.appendChild(el("h4", "", row.product_name || row.product_id));
    head.appendChild(el("span", `pill ${row.launch_authorized ? "ok" : "warn"}`, row.launch_authorized ? "Launch authorized" : customerLabel(row.launch_status)));
    card.appendChild(head);
    card.appendChild(el("p", "muted", `Product ${row.product_id} · version ${row.product_version || "not recorded"} · ${row.segments} segment(s) · ${row.icps} ICP(s)`));
    card.appendChild(el("p", "muted", `${row.leads} lead(s) · ${row.customers} customer(s) · ${row.open_deals} open deal(s) · ${row.open_tickets} open ticket(s) · ${row.communications} communication record(s)`));
    const open = el("button", "cyber-btn small", "Open customer workspace");
    open.type = "button";
    open.addEventListener("click", () => loadCustomerWorkspace(row.product_id).catch((error) => showToast(String(error.message || error))));
    card.appendChild(open);
    target.appendChild(card);
  });
}

function customerRow(title, status, lines, action) {
  const row = el("article", "info-card");
  const head = el("div", "surface-head-row");
  head.appendChild(el("h4", "", title || "Record"));
  if (status) head.appendChild(el("span", `pill ${customerPill(status)}`, customerLabel(status)));
  row.appendChild(head);
  (lines || []).filter(Boolean).forEach((line) => row.appendChild(el("p", "muted", String(line))));
  if (action) row.appendChild(action);
  return row;
}

function customerActionButton(label, path, payload, loadingText) {
  const button = el("button", "cyber-btn small", label);
  button.type = "button";
  button.addEventListener("click", () => customerRequest(loadingText || `${label}…`, path, payload));
  return button;
}

function customerSalesAssist(leadId, dealId) {
  requestJson(customerWorkspacePath("/sales/assist"), { lead_id: leadId || "", deal_id: dealId || "" })
    .then((data) => {
      if (!byId("customerSalesAssist")) return;
      byId("customerSalesAssist").hidden = false;
      render("customerSalesAssistBody", data, "customerSales");
      byId("customerSalesAssist").scrollIntoView({ behavior: "smooth", block: "nearest" });
    })
    .catch((error) => showToast(String(error.message || error)));
}

function customerLeadActions(lead) {
  const wrap = el("div", "button-row");
  const brief = el("button", "cyber-btn small", "Sales brief");
  brief.type = "button";
  brief.addEventListener("click", () => customerSalesAssist(lead.id, ""));
  wrap.appendChild(brief);
  if (!lead.qualification) {
    wrap.appendChild(customerActionButton("Qualify", customerWorkspacePath(`/leads/${encodeURIComponent(lead.id)}/qualify`), {}, "Scoring recorded criteria…"));
  }
  if (["NEW", "RESEARCHING", "QUALIFIED"].includes(lead.status)) {
    wrap.appendChild(customerActionButton("Request contact approval", customerWorkspacePath(`/leads/${encodeURIComponent(lead.id)}/status`), { status: "CONTACT_PENDING_APPROVAL" }, "Recording contact approval requirement…"));
  }
  if (["QUALIFIED", "CONTACT_PENDING_APPROVAL", "IN_CONVERSATION"].includes(lead.status)) {
    wrap.appendChild(customerActionButton("Convert to customer", customerWorkspacePath(`/leads/${encodeURIComponent(lead.id)}/convert`), {}, "Converting a qualified lead…"));
  }
  if (!["LOST", "ARCHIVED", "CONVERTED"].includes(lead.status)) {
    const lost = el("button", "cyber-btn small", "Mark lost");
    lost.type = "button";
    lost.addEventListener("click", () => {
      const reason = window.prompt("Why was this lead lost? A reason is required.");
      if (!reason) { showToast("A lost lead needs a recorded reason"); return; }
      customerRequest("Recording lost lead…", customerWorkspacePath(`/leads/${encodeURIComponent(lead.id)}/status`), { status: "LOST", lost_reason: reason });
    });
    wrap.appendChild(lost);
  }
  return wrap;
}

function customerDealActions(deal) {
  const wrap = el("div", "button-row");
  const brief = el("button", "cyber-btn small", "Sales brief");
  brief.type = "button";
  brief.addEventListener("click", () => customerSalesAssist("", deal.id));
  wrap.appendChild(brief);
  if (["closed_won", "closed_lost"].includes(deal.stage)) return wrap;
  const won = el("button", "cyber-btn small", "Close won");
  won.type = "button";
  won.addEventListener("click", () => customerRequest("Recording a closed-won estimate…", customerWorkspacePath(`/deals/${encodeURIComponent(deal.id)}/stage`), { stage: "closed_won" }));
  wrap.appendChild(won);
  const lost = el("button", "cyber-btn small", "Close lost");
  lost.type = "button";
  lost.addEventListener("click", () => {
    const reason = window.prompt("Why was this deal lost?");
    if (!reason) { showToast("A lost deal needs a recorded reason"); return; }
    customerRequest("Recording lost deal…", customerWorkspacePath(`/deals/${encodeURIComponent(deal.id)}/stage`), { stage: "closed_lost", lost_reason: reason });
  });
  wrap.appendChild(lost);
  const next = el("button", "cyber-btn small", "Advance stage");
  next.type = "button";
  next.addEventListener("click", () => {
    const stages = (customerLastDetail?.pipeline_stages || []).filter((row) => !row.terminal);
    const index = stages.findIndex((row) => row.id === deal.stage);
    const target = stages[index + 1];
    if (!target) { showToast("No further open stage exists"); return; }
    customerRequest("Moving the deal…", customerWorkspacePath(`/deals/${encodeURIComponent(deal.id)}/stage`), { stage: target.id });
  });
  wrap.appendChild(next);
  return wrap;
}

function customerCommunicationActions(item) {
  const wrap = el("div", "button-row");
  if (item.status === "draft") {
    const approve = el("button", "cyber-btn small", "Approve");
    approve.type = "button";
    approve.addEventListener("click", () => {
      const note = window.prompt("Approval note for this communication");
      if (!note) { showToast("Approval needs a note"); return; }
      customerRequest("Recording communication approval…", customerWorkspacePath(`/communications/${encodeURIComponent(item.id)}/approval`), { approved: true, note });
    });
    wrap.appendChild(approve);
    const reject = el("button", "cyber-btn small", "Cancel");
    reject.type = "button";
    reject.addEventListener("click", () => {
      const note = window.prompt("Why is this draft being cancelled?");
      if (!note) { showToast("Cancellation needs a note"); return; }
      customerRequest("Cancelling the draft…", customerWorkspacePath(`/communications/${encodeURIComponent(item.id)}/approval`), { approved: false, note });
    });
    wrap.appendChild(reject);
  }
  if (["draft", "approved"].includes(item.status)) {
    const send = el("button", "cyber-btn small", "Send");
    send.type = "button";
    send.addEventListener("click", () => customerRequest("Attempting delivery through the configured sender…", customerWorkspacePath(`/communications/${encodeURIComponent(item.id)}/send`), {}));
    wrap.appendChild(send);
  }
  return wrap;
}

function customerCustomerActions(customer) {
  const wrap = el("div", "button-row");
  const consent = customer.consent || {};
  if (consent.status === "withdrawn") {
    const restore = el("button", "cyber-btn small", "Record consent");
    restore.type = "button";
    restore.addEventListener("click", () => {
      const source = window.prompt("What recorded source grants consent?");
      if (!source) { showToast("Granting consent needs a recorded source"); return; }
      customerRequest("Recording consent…", customerWorkspacePath(`/customers/${encodeURIComponent(customer.id)}/consent`), { status: "granted", channels: (consent.channels || []), source });
    });
    wrap.appendChild(restore);
  } else {
    const withdraw = el("button", "cyber-btn small", "Withdraw consent");
    withdraw.type = "button";
    withdraw.addEventListener("click", () => {
      if (!window.confirm("Withdraw communication consent for this customer?")) return;
      customerRequest("Withdrawing consent…", customerWorkspacePath(`/customers/${encodeURIComponent(customer.id)}/consent`), { status: "withdrawn", note: "Withdrawn by operator" });
    });
    wrap.appendChild(withdraw);
  }
  const success = el("button", "cyber-btn small", "Refresh success");
  success.type = "button";
  success.addEventListener("click", () => customerRequest("Refreshing success indicators…", customerWorkspacePath(`/customers/${encodeURIComponent(customer.id)}/success`), {}));
  wrap.appendChild(success);
  if (customer.status !== "churned") {
    const churn = el("button", "cyber-btn small", "Record churn");
    churn.type = "button";
    churn.addEventListener("click", () => {
      const reason = window.prompt("Why did this customer churn? A reason is required.");
      if (!reason) { showToast("A churn decision needs a recorded reason"); return; }
      customerRequest("Recording churn…", customerWorkspacePath(`/customers/${encodeURIComponent(customer.id)}/churn`), { reason });
    });
    wrap.appendChild(churn);
  }
  const erase = el("button", "cyber-btn small", "Erase customer");
  erase.type = "button";
  erase.addEventListener("click", () => {
    if (!window.confirm("Erase this customer and directly related records? This cannot be undone.")) return;
    const reason = window.prompt("Record the verified erasure reason.");
    if (!reason) { showToast("Erasure needs a recorded reason"); return; }
    customerRequest("Erasing the customer and related records…", customerWorkspacePath(`/customers/${encodeURIComponent(customer.id)}/erasure`), { approved: true, reason });
  });
  wrap.appendChild(erase);
  return wrap;
}

function customerTicketActions(ticket) {
  const wrap = el("div", "button-row");
  const view = el("button", "cyber-btn small", "Open ticket");
  view.type = "button";
  view.addEventListener("click", () => {
    customerSelectedTicketId = ticket.id;
    requestJson(customerWorkspacePath(`/tickets/${encodeURIComponent(ticket.id)}`))
      .then((data) => {
        if (byId("customerTicketDetail")) {
          byId("customerTicketDetail").hidden = false;
          render("customerTicketDetailBody", data, "customerTicket");
        }
      })
      .catch((error) => showToast(String(error.message || error)));
  });
  wrap.appendChild(view);
  const assist = el("button", "cyber-btn small", "Ground with knowledge");
  assist.type = "button";
  assist.addEventListener("click", () => customerRequest("Reading approved product knowledge…", customerWorkspacePath(`/tickets/${encodeURIComponent(ticket.id)}/assist`), {}));
  wrap.appendChild(assist);
  if (ticket.status !== "CLOSED") {
    const reply = el("button", "cyber-btn small", "Add message");
    reply.type = "button";
    reply.addEventListener("click", () => {
      const body = window.prompt("Customer-visible message to record on this ticket");
      if (!body) { showToast("A ticket message needs a body"); return; }
      customerRequest("Recording the ticket message…", customerWorkspacePath(`/tickets/${encodeURIComponent(ticket.id)}/messages`), { body });
    });
    wrap.appendChild(reply);
  }
  if (["RESOLVED", "CLOSED"].includes(ticket.status)) {
    const reopen = el("button", "cyber-btn small", "Reopen");
    reopen.type = "button";
    reopen.addEventListener("click", () => {
      const reason = window.prompt("Why is this ticket being reopened?");
      if (!reason) { showToast("A reopened ticket needs a reason"); return; }
      customerRequest("Reopening the ticket…", customerWorkspacePath(`/tickets/${encodeURIComponent(ticket.id)}/status`), { status: "OPEN", reason });
    });
    wrap.appendChild(reopen);
  }
  if (!["RESOLVED", "CLOSED"].includes(ticket.status)) {
    const resolve = el("button", "cyber-btn small", "Resolve");
    resolve.type = "button";
    resolve.addEventListener("click", () => {
      const resolution = window.prompt("What resolved this ticket?");
      if (!resolution) { showToast("A resolution needs a recorded note"); return; }
      customerRequest("Resolving the ticket…", customerWorkspacePath(`/tickets/${encodeURIComponent(ticket.id)}/status`), { status: "RESOLVED", resolution });
    });
    wrap.appendChild(resolve);
    const escalate = el("button", "cyber-btn small", "Escalate");
    escalate.type = "button";
    escalate.addEventListener("click", () => {
      const reason = window.prompt("Why does this need escalation?");
      if (!reason) { showToast("Escalation needs a reason"); return; }
      customerRequest("Escalating the ticket…", customerWorkspacePath(`/tickets/${encodeURIComponent(ticket.id)}/status`), { status: "ESCALATED", reason });
    });
    wrap.appendChild(escalate);
  }
  return wrap;
}

function renderCustomerWorkspace(target, data) {
  customerLastDetail = data;
  customerSelectedId = data.product_id || customerSelectedId;
  const summary = data.summary || {};
  const snapshot = data.product_snapshot || {};
  const head = el("div", "surface-head-row");
  head.appendChild(el("h2", "", summary.product_name || snapshot.name || "Customer & Growth"));
  head.appendChild(el("span", `pill ${snapshot.launch_authorized ? "ok" : "warn"}`, snapshot.launch_authorized ? "Launch authorized" : customerLabel(snapshot.launch_status)));
  target.appendChild(head);
  target.appendChild(el("p", "muted", `Product ${data.product_id} · version ${snapshot.version || "not recorded"} · ${snapshot.launch_status || "launch status not recorded"} · ${summary.leads ?? 0} lead(s) · ${summary.customers ?? 0} customer(s) · ${summary.open_tickets ?? 0} open ticket(s)`));

  const snapshotCard = customerCard(target, "Product context from Phase 36", "This snapshot is read from the product record. It does not add capabilities, pricing or marketing claims.");
  snapshotCard.appendChild(el("p", "", snapshot.description || "Product description not recorded."));
  snapshotCard.appendChild(el("p", "muted", `Approved value proposition: ${snapshot.value_proposition || "not recorded"} · target segments: ${(snapshot.target_segments || []).join(", ") || "not recorded"}`));
  snapshotCard.appendChild(el("p", "muted", `Pricing assumptions: ${JSON.stringify(snapshot.pricing_assumptions || {})} · approved marketing claims: ${(snapshot.approved_marketing_claims || []).join("; ") || "none recorded"}`));
  if ((snapshot.limitations || []).length) snapshotCard.appendChild(el("p", "warn", `Known limitations: ${snapshot.limitations.join("; ")}`));

  const analytics = data.analytics || {};
  const analyticsCard = customerCard(target, "Acquisition, sales, support and retention", analytics.disclaimer || "Unavailable values are explicit.");
  const acquisition = analytics.acquisition || {};
  const sales = analytics.sales || {};
  const support = analytics.support || {};
  const retention = analytics.retention || {};
  analyticsCard.appendChild(el("p", "", `Acquisition: ${acquisition.leads_generated ?? 0} lead(s) · qualification rate ${acquisition.qualification_rate ?? "unavailable"} · CAC ${acquisition.acquisition_cost?.available ? `${acquisition.acquisition_cost.value} ${acquisition.acquisition_cost.currency || ""}` : "unavailable"}`));
  analyticsCard.appendChild(el("p", "", `Sales: pipeline estimate ${sales.pipeline_value?.value ?? "unavailable"} ${sales.pipeline_value?.currency || ""} · won ${sales.won_deals ?? 0} · lost ${sales.lost_deals ?? 0} · realized revenue ${sales.realized_revenue?.value ?? "unavailable"}`));
  analyticsCard.appendChild(el("p", "", `Support: ${support.open_tickets ?? 0} open · ${support.total_tickets ?? 0} total · resolution time ${support.resolution_time_days ?? "unavailable"} day(s) · satisfaction ${support.customer_satisfaction?.value ?? "unavailable"}`));
  analyticsCard.appendChild(el("p", "", `Retention: ${retention.customers ?? 0} customer(s) · churn rate ${retention.churn_rate ?? "unavailable"} · adoption ${retention.adoption_score?.value ?? "unavailable"} · repeat-issue customers ${retention.repeat_support_issue_customers ?? 0}`));

  const segmentsCard = customerCard(target, "Segments, ICPs and research", "Verified information and inferred assumptions are stored separately.");
  (data.segments || []).forEach((row) => segmentsCard.appendChild(customerRow(row.name, row.verification_state, [row.industry, row.company_size, row.geography, (row.needs || []).join("; "), (row.assumptions || []).length ? `Assumptions: ${row.assumptions.join("; ")}` : ""])));
  (data.icps || []).forEach((row) => segmentsCard.appendChild(customerRow(`ICP — ${row.name}`, row.verification_state, [(row.criteria ? JSON.stringify(row.criteria) : ""), (row.evidence || []).join("; ")])));
  (data.research || []).forEach((row) => segmentsCard.appendChild(customerRow(row.title, row.verification_status, [row.summary, row.source_url || row.source_reference, `Confidence ${row.confidence} · retrieved ${row.retrieved_at}`])));

  const customersCard = customerCard(target, "Customers", "Contact details and consent are recorded from lawful sources only.");
  if (!(data.customers || []).length) customersCard.appendChild(el("p", "muted", "No customers recorded."));
  (data.customers || []).forEach((row) => {
    const card = customerRow(row.name, row.status, [`${row.category} · ${row.location || "location not recorded"} · consent ${row.consent?.status || "unknown"}`, row.contact?.email || "no email recorded", row.churn_reason ? `Churn reason: ${row.churn_reason}` : ""]);
    card.appendChild(customerCustomerActions(row));
    customersCard.appendChild(card);
  });

  const leadsCard = customerCard(target, "Leads and qualification", "A score is an explainable recommendation, never verified purchase intent.");
  if (!(data.leads || []).length) leadsCard.appendChild(el("p", "muted", "No leads recorded."));
  (data.leads || []).forEach((row) => {
    const qualification = row.qualification;
    const text = qualification
      ? `Score ${Math.round(Number(qualification.score || 0) * 100)}% · intent verified ${qualification.purchase_intent_verified ? "yes" : "no"} · missing ${(qualification.missing_information || []).join("; ") || "none"}`
      : "Not qualified yet";
    const card = customerRow(row.name, row.status, [`Source ${row.source} · ${row.organization || "organization not recorded"} · consent ${row.consent_status}`, text]);
    card.appendChild(customerLeadActions(row));
    leadsCard.appendChild(card);
  });

  const pipelineCard = customerCard(target, "Sales pipeline", "Pipeline value is an estimate from recorded deals. It is not realized revenue.");
  (data.deals || []).forEach((row) => {
    const card = customerRow(row.name, row.stage, [`${row.value} ${row.currency} · probability ${Math.round(Number(row.probability || 0) * 100)}% · expected close ${row.expected_close || "not recorded"}`, `Objections: ${(row.objections || []).join("; ") || "none"} · competitors: ${(row.competitor_alternatives || []).join("; ") || "none"}`, row.lost_reason ? `Lost reason: ${row.lost_reason}` : ""]);
    card.appendChild(customerDealActions(row));
    pipelineCard.appendChild(card);
  });
  if (!(data.deals || []).length) pipelineCard.appendChild(el("p", "muted", "No deals recorded."));

  const commsCard = customerCard(target, "Communication audit", "Every outbound message is a recorded draft. Sending needs explicit approval or a narrow policy and a configured sender.");
  (data.communications || []).forEach((row) => {
    const card = customerRow(row.subject || `${customerLabel(row.channel)} message`, row.status, [`${row.recipient} · consent ${row.consent_status} · ${row.consent_reason}`, row.failure || ""]);
    card.appendChild(customerCommunicationActions(row));
    commsCard.appendChild(card);
  });
  if (!(data.communications || []).length) commsCard.appendChild(el("p", "muted", "No communication records."));

  const supportCard = customerCard(target, "Support inbox", "Assistance uses only approved, verified knowledge and never claims an action was performed.");
  (data.knowledge || []).forEach((row) => supportCard.appendChild(el("p", "muted", `Knowledge: ${row.title} · ${row.approved ? "approved" : "not approved"} · ${row.verified ? "verified" : "unverified"} · ${row.source || "source missing"}`)));
  (data.tickets || []).forEach((row) => {
    const card = customerRow(row.subject, row.status, [`${row.customer_name} · ${customerLabel(row.category)} · ${row.priority} priority · ${row.severity} severity`, row.assignee ? `Assigned ${row.assignee}` : "Unassigned", row.resolution ? `Resolution: ${row.resolution}` : ""]);
    card.appendChild(customerTicketActions(row));
    supportCard.appendChild(card);
  });
  if (!(data.tickets || []).length) supportCard.appendChild(el("p", "muted", "No support tickets."));

  const feedbackCard = customerCard(target, "Feedback and onboarding", "Feedback is linked to product, version and, where available, customer, ticket and feature.");
  (data.feedback || []).forEach((row) => feedbackCard.appendChild(customerRow(customerLabel(row.kind), row.severity, [row.summary, row.feature ? `Feature: ${row.feature}` : "", row.rating ? `Rating ${row.rating}/5` : "", row.verification_state])));
  (data.onboarding || []).forEach((row) => {
    const completed = (row.checklist || []).filter((item) => item.status === "complete").length;
    feedbackCard.appendChild(customerRow(`Onboarding ${row.customer_id}`, row.status, [`${completed}/${(row.checklist || []).length} item(s) complete · adoption ${row.adoption_score}`, row.next_follow_up ? `Next follow-up ${row.next_follow_up}` : ""]));
  });
  (data.success || []).forEach((row) => feedbackCard.appendChild(customerRow(`Success ${row.customer_id}`, row.risk, [`Adoption ${row.adoption_score} · ${(row.risk_reasons || []).join("; ")}`, row.certainty_note])));
}

function renderCustomerSalesBrief(target, data) {
  target.appendChild(el("p", "muted", "Traceable sales assistance from recorded evidence. No capability, discount or contractual term is invented."));
  const requirements = data.customer_requirement_analysis || {};
  target.appendChild(el("h4", "", "Customer requirements"));
  target.appendChild(el("p", "", `${customerLabel(requirements.state)} · ${(requirements.requirements || []).join("; ")}`));
  target.appendChild(el("h4", "", "Product fit"));
  (data.product_fit?.rows || []).forEach((row) => target.appendChild(el("p", "muted", `${row.requirement} → ${row.fit}${(row.documented_features || []).length ? `: ${row.documented_features.join("; ")}` : ""}`)));
  target.appendChild(el("p", "muted", data.product_fit?.note || ""));
  target.appendChild(el("h4", "", "Meeting preparation and discovery"));
  (data.meeting_preparation?.agenda || []).forEach((row) => target.appendChild(el("p", "", `Agenda: ${row}`)));
  (data.discovery_questions || []).forEach((row) => target.appendChild(el("p", "muted", `Question: ${row}`)));
  target.appendChild(el("h4", "", "Research and competitor context"));
  (data.sales_research?.records || []).forEach((row) => target.appendChild(el("p", "muted", `${row.title} · ${row.verification_status} · ${row.source}`)));
  target.appendChild(el("p", "muted", data.competitor_comparison?.note || ""));
  (data.objection_analysis || []).forEach((row) => target.appendChild(el("p", "", `Objection: ${row.objection} — ${row.recorded_response}`)));
  target.appendChild(el("h4", "", "Proposal draft"));
  (data.proposal_draft?.lines || []).forEach((row) => target.appendChild(el("p", "", row)));
  target.appendChild(el("p", "muted", `Discount authorized ${data.proposal_draft?.authorized_discount ? "yes" : "no"} · contract terms authorized ${data.proposal_draft?.authorized_contract_terms ? "yes" : "no"}`));
  target.appendChild(el("h4", "", "Follow-up and conversation"));
  (data.follow_up_recommendations || []).forEach((row) => target.appendChild(el("p", "", row)));
  (data.conversation_summary || []).forEach((row) => target.appendChild(el("p", "muted", row)));
  target.appendChild(el("p", "muted", `Source records: ${(data.sources || []).map((row) => row.id || row.label).join(", ") || "none"}`));
}

function renderCustomerTicket(target, data) {
  const head = el("div", "surface-head-row");
  head.appendChild(el("h3", "", data.subject || "Ticket"));
  head.appendChild(el("span", `pill ${customerPill(data.status)}`, customerLabel(data.status)));
  target.appendChild(head);
  target.appendChild(el("p", "muted", `${data.customer_name || data.customer_id} · ${customerLabel(data.category)} · ${data.priority} priority · ${data.severity} severity · created ${data.created_at}`));
  target.appendChild(el("p", "", data.description || ""));
  (data.messages || []).forEach((row) => target.appendChild(el("p", "", `Customer-visible · ${row.author}: ${row.body}`)));
  (data.internal_notes || []).forEach((row) => target.appendChild(el("p", "muted", `Internal · ${row.author}: ${row.body}`)));
  (data.assistant_runs || []).forEach((run) => {
    const card = el("article", "info-card");
    card.appendChild(el("h4", "", `Grounded assistance · ${run.classification}`));
    card.appendChild(el("p", "muted", `Grounded ${run.grounded ? "yes" : "no"} · escalation ${run.escalation_required ? "required" : "not required"} · human review ${run.human_review_required ? "required" : "not required"}`));
    (run.sources || []).forEach((source) => card.appendChild(el("p", "muted", `Source: ${source.title} · ${source.source || "source missing"}`)));
    card.appendChild(el("p", "", run.draft_response || ""));
    card.appendChild(el("p", "muted", run.verification_note || ""));
    target.appendChild(card);
  });
}

function setupCustomerWorkspace() {
  if (!byId("customerRefreshButton")) return;
  byId("customerRefreshButton").addEventListener("click", () =>
    run("customerWorkspaceList", "customerGrowth", async () => {
      await refreshCustomerStatus();
      return refreshCustomerWorkspaces();
    }, "customerRefreshButton")
  );
  byId("customerWorkspaceCreateButton").addEventListener("click", () => {
    const productId = customerValue("customerProductId");
    if (!productId) { showToast("Enter a launch-authorized Phase 36 product id"); return; }
    run("customerDetail", "customer", async () => {
      const created = await requestJson("/customer/workspaces", { product_id: productId });
      customerSelectedId = created.product_id;
      await refreshCustomerWorkspaces();
      return loadCustomerWorkspace(created.product_id);
    }, "customerWorkspaceCreateButton", { loadingText: "Reading the Phase 36 product and creating an isolated workspace…" });
  });
  byId("customerSegmentButton").addEventListener("click", () => {
    customerRequest("Recording a segment and its evidence…", customerWorkspacePath("/segments"), {
      name: customerValue("customerSegmentName"), category: customerValue("customerSegmentCategory"),
      industry: customerValue("customerSegmentIndustry"), company_size: customerValue("customerSegmentSize"),
      geography: customerValue("customerSegmentGeography"), needs: customerLines("customerSegmentNeeds"),
      pain_points: customerLines("customerSegmentPain"), evidence: customerLines("customerSegmentEvidence"),
    }, "customerSegmentButton");
  });
  byId("customerIcpButton").addEventListener("click", () => {
    let criteria;
    try { criteria = customerJson("customerIcpCriteria", "ICP criteria"); } catch (error) { showToast(String(error.message || error)); return; }
    customerRequest("Recording an Ideal Customer Profile…", customerWorkspacePath("/icps"), {
      name: customerValue("customerIcpName"), segment_id: customerValue("customerSegmentId"), criteria,
      needs: customerLines("customerIcpNeeds"), evidence: customerLines("customerIcpEvidence"),
    }, "customerIcpButton");
  });
  byId("customerResearchButton").addEventListener("click", () => {
    customerRequest("Recording sourced research…", customerWorkspacePath("/research"), {
      title: customerValue("customerResearchTitle"), summary: customerValue("customerResearchSummary"),
      source_url: customerValue("customerResearchUrl"), source_reference: customerValue("customerResearchReference"),
      confidence: customerNumber("customerResearchConfidence", 0.5),
      verification_status: customerValue("customerResearchVerification") || "public_research",
      segment_id: customerValue("customerResearchSegment"),
    }, "customerResearchButton");
  });
  byId("customerCreateButton").addEventListener("click", () => {
    customerRequest("Recording a customer with its consent state…", customerWorkspacePath("/customers"), {
      name: customerValue("customerName"), category: customerValue("customerCategory"),
      segment_id: customerValue("customerSegmentAssign"), contact: { email: customerValue("customerEmail") },
      consent: { status: customerValue("customerConsent") || "unknown", channels: customerValue("customerChannels").split(",").map((row) => row.trim()).filter(Boolean), source: customerValue("customerConsentSource") },
      contact_source: customerValue("customerContactSource"), location: customerValue("customerLocation"),
      next_action: customerValue("customerNextAction"),
    }, "customerCreateButton");
  });
  byId("customerLeadButton").addEventListener("click", () => {
    let qualificationInputs;
    try { qualificationInputs = customerJson("customerLeadScores", "Lead qualification inputs"); } catch (error) { showToast(String(error.message || error)); return; }
    customerRequest("Recording a lead from its lawful source…", customerWorkspacePath("/leads"), {
      name: customerValue("customerLeadName"), organization: customerValue("customerLeadOrg"),
      source: customerValue("customerLeadSource") || "operator_supplied", lawful_basis: customerValue("customerLeadLawfulBasis"),
      segment_id: customerValue("customerLeadSegment"), contact: { email: customerValue("customerLeadEmail") },
      consent: { status: customerValue("customerLeadConsent") || "unknown", channels: customerValue("customerLeadChannels").split(",").map((row) => row.trim()).filter(Boolean), source: customerValue("customerLeadConsentSource") },
      qualification_inputs: qualificationInputs, next_action: customerValue("customerLeadNextAction"),
    }, "customerLeadButton");
  });
  byId("customerDealButton").addEventListener("click", () => {
    customerRequest("Recording an estimated deal…", customerWorkspacePath("/deals"), {
      lead_id: customerValue("customerDealLead"), customer_id: customerValue("customerDealCustomer"),
      name: customerValue("customerDealName"), value: customerNumber("customerDealValue", 0),
      currency: customerValue("customerDealCurrency") || "USD", probability: customerNumber("customerDealProbability", 0),
      expected_close: customerValue("customerDealClose"), next_action: customerValue("customerDealNext"),
      objections: customerLines("customerDealObjections"),
    }, "customerDealButton");
  });
  byId("customerCommunicationButton").addEventListener("click", () => {
    customerRequest("Recording a consent-aware draft…", customerWorkspacePath("/communications"), {
      related_type: customerValue("customerCommType") || "customer", related_id: customerValue("customerCommId"),
      channel: customerValue("customerCommChannel") || "email", subject: customerValue("customerCommSubject"),
      body: customerValue("customerCommBody"),
    }, "customerCommunicationButton");
  });
  byId("customerKnowledgeButton").addEventListener("click", () => {
    customerRequest("Recording approved support knowledge…", customerWorkspacePath("/knowledge"), {
      title: customerValue("customerKnowledgeTitle"), body: customerValue("customerKnowledgeBody"),
      source: customerValue("customerKnowledgeSource"), approved: byId("customerKnowledgeApproved").checked,
      verified: byId("customerKnowledgeVerified").checked, steps: customerLines("customerKnowledgeSteps"),
    }, "customerKnowledgeButton");
  });
  byId("customerTicketButton").addEventListener("click", () => {
    customerRequest("Creating a support ticket…", customerWorkspacePath("/tickets"), {
      customer_id: customerValue("customerTicketCustomer"), subject: customerValue("customerTicketSubject"),
      description: customerValue("customerTicketDescription"), category: customerValue("customerTicketCategory") || "general_inquiry",
      priority: customerValue("customerTicketPriority") || "normal", severity: customerValue("customerTicketSeverity") || "normal",
    }, "customerTicketButton");
  });
  byId("customerFeedbackButton").addEventListener("click", () => {
    const payload = {
      kind: customerValue("customerFeedbackKind") || "complaint", summary: customerValue("customerFeedbackSummary"),
      customer_id: customerValue("customerFeedbackCustomer"), feature: customerValue("customerFeedbackFeature"),
      source: customerValue("customerFeedbackSource"), verified: Boolean(customerValue("customerFeedbackSource")),
    };
    const ratingRaw = customerValue("customerFeedbackRating");
    if (ratingRaw) {
      const rating = Number(ratingRaw);
      if (!Number.isFinite(rating)) { showToast("Rating must be a number from 1 to 5"); return; }
      payload.rating = rating;
    }
    customerRequest("Recording customer feedback…", customerWorkspacePath("/feedback"), payload, "customerFeedbackButton");
  });
  byId("customerOnboardingButton").addEventListener("click", () => {
    customerRequest("Starting an onboarding checklist…", customerWorkspacePath("/onboarding"), {
      customer_id: customerValue("customerOnboardingCustomer"), items: customerLines("customerOnboardingItems"),
      next_follow_up: customerValue("customerOnboardingFollowUp"),
    }, "customerOnboardingButton");
  });

  refreshCustomerStatus().catch(() => {});
  refreshCustomerWorkspaces().catch(() => {});
}

setupCustomerWorkspace();
