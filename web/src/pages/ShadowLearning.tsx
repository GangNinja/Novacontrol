import { useEffect, useMemo, useState } from "react";
import {
  ArrowRight,
  BrainCircuit,
  CheckCircle2,
  ChevronRight,
  CircleAlert,
  Clock3,
  Eye,
  FileCheck2,
  FlaskConical,
  Gauge,
  History,
  Pause,
  Play,
  Plus,
  RefreshCw,
  ShieldCheck,
  Square,
  StopCircle,
  XCircle,
} from "lucide-react";
import { apiPost } from "../api";
import { useApi, useDocumentTitle, navigate } from "../hooks";
import {
  arrayFrom,
  dateTime,
  money,
  num,
  pct,
  shortId,
  timeAgo,
  title,
} from "../lib";
import {
  Badge,
  Banner,
  Button,
  Card,
  CardHeader,
  ConfirmDialog,
  DataTable,
  EmptyState,
  ErrorBlock,
  Field,
  LoadingBlock,
  Metric,
  Modal,
  Money,
  PageHeader,
  SelectInput,
  StatusBadge,
  Tabs,
  TextArea,
  TextInput,
  useToast,
} from "../components/ui";

const SHADOW_ACTIONS: Record<string, string> = {
  start: "Start",
  pause: "Pause",
  resume: "Resume",
  stop: "Stop",
  complete: "Complete",
  cancel: "Cancel",
};

function shadowAction(status: string): string[] {
  switch (status) {
    case "created": return ["start", "cancel"];
    case "running": return ["pause", "stop", "complete", "cancel"];
    case "paused": return ["resume", "stop", "complete", "cancel"];
    case "stopped": return ["complete", "cancel"];
    case "failed": return ["cancel"];
    default: return [];
  }
}

