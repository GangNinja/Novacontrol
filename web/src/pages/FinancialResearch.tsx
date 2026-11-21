import { useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  BookOpenCheck,
  Building2,
  FileSearch,
  Gauge,
  Play,
  RefreshCw,
  ShieldCheck,
  Sparkles,
  XCircle,
} from "lucide-react";
import { apiPost } from "../api";
import { useApi, useDocumentTitle } from "../hooks";
import { arrayFrom, compact, dateTime, num, shortDate, title } from "../lib";
import {
  Banner,
  Button,
  Card,
  CardHeader,
  DataTable,
  ErrorBlock,
  Field,
  Metric,
  PageHeader,
  SelectInput,
  StatusBadge,
  TextInput,
  useToast,
  type Column,
} from "../components/ui";

const HORIZONS = [
  ["5m", "Intraday · 5 minutes"],
  ["15m", "Intraday · 15 minutes"],
  ["20m", "Intraday · 20 minutes"],
  ["60m", "Intraday · 60 minutes"],
  ["1d", "Swing · 1 trading day"],
  ["3d", "Swing · 3 trading days"],
  ["5d", "Swing · 5 trading days"],
  ["30d", "Long-term · 30 calendar days"],
  ["90d", "Long-term · 90 calendar days"],
] as const;
const OBJECTIVES = [
  ["full_research", "Full research"],
  ["company_due_diligence", "Company due diligence"],
  ["event_impact", "Event impact"],
  ["relative_valuation", "Relative valuation"],
  ["fundamental_update", "Fundamental update"],
  ["portfolio_context", "Portfolio context"],
] as const;

function ratio(value: unknown, digits = 3): string {
  const number = num(value, NaN);
  return Number.isFinite(number) ? number.toFixed(digits) : "—";
}

function percent(value: unknown, digits = 1): string {
  const number = num(value, NaN);
  return Number.isFinite(number) ? `${(number * 100).toFixed(digits)}%` : "—";
}

// A thesis factor states its own unit, so a ratio, a percentage and an amount
// in the reporting currency are never printed as the same bare number.
function factorValue(value: unknown, unit: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  const kind = String(unit || "ratio");
  if (kind === "text") return String(value);
  if (kind === "percent") return percent(value, 2);
  if (kind === "currency") return `₹ ${compact(value, "—")}`;
  return ratio(value);
}

