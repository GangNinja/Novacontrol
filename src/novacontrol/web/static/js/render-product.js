/* ── Phase 36: Product Development & Launch workspace ───────── */

let productSelectedId = "";
let productLastDetail = null;

function productPillClass(state) {
  if (state === "APPROVED_FOR_LAUNCH" || state === "READY_FOR_RELEASE_REVIEW") return "ok";
  if (state === "BLOCKED") return "warn";
  if (state === "ARCHIVED") return "";
  return "";
}

function productLabel(value) {
  return String(value || "").replace(/_/g, " ").toLowerCase().replace(/\b\w/g, (c) => c.toUpperCase());
}

function productLines(id) {
  const node = byId(id);
  if (!node) return [];
  return node.value
    .split(/\r?\n/)
    .map((row) => row.trim())
    .filter(Boolean);
}

function productJson(id, label) {
  const raw = byId(id) ? byId(id).value.trim() : "";
  if (!raw) return {};
  try {
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      throw new Error(`${label} must be a JSON object`);
    }
    return parsed;
  } catch (error) {
    throw new Error(`${label}: ${error.message || error}`);
  }
}

async function refreshProductStatus() {
  const data = await requestJson("/product/status");
  const limits = data.limits || {};
  byId("productStatus").textContent =
    `${data.products ?? 0} product project${data.products === 1 ? "" : "s"} · ` +
    `opportunity intake ${data.configured?.opportunity_loader ? "connected" : "unavailable"} · ` +
    `agents ${data.configured?.agent_registry ? "connected" : "unavailable"} · ` +
    `pytest runner ${data.configured?.test_runner ? "configured" : "unavailable"} · ` +
    `max parallel tasks ${limits.max_parallel_tasks ?? "?"} · ` +
    `external launch ${data.boundaries?.external_launch_executed ? "executed" : "never executed"}`;
  return data;
}

async function refreshProductList() {
  const data = await requestJson("/product/projects?limit=100");
  render("productList", data, "productList");
  return data;
}

async function loadProductProject(id) {
  if (!id) return null;
  productSelectedId = id;
  const data = await requestJson(`/product/projects/${encodeURIComponent(id)}`);
  productLastDetail = data;
  render("productDetail", data, "product");
  ["productPrdCard", "productArchitectureCard", "productPlanCard", "productQualityCard", "productDefectCard", "productReleaseCard", "productLaunchCard"].forEach((cardId) => {
    if (byId(cardId)) byId(cardId).hidden = false;
  });
  const gateSelect = byId("productTestGate");
  if (gateSelect) {
    gateSelect.replaceChildren();
    (data.quality_gates || []).forEach((gate) => {
      const option = el("option", "", `${gate.name} (${gate.id})`);
      option.value = gate.id;
      gateSelect.appendChild(option);
    });
  }
  if (byId("productBuildRecordCommand") && !byId("productBuildRecordCommand").value) {
    byId("productBuildRecordCommand").value = (data.environment || {}).build_command || "";
  }
  return data;
}

window.loadProductProject = (id) =>
  run("productDetail", "product", () => loadProductProject(id), null, {
    loadingText: "Loading product requirements and evidence…",
  });

function renderProductProjects(target, data) {
  return renderProductList(target, data);
}

function renderProductList(target, data) {
  const rows = Array.isArray(data.products) ? data.products : [];
  target.appendChild(el("p", "summary", `${data.total ?? rows.length} product project(s). Showing ${rows.length}.`));
  if (!rows.length) {
    target.appendChild(el("p", "muted", "No product has been created from a validated opportunity yet."));
    return;
  }
  rows.forEach((row) => {
    const card = el("article", "info-card");
    const head = el("div", "surface-head-row");
    head.appendChild(el("h4", "", row.name || "Product"));
    head.appendChild(el("span", `pill ${productPillClass(row.state)}`, productLabel(row.state)));
    card.appendChild(head);
    card.appendChild(el("p", "", `Opportunity: ${row.opportunity_title || row.opportunity_id} · validation ${productLabel(row.validation_state)}`));
    card.appendChild(el("p", "muted", `${row.tasks_completed ?? 0}/${row.tasks ?? 0} task(s) complete · ${row.open_defects ?? 0} open defect(s) · ${row.tests ?? 0} test evidence record(s) · ${row.releases ?? 0} release(s)`));
    card.appendChild(el("p", "muted", `PRD ${row.prd_approved ? `approved v${row.prd_version}` : "not approved"} · architecture ${row.architecture_approved ? "approved" : "not approved"} · readiness ${row.readiness || "not evaluated"}`));
    const open = el("button", "cyber-btn small", "Open product workspace");
    open.type = "button";
    open.addEventListener("click", () => window.loadProductProject(row.id));
    card.appendChild(open);
    target.appendChild(card);
  });
}

