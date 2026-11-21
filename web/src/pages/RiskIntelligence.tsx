import { useMemo, useState } from "react";
import type { ReactNode } from "react";
import {
  ArrowRightLeft,
  BarChart3,
  Play,
  RefreshCw,
  ShieldAlert,
} from "lucide-react";
import { apiPost, LOCAL_ADMIN_PROXY } from "../api";
import { useApi, useDocumentTitle } from "../hooks";
import { arrayFrom, dateTime, money, num, title } from "../lib";
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

const BASE = "/market/risk-intelligence";

const VIEWS = [
  { id: "overview", label: "Risk overview" },
  { id: "positions", label: "Position risk" },
  { id: "concentration", label: "Concentration & correlation" },
  { id: "volatility", label: "Volatility & drawdown" },
  { id: "liquidity", label: "Liquidity" },
  { id: "scenarios", label: "Scenarios & stress" },
  { id: "alerts", label: "Alerts & recommendations" },
] as const;

type View = (typeof VIEWS)[number]["id"];

function tone(value: unknown): "good" | "warn" | "bad" | "neutral" {
  const level = String(value || "").toLowerCase();
  if (["critical", "critical_alert", "very_low_liquidity"].includes(level)) return "bad";
  if (["high", "high_alert", "low_liquidity"].includes(level)) return "warn";
  if (["elevated", "watch", "warning"].includes(level)) return "warn";
  if (["normal", "completed", "fresh", "high_liquidity"].includes(level)) return "good";
  if (["insufficient_data", "stale_data", "calculation_failed"].includes(level)) return "bad";
  return "neutral";
}

function pct(value: unknown, digits = 2): string {
  if (value === null || value === undefined || value === "") return "—";
  const parsed = Number(value);
  return Number.isFinite(parsed) ? `${parsed.toFixed(digits)}%` : "—";
}

function numberText(value: unknown, digits = 4): string {
  // num() takes a fallback, so passing one here would print a confident number
  // for a measurement the API reported as absent. Absent stays "—".
  if (value === null || value === undefined || value === "") return "—";
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed.toFixed(digits) : "—";
}

function weightPct(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  const parsed = Number(value);
  return Number.isFinite(parsed) ? `${(parsed * 100).toFixed(2)}%` : "—";
}

function metricValue(metric: any): ReactNode {
  if (!metric || metric.value === null || metric.value === undefined) return "—";
  return `${num(metric.value, 4).toFixed(4)}${metric.unit === "percent" ? "%" : ""}`;
}

function Facts({ rows }: { rows: { label: string; value: ReactNode }[] }) {
  return (
    <dl className="key-value-list">
      {rows.map((row) => (
        <KeyValue key={row.label} label={row.label}>{row.value}</KeyValue>
      ))}
    </dl>
  );
}

function Table({
  rows,
  columns,
  emptyMessage,
}: {
  rows: any[];
  columns: Column<any>[];
  emptyMessage: string;
}) {
  return (
    <DataTable
      rows={rows}
      columns={columns}
      rowKey={(row, index) =>
        String(
          // Scenario runs and stress results carry the assessment_id they were
          // computed from, so the row's own identifier must win or three runs
          // share one React key and rows get duplicated or dropped.
          row.alert_id ||
            row.recommendation_id ||
            row.stress_id ||
            row.run_id ||
            row.scenario_id ||
            row.assessment_id ||
            row.instrument_id ||
            index,
        )
      }
      empty={<EmptyState title="No evidence" description={emptyMessage} />}
    />
  );
}

