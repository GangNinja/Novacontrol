import { useEffect, useMemo, useState } from "react";
import {
  Activity,
  AlertTriangle,
  ArrowRight,
  BarChart3,
  CheckCircle2,
  ChevronRight,
  CircleAlert,
  Clock3,
  Crosshair,
  Gauge,
  History,
  Pause,
  Play,
  Plus,
  RefreshCw,
  Search,
  ShieldCheck,
  SlidersHorizontal,
  Sparkles,
  Target,
  TrendingDown,
  TrendingUp,
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
  pctUnsigned,
  quantity,
  shortDate,
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
  SearchInput,
  SelectInput,
  StatusBadge,
  Tabs,
  TextArea,
  TextInput,
  useToast,
} from "../components/ui";
import { LineChart } from "../components/charts";

const STRATEGY_ACTIONS: Record<string, string> = { activate: "Activate", pause: "Pause", suspend: "Suspend", retire: "Retire", degrade: "Mark degraded" };

export function StrategiesPage() {
  useDocumentTitle("Strategies");
  const toast = useToast();
  const strategies = useApi<any>("/market/strategies?limit=100");
  const status = useApi<any>("/market/strategies/status");
  const catalog = useApi<any>("/market/strategies/catalog");
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState<any | null>(null);
  const [versions, setVersions] = useState<any>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [catalogId, setCatalogId] = useState("");
  const [name, setName] = useState("");
  const [saving, setSaving] = useState(false);
  const [transition, setTransition] = useState<{ strategy: any; action: string } | null>(null);
  const portfolios = useApi<any>("/market/portfolios?limit=100");
  const proposals = useApi<any>("/market/strategies/proposals?limit=50");
  const screener = useApi<any>("/market/screener?limit=20");
  const [runOpen, setRunOpen] = useState(false);
  const [runPortfolio, setRunPortfolio] = useState("");
  const [runInstruments, setRunInstruments] = useState("");
  const [runResult, setRunResult] = useState<any>(null);
  const rows = arrayFrom(strategies.data, "strategies");
  const catalogRows = arrayFrom(catalog.data, "catalog", "strategies", "items");
  const filtered = useMemo(() => {
    const text = search.trim().toLowerCase();
    return rows.filter((row) => !text || [row.name, row.catalog_id, row.strategy_id, row.category].some((value) => String(value || "").toLowerCase().includes(text)));
  }, [rows, search]);
  const selectedCatalog = catalogRows.find((item) => item.catalog_id === catalogId);
  const portfolioRows = arrayFrom(portfolios.data, "portfolios");
  const proposalRows = arrayFrom(proposals.data, "proposals");
  const screenerRows = arrayFrom(screener.data, "rows");

  useEffect(() => {
    if (!runPortfolio && portfolioRows.length) setRunPortfolio(portfolioRows[0].portfolio_id);
  }, [portfolioRows, runPortfolio]);

  const runStrategies = async () => {
    setSaving(true);
    try {
      const instrumentIds = runInstruments.split(/[\s,]+/).map((item) => item.trim()).filter(Boolean);
      const result = await apiPost("/market/strategies/run", { portfolio_id: runPortfolio, instrument_ids: instrumentIds, conflict_policy: "review" });
      setRunResult(result);
      const proposalCount = Array.isArray(result?.proposals) ? result.proposals.length : 0;
      toast.push(`Strategy run completed with ${proposalCount} risk-gated proposal(s).`, "success");
      strategies.refresh();
      status.refresh();
      proposals.refresh();
    } catch (error: any) {
      toast.push(error?.detail || "The strategy run was refused.", "error");
    } finally { setSaving(false); }
  };

  const forwardProposal = async (row: any) => {
    setSaving(true);
    try {
      await apiPost(`/market/strategies/proposals/${encodeURIComponent(row.proposal_id)}/forward`, {});
      toast.push("Proposal forwarded to paper execution.", "success");
      proposals.refresh();
      navigate("paper");
    } catch (error: any) {
      toast.push(error?.detail || "The proposal could not be forwarded.", "error");
    } finally { setSaving(false); }
  };

  const openDetail = async (row: any) => {
    setSelected(row);
    setVersions(null);
    try {
      const result = await (await import("../api")).apiGet(`/market/strategies/${encodeURIComponent(row.strategy_id)}/versions`);
      setVersions(result);
    } catch {
      setVersions({ ok: false, versions: [] });
    }
  };

  const create = async () => {
    setSaving(true);
    try {
      await apiPost("/market/strategies", {
        catalog_id: catalogId,
        name,
        config: selectedCatalog?.default_config || {},
        horizons: selectedCatalog?.horizons || undefined,
        asset_classes: selectedCatalog?.asset_classes || undefined,
      });
      toast.push("Strategy created from the catalogue defaults.", "success");
      setCreateOpen(false);
      setName("");
      strategies.refresh();
      status.refresh();
    } catch (error: any) {
      toast.push(error?.detail || "The strategy could not be created.", "error");
    } finally { setSaving(false); }
  };

  const runTransition = async () => {
    if (!transition) return;
    setSaving(true);
    try {
      await apiPost(`/market/strategies/${encodeURIComponent(transition.strategy.strategy_id)}/${transition.action}`, { reason: `operator ${transition.action} from standalone desk` });
      toast.push(`Strategy ${transition.action} request accepted.`, "success");
      setTransition(null);
      strategies.refresh();
      status.refresh();
    } catch (error: any) {
      toast.push(error?.detail || "The strategy transition was refused.", "error");
    } finally { setSaving(false); }
  };

  const counts = status.data?.counts || status.data?.strategies || {};
  return (
    <div className="page">
      <PageHeader
        eyebrow="Trading intelligence / Strategies"
        title={<>Signal engines, <em>with their limits visible.</em></>}
        description="Strategy state, configuration versions, signals and risk-gated proposals. No strategy can bypass the independent NM-08 gate."
        meta={<><span className="meta-item"><Target size={13} /> {strategies.data?.total ?? rows.length} strategies</span><span className="meta-item"><Activity size={13} /> {status.data?.signals ?? status.data?.counts?.signals ?? "—"} signals</span><span className="meta-item"><Sparkles size={13} /> {status.data?.proposals ?? status.data?.counts?.proposals ?? "—"} proposals</span></>}
        actions={<><Button variant="ghost" icon={<RefreshCw size={14} />} onClick={() => { strategies.refresh(); status.refresh(); proposals.refresh(); }}>Refresh</Button><Button variant="secondary" icon={<Crosshair size={15} />} onClick={() => setRunOpen(true)} disabled={!portfolioRows.length}>Run strategies</Button><Button variant="primary" icon={<Plus size={15} />} onClick={() => { setCatalogId(catalogRows[0]?.catalog_id || ""); setCreateOpen(true); }} disabled={!catalogRows.length}>New strategy</Button></>}
      />
      {strategies.error && <ErrorBlock message={strategies.error.detail} onRetry={strategies.refresh} />}
      <div className="metric-grid metric-grid-four">
        <Metric label="Active" value={rows.filter((row) => row.status === "active").length} help="Eligible to evaluate" icon={<CheckCircle2 size={16} />} tone="good" />
        <Metric label="Paused / suspended" value={rows.filter((row) => ["paused", "suspended"].includes(row.status)).length} help="Not evaluating" icon={<Pause size={16} />} tone="warn" />
        <Metric label="Signals" value={status.data?.counts?.signals ?? status.data?.signals ?? "—"} help="Stored strategy signals" icon={<Crosshair size={16} />} />
        <Metric label="Conflicts" value={status.data?.counts?.conflicts ?? status.data?.conflicts ?? "—"} help="Require review" icon={<AlertTriangle size={16} />} tone="warn" />
      </div>
      <Card>
        <CardHeader title="Strategy register" subtitle="Catalogued implementations and lifecycle state" action={<Badge tone="neutral">{filtered.length} shown</Badge>} />
        <div className="filter-row"><SearchInput value={search} onChange={setSearch} placeholder="Search strategy, catalogue or category…" /><Badge tone="info" dot>Risk decisions are independent</Badge></div>
        {strategies.loading ? <LoadingBlock rows={5} label="Reading strategies…" /> : (
          <DataTable rows={filtered} rowKey={(row) => row.strategy_id} onRowClick={openDetail} empty={<EmptyState icon={<Target size={22} />} title={rows.length ? "No strategies match" : "No strategies configured"} description={rows.length ? "Adjust the search." : "Create a strategy from the catalogue to begin."} action={!rows.length ? <Button variant="primary" size="sm" onClick={() => { setCatalogId(catalogRows[0]?.catalog_id || ""); setCreateOpen(true); }}>Create strategy</Button> : undefined} />} columns={[
          { key: "name", label: "Strategy", sortable: true, render: (row) => <div className="identity-cell"><span className="symbol-mark">{String(row.name || "S").slice(0, 2)}</span><div><strong>{row.name || row.catalog_id || "Strategy"}</strong><small>{row.catalog_id || "catalogue"} · {shortId(row.strategy_id, 12)}</small></div></div> },
          { key: "category", label: "Category", render: (row) => <Badge tone="neutral">{title(row.category || "—")}</Badge> },
          { key: "status", label: "State", render: (row) => <StatusBadge value={row.status} /> },
          { key: "version", label: "Version", render: (row) => <span className="mono-small">{row.version || "—"}</span> },
          { key: "config", label: "Configuration", render: (row) => <span className="mono-small">{Object.entries(row.config || {}).slice(0, 2).map(([key, value]) => `${key}=${value}`).join(" · ") || "—"}</span> },
          { key: "last_evaluated_at", label: "Last evaluated", render: (row) => timeAgo(row.last_evaluated_at), sortable: true, sortValue: (row) => row.last_evaluated_at || "" },
          { key: "action", label: "", align: "right", render: (row) => <ChevronRight size={14} className="row-chevron" /> },
        ]} />
        )}
      </Card>
      <Card>
        <CardHeader title="Proposals" subtitle="Risk-gated actions; only an approved, unexpired proposal can reach paper execution" action={<Button size="sm" variant="ghost" icon={<RefreshCw size={13} />} onClick={() => { proposals.refresh(); status.refresh(); }}>Reload</Button>} />
        {proposals.loading ? <LoadingBlock rows={3} label="Reading proposals…" /> : (
          <DataTable rows={proposalRows} rowKey={(row) => row.proposal_id} empty={<EmptyState icon={<Crosshair size={22} />} title="No proposals yet" description="Run an active strategy to evaluate stored data and create risk-gated proposals." action={<Button size="sm" variant="primary" onClick={() => setRunOpen(true)} disabled={!portfolioRows.length}>Run strategies</Button>} />} columns={[
            { key: "instrument_id", label: "Instrument", render: (row) => <span className="mono-small">{row.instrument_id || "—"}</span> },
            { key: "side", label: "Side", render: (row) => <Badge tone={String(row.side || "").toLowerCase() === "buy" ? "good" : "bad"}>{title(row.side || "—")}</Badge> },
            { key: "status", label: "State", render: (row) => <StatusBadge value={row.status} /> },
            { key: "notional", label: "Notional", align: "right", render: (row) => <Money value={row.notional} /> },
            { key: "risk_outcome", label: "Risk", render: (row) => row.risk_outcome ? <StatusBadge value={row.risk_outcome} /> : <Badge tone="neutral">Pending</Badge> },
            { key: "expires_at", label: "Expires", render: (row) => timeAgo(row.expires_at) },
            { key: "action", label: "", align: "right", render: (row) => String(row.status || "").toLowerCase() === "approved" ? <div className="row-actions"><Button size="sm" variant="secondary" onClick={(event) => { event.stopPropagation(); forwardProposal(row); }}>Forward to paper</Button></div> : <span className="muted">—</span> },
          ]} />
        )}
      </Card>
      <div className="two-col-grid">
        <Card><CardHeader title="Catalogue" subtitle="Implemented families, not placeholders" action={<Badge tone="neutral">{catalogRows.length}</Badge>} />{catalogRows.length ? <div className="catalog-list">{catalogRows.slice(0, 8).map((item) => <div className="catalog-row" key={item.catalog_id}><div><strong>{item.name || item.catalog_id}</strong><span>{item.description || title(item.category || "strategy")}</span></div><Badge tone="neutral">{title(item.category || "family")}</Badge></div>)}</div> : <EmptyState title="Catalogue unavailable" description="The backend did not return a strategy catalogue." />}</Card>
        <Card><CardHeader title="Risk boundary" subtitle="The non-negotiable path" /><div className="contract-list"><div><CheckCircle2 size={15} /><span>Signals become proposals; proposals are not orders.</span></div><div><CheckCircle2 size={15} /><span>Every proposal carries a risk decision and expiry.</span></div><div><CheckCircle2 size={15} /><span>Paper execution is the only execution path in this build.</span></div><div><CheckCircle2 size={15} /><span>Strategy transitions are recorded with a reason.</span></div></div><Button variant="secondary" size="sm" onClick={() => navigate("risk")} icon={<ShieldCheck size={14} />}>Open risk management</Button></Card>
      </div>

      <Modal open={!!selected} onClose={() => setSelected(null)} title={selected?.name || "Strategy"} description={selected ? `${title(selected.category || "strategy")} · ${selected.catalog_id || "catalogue"} · version ${selected.version || "—"}` : ""} wide footer={<><Button variant="ghost" onClick={() => setSelected(null)}>Close</Button>{selected && ["active", "paused", "under_review", "degraded"].includes(selected.status) && <Button variant="secondary" onClick={() => setTransition({ strategy: selected, action: selected.status === "active" ? "pause" : "activate" })}>{selected.status === "active" ? "Pause strategy" : "Activate strategy"}</Button>}<Button variant="primary" onClick={() => { setSelected(null); navigate("paper"); }}>Paper execution</Button></>}>
        {selected && <div className="detail-grid"><div className="detail-main"><div className="detail-section"><h3>Configuration</h3><pre className="code-block">{JSON.stringify(selected.config || {}, null, 2)}</pre></div><div className="detail-section"><h3>Version history</h3>{versions?.versions?.length ? <DataTable compact rows={versions.versions} rowKey={(row) => row.version || row.created_at} columns={[{ key: "version", label: "Version" }, { key: "status", label: "State", render: (row) => <StatusBadge value={row.status || "recorded"} /> }, { key: "created_at", label: "Recorded", render: (row) => dateTime(row.created_at) }]} /> : <p className="muted">No version history was returned for this strategy.</p>}</div></div><aside className="detail-aside"><div className="detail-fact"><span>Lifecycle</span><StatusBadge value={selected.status} /></div><div className="detail-fact"><span>Horizons</span><span>{Array.isArray(selected.horizons) ? selected.horizons.map(title).join(", ") : "—"}</span></div><div className="detail-fact"><span>Asset classes</span><span>{Array.isArray(selected.asset_classes) ? selected.asset_classes.map(title).join(", ") : "—"}</span></div><div className="detail-fact"><span>Last evaluated</span><span>{dateTime(selected.last_evaluated_at)}</span></div><div className="detail-fact"><span>Strategy ID</span><code>{selected.strategy_id}</code></div></aside></div>}
      </Modal>

      <Modal open={createOpen} onClose={() => setCreateOpen(false)} title="Create a strategy" description="The selected catalogue item supplies its declared default configuration and universe constraints." footer={<><Button variant="ghost" onClick={() => setCreateOpen(false)}>Cancel</Button><Button variant="primary" loading={saving} disabled={!catalogId || !name.trim()} onClick={create}>Create strategy</Button></>}>
        <div className="form-grid"><Field label="Catalogue family"><SelectInput value={catalogId} onChange={(event) => { setCatalogId(event.target.value); setName(event.target.value ? title(event.target.value) : ""); }}><option value="">Choose a family…</option>{catalogRows.map((item) => <option key={item.catalog_id} value={item.catalog_id}>{item.name || item.catalog_id} · {title(item.category || "")}</option>)}</SelectInput></Field><Field label="Strategy name"><TextInput value={name} onChange={(event) => setName(event.target.value)} placeholder="Desk name" /></Field></div>
        {selectedCatalog && <div className="inline-note"><strong>{selectedCatalog.name}</strong><p>{selectedCatalog.description}</p><span>{Array.isArray(selectedCatalog.horizons) ? selectedCatalog.horizons.map(title).join(", ") : "No horizons declared"}</span></div>}
      </Modal>
      <Modal open={runOpen} onClose={() => setRunOpen(false)} title="Run active strategies" description="Evaluates every active strategy over stored market data, resolves conflicts, and asks the independent risk engine about each actionable signal. A proposal is not an order." footer={<><Button variant="ghost" onClick={() => setRunOpen(false)}>Close</Button><Button variant="primary" loading={saving} disabled={!runPortfolio} onClick={runStrategies}>Run and create proposals</Button></>}>
        <Field label="Portfolio" hint="Proposals are sized against this book's valuation."><SelectInput value={runPortfolio} onChange={(event) => setRunPortfolio(event.target.value)}><option value="">Choose a portfolio…</option>{portfolioRows.map((row) => <option key={row.portfolio_id} value={row.portfolio_id}>{row.name || row.portfolio_id} · {row.horizon ? title(row.horizon) : "portfolio"}</option>)}</SelectInput></Field>
        <Field label="Instruments" hint="Comma-separated instrument ids. Without one the engine only falls back to the portfolio's holdings or discovery candidates."><TextInput value={runInstruments} onChange={(event) => setRunInstruments(event.target.value)} placeholder="XBOM:ADANIENT, NSE:RELIANCE" /></Field>
        <div className="inline-note"><strong>Need candidates?</strong><p>Use the current screener's top rows: <Button size="sm" variant="ghost" onClick={() => setRunInstruments(screenerRows.slice(0, 10).map((row) => row.instrument_id).filter(Boolean).join(", "))} disabled={!screenerRows.length}>Use top 10 from screener</Button></p></div>
        {!portfolioRows.length && <div className="inline-note"><strong>No portfolio</strong><p>Create and fund a portfolio before running strategies; proposals need a book to size against.</p></div>}
        {runResult && <div className="inline-note"><strong>Run result</strong><p>{Array.isArray(runResult.signals) ? runResult.signals.length : 0} signal(s) · {Array.isArray(runResult.proposals) ? runResult.proposals.length : 0} proposal(s) · {Array.isArray(runResult.conflicts) ? runResult.conflicts.length : 0} conflict(s) · {Array.isArray(runResult.errors) ? runResult.errors.length : 0} error(s).</p></div>}
        <Banner tone="warn" title="No execution here">This creates proposals only. An approved proposal must still be forwarded explicitly, and paper execution is the only path in this build.</Banner>
      </Modal>
      <ConfirmDialog open={!!transition} title={`${STRATEGY_ACTIONS[transition?.action || ""] || "Change"} this strategy?`} description="The backend validates the lifecycle transition and records the reason. It will not be applied if the state does not permit it." confirmLabel="Confirm transition" tone="primary" busy={saving} onConfirm={runTransition} onClose={() => setTransition(null)} />
    </div>
  );
}

