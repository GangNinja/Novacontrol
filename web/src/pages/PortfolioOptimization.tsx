import { useEffect, useMemo, useState } from "react";
import {
  Activity,
  AlertTriangle,
  BarChart3,
  FlaskConical,
  Layers3,
  Play,
  RefreshCw,
  Save,
  ShieldCheck,
  Sparkles,
} from "lucide-react";
import { apiPost } from "../api";
import { useApi, useDocumentTitle } from "../hooks";
import { arrayFrom, dateTime, num, signTone, title } from "../lib";
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

const HORIZONS = ["5m", "15m", "20m", "60m", "1d", "3d", "5d", "30d", "90d"];
const OBJECTIVES = [
  ["risk_adjusted", "Risk-adjusted return"],
  ["max_expected_return", "Maximum expected return"],
  ["min_variance", "Minimum variance"],
  ["max_sharpe", "Maximum Sharpe"],
  ["min_downside_risk", "Minimum downside risk"],
  ["diversification", "Diversification"],
  ["multi_objective", "Multi-objective"],
] as const;
const COVARIANCE_METHODS = [
  ["shrinkage", "Shrinkage"],
  ["sample", "Sample"],
  ["ewma", "EWMA"],
  ["market_model", "Market model (needs benchmark)"],
] as const;
const METHODS = [
  ["mean_variance", "Mean variance"],
  ["min_variance", "Minimum variance"],
  ["risk_parity", "Risk parity"],
  ["equal_weight", "Equal weight"],
  ["inverse_volatility", "Inverse volatility"],
  ["current", "Current book"],
] as const;

function pct(value: unknown, digits = 1): string {
  const number = num(value, NaN);
  return Number.isFinite(number) ? `${(number * 100).toFixed(digits)}%` : "—";
}

function money(value: unknown): string {
  const number = num(value, NaN);
  return Number.isFinite(number)
    ? `₹${number.toLocaleString("en-IN", { maximumFractionDigits: 0 })}`
    : "—";
}

function ratio(value: unknown): string {
  const number = num(value, NaN);
  return Number.isFinite(number) ? number.toFixed(2) : "—";
}

function decimalText(value: unknown, fallback: string): string {
  const number = num(value, NaN);
  return Number.isFinite(number) ? String(number) : fallback;
}