export function ShadowPage() {
  useDocumentTitle("Shadow Mode");
  const toast = useToast();
  const sessions = useApi<any>("/market/shadow/sessions?limit=100");
  const portfolios = useApi<any>("/market/portfolios?limit=100");
  const strategies = useApi<any>("/market/strategies?limit=100");
  const instruments = useApi<any>("/market/instruments?limit=200");
  const [selectedId, setSelectedId] = useState("");
  const [createOpen, setCreateOpen] = useState(false);
  const [name, setName] = useState("");
  const [portfolioId, setPortfolioId] = useState("");
  const [instrumentIds, setInstrumentIds] = useState("");
  const [strategyIds, setStrategyIds] = useState("");
  const [saving, setSaving] = useState(false);
  const [action, setAction] = useState<{ session: any; action: string } | null>(null);
  const rows = arrayFrom(sessions.data, "sessions");
  const portfolioRows = arrayFrom(portfolios.data, "portfolios");
  const strategyRows = arrayFrom(strategies.data, "strategies");
  const current = rows.find((row) => row.session_id === selectedId) || rows[0];
  const history = useApi<any>(current ? `/market/shadow/sessions/${encodeURIComponent(current.session_id)}/history` : null);
  const decisionLog = useApi<any>(current ? `/market/shadow/sessions/${encodeURIComponent(current.session_id)}/decisions?limit=100` : null);

  useEffect(() => {
    if (!selectedId && rows.length) setSelectedId(rows[0].session_id);
  }, [rows, selectedId]);
  useEffect(() => {
    if (!portfolioId && portfolioRows.length) setPortfolioId(portfolioRows[0].portfolio_id);
  }, [portfolioRows, portfolioId]);
  useEffect(() => {
    if (!strategyIds && strategyRows.length) setStrategyIds(strategyRows[0].strategy_id);
  }, [strategyRows, strategyIds]);

  const create = async () => {
    setSaving(true);
    try {
      const result = await apiPost<any>("/market/shadow/sessions", {
        name,
        portfolio_id: portfolioId,
        instruments: instrumentIds.split(",").map((item) => item.trim()).filter(Boolean),
        strategies: strategyIds.split(",").map((item) => item.trim()).filter(Boolean),
      });
      toast.push("Shadow session created. It has not placed an order.", "success");
      setCreateOpen(false);
      sessions.refresh();
      if (result?.session?.session_id) setSelectedId(result.session.session_id);
    } catch (error: any) {
      toast.push(error?.detail || "The shadow session could not be created.", "error");
    } finally { setSaving(false); }
  };

  const runAction = async () => {
    if (!action) return;
    setSaving(true);
    try {
      await apiPost(`/market/shadow/sessions/${encodeURIComponent(action.session.session_id)}/${action.action}`, { reason: `operator ${action.action} from standalone desk` });
      toast.push(`Shadow session ${action.action} accepted.`, "success");
      setAction(null);
      sessions.refresh();
      history.refresh();
      decisionLog.refresh();
    } catch (error: any) {
      toast.push(error?.detail || "The shadow transition was refused.", "error");
    } finally { setSaving(false); }
  };

  const historyData = history.data || {};
  const counts = historyData.counts || {};
  const decisions = arrayFrom(decisionLog.data, "decisions");

  return (
    <div className="page">
      <PageHeader eyebrow="Trading intelligence / Shadow" title={<>Observe the strategy, <em>never touch the book.</em></>} description="Shadow sessions record hypothetical decisions and projections. They have no wallet, ledger, portfolio or execution gateway, and every result is labelled hypothetical."
        meta={<><span className="meta-item"><Eye size={13} /> {sessions.data?.total ?? rows.length} sessions</span><span className="meta-item"><ShieldCheck size={13} /> No execution connection</span><span className="meta-item"><Clock3 size={13} /> {current?.last_cycle_at ? `Cycled ${timeAgo(current.last_cycle_at)}` : "No cycle yet"}</span></>}
        actions={<><Button variant="ghost" icon={<RefreshCw size={14} />} onClick={() => { sessions.refresh(); history.refresh(); decisionLog.refresh(); }}>Refresh</Button><Button variant="primary" icon={<Plus size={15} />} onClick={() => setCreateOpen(true)} disabled={!portfolioRows.length || !strategyRows.length}>New session</Button></>}
      />
      <Banner tone="warn" title="Hypothetical only">A shadow session does not mutate a wallet, ledger or paper portfolio. It is a separate observational record, not a parallel trading account.</Banner>
      {sessions.error && <ErrorBlock message={sessions.error.detail} onRetry={sessions.refresh} />}
      <div className="metric-grid metric-grid-four">
        <Metric label="Running" value={rows.filter((row) => row.status === "running").length} help="Observing" icon={<Play size={16} />} tone="good" />
        <Metric label="Paused" value={rows.filter((row) => row.status === "paused").length} help="Waiting" icon={<Pause size={16} />} tone="warn" />
        <Metric label="Cycles" value={current?.cycles ?? "—"} help="Recorded for selected" icon={<Gauge size={16} />} />
        <Metric label="Decisions" value={current?.decision_count ?? "—"} help="Hypothetical" icon={<BrainCircuit size={16} />} />
      </div>
      <div className="dashboard-grid">
        <Card><CardHeader title="Shadow sessions" subtitle="Lifecycle-controlled observation" action={<Badge tone="neutral">{rows.length}</Badge>} />{sessions.loading ? <LoadingBlock rows={4} label="Reading shadow sessions…" /> : <div className="rail-list">{rows.map((row) => <button type="button" className={`rail-item ${current?.session_id === row.session_id ? "rail-item-active" : ""}`} key={row.session_id} onClick={() => setSelectedId(row.session_id)}><span className="rail-icon"><Eye size={15} /></span><span><strong>{row.name || "Shadow session"}</strong><small>{row.portfolio_id || "Baseline snapshot"} · {timeAgo(row.created_at)}</small></span><StatusBadge value={row.status} /></button>)}</div>}{!rows.length && !sessions.loading && <EmptyState icon={<Eye size={22} />} title="No shadow sessions" description="Create a session from a portfolio and at least one strategy." action={<Button variant="primary" size="sm" onClick={() => setCreateOpen(true)}>Create session</Button>} />}</Card>
        <div className="stack">
          {current ? <Card><CardHeader title={current.name || "Shadow session"} subtitle={`${current.portfolio_id || "Baseline"} · ${shortId(current.session_id, 20)}`} action={<StatusBadge value={current.status} />} /><div className="metric-grid metric-grid-four"><Metric label="Cycles" value={current.cycles ?? 0} /><Metric label="Decisions" value={current.decision_count ?? 0} /><Metric label="Baseline" value={money(current.baseline?.total_value)} /><Metric label="Data source" value={current.market_data_source || "—"} /></div><div className="row-actions shadow-actions">{shadowAction(current.status).map((item) => <Button key={item} size="sm" variant={item === "cancel" || item === "stop" ? "danger" : item === "start" || item === "resume" ? "primary" : "secondary"} icon={item === "start" || item === "resume" ? <Play size={13} /> : item === "pause" ? <Pause size={13} /> : <Square size={13} />} onClick={() => setAction({ session: current, action: item })}>{SHADOW_ACTIONS[item]}</Button>)}</div><div className="contract-list"><div><CheckCircle2 size={15} /><span>{current.simulated ? "Backend marks this session as simulated." : "Hypothetical observation."}</span></div><div><CheckCircle2 size={15} /><span>{current.execution_connected === false ? "No execution gateway is attached." : "Execution connection must be verified."}</span></div><div><CheckCircle2 size={15} /><span>Every decision is labelled hypothetical; never included in portfolio value.</span></div></div></Card> : <Card><EmptyState title="No session selected" description="Create a shadow session to inspect its hypothetical history." /></Card>}
        </div>
      </div>
      {current && <Card className="tabs-card"><div className="tabs-head"><CardHeader title="Session history" subtitle="Recorded events from the selected session" /><Badge tone="warn">Hypothetical</Badge></div>{history.loading || decisionLog.loading ? <LoadingBlock rows={4} label="Reading session history…" /> : <><div className="metric-grid metric-grid-four"><Metric label="Decisions" value={counts.decisions ?? decisions.length} /><Metric label="Projections" value={counts.projections ?? 0} /><Metric label="Outcomes" value={counts.outcomes ?? 0} /><Metric label="Errors" value={counts.errors ?? 0} tone={counts.errors ? "bad" : "neutral"} /></div><DataTable rows={decisions.slice(0, 50)} rowKey={(row, index) => row.decision_id || String(index)} empty={<EmptyState title="No decisions yet" description="Start or cycle the session to produce hypothetical decisions." />} columns={[{ key: "decision_at", label: "Recorded", render: (row) => dateTime(row.decision_at || row.created_at) }, { key: "instrument_id", label: "Instrument", render: (row) => row.instrument_id || "—" }, { key: "action", label: "Action", render: (row) => <Badge tone="neutral">{title(row.action || row.signal_type || "decision")}</Badge> }, { key: "reason", label: "Reason", render: (row) => <span className="clamp-cell">{row.reason || row.explanation || "—"}</span> }, { key: "projected_value", label: "Projection", align: "right", render: (row) => <Money value={row.projected_value ?? row.notional} /> }]} /></>}</Card>}
      <Modal open={createOpen} onClose={() => setCreateOpen(false)} title="Create a shadow session" description="A session needs a portfolio baseline, at least one instrument and at least one strategy. It will not place an order." footer={<><Button variant="ghost" onClick={() => setCreateOpen(false)}>Cancel</Button><Button variant="primary" loading={saving} disabled={!name.trim() || !portfolioId || !instrumentIds.trim() || !strategyIds.trim()} onClick={create}>Create session</Button></>}>
        <div className="form-grid"><Field label="Session name"><TextInput value={name} onChange={(event) => setName(event.target.value)} placeholder="e.g. Momentum shadow" /></Field><Field label="Portfolio baseline"><SelectInput value={portfolioId} onChange={(event) => setPortfolioId(event.target.value)}><option value="">Choose a portfolio…</option>{portfolioRows.map((row) => <option key={row.portfolio_id} value={row.portfolio_id}>{row.name || row.portfolio_id}</option>)}</SelectInput></Field><Field label="Instruments" hint="Canonical IDs, comma separated."><TextInput value={instrumentIds} onChange={(event) => setInstrumentIds(event.target.value)} placeholder="XNSE:RELIANCE, XNSE:TCS" /></Field><Field label="Strategies" hint="Strategy IDs, comma separated."><TextInput value={strategyIds} onChange={(event) => setStrategyIds(event.target.value)} placeholder="strat_..." /></Field></div>
        <div className="inline-warning">Shadow mode is observation only: no wallet, no ledger, no paper order and no live broker.</div>
      </Modal>
      <ConfirmDialog open={!!action} title={`${SHADOW_ACTIONS[action?.action || ""] || "Change"} this shadow session?`} description="The backend checks the session lifecycle and records the transition. No financial book is changed." confirmLabel={SHADOW_ACTIONS[action?.action || ""] || "Confirm"} tone={action?.action === "cancel" || action?.action === "stop" ? "danger" : "primary"} busy={saving} onConfirm={runAction} onClose={() => setAction(null)} />
    </div>
  );
}

