import { useMemo, useState } from "react";
import type { ReactNode } from "react";
import {
  ArrowRightLeft,
  BriefcaseBusiness,
  KeyRound,
  Play,
  RefreshCw,
  RotateCcw,
  ShieldCheck,
  WalletCards,
} from "lucide-react";
import { apiPost, LOCAL_ADMIN_PROXY } from "../api";
import { useApi, useDocumentTitle } from "../hooks";
import { arrayFrom, dateTime, money, num, pctUnsigned, signTone, title } from "../lib";
import {
  Badge,
  Banner,
  Button,
  Card,
  CardHeader,
  DataTable,
  EmptyState,
  ErrorBlock,
  Field,
  KeyValue,
  LoadingBlock,
  Metric,
  PageHeader,
  SelectInput,
  TextInput,
  useToast,
  type Column,
} from "../components/ui";

const BASE = "/market/portfolio-management";

const VIEWS = [
  { id: "overview", label: "Command overview" },
  { id: "positions", label: "Position intelligence" },
  { id: "opportunities", label: "Opportunities" },
  { id: "capital", label: "Capital & cash" },
  { id: "rebalancing", label: "Rebalancing" },
  { id: "decisions", label: "Decision history" },
  { id: "autonomy", label: "Autonomous status" },
] as const;

const HORIZONS = ["5m", "15m", "20m", "60m", "1d", "3d", "5d", "30d", "90d"];

type View = (typeof VIEWS)[number]["id"];

function percent(value: unknown, digits = 2): string {
  if (value === null || value === undefined || value === "") return "—";
  const parsed = Number(value);
  return Number.isFinite(parsed) ? `${(parsed * 100).toFixed(digits)}%` : "—";
}

function actionTone(value: unknown): "good" | "warn" | "bad" | "neutral" | "info" {
  const action = String(value || "").toLowerCase();
  if (["open", "add", "deploy_capital", "deploy_cash", "strong_position"].includes(action)) return "good";
  if (["reduce", "exit", "risk_rejected", "exit_candidate", "reduce_position"].includes(action)) return "bad";
  if (["wait", "review", "retain_cash", "review_required", "wait_for_confirmation"].includes(action)) return "warn";
  if (["hold", "maintain", "no_action", "hold_position"].includes(action)) return "neutral";
  return "info";
}

function Facts({ rows }: { rows: { label: string; value: ReactNode }[] }) {
  return (
    <dl className="key-value-list">
      {rows.map((row) => <KeyValue key={row.label} label={row.label}>{row.value}</KeyValue>)}
    </dl>
  );
}

function positionColumns(): Column<any>[] {
  return [
    { key: "instrument_id", label: "Instrument", render: (row) => <strong>{row.instrument_id || "—"}</strong> },
    { key: "quantity", label: "Qty", align: "right", render: (row) => row.quantity == null ? "—" : num(row.quantity, 0).toLocaleString("en-IN") },
    { key: "average_cost", label: "Entry", align: "right", render: (row) => money(row.average_cost) },
    { key: "current_price", label: "Price", align: "right", render: (row) => money(row.current_price) },
    { key: "current_value", label: "Value", align: "right", render: (row) => money(row.current_value) },
    {
      key: "unrealized_pnl",
      label: "P&L",
      align: "right",
      render: (row) => {
        const tone = signTone(row.unrealized_pnl);
        return (
          <span className={tone === "negative" ? "metric-negative" : tone === "positive" ? "metric-positive" : "muted"}>
            {money(row.unrealized_pnl)}
          </span>
        );
      },
    },
    { key: "position_weight", label: "Weight", align: "right", render: (row) => percent(row.position_weight) },
    {
      key: "forecast",
      label: "Forecast",
      render: (row) => (
        <div className="stack-tight">
          <Badge tone={row.forecast?.direction === "up" ? "good" : row.forecast?.direction === "down" ? "bad" : "neutral"}>
            {title(row.forecast?.direction || "unavailable")}
          </Badge>
          <small className="muted">{title(row.forecast_uncertainty?.class || "unknown")} uncertainty</small>
        </div>
      ),
    },
    { key: "regime", label: "Regime", render: (row) => title(row.regime?.primary || "uncertain") },
    { key: "assessment", label: "Assessment", render: (row) => <Badge tone={actionTone(row.assessment)}>{title(row.assessment)}</Badge> },
    { key: "action", label: "Recommended", render: (row) => <Badge tone={actionTone(row.action)}>{title(row.action)}</Badge> },
  ];
}