function productTaskButton(label, task, action) {
  const button = el("button", "cyber-btn small", label);
  button.type = "button";
  button.addEventListener("click", () => {
    if (!productSelectedId || !task || !task.id) return;
    const base = `/product/projects/${encodeURIComponent(productSelectedId)}/tasks/${encodeURIComponent(task.id)}`;
    if (action === "retry") {
      productRequest("Retrying task…", `${base}/retry`, {});
    } else {
      productRequest("Running task through the existing agent registry…", `${base}/run`, {});
    }
  });
  return button;
}

function productSection(target, title, note) {
  const card = el("article", "info-card");
  card.appendChild(el("h3", "", title));
  if (note) card.appendChild(el("p", "muted", note));
  target.appendChild(card);
  return card;
}

function renderProductProject(target, data) {
  productLastDetail = data;
  productSelectedId = data.id || productSelectedId;
  const head = el("div", "surface-head-row");
  head.appendChild(el("h2", "", data.name || "Product"));
  head.appendChild(el("span", `pill ${productPillClass(data.state)}`, productLabel(data.state)));
  target.appendChild(head);
  target.appendChild(el("p", "muted", `Product ${data.id} · opportunity ${data.opportunity_id} · ${productLabel(data.validation_state)} · target ${data.release_target}`));
  if (data.conditions && data.conditions.length) {
    const resolved = new Set((data.condition_resolutions || []).filter((row) => row.resolved).map((row) => row.condition));
    const conditionCard = el("article", "info-card");
    conditionCard.appendChild(el("h4", "", "Conditional-validation conditions"));
    data.conditions.forEach((condition) => {
      const row = el("p", "muted", `${resolved.has(condition) ? "Resolved" : "Open"} · ${condition}`);
      if (!resolved.has(condition)) {
        const resolve = el("button", "cyber-btn small", "Resolve with evidence");
        resolve.type = "button";
        resolve.addEventListener("click", () => {
          const evidence = window.prompt("What evidence resolves this condition? Do not record evidence that was not produced.");
          if (!evidence) { showToast("A condition cannot be resolved without evidence"); return; }
          productRequest("Recording condition evidence…", `/product/projects/${encodeURIComponent(productSelectedId)}/conditions/resolve`, { condition, evidence: [evidence] }, null);
        });
        row.appendChild(document.createTextNode(" "));
        row.appendChild(resolve);
      }
      conditionCard.appendChild(row);
    });
    conditionCard.appendChild(el("p", "muted", "Open conditions remain launch risk; they are not silently treated as satisfied."));
    target.appendChild(conditionCard);
  }

  const boundary = data.decision_boundary || {};
  const boundaryCard = productSection(
    target,
    "Automated recommendations vs approved decisions",
    "The pipeline may draft, compute, and assess. It cannot approve its own requirements, architecture, release or launch."
  );
  boundaryCard.appendChild(el("p", "muted", `Automated: ${(boundary.automated_recommendations || []).join("; ") || "none"}`));
  boundaryCard.appendChild(el("p", "muted", `Human approvals: ${(boundary.human_approvals_required || []).join("; ") || "none"}`));
  boundaryCard.appendChild(el("p", "muted", `External launch: ${boundary.external_launch_executed ? "executed" : "not executed"} · commits/pushes: ${boundary.commits_pushed ? "yes" : "no"} · external deployment: ${boundary.external_deployment_performed ? "yes" : "no"}`));

  const progress = data.progress || {};
  const progressCard = productSection(target, "Workspace and progress", "");
  progressCard.appendChild(el("p", "", `${progress.completed ?? 0}/${progress.tasks ?? 0} task(s) complete · ${Math.round(Number(progress.ratio || 0) * 100)}% · workspace ${data.workspace_path || "not created"}`));
  progressCard.appendChild(el("p", "muted", `Workspace files: ${data.workspace?.files ?? 0} · bytes ${data.workspace?.bytes ?? 0} · secret violations ${(data.workspace?.secret_violations || []).length}`));
  if (progress.critical_path?.length) {
    progressCard.appendChild(el("p", "muted", `Critical path (dependency calculation, not a promised date): ${progress.critical_path.join(" → ")}`));
  }

  const prd = data.prd;
  const prdCard = productSection(target, "Product requirements", "Requirements are traceable to the originating opportunity. Missing information is listed; it is not invented.");
  if (!prd) {
    prdCard.appendChild(el("p", "muted", "No PRD has been generated."));
  } else {
    prdCard.appendChild(el("p", "", `${prd.status === "approved" ? "APPROVED" : String(prd.status || "draft").toUpperCase()} · version ${prd.version} · ${prd.requirements?.length || 0} requirement(s)`));
    prdCard.appendChild(el("p", "", prd.vision || "Vision missing"));
    prdCard.appendChild(el("p", "muted", `Trace: opportunity ${prd.traceability?.opportunity_id || data.opportunity_id} · validation report v${prd.traceability?.validation_report_version ?? "?"}`));
    if (prd.missing_information?.length) {
      prdCard.appendChild(el("p", "warn", `Missing information: ${prd.missing_information.join("; ")}`));
    }
    (prd.requirements || []).forEach((requirement) => {
      const row = el("article", "info-card");
      const rowHead = el("div", "surface-head-row");
      rowHead.appendChild(el("h4", "", `${requirement.id} — ${requirement.title}`));
      rowHead.appendChild(el("span", `pill ${requirement.status === "verified" ? "ok" : requirement.status === "unmet" ? "warn" : ""}`, productLabel(requirement.status)));
      row.appendChild(rowHead);
      row.appendChild(el("p", "muted", `${productLabel(requirement.kind)} · ${requirement.priority} · ${requirement.source || "source not recorded"}`));
      row.appendChild(el("p", "", requirement.description || ""));
      (requirement.acceptance_criteria || []).forEach((criterion) => row.appendChild(el("p", "muted", `Acceptance: ${criterion}`)));
      if (requirement.status !== "verified") {
        const verify = el("button", "cyber-btn small", "Record verification");
        verify.type = "button";
        verify.addEventListener("click", () => {
          const evidence = window.prompt("What evidence verifies this requirement? Do not record test or acceptance evidence that was not produced.");
          if (!evidence) { showToast("Verification evidence was not recorded"); return; }
          productRequest("Recording requirement verification…", `/product/projects/${encodeURIComponent(productSelectedId)}/requirements/${encodeURIComponent(requirement.id)}/verify`, { passed: true, evidence: [evidence] });
        });
        row.appendChild(verify);
      }
      prdCard.appendChild(row);
    });
    prdCard.appendChild(el("p", "muted", `Approval: ${prd.approved_by ? `${prd.approved_by} at ${prd.approved_at}` : "not approved"}${prd.approval_note ? ` — ${prd.approval_note}` : ""}`));
  }

  const architecture = data.architecture;
  const architectureCard = productSection(target, "Architecture and decision records", "Major technology decisions require human approval.");
  if (!architecture) {
    architectureCard.appendChild(el("p", "muted", "No technical design has been generated."));
  } else {
    architectureCard.appendChild(el("p", "", `${String(architecture.status || "draft").toUpperCase()} · version ${architecture.version} · ${architecture.components?.length || 0} component(s) · ${architecture.decisions?.length || 0} decision(s)`));
    architectureCard.appendChild(el("p", "", architecture.summary || "Summary not supplied."));
    if (architecture.missing_information?.length) architectureCard.appendChild(el("p", "warn", `Missing information: ${architecture.missing_information.join("; ")}`));
    (architecture.decisions || []).forEach((decision) => {
      architectureCard.appendChild(el("p", "muted", `${decision.id}: ${decision.title} — ${decision.decision} (${decision.status})${decision.approved_by ? ` · approved by ${decision.approved_by}` : ""}`));
    });
  }

  const plan = data.plan;
  const planCard = productSection(target, "Development plan and roadmap", "Milestones are ordered, not dated. Estimates are estimates.");
  if (!plan) {
    planCard.appendChild(el("p", "muted", "No development plan exists."));
  } else {
    planCard.appendChild(el("p", "", `${String(plan.status || "draft").toUpperCase()} · ${plan.tasks?.length || 0} task(s) · ${plan.milestones?.length || 0} milestone(s) · ${plan.delivery_plan?.estimated_hours_total ?? 0} estimated hour(s)`));
    planCard.appendChild(el("p", "muted", plan.estimate_disclaimer || ""));
    (plan.milestones || []).forEach((milestone) => {
      planCard.appendChild(el("p", "muted", `${milestone.id} ${milestone.title} — ${milestone.status} · ${(milestone.task_ids || []).length} task(s)`));
    });
    (plan.tasks || []).forEach((task) => {
      const row = el("article", "info-card");
      const rowHead = el("div", "surface-head-row");
      rowHead.appendChild(el("h4", "", `${task.id} — ${task.title}`));
      rowHead.appendChild(el("span", `pill ${task.status === "completed" ? "ok" : ["failed", "blocked", "needs_review"].includes(task.status) ? "warn" : ""}`, productLabel(task.status)));
      row.appendChild(rowHead);
      row.appendChild(el("p", "muted", `${task.role} · ${task.priority} · ${task.assigned_agent || "no registered agent"} · dependencies ${(task.dependencies || []).join(", ") || "none"} · attempts ${task.attempts || 0}`));
      row.appendChild(el("p", "muted", `Estimate: ${task.estimate_hours === null || task.estimate_hours === undefined ? "not estimated" : `${task.estimate_hours} h (${task.estimate_basis}, ${task.estimate_confidence})`}`));
      if (task.result) row.appendChild(el("p", "", task.result));
      if (task.last_error) row.appendChild(el("p", "warn", task.last_error));
      (task.evidence || []).forEach((evidence) => row.appendChild(el("p", "muted", `Evidence: ${evidence}`)));
      if (task.status === "ready" || task.status === "pending") {
        row.appendChild(productTaskButton("Run task", task, "run"));
      } else if (["failed", "needs_review", "blocked", "cancelled"].includes(task.status)) {
        row.appendChild(productTaskButton("Retry task", task, "retry"));
      }
      planCard.appendChild(row);
    });
  }

  const quality = data.quality || {};
  const qualityCard = productSection(target, "Quality gates, tests and build", "A build is not a test pass. A gate passes only from recorded evidence.");
  qualityCard.appendChild(el("p", "muted", `Build: ${quality.build_validated ? "validated" : "not validated"} · tests recorded: ${quality.test_count ?? 0} · required gates failing: ${(quality.failing_required || []).join(", ") || "none"}`));
  (quality.gates || []).forEach((gate) => {
    qualityCard.appendChild(el("p", gate.status === "passed" ? "" : "warn", `${gate.name}: ${productLabel(gate.status)} — ${gate.reason || ""}${gate.required === false ? " (optional)" : ""}`));
  });
  (data.tests || []).slice(-20).forEach((test) => {
    qualityCard.appendChild(el("p", test.status === "passed" ? "muted" : "warn", `${test.suite || test.gate} [${test.kind}] ${productLabel(test.status)} · ${test.passed}/${test.total} passed · coverage ${test.coverage === null || test.coverage === undefined ? "unavailable" : test.coverage} · ${test.recorded_at}`));
  });

  const defectCard = productSection(target, "Defects and iteration", "A defect fix needs passing test evidence; a severity does not disappear because a task completed.");
  if (!(data.defects || []).length) {
    defectCard.appendChild(el("p", "muted", "No defects recorded."));
  }
  (data.defects || []).forEach((defect) => {
    const row = el("article", "info-card");
    const rowHead = el("div", "surface-head-row");
    rowHead.appendChild(el("h4", "", `${defect.id} — ${defect.title}`));
    rowHead.appendChild(el("span", `pill ${["high", "critical"].includes(defect.severity) && ["open", "reopened"].includes(defect.status) ? "warn" : defect.status === "verified" ? "ok" : ""}`, `${defect.severity.toUpperCase()} · ${productLabel(defect.status)}`));
    row.appendChild(rowHead);
    row.appendChild(el("p", "", defect.description || ""));
    if (defect.resolution) row.appendChild(el("p", "muted", `Resolution: ${defect.resolution}`));
    if (["open", "reopened", "fixed"].includes(defect.status)) {
      const resolve = el("button", "cyber-btn small", "Resolve with test evidence");
      resolve.type = "button";
      resolve.addEventListener("click", () => {
        const resolution = window.prompt("What changed, and what evidence verifies the fix?");
        if (!resolution) return;
        const testId = window.prompt("Enter the ID of a passing recorded test result (copy it from the test list).");
        if (!testId) { showToast("A defect cannot be resolved without a passing test result"); return; }
        productRequest("Recording verified defect resolution…", `/product/projects/${encodeURIComponent(productSelectedId)}/defects/${encodeURIComponent(defect.id)}/resolve`, { resolution, test_result_ids: [testId], verified: true });
      });
      row.appendChild(resolve);
    }
    defectCard.appendChild(row);
  });

  const releaseCard = productSection(target, "Release artifacts and approval", "Phase 36 prepares releases; it never deploys or publishes them.");
  if (!(data.releases || []).length) {
    releaseCard.appendChild(el("p", "muted", "No release has been prepared."));
  }
  (data.releases || []).forEach((release) => {
    const row = el("article", "info-card");
    const rowHead = el("div", "surface-head-row");
    rowHead.appendChild(el("h4", "", `${release.version} — ${release.target}`));
    rowHead.appendChild(el("span", `pill ${release.status === "approved" ? "ok" : release.status === "rejected" ? "warn" : ""}`, productLabel(release.status)));
    row.appendChild(rowHead);
    row.appendChild(el("p", "muted", `Artifact ${release.artifact?.filename || "missing"} · ${release.artifact?.bytes ?? 0} bytes · sha256 ${release.artifact?.sha256 || "missing"}`));
    row.appendChild(el("p", "muted", `External deployment: ${release.artifact?.external_deployment_performed ? "yes" : "no"} · checks: ${(release.checks || []).map((c) => `${c.id}:${c.status}`).join(", ")}`));
    if (release.status === "draft") {
      const approve = el("button", "cyber-btn small", "Approve release");
      approve.type = "button";
      approve.addEventListener("click", () => {
        const note = byId("productReleaseNote")?.value.trim() || window.prompt("Why is this release approved?");
        if (!note) { showToast("A release approval needs a note"); return; }
        productRequest("Recording release approval…", `/product/projects/${encodeURIComponent(productSelectedId)}/release/approval`, { release_id: release.id, approved: true, note });
      });
      row.appendChild(approve);
      const reject = el("button", "cyber-btn small danger", "Reject release");
      reject.type = "button";
      reject.addEventListener("click", () => {
        const note = byId("productReleaseNote")?.value.trim() || window.prompt("Why is this release rejected?");
        if (!note) { showToast("A release rejection needs a note"); return; }
        productRequest("Recording release rejection…", `/product/projects/${encodeURIComponent(productSelectedId)}/release/approval`, { release_id: release.id, approved: false, note });
      });
      row.appendChild(reject);
    }
    releaseCard.appendChild(row);
  });

  const readiness = data.readiness;
  const launchCard = productSection(target, "Launch readiness", "Readiness is computed from evidence and approvals; it is not an external launch.");
  if (!readiness) {
    launchCard.appendChild(el("p", "muted", "Launch readiness has not been evaluated."));
  } else {
    launchCard.appendChild(el("p", "", `${productLabel(readiness.status)} · score ${readiness.score} · requirements ${readiness.requirements_completion?.verified ?? 0}/${readiness.requirements_completion?.requirements ?? 0} verified`));
    (readiness.blockers || []).forEach((blocker) => launchCard.appendChild(el("p", "warn", `Blocker: ${blocker}`)));
    (readiness.warnings_detail || []).forEach((warning) => launchCard.appendChild(el("p", "muted", `Warning: ${warning.detail || warning.id}`)));
    (readiness.checks || []).forEach((check) => {
      launchCard.appendChild(el("p", check.status === "passed" ? "muted" : check.status === "warning" ? "muted" : "warn", `${check.id}: ${check.status} — ${check.detail}`));
    });
    launchCard.appendChild(el("p", "muted", readiness.note || ""));
  }

  const approvalsCard = productSection(target, "Approval history and project events", "Every approval is a named human decision; every event is tied to this project.");
  (data.approvals || []).slice(-12).forEach((approval) => {
    approvalsCard.appendChild(el("p", "muted", `${approval.kind}: ${approval.approved ? "approved" : "rejected"} by ${approval.actor || "unknown"} at ${approval.at || "unknown"}${approval.note ? ` — ${approval.note}` : ""}`));
  });
  (data.events || []).slice(-20).forEach((event) => {
    approvalsCard.appendChild(el("p", "muted", `${event.at || ""} · ${event.type} · ${event.actor || "system"}`));
  });
}