export function LearningPage() {
  useDocumentTitle("Adaptive Learning");
  const toast = useToast();
  const dashboard = useApi<any>("/market/learning/dashboard", { refreshMs: 60_000 });
  const health = useApi<any>("/market/learning/health");
  const strategies = useApi<any>("/market/strategies?limit=100");
  const [tab, setTab] = useState("queue");
  const [target, setTarget] = useState<{ candidate: any; action: string } | null>(null);
  const [reason, setReason] = useState("");
  const [saving, setSaving] = useState(false);
  const data = dashboard.data || {};
  const queue = Array.isArray(data.approval_queue) ? data.approval_queue : [];
  const experiments = data.experiments?.recent || [];
  const alerts = Array.isArray(data.alerts) ? data.alerts : [];
  const candidates = [...queue, ...(data.candidates?.approved || []), ...(data.candidates?.pending_approval || [])].filter((row, index, list) => list.findIndex((item) => item.candidate_id === row.candidate_id) === index);
  const activeVersions = data.active_versions || {};
  const strategyNames = new Map(arrayFrom(strategies.data, "strategies").map((row) => [row.strategy_id, row.name]));
  const activeAlerts = alerts.filter((row) => String(row.status || "open").toLowerCase() === "open");

  const openAction = (candidate: any, action: string) => {
    setTarget({ candidate, action });
    setReason("");
  };

  const runAction = async () => {
    if (!target) return;
    setSaving(true);
    try {
      const base = `/market/learning/candidates/${encodeURIComponent(target.candidate.candidate_id)}`;
      if (target.action === "approve") await apiPost(`${base}/approve`, { actor: "operator", note: reason || "approved from governance queue" });
      else if (target.action === "reject") await apiPost(`${base}/reject`, { actor: "operator", reason });
      else if (target.action === "evaluate") await apiPost(`${base}/evaluate`, {});
      else if (target.action === "shadow-validate") await apiPost(`${base}/shadow-validate`, {});
      else if (target.action === "promote") await apiPost(`${base}/promote`, { actor: "operator", description: "promoted from the governance queue" });
      toast.push(`Candidate ${target.action} request accepted.`, "success");
      setTarget(null);
      dashboard.refresh();
      health.refresh();
    } catch (error: any) {
      toast.push(error?.detail || "The learning action was refused.", "error");
    } finally { setSaving(false); }
  };

  const experimentAction = async (experiment: any, action: string) => {
    setSaving(true);
    try {
      await apiPost(`/market/learning/experiments/${encodeURIComponent(experiment.experiment_id)}/${action}`, {});
      toast.push(`Experiment ${action} accepted.`, "success");
      dashboard.refresh();
    } catch (error: any) {
      toast.push(error?.detail || "The experiment action was refused.", "error");
    } finally { setSaving(false); }
  };

  const acknowledge = async (alert: any) => {
    setSaving(true);
    try {
      await apiPost(`/market/learning/alerts/${encodeURIComponent(alert.alert_id)}/acknowledge`, { actor: "operator" });
      toast.push("Learning alert acknowledged.", "success");
      dashboard.refresh();
    } catch (error: any) {
      toast.push(error?.detail || "The alert could not be acknowledged.", "error");
    } finally { setSaving(false); }
  };

  const requiresReason = target?.action === "reject";
  return (
    <div className="page">
      <PageHeader eyebrow="Trading intelligence / Learning" title={<>Adaptive learning, <em>with a human gate.</em></>} description="Evidence, out-of-sample replay, risk compatibility and explicit approval precede any promoted strategy version. Learning never executes a trade."
        meta={<><span className="meta-item"><FlaskConical size={13} /> {experiments.length} experiments</span><span className="meta-item"><FileCheck2 size={13} /> {queue.length} awaiting approval</span><span className="meta-item"><ShieldCheck size={13} /> {activeAlerts.length} open alerts</span></>}
        actions={<><Button variant="ghost" icon={<RefreshCw size={14} />} onClick={() => { dashboard.refresh(); health.refresh(); }}>Refresh</Button><Button variant="secondary" onClick={() => navigate("strategies")} icon={<ArrowRight size={14} />}>Strategy versions</Button></>}
      />
      <Banner tone="info" title="Governance is the feature">A high score is not permission. Approval, risk compatibility, optional shadow validation and a versioned promotion are separate recorded steps.</Banner>
      {dashboard.error && <ErrorBlock message={dashboard.error.detail} onRetry={dashboard.refresh} />}
      <div className="metric-grid metric-grid-four">
        <Metric label="Experiments" value={experiments.length} help="Recent learning runs" icon={<FlaskConical size={16} />} />
        <Metric label="Approval queue" value={queue.length} help="Manual review default" icon={<FileCheck2 size={16} />} tone={queue.length ? "warn" : "good"} />
        <Metric label="Active versions" value={Object.keys(activeVersions).length} help="Promoted and recorded" icon={<ShieldCheck size={16} />} tone="good" />
        <Metric label="Open alerts" value={activeAlerts.length} help="Deterioration and anomalies" icon={<CircleAlert size={16} />} tone={activeAlerts.length ? "warn" : "good"} />
      </div>
      <Card className="tabs-card"><h2 className="sr-only">Learning governance</h2><div className="tabs-head"><Tabs active={tab} onChange={setTab} tabs={[{ id: "queue", label: "Approval queue", count: queue.length }, { id: "experiments", label: "Experiments", count: experiments.length }, { id: "alerts", label: "Alerts", count: activeAlerts.length }, { id: "versions", label: "Active versions" }]} /><Badge tone="info">NM-13</Badge></div>
        {tab === "queue" && <div className="stack">{!queue.length && <EmptyState icon={<CheckCircle2 size={22} />} title="Approval queue is clear" description="No candidate is waiting for an explicit governance decision." />}{queue.map((candidate) => <article className="governance-card" key={candidate.candidate_id}><div className="governance-head"><div><div className="eyebrow">{strategyNames.get(candidate.strategy_id) || candidate.strategy_id}</div><h3>{title(candidate.catalog_id || "Candidate")} <span>·</span> {shortId(candidate.candidate_id, 14)}</h3><p>{candidate.reasons?.join(" · ") || "Awaiting an operator decision."}</p></div><StatusBadge value={candidate.status} /></div><div className="governance-metrics"><div><span>Objective</span><strong>{title(candidate.objective || "risk adjusted return")}</strong></div><div><span>Risk compliant</span><strong>{candidate.risk?.compliant ? "Yes" : "No"}</strong></div><div><span>Shadow validation</span><strong>{title(candidate.shadow_validation?.status || "not required")}</strong></div><div><span>Out-of-sample</span><strong>{candidate.evaluation?.validation ? "Recorded" : "Not evaluated"}</strong></div></div><div className="row-actions"><Button size="sm" variant="secondary" onClick={() => openAction(candidate, "evaluate")}>Evaluate</Button><Button size="sm" variant="secondary" onClick={() => openAction(candidate, "shadow-validate")}>Shadow validate</Button><Button size="sm" variant="danger" onClick={() => openAction(candidate, "reject")}>Reject</Button><Button size="sm" variant="primary" onClick={() => openAction(candidate, "approve")}>Approve</Button></div></article>)}</div>}
        {tab === "experiments" && (experiments.length ? <DataTable rows={experiments} rowKey={(row) => row.experiment_id} columns={[{ key: "experiment_id", label: "Experiment", render: (row) => <div><strong>{row.catalog_id || "Experiment"}</strong><small className="table-sub">{shortId(row.experiment_id, 18)}</small></div> }, { key: "strategy_id", label: "Strategy", render: (row) => strategyNames.get(row.strategy_id) || row.strategy_id }, { key: "status", label: "State", render: (row) => <StatusBadge value={row.status} /> }, { key: "method", label: "Method", render: (row) => row.method || "—" }, { key: "requested_candidates", label: "Candidates", align: "right" }, { key: "created_at", label: "Created", render: (row) => timeAgo(row.created_at) }, { key: "action", label: "", align: "right", render: (row) => <div className="row-actions">{row.status === "created" && <Button size="sm" variant="secondary" onClick={() => experimentAction(row, "start")}>Start</Button>}{["created", "evaluating"].includes(row.status) && <Button size="sm" variant="ghost" onClick={() => experimentAction(row, "cancel")}>Cancel</Button>}</div> }]} /> : <EmptyState icon={<FlaskConical size={22} />} title="No learning experiments" description="Experiments are created from a strategy, explicit windows and evidence through the API." />)}
        {tab === "alerts" && (activeAlerts.length ? <div className="alert-stack">{activeAlerts.map((alert) => <div className="learning-alert" key={alert.alert_id}><span className={`alert-badge alert-${String(alert.severity || "warning").toLowerCase()}`}><CircleAlert size={14} /></span><div><strong>{alert.message || alert.kind}</strong><small>{strategyNames.get(alert.strategy_id) || alert.strategy_id} · {timeAgo(alert.created_at)}</small></div><Button size="sm" variant="ghost" loading={saving} onClick={() => acknowledge(alert)}>Acknowledge</Button></div>)}</div> : <EmptyState icon={<CheckCircle2 size={22} />} title="No open learning alerts" description="No deterioration, rollback recommendation or coverage anomaly is open." />)}
        {tab === "versions" && (Object.keys(activeVersions).length ? <DataTable rows={Object.entries(activeVersions).map(([strategyId, versionId]) => ({ strategyId, versionId }))} rowKey={(row) => row.strategyId} columns={[{ key: "strategyId", label: "Strategy", render: (row) => strategyNames.get(row.strategyId) || row.strategyId }, { key: "versionId", label: "Active version", render: (row) => <span className="mono-small">{String(row.versionId)}</span> }, { key: "state", label: "State", render: () => <Badge tone="good">Promoted</Badge> }, { key: "action", label: "", align: "right", render: () => <Button size="sm" variant="ghost" onClick={() => navigate("strategies")}>Inspect strategy</Button> }]} /> : <EmptyState icon={<ShieldCheck size={22} />} title="No active learning versions" description="A promoted candidate appears only after approval and a recorded promotion." />)}
      </Card>
      <div className="two-col-grid"><Card><CardHeader title="Evidence sources" subtitle="Kept distinct, never averaged" />{data.evidence ? <pre className="json-preview">{JSON.stringify(data.evidence, null, 2)}</pre> : <EmptyState title="No evidence summary" description="The learning dashboard did not return evidence aggregates." />}</Card><Card><CardHeader title="Health" subtitle="Learning governance state" action={<StatusBadge value={health.data?.healthy ? "healthy" : "degraded"} />} /><div className="contract-list"><div><CheckCircle2 size={15} /><span>{health.data?.reason || "The learning health report was not returned."}</span></div><div><CheckCircle2 size={15} /><span>No live order, wallet or ledger mutation path exists in NM-13.</span></div><div><CheckCircle2 size={15} /><span>Automatic approval remains off unless explicitly configured on the experiment.</span></div></div><Button size="sm" variant="secondary" onClick={() => navigate("strategies")}>Review strategy versions</Button></Card></div>
      <Modal open={!!target} onClose={() => setTarget(null)} title={target ? `${title(target.action)} candidate` : "Governance action"} description={target ? `${strategyNames.get(target.candidate.strategy_id) || target.candidate.strategy_id} · ${shortId(target.candidate.candidate_id, 18)}` : ""} footer={<><Button variant="ghost" onClick={() => setTarget(null)}>Cancel</Button><Button variant={target?.action === "reject" ? "danger" : "primary"} loading={saving} disabled={requiresReason && !reason.trim()} onClick={runAction}>{title(target?.action || "Confirm")}</Button></>}>
        <Banner tone="info" title="This is a governance decision, not a trade">The backend records the actor, timestamp and result. Promotion additionally requires an approved, risk-compliant candidate and a configured strategy applier.</Banner>
        {(target?.action === "approve" || target?.action === "reject") && <Field label={target.action === "reject" ? "Reason (required)" : "Note"}><TextArea value={reason} onChange={(event) => setReason(event.target.value)} placeholder={target.action === "reject" ? "Why this candidate is rejected" : "Optional context for the approval record"} rows={4} /></Field>}
        {target?.action === "shadow-validate" && <div className="inline-note"><strong>Shadow validation uses NM-12.</strong><p>The backend will run the candidate in the isolated shadow service and reject it if validation fails.</p></div>}
      </Modal>
    </div>
  );
}