export function RiskPage() {
  useDocumentTitle("Risk Management");
  const toast = useToast();
  const status = useApi<any>("/market/risk/status");
  const exposure = useApi<any>("/market/risk/exposure");
  const decisions = useApi<any>("/market/risk/decisions?limit=100");
  const alerts = useApi<any>("/market/risk/alerts?limit=100");
  const policies = useApi<any>("/market/risk/policies");
  const stressResults = useApi<any>("/market/risk/stress?limit=50");
  const portfolios = useApi<any>("/market/portfolios?limit=100");
  const [tab, setTab] = useState("overview");
  const [stressOpen, setStressOpen] = useState(false);
  const [stressPortfolio, setStressPortfolio] = useState("");
  const [scenario, setScenario] = useState("market_decline");
  const [saving, setSaving] = useState(false);
  const [alertAction, setAlertAction] = useState<{ alert: any; action: string } | null>(null);
  const portfolioRows = arrayFrom(portfolios.data, "portfolios");
  const alertRows = arrayFrom(alerts.data, "alerts");
  const openAlerts = alertRows.filter((row) => ["open", "acknowledged"].includes(String(row.status || "open").toLowerCase()));
  const metrics = status.data?.metrics || {};
  const policyList = arrayFrom(policies.data, "policies");
  // ``status.policies`` is the policy LIST, not a count; rendering it directly
  // put policy objects in the React tree and blanked the page.
  const policyCount = Array.isArray(status.data?.policies)
    ? status.data.policies.length
    : policyList.length;
  const approved = metrics.by_outcome?.approved ?? metrics.approved ?? "—";
  const rejected = metrics.by_outcome?.rejected ?? metrics.rejected ?? "—";

  useEffect(() => {
    if (!stressPortfolio && portfolioRows.length) setStressPortfolio(portfolioRows[0].portfolio_id);
  }, [portfolioRows, stressPortfolio]);

  const runStress = async () => {
    setSaving(true);
    try {
      await apiPost("/market/risk/stress", { portfolio_id: stressPortfolio, scenario });
      toast.push("Stress scenario completed by the risk engine.", "success");
      setStressOpen(false);
      stressResults.refresh();
    } catch (error: any) {
      toast.push(error?.detail || "The stress scenario could not run.", "error");
    } finally { setSaving(false); }
  };

  const changeAlert = async () => {
    if (!alertAction) return;
    setSaving(true);
    try {
      await apiPost(`/market/risk/alerts/${encodeURIComponent(alertAction.alert.alert_id)}/${alertAction.action}`, {});
      toast.push(`Alert ${alertAction.action === "acknowledge" ? "acknowledged" : "resolved"}.`, "success");
      setAlertAction(null);
      alerts.refresh();
    } catch (error: any) {
      toast.push(error?.detail || "The alert could not be updated.", "error");
    } finally { setSaving(false); }
  };

  return (
    <div className="page">
      <PageHeader eyebrow="Trading intelligence / Risk" title={<>The independent gate, <em>always on.</em></>} description="Risk decisions are authoritative and cannot be bypassed here. The desk can inspect policy, exposure, alerts and executed stress scenarios, but it cannot approve a trade on risk's behalf."
        meta={<><span className="meta-item"><ShieldCheck size={13} /> {policyCount} policies</span><span className="meta-item"><Activity size={13} /> {metrics.decisions ?? status.data?.decisions ?? "—"} decisions</span><span className="meta-item"><CircleAlert size={13} /> {openAlerts.length} open alerts</span></>}
        actions={<><Button variant="ghost" icon={<RefreshCw size={14} />} onClick={() => { status.refresh(); exposure.refresh(); alerts.refresh(); policies.refresh(); }}>Refresh</Button><Button variant="primary" icon={<Sparkles size={15} />} onClick={() => setStressOpen(true)} disabled={!portfolioRows.length}>Run stress test</Button></>}
      />
      {status.error && <ErrorBlock message={status.error.detail} onRetry={status.refresh} />}
      <Banner tone="info" title="Risk has veto power over every order.">The NM-08 engine independently assesses each proposal. A decision is not a recommendation, and the UI never displays an expired approval as usable.</Banner>
      <div className="metric-grid metric-grid-four">
        <Metric label="Approved" value={approved} help="Risk-approved decisions" tone="good" icon={<CheckCircle2 size={16} />} />
        <Metric label="Rejected" value={rejected} help="Policy refusals" tone="bad" icon={<XCircle size={16} />} />
        <Metric label="Open alerts" value={openAlerts.length} help="Not yet acknowledged" tone="warn" icon={<AlertTriangle size={16} />} />
        <Metric label="Stress results" value={Array.isArray(stressResults.data?.stress_results) ? stressResults.data.stress_results.length : stressResults.data?.total ?? "—"} help="Stored hypothetical scenarios" icon={<Gauge size={16} />} />
      </div>
      <Card className="tabs-card">
        <div className="tabs-head"><Tabs active={tab} onChange={setTab} tabs={[{ id: "overview", label: "Overview" }, { id: "decisions", label: "Decisions", count: arrayFrom(decisions.data, "decisions").length }, { id: "alerts", label: "Alerts", count: openAlerts.length }, { id: "policies", label: "Policies", count: arrayFrom(policies.data, "policies").length }, { id: "stress", label: "Stress tests" }]} /><Button size="sm" variant="ghost" onClick={() => { decisions.refresh(); alerts.refresh(); policies.refresh(); stressResults.refresh(); }} icon={<RefreshCw size={13} />}>Reload</Button></div>
        {tab === "overview" && <div className="risk-overview"><div className="risk-exposure-card"><div className="eyebrow">Combined exposure</div><pre className="json-preview">{JSON.stringify(exposure.data || {}, null, 2)}</pre>{exposure.error && <ErrorBlock message={exposure.error.detail} onRetry={exposure.refresh} />}</div><div className="stack"><Card><CardHeader title="Policy guardrails" subtitle="Versioned risk limits" />{arrayFrom(policies.data, "policies").slice(0, 4).map((policy: any) => <div className="policy-row" key={policy.policy_id}><div><strong>{policy.label || policy.policy_id}</strong><small>{title(policy.scope || "policy")} · v{policy.version || "—"}</small></div><div className="policy-limits"><span>Position {pctUnsigned(num(policy.max_position_weight) * 100)}</span><span>Sector {pctUnsigned(num(policy.max_sector_weight) * 100)}</span></div></div>)}</Card><Card><CardHeader title="Recent decisions" subtitle="Latest gate results" action={<button type="button" className="text-link" onClick={() => setTab("decisions")}>All <ArrowRight size={13} /></button>} />{arrayFrom(decisions.data, "decisions").slice(0, 5).map((decision: any) => <div className="decision-row" key={decision.decision_id}><StatusBadge value={decision.outcome} /><div><strong>{title(decision.action || "Risk assessment")}</strong><small>{decision.explanation || "No explanation returned"} · {timeAgo(decision.created_at)}</small></div></div>)}</Card></div></div>}
        {tab === "decisions" && (decisions.loading ? <LoadingBlock rows={4} label="Reading risk decisions…" /> : <DataTable rows={arrayFrom(decisions.data, "decisions")} rowKey={(row) => row.decision_id} empty={<EmptyState title="No risk decisions" description="A decision is recorded whenever the gate assesses a proposal or plan." />} columns={[{ key: "outcome", label: "Outcome", render: (row) => <StatusBadge value={row.outcome} /> }, { key: "action", label: "Action", render: (row) => title(row.action || "assessment") }, { key: "portfolio_id", label: "Portfolio", render: (row) => row.portfolio_id || "—" }, { key: "explanation", label: "Explanation", render: (row) => <span className="clamp-cell">{row.explanation || "—"}</span> }, { key: "policy_version", label: "Policy", render: (row) => row.policy_version || "—" }, { key: "created_at", label: "Created", render: (row) => timeAgo(row.created_at), sortable: true }]} />)}
        {tab === "alerts" && (alerts.loading ? <LoadingBlock rows={4} label="Reading risk alerts…" /> : <DataTable rows={alertRows} rowKey={(row) => row.alert_id} empty={<EmptyState icon={<CheckCircle2 size={22} />} title="No risk alerts" description="The risk engine has not recorded an alert in this response." />} columns={[{ key: "severity", label: "Severity", render: (row) => <Badge tone={String(row.severity).toLowerCase() === "critical" ? "bad" : "warn"}>{title(row.severity || "warning")}</Badge> }, { key: "category", label: "Category", render: (row) => title(row.category || row.kind || "—") }, { key: "message", label: "Message", render: (row) => <span className="clamp-cell">{row.message || row.explanation || "—"}</span> }, { key: "status", label: "State", render: (row) => <StatusBadge value={row.status || "open"} /> }, { key: "created_at", label: "Opened", render: (row) => timeAgo(row.created_at), sortable: true }, { key: "action", label: "", align: "right", render: (row) => <div className="row-actions">{String(row.status).toLowerCase() === "open" && <Button size="sm" variant="ghost" onClick={() => setAlertAction({ alert: row, action: "acknowledge" })}>Acknowledge</Button>}{["open", "acknowledged"].includes(String(row.status).toLowerCase()) && <Button size="sm" variant="ghost" onClick={() => setAlertAction({ alert: row, action: "resolve" })}>Resolve</Button>}</div> }]} />)}
        {tab === "policies" && (policies.loading ? <LoadingBlock rows={4} label="Reading policies…" /> : <DataTable rows={arrayFrom(policies.data, "policies")} rowKey={(row) => row.policy_id} empty={<EmptyState title="No risk policies" description="Policies are required before risk can approve a proposal." />} columns={[{ key: "policy_id", label: "Policy", render: (row) => <div><strong>{row.label || row.policy_id}</strong><small className="table-sub">{row.policy_id}</small></div> }, { key: "version", label: "Version", render: (row) => row.version || "—" }, { key: "max_position_weight", label: "Max position", align: "right", render: (row) => pctUnsigned(num(row.max_position_weight) * 100) }, { key: "max_drawdown_pct", label: "Max drawdown", align: "right", render: (row) => pctUnsigned(num(row.max_drawdown_pct)) }, { key: "max_uncertainty", label: "Max uncertainty", render: (row) => title(row.max_uncertainty || "—") }, { key: "aggregate", label: "Scope", render: (row) => <Badge tone="neutral">{row.aggregate ? "Aggregate" : "Portfolio"}</Badge> }]} />)}
        {tab === "stress" && (stressResults.loading ? <LoadingBlock rows={4} label="Reading stress results…" /> : <><div className="filter-row"><span className="inline-meta"><Gauge size={14} /> Stress scenarios are hypothetical and labelled as such.</span><Button size="sm" variant="primary" onClick={() => setStressOpen(true)} disabled={!portfolioRows.length}>Run scenario</Button></div><DataTable rows={arrayFrom(stressResults.data, "stress_results", "results")} rowKey={(row, index) => row.result_id || row.stress_id || String(index)} empty={<EmptyState title="No stress results" description="Run a labelled scenario against a portfolio to store a result." />} columns={[{ key: "scenario", label: "Scenario", render: (row) => title(row.scenario || row.scenario_name || "—") }, { key: "portfolio_id", label: "Portfolio", render: (row) => row.portfolio_id || "—" }, { key: "loss_pct", label: "Impact", align: "right", render: (row) => <span className="negative">{pct(num(row.loss_pct ?? row.impact_pct ?? row.change_pct))}</span> }, { key: "created_at", label: "Run", render: (row) => timeAgo(row.created_at) }, { key: "hypothetical", label: "Label", render: () => <Badge tone="warn">Hypothetical</Badge> }]} /></>)}
      </Card>

      <Modal open={stressOpen} onClose={() => setStressOpen(false)} title="Run a stress scenario" description="This asks the risk engine to calculate a hypothetical impact; it does not change a portfolio, order or risk limit." footer={<><Button variant="ghost" onClick={() => setStressOpen(false)}>Cancel</Button><Button variant="primary" loading={saving} disabled={!stressPortfolio} onClick={runStress}>Run and store result</Button></>}>
        <div className="form-grid"><Field label="Portfolio"><SelectInput value={stressPortfolio} onChange={(event) => setStressPortfolio(event.target.value)}>{portfolioRows.map((row) => <option key={row.portfolio_id} value={row.portfolio_id}>{row.name || row.portfolio_id}</option>)}</SelectInput></Field><Field label="Scenario"><SelectInput value={scenario} onChange={(event) => setScenario(event.target.value)}>{["market_decline", "liquidity_shock", "volatility_spike", "sector_shock", "rates_shock"].map((item) => <option key={item} value={item}>{title(item)}</option>)}</SelectInput></Field></div>
        <Banner tone="warn" title="Hypothetical by construction">The result is a scenario, not a forecast. It must never be presented as a portfolio's actual loss.</Banner>
      </Modal>
      <ConfirmDialog open={!!alertAction} title={`${alertAction?.action === "resolve" ? "Resolve" : "Acknowledge"} this alert?`} description="The action is recorded against the risk alert. It does not alter any portfolio or policy." confirmLabel={alertAction?.action === "resolve" ? "Resolve alert" : "Acknowledge"} tone="primary" busy={saving} onConfirm={changeAlert} onClose={() => setAlertAction(null)} />
    </div>
  );
}