function opportunityColumns(): Column<any>[] {
  return [
    { key: "instrument_id", label: "Instrument", render: (row) => <strong>{row.instrument_id || "—"}</strong> },
    { key: "horizon", label: "Horizon" },
    { key: "expected_return", label: "Expected", align: "right", render: (row) => percent(row.expected_return) },
    {
      key: "forecast_uncertainty",
      label: "Uncertainty",
      render: (row) => title(row.forecast_uncertainty?.class || "unknown"),
    },
    { key: "evidence_score", label: "Evidence", align: "right", render: (row) => row.evidence_score == null ? "—" : `${num(row.evidence_score, 0).toFixed(1)}/100` },
    {
      key: "portfolio_compatibility",
      label: "Portfolio fit",
      render: (row) => (
        <div className="stack-tight">
          <Badge tone={row.portfolio_compatibility?.diversifies ? "good" : "warn"}>
            {row.portfolio_compatibility?.diversifies ? "Diversifies" : "Concentrated"}
          </Badge>
          <small className="muted">{title(row.sector || "sector unknown")}</small>
        </div>
      ),
    },
    {
      key: "research",
      label: "Research",
      render: (row) =>
        row.research?.thesis?.stance ? (
          <Badge tone={row.research.thesis.stance === "bullish" ? "good" : "warn"}>
            {title(row.research.thesis.stance)}
          </Badge>
        ) : row.research_reference?.run_id ? (
          <span className="muted">{String(row.research_reference.run_id).slice(0, 14)}…</span>
        ) : (
          <span className="muted">Not available</span>
        ),
    },
    {
      key: "liquidity",
      label: "Liquidity",
      render: (row) =>
        row.liquidity?.latest_volume == null
          ? <span className="muted">Not measured</span>
          : num(row.liquidity.latest_volume, 0).toLocaleString("en-IN"),
    },
    {
      key: "proposed_allocation_inr",
      label: "Proposed",
      align: "right",
      render: (row) => money(row.proposed_allocation_inr),
    },
    {
      key: "eligible",
      label: "Eligible",
      render: (row) => <Badge tone={row.eligible ? "good" : "neutral"}>{row.eligible ? "Yes" : title(row.reason || "no")}</Badge>,
    },
  ];
}

function decisionColumns(): Column<any>[] {
  return [
    { key: "decision_timestamp", label: "Time", render: (row) => dateTime(row.decision_timestamp) },
    { key: "cycle_id", label: "Cycle", render: (row) => <code>{String(row.cycle_id || "").slice(0, 14)}…</code> },
    { key: "instrument_id", label: "Instrument", render: (row) => row.instrument_id || "Portfolio" },
    { key: "action", label: "Decision", render: (row) => <Badge tone={actionTone(row.action)}>{title(row.action)}</Badge> },
    { key: "horizon", label: "Horizon", render: (row) => title(row.horizon || "—") },
    {
      key: "explanation",
      label: "Reasoning",
      render: (row) => <small className="muted">{row.explanation || "No explanation recorded."}</small>,
    },
    {
      key: "supporting_evidence",
      label: "Evidence",
      render: (row) => (
        <div className="stack-tight">
          <span>{arrayFrom(row.supporting_evidence).length || (row.supporting_evidence || []).length} supporting</span>
          <small className="muted">
            {arrayFrom(row.contradictory_evidence).length || (row.contradictory_evidence || []).length} contradictory
          </small>
        </div>
      ),
    },
    {
      key: "risk_engine_reference",
      label: "Risk",
      render: (row) => (
        <Badge tone={row.risk_engine_reference?.outcome === "pass" ? "good" : "warn"}>
          {title(row.risk_engine_reference?.outcome || "reference only")}
        </Badge>
      ),
    },
    {
      key: "execution_outcome",
      label: "Execution",
      render: (row) => title(row.execution_outcome || "not submitted"),
    },
  ];
}

function cycleColumns(): Column<any>[] {
  return [
    { key: "cycle_id", label: "Cycle", render: (row) => <code>{String(row.cycle_id || "").slice(0, 16)}…</code> },
    { key: "started_at", label: "Started", render: (row) => dateTime(row.started_at) },
    { key: "status", label: "Status", render: (row) => <Badge tone={actionTone(row.status)}>{title(row.status)}</Badge> },
    { key: "trigger", label: "Trigger", render: (row) => title(row.trigger) },
    { key: "horizon", label: "Horizon", render: (row) => title(row.horizon || "—") },
    { key: "decision_count", label: "Decisions", align: "right", render: (row) => row.decision_count ?? (row.decisions || []).length },
  ];
}