function Fact({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="key-value">
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

function tone(value: unknown): "good" | "warn" | "bad" | "neutral" | "info" {
  const status = String(value ?? "").toLowerCase();
  if (["completed", "healthy", "ok", "funded"].includes(status)) return "good";
  if (["completed_with_warnings", "insufficient_data", "cancelled"].includes(status)) return "warn";
  if (["failed", "solver_failed", "timed_out", "invalid_input", "infeasible_constraints", "unfunded"].includes(status)) return "bad";
  return "info";
}

export function PortfolioOptimizationPage() {
  useDocumentTitle("Portfolio Optimization");
  const toast = useToast();
  const portfolios = useApi<any>("/market/portfolios?limit=100");
  const status = useApi<any>("/market/optimization/status", { refreshMs: 30_000 });
  const runs = useApi<any>("/market/optimization/runs?limit=30", { refreshMs: 20_000 });

  const [portfolioId, setPortfolioId] = useState("");
  const [horizon, setHorizon] = useState("5d");
  const [instrumentText, setInstrumentText] = useState("");
  const [objective, setObjective] = useState("risk_adjusted");
  const [method, setMethod] = useState("mean_variance");
  const [maxInstrument, setMaxInstrument] = useState("0.18");
  const [maxSector, setMaxSector] = useState("0.30");
  const [cashReserve, setCashReserve] = useState("0.08");
  const [allowedExchanges, setAllowedExchanges] = useState("XNSE, BSE");
  const [benchmarkInstrument, setBenchmarkInstrument] = useState("");
  const [covarianceMethod, setCovarianceMethod] = useState("shrinkage");
  const [turnover, setTurnover] = useState("0.35");
  const [threshold, setThreshold] = useState("0.02");
  const [maxVolatility, setMaxVolatility] = useState("0.55");
  const [selectedRun, setSelectedRun] = useState("");
  const [busy, setBusy] = useState("");
  const [lastMessage, setLastMessage] = useState("");
  const [validation, setValidation] = useState<any>(null);

  const portfolioRows = arrayFrom(portfolios.data, "portfolios");
  const runRows = arrayFrom(runs.data, "runs");
  const run = useApi<any>(
    selectedRun ? `/market/optimization/runs/${encodeURIComponent(selectedRun)}` : null,
    { refreshMs: 20_000 },
  );
  const backtests = useApi<any>(
    selectedRun
      ? `/market/optimization/backtests?run_id=${encodeURIComponent(selectedRun)}&limit=10`
      : null,
  );

  useEffect(() => {
    if (!portfolioId && portfolioRows.length) {
      setPortfolioId(String(portfolioRows[0].portfolio_id));
    }
  }, [portfolioId, portfolioRows]);

  useEffect(() => {
    if (!selectedRun && runRows.length) {
      setSelectedRun(String(runRows[0].run_id));
    }
  }, [runRows, selectedRun]);

  const active = run.data?.run || runRows.find((row: any) => row.run_id === selectedRun) || null;
  const snapshot = active?.portfolio_snapshot || {};
  const allocation = arrayFrom(active, "allocation");
  const scenarios = arrayFrom(active, "scenarios");
  const rebalancingRows = active?.rebalancing?.intents || [];
  const diversification = active?.diversification || {};
  const risk = active?.risk || {};
  const covariance = active?.covariance || {};
  const warnings = Array.isArray(active?.warnings) ? active.warnings : [];
  const backtest = backtests.data?.backtests?.[0] || null;
  const explainability = active?.explainability || {};
  const explainRows = Array.isArray(explainability.positions) ? explainability.positions : [];
  const bindingConstraints = Array.isArray(explainability.binding_constraints)
    ? explainability.binding_constraints
    : [];

  // The selected run's own configuration backs the form, so the controls never
  // claim shrinkage / no benchmark while that run used market_model with a
  // benchmark. The stored config is the authoritative record; the run header
  // only carries the method.
  const runConfig = useApi<any>(
    active?.config_id
      ? `/market/optimization/configs/${encodeURIComponent(String(active.config_id))}`
      : null,
  );
  const runConfigData = runConfig.data?.config || null;
  const [hydratedRun, setHydratedRun] = useState("");

  useEffect(() => {
    if (!selectedRun) {
      // Clearing the selection re-arms hydration for the next pick of the same run.
      if (hydratedRun) setHydratedRun("");
      return;
    }
    if (hydratedRun === selectedRun || !runConfigData) return;
    // Once per selected run: the 20s refresh must never overwrite edits in
    // progress with the stored values.
    setMethod(String(runConfigData.method || "mean_variance"));
    setCovarianceMethod(String(runConfigData.covariance_method || "shrinkage"));
    setBenchmarkInstrument(String(runConfigData.benchmark_instrument_id || ""));
    setMaxInstrument(decimalText(runConfigData.max_instrument_weight, "0.18"));
    setMaxSector(decimalText(runConfigData.max_sector_weight, "0.30"));
    setCashReserve(decimalText(runConfigData.min_cash_reserve, "0.08"));
    setTurnover(decimalText(runConfigData.max_turnover, "0.35"));
    setThreshold(decimalText(runConfigData.rebalance_threshold, "0.02"));
    setMaxVolatility(decimalText(runConfigData.max_volatility, "0.55"));
    setHydratedRun(selectedRun);
  }, [selectedRun, hydratedRun, runConfigData]);

  const instruments = useMemo(
    () =>
      instrumentText
        .split(/[\s,]+/)
        .map((item) => item.trim())
        .filter(Boolean),
    [instrumentText],
  );

  const payload = () => ({
    portfolio_id: portfolioId.trim(),
    horizon,
    instrument_ids: instruments,
    objective,
    method,
    max_instrument_weight: Number(maxInstrument),
    max_sector_weight: Number(maxSector),
    min_cash_reserve: Number(cashReserve),
    allowed_exchanges: allowedExchanges
      .split(/[\s,]+/)
      .map((item) => item.trim().toUpperCase())
      .filter(Boolean),
    max_turnover: Number(turnover),
    rebalance_threshold: Number(threshold),
    max_volatility: Number(maxVolatility),
    liquidity_policy: "warn",
    covariance_method: covarianceMethod,
    benchmark_instrument_id: benchmarkInstrument.trim().toUpperCase(),
  });

  const execute = async (key: string, action: () => Promise<void>) => {
    setBusy(key);
    try {
      await action();
    } catch (error: any) {
      toast.push(error?.detail || error?.message || "The optimization request was refused.", "error");
    } finally {
      setBusy("");
    }
  };

  const validate = () =>
    execute("validate", async () => {
      if (!portfolioId.trim()) throw new Error("Select a portfolio first.");
      if (!instruments.length) throw new Error("Enter at least one exchange-qualified instrument.");
      const result = await apiPost<any>("/market/optimization/validate", payload(), undefined, {
        timeoutMs: 60_000,
      });
      setValidation(result);
      setLastMessage(
        result.valid
          ? `Inputs validated: ${result.eligible?.length || 0} eligible instruments, ${result.aligned_observations || 0} aligned observations.`
          : `Inputs are not runnable: ${result.detail || result.status}.`,
      );
      toast.push(result.valid ? "Inputs validated against authoritative read-only state." : "Inputs are not runnable yet.", result.valid ? "success" : "info");
    });

  const saveConfig = () =>
    execute("save", async () => {
      if (!portfolioId.trim()) throw new Error("Select a portfolio first.");
      const result = await apiPost<any>("/market/optimization/configs", payload());
      setLastMessage(`Configuration ${result.config?.config_id || ""} saved with a deterministic version.`);
      toast.push("Optimization configuration saved.", "success");
    });

  const runOptimization = () =>
    execute("run", async () => {
      if (!portfolioId.trim()) throw new Error("Select a portfolio first.");
      if (!instruments.length) throw new Error("Enter at least one exchange-qualified instrument.");
      const result = await apiPost<any>("/market/optimization/runs", payload(), undefined, {
        timeoutMs: 120_000,
      });
      const next = result.run;
      if (next?.run_id) setSelectedRun(String(next.run_id));
      setLastMessage(
        `Run ${next?.status || "unknown"}${next?.error ? `: ${next.error}` : ""}. No portfolio, cash, ledger or order state was changed.`,
      );
      toast.push(
        ["completed", "completed_with_warnings"].includes(next?.status)
          ? `Optimization ${next.status.replaceAll("_", " ")}; advisory weights only.`
          : `Optimization ended with ${next?.status || "an error"}.`,
        ["completed", "completed_with_warnings"].includes(next?.status) ? "success" : "error",
      );
      runs.refresh();
      status.refresh();
    });

  const addStress = () =>
    execute("scenario", async () => {
      if (!selectedRun) throw new Error("Select an optimization run first.");
      const shocks: Record<string, number> = {};
      for (const row of allocation) shocks[String(row.instrument_id)] = -0.1;
      await apiPost<any>(
        `/market/optimization/runs/${encodeURIComponent(selectedRun)}/scenarios`,
        { scenarios: [{ name: "all_positions_down_10pct", description: "operator stress: every target position falls 10%", shocks }] },
      );
      toast.push("Hypothetical stress added; it is not a forecast.", "success");
      run.refresh();
    });

  const runBacktest = () =>
    execute("backtest", async () => {
      if (!selectedRun) throw new Error("Select an optimization run first.");
      await apiPost<any>(
        `/market/optimization/runs/${encodeURIComponent(selectedRun)}/backtests`,
        { rebalance_frequency: 5 },
        undefined,
        { timeoutMs: 120_000 },
      );
      toast.push("Strategy comparison completed on trailing point-in-time history.", "success");
      backtests.refresh();
      run.refresh();
    });

  const allocationColumns: Column<any>[] = [
    { key: "instrument_id", label: "Instrument", sortable: true, render: (row) => <strong className="mono">{row.instrument_id}</strong> },
    { key: "sector", label: "Sector", render: (row) => title(row.sector) },
    { key: "current_weight", label: "Current", render: (row) => pct(row.current_weight) },
    { key: "target_weight", label: "Recommended", render: (row) => <strong>{pct(row.target_weight)}</strong> },
    { key: "weight_difference", label: "Change", render: (row) => <span className={`delta ${signTone(row.weight_difference)}`}>{pct(row.weight_difference)}</span> },
    { key: "target_capital", label: "Capital", render: (row) => money(row.target_capital) },
    { key: "expected_return", label: "Exp. return", render: (row) => pct(row.expected_return, 2) },
    { key: "risk_contribution", label: "Risk contribution", render: (row) => pct(row.risk_contribution) },
    { key: "liquidity_status", label: "Liquidity", render: (row) => <StatusBadge value={row.liquidity_status || "unknown"} /> },
  ];
  const explainColumns: Column<any>[] = [
    { key: "instrument_id", label: "Instrument", render: (row) => <span className="mono">{row.instrument_id}</span> },
    { key: "target_weight", label: "Target", render: (row) => pct(row.target_weight) },
    { key: "expected_return", label: "Exp. return", render: (row) => pct(row.expected_return, 2) },
    { key: "risk_contribution", label: "Risk contribution", render: (row) => pct(row.risk_contribution) },
    { key: "summary", label: "Why", render: (row) => <span className="muted">{row.summary}</span> },
  ];
  const scenarioColumns: Column<any>[] = [
    { key: "name", label: "Scenario", render: (row) => <div><strong>{title(row.name)}</strong><div className="muted">{row.description}</div></div> },
    { key: "kind", label: "Kind", render: (row) => <StatusBadge value={row.kind} /> },
    { key: "portfolio_value_change", label: "Value change", render: (row) => <span className={`delta ${signTone(row.portfolio_value_change)}`}>{money(row.portfolio_value_change)}</span> },
    { key: "portfolio_value_change_pct", label: "Portfolio impact", render: (row) => pct(row.portfolio_value_change_pct, 2) },
  ];
  const rebalancingColumns: Column<any>[] = [
    { key: "instrument_id", label: "Instrument", render: (row) => <span className="mono">{row.instrument_id || "CASH"}</span> },
    { key: "action", label: "Recommendation", render: (row) => <StatusBadge value={row.action} /> },
    { key: "current_weight", label: "Current", render: (row) => pct(row.current_weight) },
    { key: "target_weight", label: "Target", render: (row) => pct(row.target_weight) },
    { key: "weight_change", label: "Change", render: (row) => pct(row.weight_change) },
    { key: "notional", label: "Notional", render: (row) => money(row.notional) },
    { key: "estimated_transaction_cost", label: "Est. cost", render: (row) => money(row.estimated_transaction_cost) },
  ];
  const backtestColumns: Column<any>[] = [
    { key: "method", label: "Method", render: (row) => title(row.method) },
    { key: "status", label: "Status", render: (row) => <StatusBadge value={row.status} /> },
    { key: "total_return", label: "Total return", render: (row) => pct(row.total_return, 2) },
    { key: "max_drawdown", label: "Max drawdown", render: (row) => pct(row.max_drawdown, 2) },
    { key: "sharpe_ratio", label: "Sharpe", render: (row) => ratio(row.sharpe_ratio) },
    { key: "turnover", label: "Turnover", render: (row) => ratio(row.turnover) },
    { key: "trading_costs", label: "Trading costs", render: (row) => money(row.trading_costs) },
  ];
  const correlation = Array.isArray(covariance.correlation) ? covariance.correlation : [];
  const correlationInstruments = Array.isArray(covariance.instruments) ? covariance.instruments : [];

  return (
    <div className="page">
      <PageHeader
        eyebrow="PI-18 · portfolio intelligence"
        title="Portfolio Optimization"
        description="Constraint-aware capital allocation over the authoritative portfolio, PI-02 market history, PI-04 regime evidence and PI-05–PI-10 forecasts. Every output is an isolated recommendation."
        actions={
          <>
            <Button variant="ghost" icon={<RefreshCw size={14} />} onClick={() => { status.refresh(); runs.refresh(); run.refresh(); backtests.refresh(); }}>
              Refresh
            </Button>
            <Button variant="primary" icon={<Play size={15} />} loading={busy === "run"} onClick={runOptimization}>
              Run optimization
            </Button>
          </>
        }
      />

      <Banner tone="warn" title="Advisory research only">
        Target weights are recommendations, not orders. NM-07 portfolio accounting, NM-08 risk approval and NM-10 paper execution remain authoritative; this workspace cannot move cash, positions, ledgers or orders.
      </Banner>

      <div className="metric-grid">
        <Metric label="Portfolio value" value={money(snapshot.total_value)} help="NM-07 read-only valuation" icon={<BarChart3 size={16} />} />
        <Metric label="Available cash" value={money(snapshot.cash)} help="Before pending-order reserve" icon={<Layers3 size={16} />} />
        <Metric label="Invested capital" value={money(snapshot.invested)} help="Authoritative positions" />
        <Metric label="Unrealized P&L" value={money(snapshot.unrealized_pnl)} tone={snapshot.unrealized_pnl == null ? "neutral" : num(snapshot.unrealized_pnl) >= 0 ? "good" : "bad"} />
        <Metric label="Realized P&L" value={money(snapshot.realized_pnl)} tone={snapshot.realized_pnl == null ? "neutral" : num(snapshot.realized_pnl) >= 0 ? "good" : "bad"} />
        <Metric label="In optimization store" value={status.data?.runs ?? "—"} help="Isolated po_* research records" />
      </div>

      <Card>
        <CardHeader
          title="Optimization configuration"
          subtitle="Horizon, objective and every constraint are validated server-side before execution. Cash is a legitimate allocation."
          action={<span className="badge">{horizon} · {title(objective)}</span>}
        />
        <div className="form-grid">
          <Field label="Portfolio">
            <SelectInput value={portfolioId} onChange={(event) => setPortfolioId(event.target.value)}>
              <option value="">Select a portfolio</option>
              {portfolioRows.map((row: any) => (
                <option key={row.portfolio_id} value={row.portfolio_id}>{row.name || row.portfolio_id} · {row.horizon || ""}</option>
              ))}
            </SelectInput>
          </Field>
          <Field label="Investment horizon" hint="One explicit horizon per run; no mixed-horizon objective.">
            <SelectInput value={horizon} onChange={(event) => setHorizon(event.target.value)}>
              {HORIZONS.map((item) => <option key={item} value={item}>{item}</option>)}
            </SelectInput>
          </Field>
          <Field label="Objective">
            <SelectInput value={objective} onChange={(event) => setObjective(event.target.value)}>
              {OBJECTIVES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
            </SelectInput>
          </Field>
          <Field label="Allocation method">
            <SelectInput value={method} onChange={(event) => setMethod(event.target.value)}>
              {METHODS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
            </SelectInput>
          </Field>
          <Field label="Eligible instruments" hint="Exchange-qualified MIC:SYMBOL, comma separated. Only existing market-data instruments are usable.">
            <TextInput value={instrumentText} onChange={(event) => setInstrumentText(event.target.value)} placeholder="XNSE:ALPHA, XNSE:BETA" />
          </Field>
          <Field label="Allowed exchanges" hint="MIC codes the run may use; BSE-listed bars are reported as BSE, not XBOM.">
            <TextInput value={allowedExchanges} onChange={(event) => setAllowedExchanges(event.target.value)} placeholder="XNSE, BSE" />
          </Field>
          <Field label="Covariance method" hint="Market model needs a benchmark series; the others estimate from the aligned returns alone.">
            <SelectInput value={covarianceMethod} onChange={(event) => setCovarianceMethod(event.target.value)}>
              {COVARIANCE_METHODS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
            </SelectInput>
          </Field>
          <Field label="Benchmark instrument" hint="Read-only PI-02 series (MIC:SYMBOL) compared in backtests; never allocated, and it must not be one of the instruments.">
            <TextInput value={benchmarkInstrument} onChange={(event) => setBenchmarkInstrument(event.target.value)} placeholder="XNSE:NIFTY50" />
          </Field>
          <Field label="Maximum instrument weight"><TextInput value={maxInstrument} onChange={(event) => setMaxInstrument(event.target.value)} inputMode="decimal" /></Field>
          <Field label="Maximum sector weight"><TextInput value={maxSector} onChange={(event) => setMaxSector(event.target.value)} inputMode="decimal" /></Field>
          <Field label="Minimum cash reserve"><TextInput value={cashReserve} onChange={(event) => setCashReserve(event.target.value)} inputMode="decimal" /></Field>
          <Field label="Maximum turnover"><TextInput value={turnover} onChange={(event) => setTurnover(event.target.value)} inputMode="decimal" /></Field>
          <Field label="Rebalance threshold"><TextInput value={threshold} onChange={(event) => setThreshold(event.target.value)} inputMode="decimal" /></Field>
          <Field label="Maximum annualized volatility"><TextInput value={maxVolatility} onChange={(event) => setMaxVolatility(event.target.value)} inputMode="decimal" /></Field>
        </div>
        <div className="row-actions">
          <Button variant="secondary" icon={<ShieldCheck size={14} />} loading={busy === "validate"} onClick={validate}>Validate inputs</Button>
          <Button variant="ghost" icon={<Save size={14} />} loading={busy === "save"} onClick={saveConfig}>Save configuration</Button>
          <Button variant="primary" icon={<Play size={14} />} loading={busy === "run"} onClick={runOptimization}>Run optimization</Button>
        </div>
        {lastMessage && <p className="muted">{lastMessage}</p>}
        {validation && (
          <div className="fact-list">
            <Fact label="Validation status" value={<StatusBadge value={validation.status} />} />
            <Fact label="Eligible instruments" value={validation.eligible?.length ?? 0} />
            <Fact label="Aligned observations" value={validation.aligned_observations ?? 0} />
            {validation.benchmark && (
              <>
                <Fact label="Market benchmark" value={validation.benchmark.instrument_id ? <span className="mono">{validation.benchmark.instrument_id}</span> : "Not configured"} />
                <Fact
                  label="Benchmark coverage"
                  value={validation.benchmark.used
                    ? `${validation.benchmark.aligned_observations} of ${validation.benchmark.analysis_observations} aligned observations`
                    : validation.benchmark.reason}
                />
              </>
            )}
            {Array.isArray(validation.excluded) && validation.excluded.length > 0 && (
              <p className="muted">{validation.excluded.length} instrument(s) excluded with explicit reasons; see the run record.</p>
            )}
          </div>
        )}
      </Card>

      <Card>
        <CardHeader
          title="Optimization history"
          subtitle="Every run is append-only and reproducible from its recorded portfolio, market, forecast, covariance and configuration versions."
        />
        <div className="form-grid">
          <Field label="Optimization run" hint="Append-only history; selecting a run loads its stored outputs.">
            <SelectInput value={selectedRun} onChange={(event) => setSelectedRun(event.target.value)}><option value="">Select a run</option>{runRows.map((row: any) => <option key={row.run_id} value={row.run_id}>{row.run_id} · {row.horizon} · {row.status}</option>)}</SelectInput>
          </Field>
        </div>
        {run.error && <ErrorBlock message={run.error.detail} onRetry={run.refresh} />}
        {active ? (
          <div className="fact-list">
            <div className="row-actions fact-list-full">
              <StatusBadge value={active.status} />
              <span className="muted mono">{active.run_id}</span>
              <span className="muted">{dateTime(active.created_at)}</span>
              <span className="badge">{active.method} · {active.objective}</span>
              <span className="badge">{active.horizon}</span>
            </div>
            <Fact label="Portfolio snapshot version" value={<span className="mono">{active.portfolio_snapshot_version || "—"}</span>} />
            <Fact label="Market-data version" value={<span className="mono">{active.market_data_version || "—"}</span>} />
            <Fact label="Forecast version" value={<span className="mono">{active.forecast_version || "—"}</span>} />
            <Fact label="Solver" value={<span className="mono">{active.solver?.name} · {active.solver?.version}</span>} />
            <Fact label="Financial mutation" value={active.financial_mutation === false ? "None" : "Unexpected"} />
            {active.error && <div className="fact-list-full"><Banner tone="bad" title="Run error">{active.error}</Banner></div>}
            {warnings.length > 0 && <div className="fact-list-full"><Banner tone="warn" title={`${warnings.length} run warning(s)`}>{warnings.join(" · ")}</Banner></div>}
          </div>
        ) : (
          <p className="muted">No optimization run selected. Configure a portfolio and run a constrained optimization.</p>
        )}
      </Card>

      {active && (
        <>
          <Card>
            <CardHeader
              title="Recommended allocation"
              subtitle="Current versus target weights, capital, expected return and risk contribution. Cash remains unallocated when constraints or forecasts do not justify deployment."
              action={<div className="row-actions"><span className="badge">Cash {pct(active.cash_reserve)} · {money(active.cash_value)}</span></div>}
            />
            <DataTable columns={allocationColumns} rows={allocation} rowKey={(row) => String(row.instrument_id)} empty="No target allocation was produced." />
          </Card>

          <div className="dashboard-grid">
            <Card>
              <CardHeader title="Diversification" subtitle="Concentration is measured on invested weights, not on the raw number of holdings." />
              <div className="metric-grid">
                <Metric label="Holdings" value={diversification.holdings ?? 0} />
                <Metric label="Effective holdings" value={ratio(diversification.effective_holdings)} />
                <Metric label="HHI" value={ratio(diversification.hhi)} />
                <Metric label="Average correlation" value={ratio(diversification.average_correlation)} tone={diversification.average_correlation == null ? "neutral" : num(diversification.average_correlation) > 0.65 ? "warn" : "neutral"} />
                <Metric label="Diversification ratio" value={ratio(diversification.diversification_ratio)} />
                <Metric label="Effective sectors" value={ratio(diversification.effective_sectors)} />
              </div>
              {Array.isArray(diversification.warnings) && diversification.warnings.length > 0 && (
                <Banner tone="warn" title="Diversification warnings">{diversification.warnings.join(" · ")}</Banner>
              )}
              {Object.entries(diversification.sector_weights || {}).map(([sector, weight]) => (
                <Fact key={sector} label={title(sector)} value={pct(weight)} />
              ))}
            </Card>

            <Card>
              <CardHeader title="Risk analysis" subtitle="Research estimates only. NM-08 remains the sole operational risk authority." />
              <div className="metric-grid">
                <Metric label="Annualized volatility" value={pct(risk.annualized_volatility, 2)} />
                <Metric label="Sharpe" value={ratio(risk.sharpe_ratio)} />
                <Metric label="Sortino" value={ratio(risk.sortino_ratio)} />
                <Metric label="Calmar" value={ratio(risk.calmar_ratio)} />
                <Metric label="Max drawdown" value={pct(risk.max_drawdown, 2)} tone="warn" />
                <Metric label="VaR 95" value={pct(risk.var_95, 2)} />
                <Metric label="CVaR 95" value={pct(risk.cvar_95, 2)} />
                <Metric label="Downside deviation" value={pct(risk.downside_deviation, 2)} />
              </div>
              <p className="muted">{Array.isArray(risk.assumptions) ? risk.assumptions.join(" · ") : "Statistical estimates do not guarantee protection against future losses."}</p>
              <Banner tone="info" title="Authority preserved">Any operational change still requires a new NM-08 assessment and PI-12 authorization.</Banner>
            </Card>
          </div>

          <Card>
            <CardHeader title="Why this allocation" subtitle="The objective, binding limits and per-position evidence behind the recommendation." />
            <div className="fact-list">
              <Fact label="Objective" value={title(explainability.objective || active.objective)} />
              <Fact label="Method" value={title(explainability.method || active.method)} />
              <Fact label="Invested / cash" value={`${pct(explainability.invested_weight)} / ${pct(explainability.cash_weight)}`} />
              <Fact label="Rebalance action" value={<StatusBadge value={explainability.rebalance_action || "no_rebalancing_required"} />} />
              <Fact label="Covariance method" value={title(covariance.method || "—")} />
              <Fact
                label="Benchmark series"
                value={
                  active.benchmark?.used
                    ? `${active.benchmark.instrument_id} · ${active.benchmark.observations} obs`
                    : active.benchmark?.instrument_id
                      ? `${active.benchmark.instrument_id} · not used`
                      : "None configured"
                }
              />
            </div>
            {bindingConstraints.length > 0 && (
              <Banner tone="info" title="Binding constraints">{bindingConstraints.join(" · ")}</Banner>
            )}
            <DataTable columns={explainColumns} rows={explainRows} rowKey={(row) => String(row.instrument_id)} empty="No allocated position needs an explanation." />
            <p className="muted">{explainability.decision}</p>
          </Card>

          <Card>
            <CardHeader title="Correlation matrix" subtitle="Pairwise correlation of aligned historical returns; insufficient common history is never fabricated." />
            {correlation.length ? (
              <div className="table-wrap">
                <table>
                  <thead><tr><th>Instrument</th>{correlationInstruments.map((item: string) => <th key={item}>{item}</th>)}</tr></thead>
                  <tbody>
                    {correlation.map((row: number[], index: number) => (
                      <tr key={correlationInstruments[index]}>
                        <td><strong>{correlationInstruments[index]}</strong></td>
                        {row.map((value, column) => <td key={`${index}-${column}`} className={value > 0.65 && index !== column ? "badge badge-warn" : ""}>{value.toFixed(2)}</td>)}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : <p className="muted">No correlation matrix is available for this run.</p>}
          </Card>

          <Card>
            <CardHeader
              title="Scenario analysis"
              subtitle="Historical replay and hypothetical stress are labelled separately. Hypothetical outcomes are not forecasts."
              action={<Button variant="ghost" icon={<AlertTriangle size={14} />} loading={busy === "scenario"} onClick={addStress}>Add −10% stress</Button>}
            />
            <DataTable columns={scenarioColumns} rows={scenarios} rowKey={(row) => String(row.scenario_id)} empty="No scenario results are stored for this run." />
          </Card>

          <Card>
            <CardHeader
              title="Rebalancing recommendations"
              subtitle={`Threshold ${pct(active.rebalancing?.thresholds?.rebalance_threshold)} · estimated cost ${money(active.rebalancing?.estimated_transaction_costs)}. Recommendations never execute automatically.`}
              action={<StatusBadge value={active.rebalancing?.action || "no_rebalancing_required"} />}
            />
            <DataTable columns={rebalancingColumns} rows={rebalancingRows} rowKey={(row) => `${row.instrument_id}-${row.action}`} empty="No rebalancing actions exceed the configured thresholds." />
            {active.rebalancing?.funding && (
              <div className="fact-list">
                <Fact label="Funding status" value={<StatusBadge value={active.rebalancing.funding.status || "unknown"} />} />
                <Fact label="Available cash" value={money(active.rebalancing.funding.available_cash)} />
                <Fact label="Buy notional" value={money(active.rebalancing.funding.buy_notional)} />
                <Fact label="Sell notional" value={money(active.rebalancing.funding.sell_notional)} />
                <Fact label="Net cash required" value={money(active.rebalancing.funding.net_cash_required)} />
                <Fact label="Pending buy reserve" value={money(active.rebalancing.funding.pending_buy_reserve)} />
                <Fact label="Funding gap" value={money(active.rebalancing.funding.funding_gap)} />
              </div>
            )}
            {active.rebalancing?.expected_improvement !== undefined && (
              <p className="muted">
                Expected improvement estimate: {pct(active.rebalancing.expected_improvement, 3)}. Turnover {pct(active.rebalancing?.estimated_turnover)} of portfolio value.
                {active.rebalancing?.risk_change != null && ` Risk change ${pct(active.rebalancing.risk_change, 3)}.`}
                {active.rebalancing?.funding?.status === "unfunded" && ` Funding gap ${money(active.rebalancing.funding.funding_gap)} exceeds available cash.`}
              </p>
            )}
          </Card>

          <Card>
            <CardHeader
              title="Strategy comparison"
              subtitle="Identical periods, universe, capital, costs, slippage and rebalancing frequency. Trailing history only; performance is not a guarantee."
              action={<Button variant="secondary" icon={<FlaskConical size={14} />} loading={busy === "backtest"} onClick={runBacktest}>Run comparison</Button>}
            />
            {backtest ? (
              <>
                {backtest.benchmark && (
                  <div className="fact-list">
                    <Fact
                      label="Benchmark"
                      value={
                        backtest.benchmark.kind === "market_series"
                          ? `${backtest.benchmark.instrument_id} · market series`
                          : `${title(backtest.benchmark.instrument_id)} · strategy`
                      }
                    />
                    <Fact label="Benchmark source" value={backtest.benchmark.source || "—"} />
                    <Fact label="Benchmark total return" value={pct(backtest.benchmark.metrics?.total_return, 2)} />
                    <Fact label="Benchmark max drawdown" value={pct(backtest.benchmark.metrics?.max_drawdown, 2)} />
                    <Fact label="Benchmark Sharpe" value={ratio(backtest.benchmark.metrics?.sharpe_ratio)} />
                    <Fact label="Benchmark observations" value={backtest.benchmark.observations ?? "—"} />
                  </div>
                )}
                <DataTable columns={backtestColumns} rows={backtest.method_summary || []} rowKey={(row) => String(row.method)} empty="The backtest returned no strategies." />
                {Array.isArray(backtest.assumptions) && <p className="muted">{backtest.assumptions.join(" · ")}</p>}
                {backtest.look_ahead_controls && (
                  <Fact label="Look-ahead controls" value={backtest.look_ahead_controls.trailing_window_only ? "Trailing windows only" : "Not verified"} />
                )}
              </>
            ) : <p className="muted">No strategy comparison has been run for this optimization.</p>}
          </Card>

          <Card>
            <CardHeader title="Handoff and provenance" subtitle="Structured advisory contract for the existing intelligence and execution workflows." />
            <div className="dashboard-grid">
              <div>
                <Fact label="Advisory only" value={active.handoff?.advisory_only === true ? "Yes" : "No"} />
                <Fact label="Production authority" value={active.handoff?.production_authority === true ? "Yes" : "No"} />
                <Fact label="Order submitted" value={active.handoff?.order_submitted === true ? "Yes" : "No"} />
                <Fact label="Requires NM-08 reassessment" value={active.handoff?.requires_nm08_reassessment === true ? "Yes" : "No"} />
                <Fact label="Requires PI-12 orchestration" value={active.handoff?.requires_pi12_orchestration === true ? "Yes" : "No"} />
              </div>
              <div>
                <Fact label="Currency" value="1 NC = ₹1 · INR display" />
                <Fact label="NM-08 authority" value={active.authority?.risk || "NM-08"} />
                <Fact label="NM-10 authority" value={active.authority?.execution || "NM-10"} />
                <Fact label="Optimizer can mutate portfolio" value={active.authority?.optimization_can_mutate_portfolio === true ? "Yes" : "No"} />
              </div>
            </div>
            <p className="muted">{Array.isArray(active.notes) ? active.notes.join(" · ") : "Recommendations only; authoritative services remain in control."}</p>
          </Card>
        </>
      )}

      <Card>
        <CardHeader title="Research scope and limitations" subtitle="What this workspace does and does not claim." />
        <div className="stack-sm muted">
          <p><Sparkles size={13} /> Optimization is a mathematical allocation under stated constraints, not a promise of profit.</p>
          <p><Activity size={13} /> Covariance and scenarios use aligned historical observations; regime shifts and unmodelled risks can invalidate them.</p>
          <p><ShieldCheck size={13} /> Daily volume is an estimated liquidity proxy, not confirmed executable capacity.</p>
        </div>
      </Card>
    </div>
  );
}