export function PaperPage() {
  useDocumentTitle("Paper Trading");
  const toast = useToast();
  const status = useApi<any>("/market/execution/status");
  const orders = useApi<any>("/market/execution/orders?limit=200");
  const fills = useApi<any>("/market/execution/fills");
  const reconciliations = useApi<any>("/market/execution/reconciliations?limit=50");
  const approved = useApi<any>("/market/strategies/proposals?status=approved&limit=100");
  const [tab, setTab] = useState("orders");
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState<any | null>(null);
  const [action, setAction] = useState<{ order: any; action: string } | null>(null);
  const [submitOpen, setSubmitOpen] = useState(false);
  const [proposalId, setProposalId] = useState("");
  const [orderQuantity, setOrderQuantity] = useState("");
  const [orderType, setOrderType] = useState("market");
  const [limitPrice, setLimitPrice] = useState("");
  const [triggerPrice, setTriggerPrice] = useState("");
  const [expiresAt, setExpiresAt] = useState("");
  const [reconciling, setReconciling] = useState(false);
  const [saving, setSaving] = useState(false);
  const rows = arrayFrom(orders.data, "orders");
  const fillRows = arrayFrom(fills.data, "fills");
  const approvedRows = arrayFrom(approved.data, "proposals");
  const requiresLimit = ["limit", "stop_limit"].includes(orderType);
  const requiresTrigger = ["stop", "stop_limit"].includes(orderType);
  const orderValid = Boolean(
    proposalId.trim()
      && (!requiresLimit || Number(limitPrice) > 0)
      && (!requiresTrigger || Number(triggerPrice) > 0),
  );
  const filtered = useMemo(() => {
    const text = search.toLowerCase().trim();
    return rows.filter((row) => !text || [row.order_id, row.instrument_id, row.portfolio_id, row.strategy_id, row.status, row.side].some((value) => String(value || "").toLowerCase().includes(text)));
  }, [rows, search]);

  const runOrderAction = async () => {
    if (!action) return;
    setSaving(true);
    try {
      await apiPost(`/market/execution/orders/${encodeURIComponent(action.order.order_id)}/${action.action}`, action.action === "cancel" ? { reason: "cancelled by operator" } : {});
      toast.push(`Paper order ${action.action === "process" ? "processed" : "cancelled"}.`, "success");
      setAction(null);
      orders.refresh();
      fills.refresh();
      status.refresh();
      reconciliations.refresh();
    } catch (error: any) {
      toast.push(error?.detail || "The paper order could not be updated.", "error");
    } finally { setSaving(false); }
  };

  const submitOrder = async () => {
    setSaving(true);
    try {
      await apiPost("/market/execution/orders", {
        proposal_id: proposalId,
        quantity: orderQuantity || undefined,
        order_type: orderType,
        limit_price: requiresLimit ? limitPrice : undefined,
        trigger_price: requiresTrigger ? triggerPrice : undefined,
        expires_at: expiresAt ? new Date(expiresAt).toISOString() : undefined,
      });
      toast.push("Paper order submitted to the simulation engine.", "success");
      setSubmitOpen(false);
      setProposalId("");
      setOrderQuantity("");
      setLimitPrice("");
      setTriggerPrice("");
      setExpiresAt("");
      orders.refresh();
      fills.refresh();
      status.refresh();
      approved.refresh();
      reconciliations.refresh();
    } catch (error: any) {
      toast.push(error?.detail || "The paper order was refused.", "error");
    } finally { setSaving(false); }
  };

  const reconcileNow = async () => {
    setReconciling(true);
    try {
      await apiPost("/market/execution/reconcile", {});
      toast.push("Reconciliation completed and stored.", "success");
      reconciliations.refresh();
      orders.refresh();
      fills.refresh();
    } catch (error: any) {
      toast.push(error?.detail || "Reconciliation failed.", "error");
    } finally { setReconciling(false); }
  };

  return (
    <div className="page">
      <PageHeader eyebrow="Trading intelligence / Paper" title={<>Simulated execution, <em>real accounting discipline.</em></>} description="Paper orders move simulated NovaCoin through the ledger, with fees, slippage and reconciliation. This page has no broker connection and no live route."
        meta={<><span className="meta-item"><Gauge size={13} /> {orders.data?.total ?? rows.length} orders</span><span className="meta-item"><CheckCircle2 size={13} /> {orders.data?.paper_only ? "Paper only" : "Paper policy"}</span><span className="meta-item"><Clock3 size={13} /> {status.data?.lifecycle ? title(status.data.lifecycle) : "Execution status"}</span></>}
        actions={<><Button variant="ghost" icon={<RefreshCw size={14} />} onClick={() => { orders.refresh(); fills.refresh(); status.refresh(); approved.refresh(); reconciliations.refresh(); }}>Refresh</Button><Button variant="primary" icon={<Plus size={15} />} onClick={() => { approved.refresh(); setSubmitOpen(true); }}>Submit from proposal</Button></>}
      />
      <h2 className="sr-only">Paper execution</h2>
      <Banner tone="warn" title="Paper execution only">Every fill on this page is simulated. A proposal must already have passed the independent risk gate. The desk cannot create a live broker order.</Banner>
      {orders.error && <ErrorBlock message={orders.error.detail} onRetry={orders.refresh} />}
      <div className="metric-grid metric-grid-four">
        <Metric label="Open orders" value={rows.filter((row) => ["submitted", "working", "partially_filled", "triggered"].includes(String(row.status).toLowerCase())).length} help="Live in the paper engine" icon={<Activity size={16} />} tone="accent" />
        <Metric label="Filled" value={rows.filter((row) => row.status === "filled").length} help={`${fillRows.length} fill records`} icon={<CheckCircle2 size={16} />} tone="good" />
        <Metric label="Total orders" value={orders.data?.total ?? rows.length} help="Stored paper history" icon={<History size={16} />} />
        <Metric label="Reconciliations" value={Array.isArray(reconciliations.data?.reconciliations) ? reconciliations.data.reconciliations.length : reconciliations.data?.total ?? "—"} help="Ledger/order checks" icon={<ShieldCheck size={16} />} />
      </div>
      <Card className="tabs-card">
        <div className="tabs-head"><Tabs active={tab} onChange={setTab} tabs={[{ id: "orders", label: "Orders", count: rows.length }, { id: "fills", label: "Fills", count: fillRows.length }, { id: "reconciliation", label: "Reconciliation" }]} /><Button size="sm" variant="ghost" icon={<RefreshCw size={13} />} onClick={() => { orders.refresh(); fills.refresh(); reconciliations.refresh(); }}>Reload</Button></div>
        {tab === "orders" && <><div className="filter-row"><SearchInput value={search} onChange={setSearch} placeholder="Search order, instrument, portfolio or state…" /><Badge tone="info" dot>{status.data?.paper_only === false ? "Policy mismatch" : "Paper engine"}</Badge></div>{orders.loading ? <LoadingBlock rows={4} label="Reading paper orders…" /> : <DataTable rows={filtered} rowKey={(row) => row.order_id} onRowClick={setSelected} empty={<EmptyState icon={<Gauge size={22} />} title={rows.length ? "No orders match" : "No paper orders yet"} description={rows.length ? "Adjust the search." : "Submit an already-approved strategy proposal to create one."} />} columns={[
          { key: "order_id", label: "Order", render: (row) => <div><strong className="mono-small">{shortId(row.order_id, 16)}</strong><small className="table-sub">{dateTime(row.created_at)}</small></div>, sortable: true },
          { key: "instrument_id", label: "Instrument", render: (row) => row.instrument_id || "—", sortable: true },
          { key: "side", label: "Side", render: (row) => <Badge tone={String(row.side).toLowerCase() === "buy" ? "good" : "bad"}>{title(row.side)}</Badge> },
          { key: "quantity", label: "Qty", align: "right", render: (row) => quantity(row.quantity) },
          { key: "filled_quantity", label: "Filled", align: "right", render: (row) => quantity(row.filled_quantity) },
          { key: "status", label: "State", render: (row) => <StatusBadge value={row.status} /> },
          { key: "reference_price", label: "Reference", align: "right", render: (row) => <Money value={row.reference_price} /> },
          { key: "action", label: "", align: "right", render: (row) => <div className="row-actions">{row.can_cancel && <Button size="sm" variant="ghost" onClick={(event) => { event.stopPropagation(); setAction({ order: row, action: "cancel" }); }}>Cancel</Button>}{["submitted", "working", "partially_filled"].includes(String(row.status).toLowerCase()) && <Button size="sm" variant="secondary" onClick={(event) => { event.stopPropagation(); setAction({ order: row, action: "process" }); }}>Process</Button>}</div> }]}
        />}</>}
        {tab === "fills" && (fills.loading ? <LoadingBlock rows={4} label="Reading fills…" /> : <DataTable rows={fillRows} rowKey={(row) => row.fill_id} empty={<EmptyState title="No fills recorded" description="A fill appears after the paper engine executes an order." />} columns={[{ key: "executed_at", label: "Executed", render: (row) => dateTime(row.executed_at), sortable: true }, { key: "instrument_id", label: "Instrument" }, { key: "side", label: "Side", render: (row) => <Badge tone={String(row.side || "buy").toLowerCase() === "buy" ? "good" : "bad"}>{title(row.side || "—")}</Badge> }, { key: "quantity", label: "Quantity", align: "right" }, { key: "price", label: "Price", align: "right", render: (row) => <Money value={row.price} /> }, { key: "slippage_amount", label: "Slippage", align: "right", render: (row) => <Money value={row.slippage_amount} /> }, { key: "total_fees", label: "Fees", align: "right", render: (row) => <Money value={row.total_fees} /> }, { key: "net_cash", label: "Net cash", align: "right", render: (row) => <Money value={row.net_cash} /> }]} />)}
        {tab === "reconciliation" && <div className="stack"><div className="filter-row"><span className="inline-meta"><ShieldCheck size={14} /> Compares order fills with wallet ledger entries; it never repairs or deletes evidence.</span><Button size="sm" variant="secondary" loading={reconciling} onClick={reconcileNow}>Reconcile now</Button></div>{reconciliations.loading ? <LoadingBlock rows={3} label="Reading reconciliations…" /> : <DataTable rows={arrayFrom(reconciliations.data, "reconciliations")} rowKey={(row, index) => row.reconciliation_id || String(index)} empty={<EmptyState icon={<ShieldCheck size={22} />} title="No reconciliations" description="Run a ledger reconciliation to compare simulated order effects with the immutable ledger." />} columns={[{ key: "created_at", label: "Run", render: (row) => dateTime(row.created_at) }, { key: "order_id", label: "Order", render: (row) => shortId(row.order_id, 16) }, { key: "status", label: "State", render: (row) => <StatusBadge value={row.status || (row.ok ? "ok" : "failed")} /> }, { key: "difference", label: "Difference", align: "right", render: (row) => <Money value={row.difference ?? row.delta} /> }, { key: "detail", label: "Detail", render: (row) => row.detail || row.reason || "—" }]} />}<div className="inline-note"><strong>No broker is connected.</strong><p>Reconciliation compares the simulated paper book with the wallet ledger. It does not reach an exchange.</p></div></div>}
      </Card>

      <Modal open={!!selected} onClose={() => setSelected(null)} title={selected ? `Paper order ${shortId(selected.order_id, 18)}` : "Paper order"} description={selected ? `${title(selected.side)} ${selected.instrument_id} · ${title(selected.status)}` : ""} wide footer={<><Button variant="ghost" onClick={() => setSelected(null)}>Close</Button>{selected?.can_cancel && <Button variant="danger" onClick={() => setAction({ order: selected, action: "cancel" })}>Cancel order</Button>}</>}>
        {selected && <div className="detail-grid"><div className="detail-main"><div className="summary-grid"><div><span>Requested</span><strong>{quantity(selected.requested_quantity || selected.quantity)}</strong></div><div><span>Filled</span><strong>{quantity(selected.filled_quantity)}</strong></div><div><span>Remaining</span><strong>{quantity(selected.remaining_quantity)}</strong></div><div><span>Reference</span><strong>{money(selected.reference_price)}</strong></div></div><div className="detail-section"><h3>Provenance</h3><div className="fact-list"><div><span>Proposal</span><strong className="mono-small">{selected.proposal_id || "—"}</strong></div><div><span>Strategy</span><strong className="mono-small">{selected.strategy_id || "—"}</strong></div><div><span>Risk decision</span><strong className="mono-small">{selected.risk_decision_id || "—"}</strong></div><div><span>Execution config</span><strong>{selected.execution_config_version || "—"}</strong></div></div></div>{Array.isArray(selected.history) && <div className="detail-section"><h3>Transitions</h3><pre className="code-block">{JSON.stringify(selected.history, null, 2)}</pre></div>}</div><aside className="detail-aside"><div className="detail-fact"><span>State</span><StatusBadge value={selected.status} /></div><div className="detail-fact"><span>Order type</span><span>{title(selected.order_type)}</span></div><div className="detail-fact"><span>Created</span><span>{dateTime(selected.created_at)}</span></div><div className="detail-fact"><span>Paper only</span><Badge tone="good">Yes</Badge></div><div className="detail-fact"><span>Broker</span><Badge tone="neutral">None</Badge></div></aside></div>}
      </Modal>

      <Modal open={submitOpen} onClose={() => setSubmitOpen(false)} title="Submit an approved proposal" description="The paper engine accepts only an already-approved proposal with a live risk decision. This form cannot bypass that gate." footer={<><Button variant="ghost" onClick={() => setSubmitOpen(false)}>Cancel</Button><Button variant="primary" loading={saving} disabled={!orderValid} onClick={submitOrder}>Submit paper order</Button></>}>
        <Field label="Approved proposal" hint="Only an approved, unexpired NM-09 proposal is accepted by the paper engine.">
          <SelectInput value={proposalId} onChange={(event) => setProposalId(event.target.value)}>
            <option value="">Choose an approved proposal…</option>
            {approvedRows.map((row) => <option key={row.proposal_id} value={row.proposal_id}>{row.instrument_id} · {title(row.side)} · {money(row.notional)}</option>)}
          </SelectInput>
        </Field>
        {!approved.loading && !approvedRows.length && <div className="inline-note"><strong>No approved proposals</strong><p>Run an active strategy on the Strategies page, then forward its approved proposal from the Proposals panel.</p></div>}
        {approvedRows.filter((row) => row.proposal_id === proposalId).map((row) => <div className="inline-note" key={row.proposal_id}><strong>{row.instrument_id}</strong><p>{title(row.side)} · notional {money(row.notional)} · risk {row.risk_outcome || "pending"} · expires {timeAgo(row.expires_at)}</p></div>)}
        <div className="form-grid"><Field label="Order type"><SelectInput value={orderType} onChange={(event) => { const next = event.target.value; setOrderType(next); if (!["limit", "stop_limit"].includes(next)) setLimitPrice(""); if (!["stop", "stop_limit"].includes(next)) setTriggerPrice(""); }}><option value="market">Market</option><option value="limit">Limit</option><option value="stop">Stop</option><option value="stop_limit">Stop limit</option></SelectInput></Field><Field label="Quantity" hint="Leave empty for the proposal's intended size."><TextInput value={orderQuantity} onChange={(event) => setOrderQuantity(event.target.value)} inputMode="decimal" placeholder="Auto" /></Field>{requiresLimit && <Field label="Limit price" hint="Required for a limit or stop-limit order."><TextInput value={limitPrice} onChange={(event) => setLimitPrice(event.target.value)} inputMode="decimal" placeholder="e.g. 240.00" /></Field>}{requiresTrigger && <Field label="Trigger price" hint="Required for a stop or stop-limit order."><TextInput value={triggerPrice} onChange={(event) => setTriggerPrice(event.target.value)} inputMode="decimal" placeholder="e.g. 250.00" /></Field>}<Field label="Expires at" hint="Optional. The backend refuses an expiry in the past."><TextInput type="datetime-local" value={expiresAt} onChange={(event) => setExpiresAt(event.target.value)} /></Field></div>
        <Banner tone="warn" title="Simulation only">The order will be filled against recorded market references with paper fees and slippage, not a live venue.</Banner>
      </Modal>
      <ConfirmDialog open={!!action} title={action?.action === "cancel" ? "Cancel this paper order?" : "Process this paper order?"} description={action?.action === "cancel" ? "The order is cancelled with a recorded reason; no live order exists." : "The paper engine will evaluate the order against its current market reference. It cannot create real money movement."} confirmLabel={action?.action === "cancel" ? "Cancel order" : "Process order"} tone={action?.action === "cancel" ? "danger" : "primary"} busy={saving} onConfirm={runOrderAction} onClose={() => setAction(null)} />
    </div>
  );
}