export function PortfolioManagementPage() {
  useDocumentTitle("Portfolio Management");
  const toast = useToast();
  const [view, setView] = useState<View>("overview");
  const [portfolio, setPortfolio] = useState("");
  const [horizon, setHorizon] = useState("5d");
  const [operatorToken, setOperatorToken] = useState("");
  const [busy, setBusy] = useState("");
  const [revision, setRevision] = useState(0);

  const portfolios = useApi<any>("/market/portfolios?limit=200");
  const portfolioRows = arrayFrom(portfolios.data, "portfolios");
  const selected = portfolio || String(portfolioRows[0]?.portfolio_id || "");
  const encoded = encodeURIComponent(selected);
  const status = useApi<any>(`${BASE}/status`, { refreshMs: 20_000, operatorToken, refreshKey: revision });
  const config = useApi<any>(`${BASE}/config`, { refreshKey: revision });
  const cycles = useApi<any>(`${BASE}/cycles?portfolio_id=${encoded}&limit=50`, { refreshKey: revision });
  const cycleRows = arrayFrom(cycles.data, "items");
  const latestCycleId = String(cycleRows[0]?.cycle_id || "");
  const cycle = useApi<any>(latestCycleId ? `${BASE}/cycles/${encodeURIComponent(latestCycleId)}` : null, { refreshKey: revision });
  const positions = useApi<any>(selected ? `${BASE}/portfolios/${encoded}/positions` : null, { refreshKey: revision });
  const opportunities = useApi<any>(selected ? `${BASE}/portfolios/${encoded}/opportunities` : null, { refreshKey: revision });
  const capital = useApi<any>(selected ? `${BASE}/portfolios/${encoded}/capital` : null, { refreshKey: revision });
  const cash = useApi<any>(selected ? `${BASE}/portfolios/${encoded}/cash` : null, { refreshKey: revision });
  const exposure = useApi<any>(selected ? `${BASE}/portfolios/${encoded}/exposure` : null, { refreshKey: revision });
  const rebalancing = useApi<any>(selected ? `${BASE}/portfolios/${encoded}/rebalancing` : null, { refreshKey: revision });
  const decisions = useApi<any>(selected ? `${BASE}/portfolios/${encoded}/decisions?limit=100` : null, { refreshKey: revision });
  const analytics = useApi<any>(selected ? `${BASE}/portfolios/${encoded}/analytics` : null, { refreshKey: revision });

  const state = status.data || {};
  const latest = cycle.data || cycleRows[0] || {};
  const snapshot = latest.snapshot || {};
  const valuation = latest.analytics || {};
  const positionRows = arrayFrom(positions.data, "positions");
  const opportunityRows = arrayFrom(opportunities.data, "opportunities");
  const decisionRows = arrayFrom(decisions.data, "decisions");
  const policy = config.data?.config || {};
  const allocationRows = useMemo(() => {
    const rows = new Map<string, Record<string, unknown>>();
    for (const row of arrayFrom(rebalancing.data, "current_allocation")) {
      const instrument = String(row.instrument_id || "");
      if (instrument) rows.set(instrument, { instrument_id: instrument, current_weight: row.weight });
    }
    for (const row of arrayFrom(rebalancing.data, "target_allocation")) {
      const instrument = String(row.instrument_id || "");
      if (!instrument) continue;
      rows.set(instrument, { ...(rows.get(instrument) || { instrument_id: instrument }), target_weight: row.weight });
    }
    return [...rows.values()];
  }, [rebalancing.data]);

  const handles = useMemo(
    () => ({
      operatorToken: operatorToken.trim(),
      enabled:
        Boolean(operatorToken.trim()) || LOCAL_ADMIN_PROXY || Boolean(state.operator_authorized),
    }),
    [operatorToken, state.operator_authorized],
  );

  async function reload() {
    setRevision((value) => value + 1);
  }

  async function runReview() {
    if (!selected) return;
    setBusy("review");
    try {
      await apiPost(
        `${BASE}/review`,
        { portfolio_id: selected, horizon, trigger: "manual", request_id: crypto.randomUUID() },
        undefined,
        { operatorToken: handles.operatorToken },
      );
      toast.push("Review completed: the advisory management cycle was recorded.", "success");
      await reload();
    } catch (error: any) {
      toast.push(`Review refused: ${error?.detail || String(error?.message || error)}`, "error");
    } finally {
      setBusy("");
    }
  }

  async function handoff() {
    if (!latestCycleId) return;
    setBusy("handoff");
    try {
      const result = await apiPost(
        `${BASE}/cycles/${encodeURIComponent(latestCycleId)}/handoff`,
        { max_changes: 10 },
        undefined,
        { operatorToken: handles.operatorToken },
      );
      toast.push(
        `Handoff recorded: ${result.handoff?.reason || `PI-12 status ${title(result.handoff?.status)}.`}`,
        result.handoff?.executions?.length ? "success" : "info",
      );
      await reload();
    } catch (error: any) {
      toast.push(`Handoff refused: ${error?.detail || String(error?.message || error)}`, "error");
    } finally {
      setBusy("");
    }
  }

  async function reconcile() {
    if (!latestCycleId) return;
    setBusy("reconcile");
    try {
      await apiPost(
        `${BASE}/cycles/${encodeURIComponent(latestCycleId)}/reconcile`,
        {},
        undefined,
        { operatorToken: handles.operatorToken },
      );
      toast.push("Reconciliation recorded. NM-10 remains the execution authority.", "success");
      await reload();
    } catch (error: any) {
      toast.push(`Reconciliation refused: ${error?.detail || String(error?.message || error)}`, "error");
    } finally {
      setBusy("");
    }
  }

  async function recover() {
    setBusy("recover");
    try {
      const result = await apiPost(`${BASE}/recover`, { reason: "PI-21 operator recovery" }, undefined, {
        operatorToken: handles.operatorToken,
      });
      toast.push(
        `Recovery completed. Autonomy mode is ${title(result.mode || "disabled")}; it was not resumed automatically.`,
        "info",
      );
      await reload();
    } catch (error: any) {
      toast.push(`Recovery refused: ${error?.detail || String(error?.message || error)}`, "error");
    } finally {
      setBusy("");
    }
  }

  return (
    <div className="page">
      <PageHeader
        title="Portfolio Management"
        description="Autonomous portfolio review, position intelligence, capital deployment and controlled rebalancing. Advisory only: NM-08 approves risk, NM-10 executes paper orders, PI-12 owns autonomy."
        actions={
          <div className="page-actions">
            <SelectInput
              value={horizon}
              onChange={(event) => setHorizon(event.target.value)}
              aria-label="Management horizon"
            >
              {HORIZONS.map((item) => (
                <option key={item} value={item}>{item}</option>
              ))}
            </SelectInput>
            <Button variant="secondary" onClick={reload} disabled={Boolean(busy)}>
              <RefreshCw size={15} /> Refresh
            </Button>
            <Button variant="primary" onClick={runReview} disabled={!selected || !handles.enabled || Boolean(busy)}>
              <Play size={15} /> Run review
            </Button>
          </div>
        }
      />

      <Banner tone="info" title="Financial authorities are external">
        Recommendations never mutate positions, cash, ledger or fills and never submit an NM-10 order directly. A
        handoff only continues a persisted AT-03 proposal that NM-08 already approved.
      </Banner>

      <Card>
        <CardHeader
          title="Portfolio scope"
          subtitle="Reviews use the authoritative NM-07 snapshot; long-only, paper only, 1 NC = ₹1."
          action={
            <SelectInput
              value={selected}
              onChange={(event) => setPortfolio(event.target.value)}
              aria-label="Portfolio"
            >
              <option value="">Select portfolio</option>
              {portfolioRows.map((row: any) => (
                <option key={row.portfolio_id} value={row.portfolio_id}>
                  {row.name || row.portfolio_id}
                </option>
              ))}
            </SelectInput>
          }
        />
        {portfolios.error ? <ErrorBlock message={portfolios.error.message} onRetry={portfolios.refresh} /> : null}
        {!portfolioRows.length && !portfolios.loading ? (
          <EmptyState
            icon={<BriefcaseBusiness size={22} />}
            title="No authoritative portfolio"
            description="Create a portfolio before running a management review."
          />
        ) : null}
      </Card>

      <div className="tabs" role="tablist" aria-label="Portfolio management views">
        {VIEWS.map((item) => (
          <button
            key={item.id}
            type="button"
            role="tab"
            aria-selected={view === item.id}
            className={`tab ${view === item.id ? "tab-active" : ""}`}
            onClick={() => setView(item.id)}
          >
            {item.label}
          </button>
        ))}
      </div>

      {view === "overview" ? (
        <OverviewView
          latest={latest}
          snapshot={snapshot}
          valuation={valuation}
          state={state}
          loading={cycle.loading}
          error={cycle.error}
          onRetry={cycle.refresh}
        />
      ) : null}

      {view === "positions" ? (
        <Card>
          <CardHeader title="Position intelligence" subtitle="Forward-looking evidence, not current P&L alone, drives each assessment." />
          {positions.loading ? <LoadingBlock rows={5} label="Reading position assessments…" /> : null}
          {positions.error ? <ErrorBlock message={positions.error.message} onRetry={positions.refresh} /> : null}
          {!positions.loading && !positions.error ? (
            <DataTable
              columns={positionColumns()}
              rows={positionRows}
              rowKey={(row, index) => String(row.assessment_id || row.instrument_id || index)}
              empty={<EmptyState title="No positions" description="The selected portfolio has no authoritative holdings." />}
            />
          ) : null}
        </Card>
      ) : null}

      {view === "opportunities" ? (
        <Card>
          <CardHeader title="Opportunity intelligence" subtitle="Only stored, valid forecasts can create an opportunity; capital is never forced into the market." />
          {opportunities.loading ? <LoadingBlock rows={5} label="Reading opportunity assessments…" /> : null}
          {opportunities.error ? <ErrorBlock message={opportunities.error.message} onRetry={opportunities.refresh} /> : null}
          {!opportunities.loading && !opportunities.error ? (
            <DataTable
              columns={opportunityColumns()}
              rows={opportunityRows}
              rowKey={(row, index) => String(row.opportunity_id || row.instrument_id || index)}
              empty={<EmptyState title="No eligible opportunities" description="No stored forecast currently clears evidence, portfolio-fit and capital rules." />}
            />
          ) : null}
        </Card>
      ) : null}

      {view === "capital" ? (
        <div className="two-col-grid">
          <Card>
            <CardHeader title="Capital deployment" subtitle="Available virtual capital reconciled with commitments and reserve policy." />
            {capital.error ? <ErrorBlock message={capital.error.message} onRetry={capital.refresh} /> : null}
            <div className="metric-grid">
              <Metric label="Available" value={money(capital.data?.available_inr)} icon={<WalletCards size={16} />} />
              <Metric label="Reserved" value={money(capital.data?.reserved_inr)} icon={<ShieldCheck size={16} />} />
              <Metric label="Proposed deployment" value={money(capital.data?.proposed_deployment_inr)} icon={<ArrowRightLeft size={16} />} />
              <Metric label="Unallocated" value={money(capital.data?.unallocated_inr)} icon={<WalletCards size={16} />} />
            </div>
            <Facts
              rows={[
                { label: "Recommendation", value: <Badge tone={actionTone(capital.data?.recommendation)}>{title(capital.data?.recommendation)}</Badge> },
                { label: "Reason", value: capital.data?.reason || "—" },
                { label: "Reserve target", value: percent(capital.data?.reserve_target_fraction) },
                { label: "Deployable", value: money(capital.data?.deployable_inr) },
                { label: "Pending commitments", value: money(capital.data?.pending_commitments_nc) },
              ]}
            />
          </Card>
          <Card>
            <CardHeader title="Cash and reserves" subtitle="Unused cash is a valid portfolio position, not a failed optimisation." />
            {cash.error ? <ErrorBlock message={cash.error.message} onRetry={cash.refresh} /> : null}
            <div className="metric-grid">
              <Metric label="Cash ratio" value={percent(cash.data?.cash_ratio)} />
              <Metric label="Reserve target" value={money(cash.data?.target_reserve_nc)} />
              <Metric label="Available NC" value={money(cash.data?.available_nc)} />
              <Metric label="Reserved NC" value={money(cash.data?.reserved_nc)} />
            </div>
            <Facts
              rows={[
                { label: "Recommendation", value: <Badge tone={actionTone(cash.data?.recommendation)}>{title(cash.data?.recommendation)}</Badge> },
                { label: "Reason", value: cash.data?.reason || "—" },
                { label: "Expected capital needs", value: money(cash.data?.expected_capital_requirements_nc) },
                { label: "Balance modified", value: cash.data?.balance_modified ? "No" : "No" },
              ]}
            />
          </Card>
          <Card className="pm-span-2">
            <CardHeader title="Capital allocation history" subtitle="Each cycle is immutable management research; accounting remains in the NM-07 ledger." />
            <DataTable
              columns={cycleColumns()}
              rows={cycleRows}
              rowKey={(row, index) => String(row.cycle_id || index)}
              compact
              empty={<EmptyState title="No management cycles" description="Run a review to record capital recommendations." />}
            />
          </Card>
        </div>
      ) : null}

      {view === "rebalancing" ? (
        <div className="two-col-grid">
          <Card>
            <CardHeader title="Rebalancing proposal" subtitle="Thresholds, turnover, costs and expected improvement bound every proposed change." />
            {rebalancing.error ? <ErrorBlock message={rebalancing.error.message} onRetry={rebalancing.refresh} /> : null}
            <Facts
              rows={[
                { label: "Source", value: rebalancing.data?.source || "—" },
                { label: "Action", value: <Badge tone={actionTone(rebalancing.data?.action)}>{title(rebalancing.data?.action)}</Badge> },
                { label: "Turnover", value: percent(rebalancing.data?.turnover) },
                { label: "Turnover limit", value: percent(rebalancing.data?.turnover_limit) },
                { label: "Estimated cost", value: money(rebalancing.data?.estimated_cost_inr) },
                { label: "Expected impact", value: rebalancing.data?.expected_portfolio_impact?.summary || "Not quantified" },
                { label: "Risk restrictions", value: arrayFrom(rebalancing.data?.risk_restrictions).join(" · ") || "None recorded" },
                { label: "Why", value: rebalancing.data?.reason || "—" },
              ]}
            />
          </Card>
          <Card>
            <CardHeader title="Exposure research" subtitle="Research thresholds never override NM-08 risk decisions." />
            {exposure.error ? <ErrorBlock message={exposure.error.message} onRetry={exposure.refresh} /> : null}
            <div className="metric-grid">
              <Metric label="HHI" value={exposure.data?.concentration?.hhi == null ? "—" : num(exposure.data.concentration.hhi).toFixed(4)} />
              <Metric label="Top three" value={percent(exposure.data?.concentration?.top_three_weight)} />
              <Metric label="Div. score" value={exposure.data?.diversification?.score == null ? "—" : num(exposure.data.diversification.score).toFixed(4)} />
              <Metric label="Risk outcome" value={title(exposure.data?.risk_outcome)} />
            </div>
            <div className="stack-tight">
              {(exposure.data?.warnings || []).map((warning: any, index: number) => (
                <Badge key={`${warning.code}-${index}`} tone="warn">{title(warning.code)}</Badge>
              ))}
            </div>
          </Card>
          <Card className="pm-span-2">
            <CardHeader title="Allocation comparison" subtitle="Authoritative current weights beside the PI-18 target or management-maintenance target." />
            <DataTable
              columns={[
                { key: "instrument_id", label: "Instrument" },
                { key: "current_weight", label: "Current", align: "right", render: (row) => percent(row.current_weight) },
                { key: "target_weight", label: "Target", align: "right", render: (row) => percent(row.target_weight) },
              ]}
              rows={allocationRows}
              rowKey={(row, index) => String(row.instrument_id || index)}
              compact
              empty={<EmptyState title="No allocation comparison" description="Run a review to capture current and target weights." />}
            />
          </Card>
          <Card className="pm-span-2">
            <CardHeader title="Proposed changes" subtitle="PI-12, NM-08 and NM-10 remain the only route to a paper order." />
            <DataTable
              columns={[
                { key: "instrument_id", label: "Instrument" },
                { key: "action", label: "Action", render: (row) => <Badge tone={actionTone(row.action)}>{title(row.action)}</Badge> },
                { key: "current_weight", label: "Current", align: "right", render: (row) => percent(row.current_weight) },
                { key: "target_weight", label: "Target", align: "right", render: (row) => percent(row.target_weight) },
                { key: "notional_inr", label: "Notional", align: "right", render: (row) => money(row.notional_inr) },
                { key: "estimated_cost_nc", label: "Cost", align: "right", render: (row) => money(row.estimated_cost_nc) },
                { key: "expected_improvement", label: "Expected impact", align: "right", render: (row) => percent(row.expected_improvement) },
              ]}
              rows={rebalancing.data?.changes || []}
              rowKey={(row, index) => String(row.instrument_id || index)}
              empty={<EmptyState title="No changes proposed" description="No allocation gap cleared the threshold, cost and improvement rules." />}
            />
          </Card>
        </div>
      ) : null}

      {view === "decisions" ? (
        <Card>
          <CardHeader
            title="Decision history"
            subtitle="Every decision carries supporting and contradictory evidence. Recommendation quality is not inferred from a profitable trade."
            action={<Badge tone="info">{decisionRows.length} decisions</Badge>}
          />
          {decisions.loading ? <LoadingBlock rows={6} label="Reading decision history…" /> : null}
          {decisions.error ? <ErrorBlock message={decisions.error.message} onRetry={decisions.refresh} /> : null}
          {!decisions.loading && !decisions.error ? (
            <DataTable
              columns={decisionColumns()}
              rows={decisionRows}
              rowKey={(row, index) => String(row.decision_id || index)}
              empty={<EmptyState title="No decisions recorded" description="Run a management review first." />}
            />
          ) : null}
        </Card>
      ) : null}

      {view === "autonomy" ? (
        <div className="two-col-grid">
          <Card>
            <CardHeader title="Autonomous management status" subtitle="Modes, operator permission, lease and kill-switch are server-authoritative." />
            {status.error ? <ErrorBlock message={status.error.message} onRetry={status.refresh} /> : null}
            <div className="metric-grid">
              <Metric label="Mode" value={title(state.mode || "disabled")} icon={<ShieldCheck size={16} />} />
              <Metric label="Runtime" value={title(state.runtime_state || "unknown")} />
              <Metric label="Lease" value={state.lease_valid ? "Valid" : "Not held"} />
              <Metric label="Kill switch" value={state.kill_switch ? "Active" : "Clear"} />
            </div>
            <Facts
              rows={[
                { label: "Operator authorization", value: state.operator_authorized || LOCAL_ADMIN_PROXY ? "Presented" : state.operator_configured ? "Not presented" : "Not configured" },
                { label: "Latest cycle", value: state.latest_cycle_status ? title(state.latest_cycle_status) : "None" },
                { label: "Current cycle", value: state.active_cycle_id || "None" },
                { label: "Pending orders", value: String(latest.snapshot?.pending_orders?.length || 0) },
                { label: "Outstanding reconciliation", value: title(latest.handoff?.status || "none") },
                { label: "One cycle at a time", value: state.safety?.one_cycle_at_a_time === false ? "No" : "Yes" },
                { label: "Recovery resumes autonomy", value: state.safety?.recovery_resumes_autonomy ? "Yes" : "No" },
              ]}
            />
          </Card>
          <Card>
            <CardHeader title="Operator controls" subtitle="A dedicated operator token is required. It is held in memory only." />
            <Field label="Autonomy operator token" hint={LOCAL_ADMIN_PROXY ? "The local admin proxy already authorizes this session; no token is needed here." : "Sent as X-Autonomy-Operator-Token."}>
              <TextInput
                type="password"
                value={operatorToken}
                onChange={(event) => setOperatorToken(event.target.value)}
                placeholder="Operator token"
                autoComplete="off"
              />
            </Field>
            <div className="page-actions">
              <Button variant="primary" onClick={handoff} disabled={!latestCycleId || !handles.enabled || Boolean(busy)}>
                <ArrowRightLeft size={15} /> Hand off approved proposals
              </Button>
              <Button variant="secondary" onClick={reconcile} disabled={!latestCycleId || !handles.enabled || Boolean(busy)}>
                <RefreshCw size={15} /> Reconcile outcomes
              </Button>
              <Button variant="danger" onClick={recover} disabled={!handles.enabled || Boolean(busy)}>
                <RotateCcw size={15} /> Recover to disabled
              </Button>
              <Button variant="ghost" onClick={() => setOperatorToken("")} disabled={!operatorToken}>
                <KeyRound size={15} /> Forget token
              </Button>
            </div>
            <p className="muted">
              Kill-switch and mode restrictions are re-checked before every handoff. A submitted paper order is never
              assumed filled; reconcile it through NM-10.
            </p>
          </Card>
          <Card>
            <CardHeader title="Management policy" subtitle="Advisory research thresholds. These are not NM-08 risk limits." />
            <Facts
              rows={[
                { label: "Version", value: config.data?.version ?? "—" },
                { label: "Cash reserve", value: percent(policy.cash_reserve_target) },
                { label: "Max position", value: percent(policy.max_position_weight) },
                { label: "Max sector", value: percent(policy.max_sector_weight) },
                { label: "Max turnover", value: percent(policy.max_turnover) },
                { label: "Minimum trade", value: money(policy.min_trade_notional) },
                { label: "Minimum evidence", value: `${num(policy.min_evidence_score, 0).toFixed(1)}/100` },
              ]}
            />
          </Card>
          <Card>
            <CardHeader title="Cycle history" subtitle="Stable cycle ids and idempotency keys prevent duplicate work after a restart." />
            <DataTable
              columns={cycleColumns()}
              rows={cycleRows}
              rowKey={(row, index) => String(row.cycle_id || index)}
              compact
              empty={<EmptyState title="No management cycles" description="Run a review to start the history." />}
            />
          </Card>
        </div>
      ) : null}
    </div>
  );
}