function positionColumns(): Column<any>[] {
  return [
    { key: "instrument_id", label: "Instrument", render: (row) => <strong>{row.instrument_id || "—"}</strong> },
    { key: "sector", label: "Sector", render: (row) => row.sector || "Unknown" },
    { key: "weight", label: "Weight", align: "right", render: (row) => weightPct(row.weight) },
    { key: "market_value_nc", label: "Value (NC)", align: "right", render: (row) => money(row.market_value_nc) },
    { key: "return_pct", label: "Return", align: "right", render: (row) => pct(row.return_pct) },
    {
      key: "volatility",
      label: "Volatility",
      align: "right",
      render: (row) => metricValue(row.volatility?.annualized_volatility_pct),
    },
    {
      key: "drawdown",
      label: "Drawdown",
      align: "right",
      render: (row) => pct(row.drawdown?.current_drawdown_pct),
    },
    { key: "liquidity_class", label: "Liquidity", render: (row) => <Badge tone={tone(row.liquidity_class)}>{title(row.liquidity_class)}</Badge> },
    { key: "risk_level", label: "Risk", render: (row) => <Badge tone={tone(row.risk_level)}>{title(row.risk_level)}</Badge> },
    { key: "recommended_action", label: "Response", render: (row) => title(row.recommended_action || "maintain") },
    { key: "nm08_reference", label: "NM-08", render: (row) => row.nm08_reference || <span className="muted">Not supplied</span> },
  ];
}

function alertColumns(): Column<any>[] {
  return [
    { key: "severity", label: "Severity", render: (row) => <Badge tone={tone(row.severity)}>{title(row.severity)}</Badge> },
    { key: "risk_category", label: "Category", render: (row) => title(row.risk_category) },
    { key: "instrument_id", label: "Reference", render: (row) => row.instrument_id || "Portfolio" },
    { key: "explanation", label: "Evidence", render: (row) => <small className="muted">{row.explanation || "—"}</small> },
    { key: "occurrences", label: "Seen", align: "right", render: (row) => row.occurrences ?? row.occurrence_count ?? 1 },
    { key: "last_seen_at", label: "Last seen", render: (row) => dateTime(row.last_seen || row.last_seen_at || row.observed_at) },
    { key: "status", label: "Status", render: (row) => <Badge tone={tone(row.status)}>{title(row.status)}</Badge> },
    { key: "recommended_response", label: "Response", render: (row) => title(row.recommended_response) },
  ];
}

function recommendationColumns(): Column<any>[] {
  return [
    { key: "action", label: "Recommendation", render: (row) => <Badge tone={tone(row.severity)}>{title(row.action)}</Badge> },
    { key: "instrument_id", label: "Reference", render: (row) => row.instrument_id || "Portfolio" },
    { key: "risk_category", label: "Category", render: (row) => title(row.risk_category) },
    { key: "severity", label: "Severity", render: (row) => title(row.severity) },
    { key: "suggested_response", label: "Suggested response" },
    { key: "horizon", label: "Horizon", render: (row) => title(row.horizon) },
    { key: "nm08_reference", label: "NM-08", render: (row) => row.nm08_reference || <span className="muted">Pending authority</span> },
  ];
}

function scenarioColumns(): Column<any>[] {
  return [
    { key: "name", label: "Scenario", render: (row) => <strong>{row.name || row.kind}</strong> },
    { key: "kind", label: "Kind", render: (row) => title(row.kind) },
    { key: "shock_pct", label: "Shock", align: "right", render: (row) => pct(row.shock_pct) },
    { key: "sector", label: "Sector", render: (row) => row.sector || "All" },
    { key: "instrument_id", label: "Instrument", render: (row) => row.instrument_id || "All" },
    { key: "created_at", label: "Created", render: (row) => dateTime(row.created_at) },
  ];
}

function stressColumns(): Column<any>[] {
  return [
    { key: "mode", label: "Mode", render: (row) => title(row.mode) },
    { key: "status", label: "Status", render: (row) => <Badge tone={tone(row.status)}>{title(row.status)}</Badge> },
    { key: "estimated_change_nc", label: "Estimated change", align: "right", render: (row) => money(row.estimated_change_nc ?? row.result?.estimated_change_nc ?? row.result?.estimated_loss_nc) },
    { key: "estimated_portfolio_value_nc", label: "Estimated value", align: "right", render: (row) => money(row.estimated_portfolio_value_nc ?? row.result?.estimated_portfolio_value_nc ?? row.result?.estimated_value_nc) },
    { key: "methodology", label: "Methodology", render: (row) => <small className="muted">{row.methodology || row.result?.methodology || "—"}</small> },
    { key: "created_at", label: "Run", render: (row) => dateTime(row.created_at || row.generated_at) },
  ];
}