async function refreshProductAfterMutation() {
  if (!productSelectedId) return null;
  await refreshProductList();
  return loadProductProject(productSelectedId);
}

function productRequest(loadingText, path, payload, buttonId) {
  if (!productSelectedId) { showToast("Select a product first"); return; }
  run("productDetail", "product", async () => {
    await requestJson(path, payload);
    return refreshProductAfterMutation();
  }, buttonId || null, { loadingText });
}

function setupProductWorkspace() {
  if (!byId("productRefreshButton")) return;
  byId("productRefreshButton").addEventListener("click", () =>
    run("productList", "productList", async () => {
      await refreshProductStatus();
      return refreshProductList();
    }, "productRefreshButton")
  );

  byId("productCreateButton").addEventListener("click", () => {
    const opportunityId = byId("productOpportunityId").value.trim();
    if (!opportunityId) { showToast("Enter the Phase 35 opportunity id"); return; }
    const conditional = byId("productConditionalApproval").checked;
    const note = byId("productConditionalNote").value.trim();
    if (conditional && !note) { showToast("Conditional acceptance needs an approval note"); return; }
    run("productDetail", "product", async () => {
      const created = await requestJson("/product/projects", {
        opportunity_id: opportunityId,
        name: byId("productName").value.trim(),
        approved: conditional,
        approval_note: note,
        release_target: byId("productReleaseTarget").value,
        environment: {
          language: byId("productLanguage").value.trim() || "python",
          build_command: byId("productBuildCommand").value.trim(),
          test_command: byId("productTestCommand").value.trim(),
        },
      });
      productSelectedId = created.id;
      await refreshProductList();
      return loadProductProject(created.id);
    }, "productCreateButton", { loadingText: "Importing validated opportunity and creating isolated workspace…" });
  });

  byId("productPrdGenerateButton").addEventListener("click", () => {
    const users = byId("productPrdUsers").value.split(",").map((item) => item.trim()).filter(Boolean);
    productRequest("Generating a traceable PRD revision…", `/product/projects/${encodeURIComponent(productSelectedId)}/prd`, {
      vision: byId("productPrdVision").value.trim(),
      problem_statement: byId("productPrdProblem").value.trim(),
      target_users: users,
      personas: productLines("productPrdPersonas"),
      user_journeys: productLines("productPrdJourneys"),
      functional_requirements: productLines("productPrdFunctional"),
      non_functional_requirements: productLines("productPrdNonFunctional"),
      security_requirements: productLines("productPrdSecurity"),
      performance_requirements: productLines("productPrdPerformance"),
      accessibility_requirements: productLines("productPrdAccessibility"),
      constraints: productLines("productPrdConstraints"),
      dependencies: productLines("productPrdDependencies"),
      acceptance_criteria: productLines("productPrdAcceptance"),
      success_metrics: productLines("productPrdMetrics"),
      explicit_exclusions: productLines("productPrdExclusions"),
    }, "productPrdGenerateButton");
  });
  byId("productPrdApproveButton").addEventListener("click", () => {
    const note = byId("productPrdNote").value.trim();
    if (!note) { showToast("A requirements approval needs a note"); return; }
    productRequest("Recording human requirements approval…", `/product/projects/${encodeURIComponent(productSelectedId)}/prd/approval`, { approved: true, note }, "productPrdApproveButton");
  });
  byId("productPrdRevisionButton").addEventListener("click", () => {
    const note = byId("productPrdNote").value.trim();
    if (!note) { showToast("A revision request needs a note"); return; }
    productRequest("Recording a requirements revision request…", `/product/projects/${encodeURIComponent(productSelectedId)}/prd/approval`, { approved: false, note }, "productPrdRevisionButton");
  });

  byId("productArchitectureGenerateButton").addEventListener("click", () => {
    let payload;
    try { payload = productJson("productArchitectureJson", "Architecture JSON"); } catch (error) { showToast(String(error.message || error)); return; }
    productRequest("Generating technical design and architecture decisions…", `/product/projects/${encodeURIComponent(productSelectedId)}/architecture`, payload, "productArchitectureGenerateButton");
  });
  byId("productArchitectureApproveButton").addEventListener("click", () => {
    const note = byId("productArchitectureNote").value.trim();
    if (!note) { showToast("An architecture decision needs a note"); return; }
    productRequest("Recording human architecture approval…", `/product/projects/${encodeURIComponent(productSelectedId)}/architecture/approval`, { approved: true, note }, "productArchitectureApproveButton");
  });
  byId("productArchitectureRejectButton").addEventListener("click", () => {
    const note = byId("productArchitectureNote").value.trim();
    if (!note) { showToast("An architecture rejection needs a note"); return; }
    productRequest("Recording architecture rejection…", `/product/projects/${encodeURIComponent(productSelectedId)}/architecture/approval`, { approved: false, note }, "productArchitectureRejectButton");
  });

  byId("productPlanCreateButton").addEventListener("click", () => {
    let payload;
    try { payload = productJson("productPlanJson", "Plan JSON"); } catch (error) { showToast(String(error.message || error)); return; }
    productRequest("Building a milestone and task dependency plan…", `/product/projects/${encodeURIComponent(productSelectedId)}/plan`, payload, "productPlanCreateButton");
  });
  byId("productPlanApproveButton").addEventListener("click", () => {
    const note = byId("productPlanNote").value.trim();
    if (!note) { showToast("A plan decision needs a note"); return; }
    productRequest("Recording plan approval…", `/product/projects/${encodeURIComponent(productSelectedId)}/plan/approval`, { approved: true, note }, "productPlanApproveButton");
  });
  byId("productStartDevelopmentButton").addEventListener("click", () =>
    productRequest("Starting development in the isolated workspace…", `/product/projects/${encodeURIComponent(productSelectedId)}/development/start`, {}, "productStartDevelopmentButton")
  );
  byId("productRunReadyButton").addEventListener("click", () =>
    productRequest("Executing dependency-ready tasks through the existing agents…", `/product/projects/${encodeURIComponent(productSelectedId)}/tasks/run-ready`, { limit: 4 }, "productRunReadyButton")
  );
  byId("productCorrectiveButton").addEventListener("click", () =>
    productRequest("Creating bounded corrective tasks…", `/product/projects/${encodeURIComponent(productSelectedId)}/corrective-tasks`, {}, "productCorrectiveButton")
  );

  byId("productValidateEnvironmentButton").addEventListener("click", () =>
    productRequest("Validating workspace and runtime only…", `/product/projects/${encodeURIComponent(productSelectedId)}/environment/validate`, {}, "productValidateEnvironmentButton")
  );
  byId("productBuildRecordButton").addEventListener("click", () =>
    productRequest("Recording the actual build result…", `/product/projects/${encodeURIComponent(productSelectedId)}/build`, {
      command: byId("productBuildRecordCommand").value.trim(),
      exit_code: Number(byId("productBuildExit").value),
      output: byId("productBuildOutput").value.trim(),
      validated: byId("productBuildValidated").checked,
    }, "productBuildRecordButton")
  );
  byId("productTestRecordButton").addEventListener("click", () => {
    const counts = byId("productTestCounts").value.split("/").map((item) => Number(item.trim()) || 0);
    const coverageRaw = byId("productTestCoverage").value.trim();
    const payload = {
      gate: byId("productTestGate").value,
      kind: byId("productTestKind").value.trim() || byId("productTestGate").value,
      suite: byId("productTestGate").value,
      status: byId("productTestStatus").value,
      total: counts[0] || 0,
      passed: counts[1] || 0,
      failed: counts[2] || 0,
      command: byId("productTestCommandRecord").value.trim(),
      output: byId("productTestOutput").value.trim(),
    };
    if (coverageRaw) payload.coverage = Number(coverageRaw);
    productRequest("Recording actual test evidence…", `/product/projects/${encodeURIComponent(productSelectedId)}/tests`, payload, "productTestRecordButton");
  });
  byId("productQualityRunButton").addEventListener("click", () =>
    productRequest("Running supported pytest quality gates in the isolated workspace…", `/product/projects/${encodeURIComponent(productSelectedId)}/quality/run`, {}, "productQualityRunButton")
  );

  byId("productDefectOpenButton").addEventListener("click", () => {
    const title = byId("productDefectTitle").value.trim();
    if (!title) { showToast("A defect needs a title"); return; }
    productRequest("Opening a traced defect…", `/product/projects/${encodeURIComponent(productSelectedId)}/defects`, {
      title,
      severity: byId("productDefectSeverity").value,
      requirement_id: byId("productDefectRequirement").value.trim(),
      description: byId("productDefectDescription").value.trim(),
    }, "productDefectOpenButton");
  });

  byId("productPrepareReleaseButton").addEventListener("click", () => {
    let operational;
    try { operational = productJson("productReleaseOperational", "Operational JSON"); } catch (error) { showToast(String(error.message || error)); return; }
    productRequest("Preparing a release source bundle and documentation…", `/product/projects/${encodeURIComponent(productSelectedId)}/release`, {
      version: byId("productReleaseVersion").value.trim(),
      target: byId("productReleaseArtifactTarget").value,
      notes: byId("productReleaseNotes").value.trim(),
      operational,
    }, "productPrepareReleaseButton");
  });

  byId("productEvaluateLaunchButton").addEventListener("click", () =>
    productRequest("Evaluating launch readiness from recorded evidence…", `/product/projects/${encodeURIComponent(productSelectedId)}/launch-readiness`, {}, "productEvaluateLaunchButton")
  );
  byId("productApproveLaunchButton").addEventListener("click", () => {
    const note = byId("productLaunchNote").value.trim();
    if (!note) { showToast("A launch approval needs a named human note"); return; }
    productRequest("Recording human launch authorization only…", `/product/projects/${encodeURIComponent(productSelectedId)}/launch/approval`, { approved: true, note }, "productApproveLaunchButton");
  });
  byId("productRejectLaunchButton").addEventListener("click", () => {
    const note = byId("productLaunchNote").value.trim();
    if (!note) { showToast("A launch rejection needs a note"); return; }
    productRequest("Recording launch rejection…", `/product/projects/${encodeURIComponent(productSelectedId)}/launch/approval`, { approved: false, note }, "productRejectLaunchButton");
  });
  byId("productCancelButton").addEventListener("click", () => {
    const reason = window.prompt("Why is this product being cancelled?");
    if (!reason) { showToast("Cancellation needs a reason"); return; }
    productRequest("Cancelling pending and running product tasks…", `/product/projects/${encodeURIComponent(productSelectedId)}/cancel`, { reason }, "productCancelButton");
  });
  byId("productArchiveButton").addEventListener("click", () => {
    const reason = window.prompt("Archive note (optional)") || "Archived by operator.";
    productRequest("Archiving the product while retaining its records…", `/product/projects/${encodeURIComponent(productSelectedId)}/archive`, { reason }, "productArchiveButton");
  });

  refreshProductStatus().catch(() => {});
  refreshProductList().catch(() => {});
}

setupProductWorkspace();