function OverviewView({
  latest,
  snapshot,
  valuation,
  state,
  loading,
  error,
  onRetry,
}: {
  latest: any;
  snapshot: any;
  valuation: any;
  state: any;
  loading?: boolean;
  error?: any;
  onRetry?: () => void;
}) {
  if (loading) return <LoadingBlock rows={6} label="Reading management cycle…" />;
  if (error) return <ErrorBlock message={error.message} onRetry={onRetry} />;
  return (
    <>
      <div className="metric-grid">
        <Metric label="Total portfolio value" value={money(snapshot.valuation?.total_value)} />
        <Metric label="Invested capital" value={money(snapshot.valuation?.invested)} />
        <Metric label="Available NC" value={money(snapshot.cash?.available_nc)} />
        <Metric label="INR equivalent" value={money(snapshot.valuation?.total_value)} />
        <Metric label="Realized P&L" value={money(valuation.realized_pnl_nc)} />
        <Metric label="Unrealized P&L" value={money(valuation.unrealized_pnl_nc)} />
        <Metric label="Cash ratio" value={percent(valuation.cash_ratio)} />
        <Metric label="Current exposure" value={percent(valuation.capital_utilization)} />
        <Metric label="Management status" value={title(latest.status || state.latest_cycle_status || "not run")} />
      </div>
      <div className="two-col-grid">
        <Card>
          <CardHeader title="Latest cycle" subtitle="Snapshot, evidence versions and decision configuration are pinned to this cycle." />
          <Facts
            rows={[
              { label: "Cycle", value: latest.cycle_id || "—" },
              { label: "Horizon", value: title(latest.horizon || "—") },
              { label: "Status", value: <Badge tone={actionTone(latest.status)}>{title(latest.status || "—")}</Badge> },
              { label: "Snapshot version", value: latest.portfolio_snapshot_version || snapshot.snapshot_version || "—" },
              { label: "Configuration", value: latest.decision_configuration_version || "—" },
              { label: "Completed", value: dateTime(latest.completed_at) },
            ]}
          />
        </Card>
        <Card>
          <CardHeader title="Exposure summary" subtitle="Instrument HHI is a management research metric, not an NM-08 decision." />
          <Facts
            rows={[
              { label: "Positions", value: String(snapshot.positions?.length || 0) },
              { label: "Warnings", value: String((latest.warnings || []).length) },
              { label: "Data status", value: title(latest.data_status || "—") },
              { label: "Advisory only", value: latest.advisory_only === false ? "No" : "Yes" },
              { label: "Order submitted", value: latest.order_submission ? "Yes (through PI-12)" : "No" },
              { label: "Financial mutation", value: latest.financial_mutation ? "Through authoritative services" : "No" },
            ]}
          />
        </Card>
        <Card className="pm-span-2">
          <CardHeader title="Cycle warnings" subtitle="Missing evidence is reported, never treated as zero." />
          {(latest.warnings || []).length ? (
            <div className="chip-row">
              {latest.warnings.map((warning: string) => <Badge key={warning} tone="warn">{title(warning)}</Badge>)}
            </div>
          ) : (
            <p className="muted">No management-cycle warnings recorded.</p>
          )}
        </Card>
      </div>
    </>
  );
}