export function RiskIntelligencePage() {
  useDocumentTitle("Risk Intelligence");
  const toast = useToast();
  const [view, setView] = useState<View>("overview");
  const [portfolio, setPortfolio] = useState("");
  const [horizon, setHorizon] = useState("5d");
  const [operatorToken, setOperatorToken] = useState("");
  const [busy, setBusy] = useState("");
  const [revision, setRevision] = useState(0);
  const [scenarioKind, setScenarioKind] = useState("market_decline");
  const [scenarioShock, setScenarioShock] = useState("-10");
  const [scenarioSector, setScenarioSector] = useState("");
  const [scenarioInstrument, setScenarioInstrument] = useState("");
  const [stressMode, setStressMode] = useState("volatility");
  const [stressShock, setStressShock] = useState("-10");

  const portfolios = useApi<any>("/market/portfolios?limit=200");
  const portfolioRows = arrayFrom(portfolios.data, "portfolios");
  const selected = portfolio || String(portfolioRows[0]?.portfolio_id || "");
  const encoded = encodeURIComponent(selected);
  const status = useApi<any>(`${BASE}/status`, { refreshMs: 20_000, operatorToken, refreshKey: revision });
  const config = useApi<any>(`${BASE}/config`, { refreshKey: revision });
  const overview = useApi<any>(selected ? `${BASE}/portfolios/${encoded}/overview` : null, { refreshKey: revision });
  const positions = useApi<any>(selected ? `${BASE}/portfolios/${encoded}/positions` : null, { refreshKey: revision });
  const concentration = useApi<any>(selected ? `${BASE}/portfolios/${encoded}/concentration` : null, { refreshKey: revision });
  const correlation = useApi<any>(selected ? `${BASE}/portfolios/${encoded}/correlation` : null, { refreshKey: revision });
  const volatility = useApi<any>(selected ? `${BASE}/portfolios/${encoded}/volatility` : null, { refreshKey: revision });
  const drawdown = useApi<any>(selected ? `${BASE}/portfolios/${encoded}/drawdown` : null, { refreshKey: revision });
  const liquidity = useApi<any>(selected ? `${BASE}/portfolios/${encoded}/liquidity` : null, { refreshKey: revision });
  const history = useApi<any>(selected ? `${BASE}/portfolios/${encoded}/history?limit=50` : null, { refreshKey: revision });
  const alerts = useApi<any>(`${BASE}/alerts?portfolio_id=${encoded}&limit=100`, { refreshKey: revision });
  const recommendations = useApi<any>(selected ? `${BASE}/portfolios/${encoded}/recommendations` : null, { refreshKey: revision });
  const scenarios = useApi<any>(selected ? `${BASE}/scenarios?portfolio_id=${encoded}` : null, { refreshKey: revision });
  const scenarioRuns = useApi<any>(selected ? `${BASE}/portfolios/${encoded}/scenario-runs` : null, { refreshKey: revision });
  const stresses = useApi<any>(selected ? `${BASE}/portfolios/${encoded}/stress-tests` : null, { refreshKey: revision });

  const latest = overview.data || {};
  const positionRows = arrayFrom(positions.data, "positions");
  const alertRows = arrayFrom(alerts.data, "alerts");
  const recommendationRows = arrayFrom(recommendations.data, "recommendations");
  const scenarioRows = arrayFrom(scenarios.data, "scenarios");
  const stressRows = arrayFrom(stresses.data, "stress_results");
  const scenarioRunRows = arrayFrom(scenarioRuns.data, "scenario_runs");
  const policy = config.data?.config || {};
  const state = status.data || {};
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

  async function runAnalysis() {
    if (!selected) return;
    setBusy("analyze");
    try {
      await apiPost(
        `${BASE}/analyze`,
        { portfolio_id: selected, horizon, trigger: "manual", request_id: crypto.randomUUID() },
        undefined,
        { operatorToken: handles.operatorToken },
      );
      toast.push("Risk assessment recorded from the latest authoritative snapshot.", "success");
      await reload();
    } catch (error: any) {
      toast.push(`Risk analysis refused: ${error?.detail?.message || error?.detail || String(error?.message || error)}`, "error");
    } finally {
      setBusy("");
    }
  }

  async function createAndRunScenario() {
    if (!selected) return;
    setBusy("scenario");
    try {
      const created = await apiPost(
        `${BASE}/scenarios`,
        {
          portfolio_id: selected,
          kind: scenarioKind,
          shock_pct: Number(scenarioShock),
          sector: scenarioSector.trim(),
          instrument_id: scenarioInstrument.trim(),
        },
        undefined,
        { operatorToken: handles.operatorToken },
      );
      await apiPost(
        `${BASE}/portfolios/${encoded}/scenarios/run`,
        { scenario_id: created.scenario?.scenario_id },
        undefined,
        { operatorToken: handles.operatorToken },
      );
      toast.push("Hypothetical scenario run recorded. It is not a forecast or an order.", "success");
      await reload();
    } catch (error: any) {
      toast.push(`Scenario refused: ${error?.detail?.message || error?.detail || String(error?.message || error)}`, "error");
    } finally {
      setBusy("");
    }
  }

  async function runStress() {
    if (!selected) return;
    setBusy("stress");
    try {
      await apiPost(
        `${BASE}/portfolios/${encoded}/stress-tests`,
        { mode: stressMode, shock_pct: Number(stressShock) },
        undefined,
        { operatorToken: handles.operatorToken },
      );
      toast.push("Isolated stress test recorded; balances were not modified.", "success");
      await reload();
    } catch (error: any) {
      toast.push(`Stress test refused: ${error?.detail?.message || error?.detail || String(error?.message || error)}`, "error");
    } finally {
      setBusy("");
    }
  }

  if (!portfolioRows.length && !portfolios.loading) {
    return (
      <div className="page">
        <PageHeader title="Risk Intelligence" description="Advanced portfolio risk analysis, scenarios, stress tests and protective recommendations. Advisory only." />
        <EmptyState icon={<ShieldAlert size={22} />} title="No authoritative portfolio" description="Create a portfolio before running a risk assessment." />
      </div>
    );
  }

  return (
    <div className="page">
      <PageHeader
        title="Risk Intelligence"
        description="Explainable instrument, position, sector, portfolio, regime and liquidity risk. NM-08 remains the sole risk authority; NM-10 remains the sole paper-execution engine."
        actions={
          <div className="page-actions">
            <SelectInput value={horizon} onChange={(event) => setHorizon(event.target.value)} aria-label="Assessment horizon">
              {["1d", "3d", "5d", "30d", "90d"].map((item) => <option key={item} value={item}>{item}</option>)}
            </SelectInput>
            <Button variant="secondary" onClick={reload} disabled={Boolean(busy)}><RefreshCw size={15} /> Refresh</Button>
            <Button variant="primary" onClick={runAnalysis} disabled={!selected || !handles.enabled || Boolean(busy)}><Play size={15} /> Run assessment</Button>
          </div>
        }
      />

      <Banner tone="info" title="Advisory analysis, authoritative controls">
        This workspace never mutates positions, cash, the ledger or fills and never submits an order. Recommendations
        remain advisory until PI-21 review, an NM-08 assessment and the existing PI-12/NM-10 paper workflow authorize them.
      </Banner>

      <Card>
        <CardHeader
          title="Portfolio scope"
          subtitle="Analytics use the authoritative NM-07 snapshot. Missing data is reported, never treated as zero risk."
          action={
            <SelectInput value={selected} onChange={(event) => setPortfolio(event.target.value)} aria-label="Portfolio">
              <option value="">Select portfolio</option>
              {portfolioRows.map((row: any) => <option key={row.portfolio_id} value={row.portfolio_id}>{row.name || row.portfolio_id}</option>)}
            </SelectInput>
          }
        />
        <div className="field-grid">
          <Field label="Operator token" hint={LOCAL_ADMIN_PROXY ? "The local admin proxy already authorizes this session; no token is needed here." : "Required only for analysis, scenario, stress and alert actions."}>
            <TextInput type="password" value={operatorToken} onChange={(event) => setOperatorToken(event.target.value)} autoComplete="off" />
          </Field>
          <Facts rows={[
            { label: "Mode", value: title(state.mode || "unavailable") },
            { label: "Operator", value: state.operator_authorized || LOCAL_ADMIN_PROXY ? <Badge tone="good">Authorized</Badge> : <Badge tone="warn">Token required</Badge> },
            { label: "Latest assessment", value: state.latest_assessment_status ? title(state.latest_assessment_status) : "Not run" },
            { label: "Kill switch", value: state.kill_switch ? <Badge tone="bad">Active</Badge> : <Badge tone="good">Clear</Badge> },
          ]} />
        </div>
        {config.error ? <ErrorBlock message={config.error.message} onRetry={config.refresh} /> : null}
      </Card>

      <div className="segmented-control" role="tablist" aria-label="Risk intelligence views">
        {VIEWS.map((item) => (
          <button key={item.id} type="button" role="tab" aria-selected={view === item.id} className={view === item.id ? "active" : ""} onClick={() => setView(item.id)}>
            {item.label}
          </button>
        ))}
      </div>

      {overview.loading && !overview.data ? <LoadingBlock label="Loading risk evidence" /> : null}
      {overview.error ? <ErrorBlock message={overview.error.message} onRetry={overview.refresh} /> : null}
      {!overview.loading && !overview.data?.assessment_id ? (
        <Card>
          <EmptyState icon={<ShieldAlert size={22} />} title="No risk assessment yet" description="Run an assessment to record evidence from the latest authoritative portfolio snapshot." action={<Button onClick={runAnalysis} disabled={!handles.enabled || Boolean(busy)}>Run assessment</Button>} />
        </Card>
      ) : null}

      {view === "overview" && overview.data?.assessment_id ? (
        <>
          <div className="metric-grid">
            <Metric label="Portfolio risk" value={title(latest.risk_level)} tone={tone(latest.risk_level)} help={`Trend: ${title(latest.trend)}`} />
            <Metric label="Data status" value={title(latest.data_status)} tone={tone(latest.data_status)} help={`Evidence ${dateTime(latest.generated_at)}`} />
            <Metric label="Drawdown" value={pct(latest.drawdown?.current_drawdown_pct)} tone={tone(latest.drawdown?.risk_level)} help={`Max ${pct(latest.drawdown?.max_drawdown_pct)}`} />
            <Metric label="Volatility" value={metricValue(latest.volatility?.annualized_volatility_pct)} tone={tone(latest.volatility?.risk_level)} help="Annualised historical" />
            <Metric label="Concentration" value={title(latest.concentration?.risk_level)} tone={tone(latest.concentration?.risk_level)} help={`HHI ${numberText(latest.concentration?.hhi, 4)}`} />
            <Metric label="Warnings" value={String(latest.warnings?.length || 0)} tone={latest.warnings?.length ? "warn" : "good"} help={`${latest.alerts?.length || 0} active alerts`} />
          </div>
          <div className="grid-two">
            <Card>
              <CardHeader title="Portfolio evidence" subtitle="Monetary values in NC and INR (1 NC = ₹1)." />
              <Facts rows={[
                { label: "Total value", value: `${money(latest.portfolio?.total_value_nc)} / ₹${money(latest.portfolio?.total_value_inr)}` },
                { label: "Invested", value: `${money(latest.portfolio?.invested_nc)} / ₹${money(latest.portfolio?.invested_inr)}` },
                { label: "Available cash", value: `${money(latest.portfolio?.available_cash_nc)} / ₹${money(latest.portfolio?.available_cash_inr)}` },
                { label: "Pending exposure", value: money(latest.pending_orders?.notional_nc) },
                { label: "Cash weight", value: weightPct(latest.capital?.cash_weight) },
                { label: "Pending / available cash", value: latest.capital?.pending_to_available_cash_pct == null ? "—" : pct(latest.capital.pending_to_available_cash_pct) },
                { label: "Market regime", value: `${title(latest.regime?.primary || "Uncertain")} (${latest.regime?.confidence == null ? "uncertain" : num(latest.regime.confidence, 3).toFixed(3)})` },
                { label: "Composite score", value: "None — no arbitrary weights" },
                { label: "Evidence older than", value: `${numberText(latest.data_freshness?.age_hours, 2)} h` },
              ]} />
            </Card>
            <Card>
              <CardHeader title="Risk contributors" subtitle="Headline level is the maximum explained component." />
              <Table rows={arrayFrom(latest.risk_contributors)} columns={[
                { key: "kind", label: "Component", render: (row) => title(row.kind) },
                { key: "risk_level", label: "Level", render: (row) => <Badge tone={tone(row.risk_level)}>{title(row.risk_level)}</Badge> },
                { key: "metric", label: "Metric", align: "right", render: (row) => row.metric ?? "—" },
              ]} emptyMessage="No contributor is available." />
            </Card>
          </div>
        </>
      ) : null}

      {view === "positions" && (
        <Card>
          <CardHeader title="Position risk" subtitle="Size, volatility, drawdown, liquidity, forecast and NM-08 reference per holding." />
          {positions.error ? <ErrorBlock message={positions.error.message} onRetry={positions.refresh} /> : <Table rows={positionRows} columns={positionColumns()} emptyMessage="No positions in the latest assessment." />}
        </Card>
      )}

      {view === "concentration" && (
        <div className="grid-two">
          <Card>
            <CardHeader title="Concentration" subtitle="Instrument, sector, industry, HHI and effective holdings." />
            <Facts rows={[
              { label: "Risk level", value: <Badge tone={tone(latest.concentration?.risk_level)}>{title(latest.concentration?.risk_level)}</Badge> },
              { label: "HHI", value: numberText(latest.concentration?.hhi, 6) },
              { label: "Effective holdings", value: numberText(latest.concentration?.effective_holdings, 3) },
              { label: "Top one", value: weightPct(latest.concentration?.top_one_weight) },
              { label: "Top three", value: weightPct(latest.concentration?.top_three_weight) },
              { label: "Largest sector", value: `${latest.concentration?.largest_sector || "Unknown"} (${weightPct(latest.concentration?.largest_sector_weight)})` },
            ]} />
          </Card>
          <Card>
            <CardHeader title="Correlation" subtitle="Only timestamp-aligned observations are paired." />
            <Facts rows={[
              { label: "Status", value: <Badge tone={tone(latest.correlation?.status)}>{title(latest.correlation?.status)}</Badge> },
              { label: "Risk level", value: <Badge tone={tone(latest.correlation?.risk_level)}>{title(latest.correlation?.risk_level)}</Badge> },
              { label: "Valid pairs", value: latest.correlation?.valid_pair_count ?? 0 },
              { label: "High correlations", value: arrayFrom(latest.correlation?.highly_correlated).length },
              { label: "Alignment", value: latest.correlation?.alignment || "Exact timestamps" },
            ]} />
            <Table rows={arrayFrom(latest.correlation?.pairs)} columns={[
              { key: "instrument_a", label: "A" },
              { key: "instrument_b", label: "B" },
              { key: "correlation", label: "Correlation", align: "right", render: (row) => numberText(row.correlation, 4) },
              { key: "paired_observations", label: "Obs", align: "right" },
              { key: "change_state", label: "Change", render: (row) => title(row.change_state) },
            ]} emptyMessage="No pair has sufficient aligned observations." />
          </Card>
        </div>
      )}

      {view === "volatility" && (
        <div className="grid-two">
          <Card>
            <CardHeader title="Volatility and downside" subtitle="Historical estimates, not forecasts." />
            <Facts rows={[
              { label: "Annualised volatility", value: metricValue(latest.volatility?.annualized_volatility_pct) },
              { label: "Downside deviation", value: pct(latest.volatility?.downside_deviation_pct) },
              { label: "VaR", value: metricValue(latest.volatility?.var) },
              { label: "CVaR / expected shortfall", value: metricValue(latest.volatility?.cvar) },
              { label: "Observations", value: latest.volatility?.observations ?? 0 },
              { label: "Limitation", value: <small className="muted">VaR is a quantile loss, not the maximum possible loss.</small> },
            ]} />
          </Card>
          <Card>
            <CardHeader title="Drawdown" subtitle="Peak-to-trough from authoritative valuations." />
            <Facts rows={[
              { label: "Current", value: pct(latest.drawdown?.current_drawdown_pct) },
              { label: "Maximum", value: pct(latest.drawdown?.max_drawdown_pct) },
              { label: "Current (NC)", value: money(latest.drawdown?.current_drawdown_nc) },
              { label: "Maximum (NC)", value: money(latest.drawdown?.max_drawdown_nc) },
              { label: "Peak value", value: money(latest.drawdown?.peak_value_nc) },
              { label: "Episodes", value: arrayFrom(latest.drawdown?.episodes).length },
            ]} />
          </Card>
        </div>
      )}

      {view === "liquidity" && (
        <Card>
          <CardHeader title="Liquidity risk" subtitle="Position/ADV days with explicit participation assumptions; no fabricated depth." />
          <div className="metric-grid">
            <Metric label="Liquidity class" value={title(latest.liquidity?.liquidity_class)} tone={tone(latest.liquidity?.liquidity_class)} help="Weighted portfolio view" />
            <Metric label="Weighted days" value={numberText(latest.liquidity?.weighted_days_to_liquidate, 4)} help="At assumed participation" />
            <Metric label="Worst position" value={latest.liquidity?.worst_position?.instrument_id || "—"} tone={tone(latest.liquidity?.worst_position?.risk_level)} help={`${numberText(latest.liquidity?.worst_position?.days_to_liquidate, 4)} days`} />
            <Metric label="Bid-ask spread" value={latest.liquidity?.worst_position?.bid_ask_spread_bps == null ? "Unavailable" : `${num(latest.liquidity.worst_position.bid_ask_spread_bps, 2).toFixed(2)} bps`} help="Never fabricated" />
          </div>
          <Table rows={arrayFrom(latest.liquidity?.positions)} columns={[
            { key: "instrument_id", label: "Instrument", render: (row) => row.instrument_id || "—" },
            { key: "liquidity_class", label: "Class", render: (row) => <Badge tone={tone(row.liquidity_class)}>{title(row.liquidity_class)}</Badge> },
            { key: "position_to_adv", label: "Position / ADV", align: "right", render: (row) => numberText(row.position_to_adv, 6) },
            { key: "days_to_liquidate", label: "Days", align: "right", render: (row) => numberText(row.days_to_liquidate, 4) },
            { key: "estimated_impact_bps_range", label: "Impact range (bps)", align: "right", render: (row) => (row.estimated_impact_bps_range == null ? "—" : `${numberText(row.estimated_impact_bps_range.low, 2)}–${numberText(row.estimated_impact_bps_range.high, 2)}`) },
          ]} emptyMessage="No liquidity evidence is available." />
        </Card>
      )}

      {view === "scenarios" && (
        <>
          <div className="grid-two">
            <Card>
              <CardHeader title="New hypothetical scenario" subtitle="A scenario is an assumption, never a prediction or order." />
              <div className="field-grid">
                <Field label="Kind">
                  <SelectInput value={scenarioKind} onChange={(event) => setScenarioKind(event.target.value)}>
                    {["market_decline", "market_rally", "sector_decline", "sector_rally", "instrument_decline", "volatility_expansion", "liquidity_deterioration", "correlated_decline", "cash_depletion", "multiple_pending_orders", "combined"].map((item) => <option key={item} value={item}>{title(item)}</option>)}
                  </SelectInput>
                </Field>
                <Field label="Shock %"><TextInput type="number" value={scenarioShock} onChange={(event) => setScenarioShock(event.target.value)} /></Field>
                <Field label="Sector (optional)"><TextInput value={scenarioSector} onChange={(event) => setScenarioSector(event.target.value)} placeholder="Technology" /></Field>
                <Field label="Instrument (optional)"><TextInput value={scenarioInstrument} onChange={(event) => setScenarioInstrument(event.target.value)} placeholder="XNSE:INFY" /></Field>
              </div>
              <Button onClick={createAndRunScenario} disabled={!selected || !handles.enabled || Boolean(busy)}><ArrowRightLeft size={15} /> Create and run scenario</Button>
            </Card>
            <Card>
              <CardHeader title="Stress test" subtitle="Historical, hypothetical, volatility, correlation, liquidity or combined." />
              <div className="field-grid">
                <Field label="Mode">
                  <SelectInput value={stressMode} onChange={(event) => setStressMode(event.target.value)}>
                    {["historical", "hypothetical", "volatility", "correlation", "liquidity", "combined"].map((item) => <option key={item} value={item}>{title(item)}</option>)}
                  </SelectInput>
                </Field>
                <Field label="Shock %"><TextInput type="number" value={stressShock} onChange={(event) => setStressShock(event.target.value)} /></Field>
              </div>
              <Button onClick={runStress} disabled={!selected || !handles.enabled || Boolean(busy)}><BarChart3 size={15} /> Run stress test</Button>
            </Card>
          </div>
          <Card>
            <CardHeader title="Saved scenarios" subtitle="Bounded, reproducible hypothetical configurations." />
            {scenarios.error ? <ErrorBlock message={scenarios.error.message} onRetry={scenarios.refresh} /> : <Table rows={scenarioRows} columns={scenarioColumns()} emptyMessage="No scenario has been saved." />}
          </Card>
          <Card>
            <CardHeader title="Scenario runs" subtitle="Estimated impacts and provenance digests." />
            <Table rows={scenarioRunRows} columns={[
              { key: "scenario.kind", label: "Scenario", render: (row) => title(row.scenario?.kind) },
              { key: "status", label: "Status", render: (row) => <Badge tone={tone(row.status)}>{title(row.status)}</Badge> },
              { key: "estimated_change_nc", label: "Estimated change", align: "right", render: (row) => money(row.estimated_change_nc) },
              { key: "estimated_change_pct", label: "Change %", align: "right", render: (row) => pct(row.estimated_change_pct) },
              { key: "remaining_cash_nc", label: "Remaining cash", align: "right", render: (row) => money(row.remaining_cash_nc) },
              { key: "generated_at", label: "Run", render: (row) => dateTime(row.generated_at) },
            ]} emptyMessage="No scenario run has been recorded." />
          </Card>
          <Card>
            <CardHeader title="Stress results" subtitle="Separate from historical performance and never used as a forecast." />
            {stresses.error ? <ErrorBlock message={stresses.error.message} onRetry={stresses.refresh} /> : <Table rows={stressRows} columns={stressColumns()} emptyMessage="No stress test has been recorded." />}
          </Card>
        </>
      )}

      {view === "alerts" && (
        <>
          <Card>
            <CardHeader title="Early warnings" subtitle="Deduplicated by stable alert key with a configured cooldown." />
            {alerts.error ? <ErrorBlock message={alerts.error.message} onRetry={alerts.refresh} /> : <Table rows={alertRows} columns={alertColumns()} emptyMessage="No active warnings." />}
          </Card>
          <Card>
            <CardHeader title="Protective recommendations" subtitle="Advisory only; any action must pass NM-08 and the PI-21/PI-12 workflow." />
            <Table rows={recommendationRows} columns={recommendationColumns()} emptyMessage="No recommendations are recorded." />
          </Card>
          <Card>
            <CardHeader title="Risk history" subtitle="Immutable assessment records with status and warning counts." />
            <Table rows={arrayFrom(history.data, "items")} columns={[
              { key: "generated_at", label: "Recorded", render: (row) => dateTime(row.generated_at) },
              { key: "risk_level", label: "Risk", render: (row) => <Badge tone={tone(row.risk_level)}>{title(row.risk_level)}</Badge> },
              { key: "data_status", label: "Data", render: (row) => title(row.data_status) },
              { key: "warning_count", label: "Warnings", align: "right", render: (row) => row.warning_count ?? (row.warnings || []).length },
              { key: "trend", label: "Trend", render: (row) => title(row.trend) },
              { key: "assessment_id", label: "Assessment", render: (row) => <code>{String(row.assessment_id || "").slice(0, 16)}…</code> },
            ]} emptyMessage="No assessment history." />
          </Card>
        </>
      )}

      {latest.limitations?.length ? (
        <Banner tone="warn" title="Methodology limits">
          {latest.limitations.join(" ")}
        </Banner>
      ) : null}
      {policy.min_observations ? (
        <p className="muted">Validated configuration: at least {policy.min_observations} observations, {policy.max_correlation_pairs} correlation pairs, VaR confidence {policy.var_confidence}.</p>
      ) : null}
    </div>
  );
}