export function BacktestingPage() {
  useDocumentTitle("Backtesting");
  const toast = useToast();
  const backtests = useApi<any>("/market/backtests?limit=100");
  const status = useApi<any>("/market/backtests/status");
  const catalog = useApi<any>("/market/strategies/catalog");
  const policies = useApi<any>("/market/risk/policies");
  const instruments = useApi<any>("/market/instruments?limit=200");
  const [selectedId, setSelectedId] = useState("");
  const [createOpen, setCreateOpen] = useState(false);
  const [name, setName] = useState("");
  const [strategyCatalog, setStrategyCatalog] = useState("");
  const [instrumentIds, setInstrumentIds] = useState("");
  const [start, setStart] = useState(() => { const date = new Date(); date.setMonth(date.getMonth() - 6); return date.toISOString().slice(0, 10); });
  const [end, setEnd] = useState(() => new Date().toISOString().slice(0, 10));
  const [capital, setCapital] = useState("100000");
  const [riskPolicyId, setRiskPolicyId] = useState("");
  const [saving, setSaving] = useState(false);
  const [action, setAction] = useState<{ row: any; action: string } | null>(null);
  const rows = arrayFrom(backtests.data, "backtests");
  const catalogRows = arrayFrom(catalog.data, "catalog", "strategies", "items");
  const policyRows = arrayFrom(policies.data, "policies");
  const current = rows.find((row) => row.backtest_id === selectedId) || rows[0];
  const detail = useApi<any>(current ? `/market/backtests/${encodeURIComponent(current.backtest_id)}` : null);
  const results = useApi<any>(current ? `/market/backtests/${encodeURIComponent(current.backtest_id)}/results` : null);
  const trades = useApi<any>(current ? `/market/backtests/${encodeURIComponent(current.backtest_id)}/trades` : null);
  const dataQuality = useApi<any>(current ? `/market/backtests/${encodeURIComponent(current.backtest_id)}/data-quality` : null);
  const selectedCatalog = catalogRows.find((row) => row.catalog_id === strategyCatalog);
  const selectedPolicy = policyRows.find((row) => row.policy_id === riskPolicyId) || policyRows[0];
  const result = results.data || {};
  const metrics = result.metrics || detail.data?.metrics || {};
  const resultCurve = arrayFrom(result, "equity_curve", "curve");
  const curves = (resultCurve.length ? resultCurve : arrayFrom(detail.data, "equity")).map((row) => ({ label: shortDate(row.at || row.timestamp || row.date), value: num(row.equity ?? row.value ?? row.total_value, NaN) })).filter((row) => Number.isFinite(row.value));
  const executionConfig = detail.data?.config?.execution_config || {};
  const feeSchedule = [
    executionConfig.fee_bps != null ? `Fee ${executionConfig.fee_bps} bps` : null,
    executionConfig.brokerage_bps != null ? `Brokerage ${executionConfig.brokerage_bps} bps` : null,
    executionConfig.exchange_bps != null ? `Exchange ${executionConfig.exchange_bps} bps` : null,
    executionConfig.gst_percent != null ? `GST ${executionConfig.gst_percent}%` : null,
  ].filter(Boolean).join(" · ") || "—";
  const slippageModel = [
    executionConfig.slippage_bps != null ? `Slippage ${executionConfig.slippage_bps} bps` : null,
    executionConfig.spread_bps != null ? `Spread ${executionConfig.spread_bps} bps` : null,
    executionConfig.impact_coefficient_bps != null ? `Impact ${executionConfig.impact_coefficient_bps} bps` : null,
  ].filter(Boolean).join(" · ") || "—";

  useEffect(() => {
    if (!selectedId && rows.length) setSelectedId(rows[0].backtest_id);
  }, [rows, selectedId]);
  useEffect(() => {
    if (!strategyCatalog && catalogRows.length) setStrategyCatalog(catalogRows[0].catalog_id);
  }, [catalogRows, strategyCatalog]);
  useEffect(() => {
    if (!riskPolicyId && policyRows.length) setRiskPolicyId(policyRows[0].policy_id);
  }, [policyRows, riskPolicyId]);

  const create = async () => {
    setSaving(true);
    try {
      const config = selectedCatalog?.default_config || {};
      const tickers = instrumentIds.split(",").map((item) => item.trim()).filter(Boolean);
      const payload = {
        name,
        strategy_catalog_id: strategyCatalog,
        strategy_version: "web-1.0",
        strategy_config: config,
        instruments: tickers,
        start: new Date(`${start}T00:00:00Z`).toISOString(),
        end: new Date(`${end}T00:00:00Z`).toISOString(),
        initial_capital: capital,
        evaluation_frequency: "1d",
        risk_policy_version: selectedPolicy?.version || "nm08-backtest-v1",
        risk_policy: selectedPolicy || {},
        max_position_weight: selectedPolicy?.max_position_weight || "0.25",
        max_gross_exposure: "1.00",
      };
      const created = await apiPost<any>("/market/backtests", payload);
      const backtestId = created?.backtest_id;
      if (backtestId) {
        try {
          await apiPost(`/market/backtests/${encodeURIComponent(backtestId)}/start`, {});
          toast.push("Backtest created and started.", "success");
        } catch {
          toast.push("Backtest created. Review it before starting.", "info");
        }
        setSelectedId(backtestId);
      }
      setCreateOpen(false);
      backtests.refresh();
      status.refresh();
    } catch (error: any) {
      toast.push(error?.detail || "The backtest could not be created.", "error");
    } finally { setSaving(false); }
  };

  const runAction = async () => {
    if (!action) return;
    setSaving(true);
    try {
      await apiPost(`/market/backtests/${encodeURIComponent(action.row.backtest_id)}/${action.action}`, {});
      toast.push(`Backtest ${action.action === "start" ? "started" : "cancelled"}.`, "success");
      setAction(null);
      backtests.refresh();
      detail.refresh();
    } catch (error: any) {
      toast.push(error?.detail || "The backtest action was refused.", "error");
    } finally { setSaving(false); }
  };

  return (
    <div className="page">
      <PageHeader eyebrow="Trading intelligence / Backtesting" title={<>Historical replay, <em>reproducible by construction.</em></>} description="Backtests replay recorded history with explicit assumptions, costs and risk. They are not live performance and never touch the paper wallet or portfolio."
        meta={<><span className="meta-item"><BarChart3 size={13} /> {backtests.data?.total ?? rows.length} runs</span><span className="meta-item"><ShieldCheck size={13} /> {status.data?.engine_version || "Replay engine"}</span><span className="meta-item"><Clock3 size={13} /> {status.data?.running ?? 0} running</span></>}
        actions={<><Button variant="ghost" icon={<RefreshCw size={14} />} onClick={() => { backtests.refresh(); detail.refresh(); results.refresh(); }}>Refresh</Button><Button variant="primary" icon={<Plus size={15} />} onClick={() => setCreateOpen(true)} disabled={!catalogRows.length || !policyRows.length}>New backtest</Button></>}
      />
      <Banner tone="info" title="Historical, not hypothetical-live">A backtest is a replay of recorded bars with a random seed and a persisted reproducibility hash. It is not evidence that a strategy would have made money in real time.</Banner>
      {backtests.error && <ErrorBlock message={backtests.error.detail} onRetry={backtests.refresh} />}
      <div className="dashboard-grid">
        <Card className="backtest-list-card"><CardHeader title="Runs" subtitle="Newest first" action={<Badge tone="neutral">{rows.length}</Badge>} />{backtests.loading ? <LoadingBlock rows={4} label="Reading backtests…" /> : <div className="rail-list">{rows.map((row) => <button type="button" className={`rail-item ${current?.backtest_id === row.backtest_id ? "rail-item-active" : ""}`} key={row.backtest_id} onClick={() => setSelectedId(row.backtest_id)}><span className="rail-icon"><BarChart3 size={15} /></span><span><strong>{row.name || row.strategy_catalog_id || "Backtest"}</strong><small>{title(row.strategy_catalog_id || "strategy")} · {timeAgo(row.created_at)}</small></span><StatusBadge value={row.status || "created"} /></button>)}</div>}{!rows.length && !backtests.loading && <EmptyState title="No backtests yet" description="A backtest needs a strategy config, a historical window and a risk policy snapshot." action={<Button size="sm" variant="primary" onClick={() => setCreateOpen(true)}>Create one</Button>} />}</Card>
        <div className="stack">
          {current ? <Card><CardHeader title={current.name || current.strategy_catalog_id || "Backtest result"} subtitle={`${title(current.status || "created")} · ${shortId(current.backtest_id, 20)}`} action={<div className="row-actions">{["created", "running", "paused"].includes(String(current.status)) && <Button size="sm" variant="secondary" onClick={() => setAction({ row: current, action: current.status === "created" ? "start" : "cancel" })}>{current.status === "created" ? "Start" : "Cancel"}</Button>}</div>} />{detail.loading ? <LoadingBlock rows={3} label="Reading the run…" /> : <><div className="metric-grid metric-grid-four"><Metric label="Total return" value={pct(num(metrics.total_return) * 100)} help="Net of costs" tone={num(metrics.total_return) < 0 ? "bad" : "good"} /><Metric label="Max drawdown" value={pct(num(metrics.max_drawdown_pct ?? metrics.max_drawdown) * 100)} help="Historical replay" tone="bad" /><Metric label="Sharpe" value={metrics.sharpe ?? "—"} help="If observations allow" /><Metric label="Trades" value={metrics.trades ?? arrayFrom(trades.data, "trades").length} help={`Seed ${detail.data?.config?.random_seed ?? "—"}`} /></div><div className="chart-block"><div className="chart-block-head"><span>Equity curve</span><Badge tone="neutral">{curves.length ? "Measured replay" : "No curve"}</Badge></div><LineChart data={curves} height={280} color="var(--gold)" empty="No recorded equity curve for this run" /></div><div className="two-col-grid"><div className="detail-section"><h3>Assumptions</h3><div className="fact-list"><div><span>Strategy</span><strong>{title(detail.data?.config?.strategy_catalog_id || "—")}</strong></div><div><span>Fees</span><strong>{feeSchedule}</strong></div><div><span>Slippage</span><strong>{slippageModel}</strong></div><div><span>Benchmark</span><strong>{detail.data?.config?.benchmark || "None"}</strong></div></div></div><div className="detail-section"><h3>Data quality</h3><pre className="json-preview">{JSON.stringify(dataQuality.data || result.data_quality || {}, null, 2)}</pre></div></div><DataTable compact rows={arrayFrom(trades.data, "trades").slice(0, 25)} rowKey={(row) => row.trade_id} empty={<EmptyState title="No trades" description="A completed run will list its replayed trades." />} columns={[{ key: "executed_at", label: "Executed", render: (row) => dateTime(row.executed_at) }, { key: "instrument_id", label: "Instrument" }, { key: "side", label: "Side", render: (row) => <Badge tone={row.side === "buy" ? "good" : "bad"}>{title(row.side)}</Badge> }, { key: "quantity", label: "Qty", align: "right" }, { key: "price", label: "Price", align: "right", render: (row) => <Money value={row.price} /> }, { key: "fees", label: "Fees", align: "right", render: (row) => <Money value={row.fees} /> }]} /></>}</Card> : <Card><EmptyState icon={<BarChart3 size={24} />} title="No backtest selected" description="Create a reproducible replay to inspect its equity curve, drawdown and assumptions." action={<Button variant="primary" onClick={() => setCreateOpen(true)} disabled={!catalogRows.length || !policyRows.length}>Create backtest</Button>} /></Card>}
          <Card><CardHeader title="Reproducibility contract" subtitle="What the backend persists" /><div className="contract-list"><div><CheckCircle2 size={15} /><span>Configuration hash and random seed are persisted.</span></div><div><CheckCircle2 size={15} /><span>Risk policy snapshot is part of the run.</span></div><div><CheckCircle2 size={15} /><span>Fees, slippage and evaluation frequency are explicit.</span></div><div><CheckCircle2 size={15} /><span>No paper order or wallet mutation is produced.</span></div></div></Card>
        </div>
      </div>
      <Modal open={createOpen} onClose={() => setCreateOpen(false)} title="Create a backtest" description="The backend validates the strategy configuration, historical window and risk snapshot before creating a run." footer={<><Button variant="ghost" onClick={() => setCreateOpen(false)}>Cancel</Button><Button variant="primary" loading={saving} disabled={!name.trim() || !strategyCatalog || !instrumentIds.trim()} onClick={create}>Create and start</Button></>} wide>
        <div className="form-grid"><Field label="Run name"><TextInput value={name} onChange={(event) => setName(event.target.value)} placeholder="e.g. Trend SMA 2025H2" /></Field><Field label="Strategy family"><SelectInput value={strategyCatalog} onChange={(event) => setStrategyCatalog(event.target.value)}><option value="">Choose…</option>{catalogRows.map((item) => <option key={item.catalog_id} value={item.catalog_id}>{item.name || item.catalog_id}</option>)}</SelectInput></Field><Field label="Risk policy"><SelectInput value={riskPolicyId} onChange={(event) => setRiskPolicyId(event.target.value)}>{policyRows.map((policy) => <option key={policy.policy_id} value={policy.policy_id}>{policy.label || policy.policy_id} · v{policy.version}</option>)}</SelectInput></Field><Field label="Instruments" hint="Canonical IDs separated by commas"><TextInput value={instrumentIds} onChange={(event) => setInstrumentIds(event.target.value)} placeholder="XNSE:RELIANCE, XNSE:TCS" /></Field><Field label="Start date"><TextInput type="date" value={start} onChange={(event) => setStart(event.target.value)} /></Field><Field label="End date"><TextInput type="date" value={end} onChange={(event) => setEnd(event.target.value)} /></Field><Field label="Initial capital"><TextInput value={capital} onChange={(event) => setCapital(event.target.value)} inputMode="decimal" /></Field></div>
        <div className="inline-warning">A backtest is historical simulation. It cannot prove future returns and does not connect to paper or live execution.</div>
      </Modal>
      <ConfirmDialog open={!!action} title={action?.action === "start" ? "Start this backtest?" : "Cancel this backtest?"} description="The backend validates the run state and records the transition. Nothing is sent to a broker." confirmLabel={action?.action === "start" ? "Start replay" : "Cancel run"} tone={action?.action === "start" ? "primary" : "danger"} busy={saving} onConfirm={runAction} onClose={() => setAction(null)} />
    </div>
  );
}