function Fact({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="key-value">
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

export function FinancialResearchPage() {
  useDocumentTitle("Financial Research");
  const toast = useToast();
  const status = useApi<any>("/market/research/status", { refreshMs: 30_000 });
  const runs = useApi<any>("/market/research/runs?limit=30", { refreshMs: 15_000 });
  const [instrumentId, setInstrumentId] = useState("");
  const [companyName, setCompanyName] = useState("");
  const [exchange, setExchange] = useState("");
  const [horizon, setHorizon] = useState("5d");
  const [objective, setObjective] = useState("full_research");
  const [asOf, setAsOf] = useState("");
  const [selectedRun, setSelectedRun] = useState("");
  const [busy, setBusy] = useState("");
  const [tab, setTab] = useState("company");

  const runRows = arrayFrom(runs.data, "runs");
  const detail = useApi<any>(
    selectedRun ? `/market/research/runs/${encodeURIComponent(selectedRun)}` : null,
    { refreshMs: 12_000 },
  );
  const active = detail.data || runRows.find((row: any) => row.run_id === selectedRun) || null;
  const terminal = ["completed", "completed_with_warnings", "partial_evidence", "insufficient_evidence", "source_unavailable", "stale_data", "failed", "cancelled"].includes(String(active?.status));

  useEffect(() => {
    if (!selectedRun && runRows.length) setSelectedRun(String(runRows[0].run_id));
  }, [runRows, selectedRun]);

  const start = async () => {
    if (!instrumentId.trim()) {
      toast.push("Enter an exchange-qualified instrument, for example XNSE:ACME.", "error");
      return;
    }
    setBusy("start");
    try {
      const payload: Record<string, any> = {
        instrument_id: instrumentId.trim().toUpperCase(),
        company_name: companyName.trim(),
        exchange: exchange.trim().toUpperCase(),
        horizon,
        objective,
      };
      if (asOf.trim()) payload.as_of = new Date(asOf).toISOString();
      const created = await apiPost<any>("/market/research/runs", payload);
      const runId = String(created.run_id);
      setSelectedRun(runId);
      const result = await apiPost<any>(
        `/market/research/runs/${encodeURIComponent(runId)}/run`,
        {},
        undefined,
        { timeoutMs: 120_000 },
      );
      toast.push(
        `Research ${title(result.status)} · ${title(result.thesis?.stance || "no stance")}`,
        result.status === "failed" ? "error" : "success",
      );
      runs.refresh();
      detail.refresh();
      status.refresh();
    } catch (error: any) {
      toast.push(error?.detail || error?.message || "Research could not start.", "error");
    } finally {
      setBusy("");
    }
  };

  const refreshResearch = async () => {
    if (!selectedRun) return;
    setBusy("refresh");
    try {
      const result = await apiPost<any>(
        `/market/research/runs/${encodeURIComponent(selectedRun)}/refresh`,
        {},
        undefined,
        { timeoutMs: 120_000 },
      );
      setSelectedRun(String(result.run_id));
      toast.push("A new research version was created; the prior report was preserved.", "success");
      runs.refresh();
      detail.refresh();
    } catch (error: any) {
      toast.push(error?.detail || error?.message || "Refresh was refused.", "error");
    } finally {
      setBusy("");
    }
  };

  const cancelResearch = async () => {
    if (!selectedRun) return;
    setBusy("cancel");
    try {
      await apiPost(`/market/research/runs/${encodeURIComponent(selectedRun)}/cancel`);
      toast.push("Research cancellation recorded.", "info");
      runs.refresh();
      detail.refresh();
    } catch (error: any) {
      toast.push(error?.detail || error?.message || "Cancellation was refused.", "error");
    } finally {
      setBusy("");
    }
  };

  const company = active?.company || {};
  const financials = active?.financials || {};
  const ratios = financials.ratios || {};
  const events = arrayFrom(active?.events, "events");
  const news = arrayFrom(active?.news, "articles");
  const peers = arrayFrom(active?.peers, "peers");
  const valuation = active?.valuation?.result;
  const thesis = active?.thesis || {};
  const evidence = Array.isArray(active?.evidence) ? active.evidence : [];
  const claims = Array.isArray(active?.claims) ? active.claims : [];
  const warnings = Array.isArray(active?.warnings) ? active.warnings : [];
  const missing = Array.isArray(company.missing_fields) ? company.missing_fields : [];
  const handoff = active?.handoff || {};
  const authority = handoff.authority || {};
  const handoffPackages = handoff.packages && typeof handoff.packages === "object"
    ? Object.entries(handoff.packages).map(([name, body]) => ({ name, body: (body || {}) as any }))
    : [];

  const statementRows = useMemo(() => {
    const rows = Array.isArray(financials.statements) ? [...financials.statements] : [];
    // Newest period first; an annual statement and its Q4 sibling share a
    // period end, so the statement type and period type break the tie.
    return rows.sort((left: any, right: any) => {
      const byDate = String(right.period_end || "").localeCompare(String(left.period_end || ""));
      if (byDate !== 0) return byDate;
      const byStatement = String(left.statement_type || "").localeCompare(String(right.statement_type || ""));
      if (byStatement !== 0) return byStatement;
      return String(left.period_type || "").localeCompare(String(right.period_type || ""));
    });
  }, [financials.statements]);
  const statementColumns: Column<any>[] = [
    { key: "period_end", label: "Period", render: (row) => shortDate(row.period_end) },
    { key: "statement_type", label: "Statement", render: (row) => title(row.statement_type) },
    { key: "period_type", label: "Period type", render: (row) => title(row.period_type) },
    { key: "basis", label: "Basis", render: (row) => title(row.basis) },
    { key: "revenue", label: "Revenue", align: "right", render: (row) => ratio(row.values?.revenue, 0) },
    { key: "operating_profit", label: "Operating profit", align: "right", render: (row) => ratio(row.values?.operating_profit, 0) },
    { key: "net_income", label: "Net income", align: "right", render: (row) => ratio(row.values?.net_income, 0) },
    { key: "eps", label: "EPS", align: "right", render: (row) => ratio(row.values?.eps) },
    { key: "currency", label: "Currency", render: (row) => `${row.normalized_currency || row.currency} × ${ratio(row.multiplier, 0)}` },
  ];
  const ratioRows = Object.entries(ratios.values || {})
    .filter(([, value]) => value !== null && value !== undefined)
    .map(([name, value]) => ({ name, value, status: ratios.statuses?.[name] || "value", formula: ratios.formulas?.[name] || "" }));
  const eventColumns: Column<any>[] = [
    { key: "event_type", label: "Event", render: (row) => title(row.event_type) },
    { key: "announced_at", label: "Announced", render: (row) => dateTime(row.announced_at) },
    { key: "effective_at", label: "Effective", render: (row) => (row.effective_at ? dateTime(row.effective_at) : "—") },
    { key: "occurrence", label: "Occurrence", render: (row) => <StatusBadge value={row.occurrence} /> },
    { key: "verification", label: "Verification", render: (row) => <StatusBadge value={row.verification} /> },
    { key: "source_name", label: "Source", render: (row) => row.source_name || "—" },
  ];
  const newsColumns: Column<any>[] = [
    { key: "headline", label: "Headline", render: (row) => <span>{row.headline}{row.instruction_risk && <AlertTriangle size={12} />}</span> },
    { key: "published_at", label: "Published", render: (row) => dateTime(row.published_at) },
    { key: "source_name", label: "Source", render: (row) => row.source_name || "—" },
    { key: "sentiment", label: "Sentiment", render: (row) => <StatusBadge value={row.sentiment} /> },
    { key: "verification", label: "Verification", render: (row) => <StatusBadge value={row.verification} /> },
  ];
  const peerColumns: Column<any>[] = [
    { key: "instrument_id", label: "Peer", render: (row) => <span className="mono">{row.instrument_id}</span> },
    { key: "name", label: "Name" },
    { key: "metrics", label: "Comparison", render: (row) => {
      const metrics = Object.entries(row.metrics || {});
      return metrics.slice(0, 4).map(([metric, item]: any) => `${title(metric)} ${ratio(item?.peer)} vs ${ratio(item?.target)}`).join(" · ") || "—";
    } },
  ];
  const evidenceColumns: Column<any>[] = [
    { key: "claim", label: "Evidence", render: (row) => row.claim || row.headline || "—" },
    { key: "source_name", label: "Source", render: (row) => row.source_name || "—" },
    { key: "published_at", label: "Published", render: (row) => dateTime(row.published_at) },
    { key: "verification", label: "Verification", render: (row) => <StatusBadge value={row.verification} /> },
    { key: "is_ai_interpretation", label: "AI interpretation", render: (row) => (row.is_ai_interpretation ? "Yes" : "No") },
    { key: "instruction_risk", label: "Instruction risk", render: (row) => (row.instruction_risk ? "Untrusted text retained" : "None") },
  ];
  const factorRows = useMemo(
    () => [
      ...(thesis.positive_factors || []).map((item: any) => ({ ...item, side: "positive" })),
      ...(thesis.negative_factors || []).map((item: any) => ({ ...item, side: "negative" })),
    ],
    [thesis],
  );
  const factorColumns: Column<any>[] = [
    { key: "factor", label: "Factor", render: (row) => title(row.factor) },
    { key: "side", label: "Side", render: (row) => <StatusBadge value={row.side === "positive" ? "positive" : "negative"} /> },
    { key: "value", label: "Value", render: (row) => factorValue(row.value, row.unit) },
    { key: "rationale", label: "Interpretation", render: (row) => row.rationale },
  ];

  return (
    <div className="page">
      <PageHeader
        eyebrow="PI-19 · company intelligence"
        title="Financial Research"
        description="Evidence-based company research across statements, events, news, sectors, peers and valuation. Claims keep source, publication time and verification; research never submits an order or changes an allocation."
        actions={
          <>
            <Button variant="ghost" icon={<RefreshCw size={14} />} onClick={() => { status.refresh(); runs.refresh(); detail.refresh(); }}>Refresh</Button>
            <Button variant="primary" icon={<Play size={15} />} loading={busy === "start"} onClick={start}>Start research</Button>
          </>
        }
      />

      <Banner tone="warn" title="Advisory research only">
        A thesis is not an execution authorization. NM-08 remains the sole risk authority, NM-10 remains the sole paper-execution authority, and research cannot mutate portfolios, ledgers, orders or autonomy modes.
      </Banner>

      <div className="metric-grid">
        <Metric label="Research store" value={status.data?.runs ?? "—"} help="Isolated pr_* records" icon={<BookOpenCheck size={16} />} />
        <Metric label="Selected status" value={<StatusBadge value={active?.status || "no run"} />} help={active?.horizon || "Choose a horizon"} icon={<Gauge size={16} />} />
        <Metric label="Research stance" value={title(thesis.stance || "not generated")} help="Not BUY/SELL; uncertain is allowed" icon={<Sparkles size={16} />} />
        <Metric label="Evidence" value={evidence.length} help={`${claims.length} claims · ${warnings.length} warnings`} icon={<FileSearch size={16} />} />
      </div>

      <Card>
        <CardHeader
          title="Research request"
          subtitle="One explicit instrument, horizon and objective. Evidence after the cutoff is excluded; missing statements are reported, never invented."
          action={<span className="badge">advisory</span>}
        />
        <div className="form-grid">
          <Field label="Instrument" hint="Exchange-qualified MIC:SYMBOL">
            <TextInput value={instrumentId} onChange={(event) => setInstrumentId(event.target.value)} placeholder="XNSE:ACME" />
          </Field>
          <Field label="Company name (optional)"><TextInput value={companyName} onChange={(event) => setCompanyName(event.target.value)} placeholder="Acme Industries" /></Field>
          <Field label="Exchange (optional)"><TextInput value={exchange} onChange={(event) => setExchange(event.target.value)} placeholder="XNSE" /></Field>
          <Field label="Horizon"><SelectInput value={horizon} onChange={(event) => setHorizon(event.target.value)}>{HORIZONS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</SelectInput></Field>
          <Field label="Objective"><SelectInput value={objective} onChange={(event) => setObjective(event.target.value)}>{OBJECTIVES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</SelectInput></Field>
          <Field label="As-of / cutoff (optional)" hint="ISO timestamp; blank means now"><TextInput value={asOf} onChange={(event) => setAsOf(event.target.value)} placeholder="2026-01-31T00:00:00Z" /></Field>
        </div>
        <div className="row-actions">
          <Button variant="primary" icon={<Play size={14} />} loading={busy === "start"} onClick={start}>Start and run</Button>
          <Button variant="ghost" icon={<RefreshCw size={14} />} loading={busy === "refresh"} disabled={!terminal || !selectedRun} onClick={refreshResearch}>New version</Button>
          <Button variant="ghost" icon={<XCircle size={14} />} loading={busy === "cancel"} disabled={terminal || !selectedRun} onClick={cancelResearch}>Cancel</Button>
        </div>
      </Card>

      <Card>
        <CardHeader
          title="Research history"
          subtitle="Reports are versioned. A refresh creates a new run and preserves the prior thesis, evidence and timestamps."
          action={<SelectInput value={selectedRun} onChange={(event) => setSelectedRun(event.target.value)}><option value="">Select a run</option>{runRows.map((row: any) => <option key={row.run_id} value={row.run_id}>{row.run_id} · {row.horizon} · {row.status}</option>)}</SelectInput>}
        />
        {detail.error && <ErrorBlock message={detail.error.detail} onRetry={detail.refresh} />}
        {active ? (
          <div className="stack-sm">
            <div className="row-actions run-meta">
              <StatusBadge value={active.status} />
              <span className="muted mono">{active.run_id}</span>
              <span className="muted">{dateTime(active.created_at)}</span>
              <span className="badge">{active.horizon}</span>
              <span className="badge">v{active.version}</span>
            </div>
            {active.error && <Banner tone="bad" title="Research error">{active.error}</Banner>}
            {warnings.length > 0 && <Banner tone="warn" title={`${warnings.length} research warning(s)`}>{warnings.slice(0, 8).join(" · ")}</Banner>}
            <div className="tabs" role="tablist">
              {["company", "financials", "events", "news", "sector", "peers", "valuation", "thesis", "evidence"].map((id) => (
                <button type="button" role="tab" aria-selected={tab === id} className={`tab${tab === id ? " tab-active" : ""}`} key={id} onClick={() => setTab(id)}>{title(id)}</button>
              ))}
            </div>
          </div>
        ) : <p className="muted">No research run selected. Start a company research request to build its evidence trail.</p>}
      </Card>

      {active && tab === "company" && (
        <Card>
          <CardHeader title="Company overview" subtitle="Profile fields carry their own evidence and publication time. Missing fields are not filled with model guesses." />
          <div className="stack-sm">
            <div className="dashboard-grid">
              <div>
                <Fact label="Company" value={company.name || active.company_name || "—"} />
                <Fact label="Symbol" value={<span className="mono">{company.symbol || "—"}</span>} />
                <Fact label="Exchange" value={company.exchange || active.exchange || "—"} />
                <Fact label="Sector" value={title(company.sector || "unknown")} />
                <Fact label="Industry" value={title(company.industry || "unknown")} />
              </div>
              <div>
                <Fact label="Research status" value={<StatusBadge value={active.status} />} />
                <Fact label="As of" value={dateTime(active.as_of)} />
                <Fact label="Publication cutoff" value={dateTime(active.publication_cutoff || active.as_of)} />
                <Fact label="Version" value={active.version} />
                <Fact label="Parent version" value={active.parent_run_id || "—"} />
              </div>
            </div>
            <Banner tone="info" title="Business description">{company.description || "No sourced description was available."}</Banner>
            {missing.length > 0 && <Banner tone="warn" title="Unavailable profile fields">{missing.map((field: string) => title(field)).join(" · ")}</Banner>}
          </div>
        </Card>
      )}

      {active && tab === "financials" && (
        <>
          <Card>
            <CardHeader title="Income, balance sheet and cash flow" subtitle="Reported values are preserved; normalized values state their unit multiplier and currency. Consolidated and standalone statements are never mixed." />
            <DataTable columns={statementColumns} rows={statementRows} rowKey={(row) => String(row.statement_id)} empty={<p className="muted">No point-in-time financial statement was available. The research does not fabricate one.</p>} />
            {financials.ttm && <Banner tone="info" title={`TTM · ${shortDate(financials.ttm.period_end)}`}>TTM is calculated only from four contiguous quarters. {financials.ttm.warnings?.join(" · ")}</Banner>}
          </Card>
          <Card>
            <CardHeader title="Fundamental ratios" subtitle="Undefined ratios stay unavailable rather than becoming misleading numbers. Formula and input provenance are retained." action={<span className="badge">{ratios.period ? (String(ratios.period).toLowerCase() === "ttm" ? "TTM" : title(ratios.period)) : "latest"}</span>} />
            <DataTable columns={[
              { key: "name", label: "Metric", render: (row) => title(row.name) },
              { key: "value", label: "Value", align: "right", render: (row) => ratio(row.value) },
              { key: "formula", label: "Formula / method" },
            ]} rows={ratioRows} rowKey={(row) => row.name} empty={<p className="muted">No ratio could be calculated from available inputs.</p>} />
            {Array.isArray(ratios.warnings) && ratios.warnings.length > 0 && <Banner tone="warn" title="Ratio limitations">{ratios.warnings.slice(0, 6).join(" · ")}</Banner>}
          </Card>
        </>
      )}

      {active && tab === "events" && (
        <Card>
          <CardHeader title="Corporate events" subtitle="Announcement is not occurrence. Effective dates are separate, and rumours are never confirmed events." />
          <DataTable columns={eventColumns} rows={events} rowKey={(row) => String(row.event_id)} empty={<p className="muted">No verified point-in-time corporate event was found.</p>} />
        </Card>
      )}

      {active && tab === "news" && (
        <Card>
          <CardHeader title="News intelligence" subtitle="Deterministic lexicon sentiment is not a stock-price forecast. AI summaries are never independent sources." />
          <DataTable columns={newsColumns} rows={news} rowKey={(row) => String(row.article_id)} empty={<p className="muted">No source-attributed point-in-time news was available.</p>} />
        </Card>
      )}

      {active && tab === "sector" && (
        <Card>
          <CardHeader title="Sector and macro context" subtitle="Sector metrics keep their source and period. Macro relevance is never assumed for every instrument." />
          <div className="fact-list">
            <Fact label="Company sector (registry)" value={title(active.sector?.target_sector || "unknown")} />
            <Fact label="Company industry (registry)" value={title(active.sector?.target_industry || "unknown")} />
            <Fact label="Snapshot sector" value={title(active.sector?.snapshot?.sector || "not stated")} />
            <Fact label="Snapshot industry" value={title(active.sector?.snapshot?.industry || "not stated")} />
            <Fact label="Relative strength" value={percent(active.sector?.snapshot?.relative_strength)} />
            <Fact label="Performance" value={percent(active.sector?.snapshot?.performance)} />
            <Fact label="Source" value={active.sector?.snapshot?.source_name || "—"} />
            <Fact label="Published" value={active.sector?.snapshot?.published_at ? dateTime(active.sector.snapshot.published_at) : "—"} />
          </div>
          {String(active.sector?.snapshot?.sector || "").toLowerCase() !== "" &&
            String(active.sector?.snapshot?.sector || "").toLowerCase() !==
              String(active.sector?.target_sector || "").toLowerCase() && (
              <Banner tone="warn" title="Classifications differ">
                {`The reference snapshot classifies this instrument as ${title(active.sector.snapshot.sector)}, while the registry identity says ${title(active.sector?.target_sector || "unknown")}. Both are shown with their own source and period; neither is substituted for the other.`}
              </Banner>
            )}
          <DataTable columns={[
            { key: "indicator", label: "Macro indicator" },
            { key: "value", label: "Value", align: "right", render: (row) => `${ratio(row.value)} ${row.unit || ""}` },
            { key: "period", label: "Period" },
            { key: "relevant", label: "Relevance", render: (row) => (row.relevant ? "Caller-linked" : "Not established") },
            { key: "revision_status", label: "Revision" },
          ]} rows={Array.isArray(active.macro?.observations) ? active.macro.observations : []} rowKey={(row) => `${row.indicator}-${row.period}`} empty={<p className="muted">No macro observation was supplied. Macro is optional and never fabricated.</p>} />
        </Card>
      )}

      {active && tab === "peers" && (
        <Card>
          <CardHeader title="Peer comparison" subtitle="Peer selection uses documented industry, currency and market-cap criteria. Different businesses are not silently treated as comparable." action={<span className="badge">{active.peers?.selected_count ?? 0} selected</span>} />
          <DataTable columns={peerColumns} rows={peers} rowKey={(row) => String(row.instrument_id)} empty={<p className="muted">No peer passed the documented comparison criteria.</p>} />
          {Array.isArray(active.peers?.excluded) && active.peers.excluded.length > 0 && <Banner tone="warn" title="Excluded peers">{active.peers.excluded.map((row: any) => `${row.instrument_id}: ${(row.reasons || []).join("; ")}`).join(" · ")}</Banner>}
        </Card>
      )}

      {active && tab === "valuation" && (
        <Card>
          <CardHeader title="Valuation intelligence" subtitle="A derived estimate is not an objectively verified price. DCF assumptions, sensitivity and limitations are shown explicitly." />
          {valuation ? (
            <div className="stack-sm">
              <div className="metric-grid">
                <Metric label="Method" value={title(valuation.method)} />
                <Metric label="Estimate / share" value={valuation.estimate == null ? "—" : ratio(valuation.estimate)} />
                <Metric label="Range" value={`${valuation.low == null ? "—" : ratio(valuation.low)} – ${valuation.high == null ? "—" : ratio(valuation.high)}`} />
                <Metric label="Currency" value={valuation.currency || "INR"} />
              </div>
              <Banner tone="info" title="Estimate basis">{valuation.inputs?.estimate_basis || "assumption-driven"}. {valuation.warnings?.join(" · ")}</Banner>
              {Array.isArray(valuation.sensitivity) && valuation.sensitivity.length > 0 && (
                <DataTable columns={[
                  { key: "discount_rate", label: "Discount rate", render: (row) => percent(row.discount_rate, 1) },
                  { key: "terminal_growth", label: "Terminal growth", render: (row) => percent(row.terminal_growth, 1) },
                  { key: "value_per_share", label: "Value / share", align: "right", render: (row) => row.value_per_share == null ? "—" : ratio(row.value_per_share) },
                ]} rows={valuation.sensitivity} rowKey={(row) => `${row.discount_rate}-${row.terminal_growth}`} />
              )}
              <Banner tone="warn" title="Limitations">{valuation.limitations?.join(" · ")}</Banner>
            </div>
          ) : <p className="muted">No valuation was produced because sufficient reported inputs and explicit assumptions were not both available.</p>}
        </Card>
      )}

      {active && tab === "thesis" && (
        <>
          <Card>
            <CardHeader title="Investment thesis" subtitle="The stance is a research conclusion, not a BUY/SELL instruction. Counter-evidence and unresolved questions remain visible." action={<StatusBadge value={thesis.stance || "insufficient_evidence"} />} />
            <div className="stack-sm">
              <Banner tone={thesis.stance === "bearish" ? "bad" : thesis.stance === "bullish" ? "good" : "info"} title="Conclusion">{thesis.conclusion || "No thesis was generated."}</Banner>
              <div className="metric-grid">
                <Metric label="Source reliability" value={title(thesis.confidence?.source_reliability)} />
                <Metric label="Evidence strength" value={title(thesis.confidence?.evidence_strength)} />
                <Metric label="Forecast confidence" value={title(thesis.confidence?.forecast_confidence)} />
                <Metric label="Decision confidence" value={title(thesis.confidence?.decision_confidence)} />
              </div>
              <p className="muted">{thesis.confidence?.combined_probability_note}</p>
              <DataTable columns={factorColumns} rows={factorRows} rowKey={(row) => `${row.side}-${row.factor}-${row.value}`} empty={<p className="muted">No positive or negative factor had sufficient evidence.</p>} />
              {Array.isArray(thesis.unresolved_questions) && thesis.unresolved_questions.length > 0 && <Banner tone="warn" title="Unresolved questions">{thesis.unresolved_questions.join(" · ")}</Banner>}
              {Array.isArray(thesis.assumptions) && thesis.assumptions.length > 0 && <p className="muted">{thesis.assumptions.join(" · ")}</p>}
            </div>
          </Card>
          <Card>
            <CardHeader title="Forecast and portfolio context" subtitle="Forecast disagreement is reported separately. Portfolio context is read-only and never changes an allocation." />
            <div className="fact-list">
              <Fact label="Forecast available" value={thesis.forecast_context?.available ? "Yes" : "No"} />
              <Fact label="Expected return mean" value={percent(thesis.forecast_context?.expected_return_mean, 2)} />
              <Fact label="Disagreement" value={ratio(thesis.forecast_context?.disagreement)} />
              <Fact label="Portfolio" value={thesis.portfolio_context?.portfolio_id || "not supplied"} />
              <Fact label="Allocation change" value={thesis.portfolio_context?.can_change_allocation === false ? "Prohibited" : "Not authorized"} />
              <Fact label="Requires PI-18" value={thesis.portfolio_context?.requires_pi18_optimizer ? "Yes" : "—"} />
            </div>
          </Card>
        </>
      )}

      {active && tab === "evidence" && (
        <>
          <Card>
            <CardHeader title="Evidence explorer" subtitle="Every eligible record keeps source, publication/retrieval time, verification and content hash. Conflicting evidence is preserved, not averaged." />
            <DataTable columns={evidenceColumns} rows={evidence} rowKey={(row) => String(row.evidence_id)} empty={<p className="muted">No eligible evidence exists for this run.</p>} />
          </Card>
          <Card>
            <CardHeader title="Material claims" subtitle="AI interpretation is not independent verification; circular agent citation cannot promote a claim." />
            <DataTable columns={[
              { key: "claim", label: "Claim" },
              { key: "source_name", label: "Source" },
              { key: "publication_time", label: "Published", render: (row) => row.publication_time ? dateTime(row.publication_time) : "—" },
              { key: "status", label: "Verification", render: (row) => <StatusBadge value={row.status} /> },
            ]} rows={claims} rowKey={(row) => String(row.claim_id)} empty={<p className="muted">No material claim was recorded.</p>} />
          </Card>
          <Card>
            <CardHeader title="Feature set and authority" subtitle="Feature evidence is versioned and point-in-time; research holds no execution authority." />
            <div className="fact-list">
              <Fact label="Feature set" value={active.features?.feature_set_version || "—"} />
              <Fact label="Production forecasting" value={active.features?.production_forecasting_use ? "Enabled" : "Not enabled"} />
              <Fact label="Automatic retraining" value={active.features?.automatic_retraining ? "Enabled" : "Disabled"} />
              <Fact label="Research can submit orders" value={active.can_submit_orders ? "Unexpected" : "No"} />
              <Fact label="Research can mutate portfolio" value={authority.research_can_mutate_portfolio ? "Unexpected" : "No"} />
              <Fact label="Research can write ledger" value={authority.research_can_write_ledger ? "Unexpected" : "No"} />
              <Fact label="Research can change autonomy" value={authority.research_can_change_autonomy ? "Unexpected" : "No"} />
              <Fact label="Experimental RL" value={authority.experimental_rl || "advisory only"} />
              <Fact label="NM-08 authority" value={authority.nm08 || (active.nm08_risk_authority ? "sole risk authority" : "Unexpected")} />
              <Fact label="NM-10 authority" value={authority.nm10 || (active.nm10_execution_authority ? "sole paper-execution authority" : "Unexpected")} />
            </div>
          </Card>
          <Card>
            <CardHeader title="Integration handoff" subtitle="Structured context for PI-12, PI-17, PI-18 and PI-04. No component receives execution authority." />
            <div className="fact-list">
              <Fact label="Contract" value={handoff.contract || "—"} />
              <Fact label="Evidence references preserved" value={handoff.evidence_references_preserved ? "Yes" : "No"} />
              <Fact label="PI-12 allowed modes" value={Array.isArray(handoff.pi12_context?.allowed_modes) && handoff.pi12_context.allowed_modes.length ? handoff.pi12_context.allowed_modes.join(" · ") : "—"} />
              <Fact label="PI-12 cannot submit orders" value={handoff.pi12_context?.cannot_submit_orders === false ? "Unexpected" : "Yes"} />
              <Fact label="PI-12 can change autonomy" value={handoff.pi12_context?.can_change_autonomy_mode ? "Unexpected" : "No"} />
              <Fact label="PI-18 advisory only" value={handoff.pi18_inputs?.advisory_only === false ? "Unexpected" : "Yes"} />
              <Fact label="PI-18 can override constraints" value={handoff.pi18_inputs?.cannot_override_constraints === false ? "Unexpected" : "No"} />
            </div>
            <DataTable columns={[
              { key: "name", label: "Package", render: (row) => title(row.name) },
              { key: "fields", label: "Declared fields", render: (row) => Object.keys(row.body || {}).join(", ") || "—" },
              { key: "cannot", label: "Cannot assert", render: (row) => (Array.isArray(row.body?.cannot_assert) ? row.body.cannot_assert.join(" ") : "—") },
            ]} rows={handoffPackages} rowKey={(row) => String(row.name)} empty={<p className="muted">This run produced no handoff packages.</p>} />
            {handoff.circular_evidence_rule && <p className="muted">{handoff.circular_evidence_rule}</p>}
          </Card>
        </>
      )}

      <Card>
        <CardHeader title="Scope and limitations" subtitle="What this workspace does and does not claim." />
        <div className="stack-sm muted">
          <p><Building2 size={13} /> Company identity is resolved from the existing instrument registry; profile facts require evidence.</p>
          <p><FileSearch size={13} /> Financial statements and filings arrive only through configured adapters; the UI never invents a missing period.</p>
          <p><ShieldCheck size={13} /> Research remains advisory. NM-08 risk approval and NM-10 paper execution are required for any operational action.</p>
        </div>
      </Card>
    </div>
  );
}
