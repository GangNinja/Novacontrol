import { useEffect, useMemo, useState } from "react";
import {
  BarChart3,
  FlaskConical,
  Play,
  RefreshCw,
  ShieldCheck,
  Target,
} from "lucide-react";
import { apiPost } from "../api";
import { useApi, useDocumentTitle, navigate } from "../hooks";
import {
  arrayFrom,
  dateTime,
  num,
  pct,
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
  DataTable,
  EmptyState,
  ErrorBlock,
  Field,
  LoadingBlock,
  Metric,
  PageHeader,
  SelectInput,
  StatusBadge,
  TextInput,
  useToast,
} from "../components/ui";
import { LineChart } from "../components/charts";

const HORIZONS = ["5m", "15m", "20m", "1h", "1d", "3d", "5d", "30d", "90d"];
const SOURCES = [
  { value: "lifecycle", label: "PI-09 lifecycle outcomes" },
  { value: "nm06", label: "NM-06 evaluated forecasts" },
  { value: "both", label: "Both stores (model comparison)" },
];
const NM06_HORIZON: Record<string, string> = {
  "5m": "next_session",
  "15m": "next_session",
  "20m": "next_session",
  "1h": "next_session",
  "1d": "next_session",
  "3d": "few_sessions",
  "5d": "one_week",
  "30d": "one_month",
  "90d": "three_months",
};
const FORECAST_HOLDING_DAYS: Record<string, number> = {
  next_session: 1,
  few_sessions: 3,
  one_week: 5,
  one_month: 30,
  three_months: 90,
};

function metric(value: any, scale = 1, digits = 4): string {
  if (value === null || value === undefined || value === "") return "—";
  const number = num(value, NaN);
  return Number.isFinite(number) ? (number * scale).toFixed(digits) : "—";
}

function pctMetric(value: any): string {
  if (value === null || value === undefined || value === "") return "—";
  return pct(num(value, 0) * 100);
}

function todayMinus(months: number): string {
  const date = new Date();
  date.setMonth(date.getMonth() - months);
  return date.toISOString().slice(0, 10);
}

export function PredictionEvaluationPage() {
  useDocumentTitle("Prediction Evaluation");
  const toast = useToast();
  const status = useApi<any>("/market/evaluations/status");
  const contract = useApi<any>("/market/evaluations/contracts");
  const evaluations = useApi<any>("/market/evaluations?limit=50");
  const instruments = useApi<any>("/market/instruments?limit=200");
  const policies = useApi<any>("/market/risk/policies");
  const backtests = useApi<any>("/market/backtests?limit=50");
  const [source, setSource] = useState("both");
  const [instrument, setInstrument] = useState("");
  const [modelFilter, setModelFilter] = useState("");
  const [horizon, setHorizon] = useState("");
  const [start, setStart] = useState(todayMinus(12));
  const [end, setEnd] = useState(new Date().toISOString().slice(0, 10));
  const [threshold, setThreshold] = useState("0.005");
  const [confidence, setConfidence] = useState("0.80");
  const [minSamples, setMinSamples] = useState("20");
  const [folds, setFolds] = useState("0");
  const [running, setRunning] = useState(false);
  const [selectedId, setSelectedId] = useState("");
  const [backtestId, setBacktestId] = useState("");
  const [capital, setCapital] = useState("100000");
  const [benchmark, setBenchmark] = useState("");
  const [brokerageBps, setBrokerageBps] = useState("3");
  const [spreadBps, setSpreadBps] = useState("8");
  const [impactBps, setImpactBps] = useState("12");
  const [creatingReplay, setCreatingReplay] = useState(false);

  const rows = arrayFrom(evaluations.data, "evaluations");
  const current = rows.find((row) => row.evaluation_id === selectedId) || rows[0];
  const detail = useApi<any>(current ? `/market/evaluations/${encodeURIComponent(current.evaluation_id)}` : null);
  const report = detail.data || {};
  const metrics = report.metrics || {};
  const price = metrics.price || {};
  const direction = metrics.direction || {};
  const probabilities = metrics.probabilities || {};
  const intervals = metrics.intervals || {};
  const regimeRows = arrayFrom(metrics, "regimes");
  const modelRows = arrayFrom(metrics.models, "models");
  const scorecardRows = arrayFrom(report.model_evidence, "scorecards");
  const lifecycleRows = arrayFrom(metrics.lifecycle, "by_stage");
  const backtestRows = arrayFrom(backtests.data, "backtests");
  const selectedBacktest = backtestRows.find((row) => row.backtest_id === backtestId) || backtestRows[0];
  const backtestDetail = useApi<any>(selectedBacktest ? `/market/backtests/${encodeURIComponent(selectedBacktest.backtest_id)}` : null);
  const backtestResults = useApi<any>(selectedBacktest ? `/market/backtests/${encodeURIComponent(selectedBacktest.backtest_id)}/results` : null);
  const backtestTrades = useApi<any>(selectedBacktest ? `/market/backtests/${encodeURIComponent(selectedBacktest.backtest_id)}/trades` : null);
  const backtestMetrics = backtestResults.data?.metrics || backtestDetail.data?.metrics || {};
  const benchmarkResult = backtestResults.data?.benchmark || backtestDetail.data?.benchmark || {};
  const policyRows = arrayFrom(policies.data, "policies");
  const selectedPolicy = policyRows[0];

  const curve = useMemo(() => {
    const rowsIn = arrayFrom(backtestResults.data, "equity_curve", "curve");
    const fallback = arrayFrom(backtestDetail.data, "equity");
    return (rowsIn.length ? rowsIn : fallback)
      .map((row) => ({
        label: shortDate(row.at || row.timestamp || row.date),
        value: num(row.equity ?? row.value ?? row.total_value, NaN),
      }))
      .filter((row) => Number.isFinite(row.value));
  }, [backtestResults.data, backtestDetail.data]);

  useEffect(() => {
    if (!selectedId && rows.length) setSelectedId(rows[0].evaluation_id);
  }, [rows, selectedId]);

  useEffect(() => {
    if (!backtestId && backtestRows.length) setBacktestId(backtestRows[0].backtest_id);
  }, [backtestRows, backtestId]);

  const configuredInstruments: string[] = arrayFrom(report.config || {}, "instruments").map((item) => String(item));
  const replayInstruments = configuredInstruments.length
    ? configuredInstruments
    : instrument
      ? [instrument]
      : [];
  const configuredHorizons: string[] = arrayFrom(report.config || {}, "horizons").map((item) => String(item));
  const configuredModels: string[] = arrayFrom(report.config || {}, "models").map((item) => String(item));
  const replayHorizon = configuredHorizons[0] || horizon;
  const modelFilterOptions = Array.from(
    new Set(
      [
        "ensemble",
        ...configuredModels,
        ...modelRows.map((row: any) => String(row.model_id || "")),
        ...scorecardRows.map((row: any) => String(row.model_id || "")),
      ].filter(Boolean),
    ),
  ).sort();

  const runEvaluation = async () => {
    setRunning(true);
    try {
      const body = await apiPost<any>("/market/evaluations", {
        name: `Forecast evaluation ${new Date().toISOString().slice(0, 16).replace("T", " ")}`,
        sources: source === "both" ? ["lifecycle", "nm06"] : [source],
        instruments: instrument ? [instrument] : [],
        models: modelFilter.trim() ? [modelFilter.trim()] : [],
        horizons: horizon ? [horizon] : [],
        start,
        end,
        as_of: new Date().toISOString(),
        neutral_threshold: Number(threshold),
        interval_confidence: Number(confidence),
        min_samples: Number(minSamples),
        walk_forward_folds: Number(folds),
      });
      setSelectedId(body.evaluation_id);
      evaluations.refresh();
      toast.push(
        body.samples
          ? `Evaluation stored with ${body.samples} records.`
          : "Evaluation stored; no matching evaluated records, so no accuracy claim is made.",
        body.samples ? "success" : "info",
      );
    } catch (error: any) {
      toast.push(error?.detail || "The evaluation could not be created.", "error");
    } finally {
      setRunning(false);
    }
  };

  const createForecastReplay = async () => {
    if (!report.evaluation_id) return;
    setCreatingReplay(true);
    try {
      const forecastHorizon = NM06_HORIZON[replayHorizon] || "one_week";
      const payload = {
        name: `Forecast replay ${shortId(report.evaluation_id, 8)}`,
        strategy_catalog_id: "forecast_assisted",
        strategy_version: "pi11-web-1.0",
        strategy_config: {
          trend_period: 20,
          min_return: "0.005",
          min_probability: "0.55",
          max_uncertainty: "high",
          forecast_horizon: forecastHorizon,
          max_age_hours: 168,
          allowed_model_states: ["active", "validated"],
          horizon_days: FORECAST_HOLDING_DAYS[forecastHorizon] || 5,
          max_data_age_hours: 72,
          evaluation_id: report.evaluation_id,
        },
        instruments: replayInstruments,
        start: new Date(`${start}T00:00:00Z`).toISOString(),
        end: new Date(`${end}T23:59:59Z`).toISOString(),
        initial_capital: capital,
        evaluation_frequency: "1d",
        horizon: "short_term",
        // `models` in the evaluation config are model identifiers, while the
        // replay filter expects a persisted model version. Leave it open rather
        // than silently filtering every forecast out with a mismatched value.
        forecast_model_version: "",
        risk_policy_version: selectedPolicy?.version || "nm08-backtest-v1",
        risk_policy: selectedPolicy || {},
        max_position_weight: selectedPolicy?.max_position_weight || "0.25",
        max_gross_exposure: "1.00",
        benchmark: benchmark.trim(),
        execution_config: {
          brokerage_bps: brokerageBps,
          spread_bps: spreadBps,
          impact_coefficient_bps: impactBps,
        },
      };
      const created = await apiPost<any>("/market/backtests", payload);
      setBacktestId(created.backtest_id);
      try {
        await apiPost(`/market/backtests/${encodeURIComponent(created.backtest_id)}/start`, {});
        toast.push("Forecast-assisted replay created and completed.", "success");
      } catch (error: any) {
        toast.push(error?.detail || "Replay created; open Backtesting to inspect it.", "info");
      }
      backtests.refresh();
    } catch (error: any) {
      toast.push(error?.detail || "The forecast replay could not be created.", "error");
    } finally {
      setCreatingReplay(false);
    }
  };

  return (
    <div className="page">
      <PageHeader
        eyebrow="Intelligence / Prediction evaluation"
        title={<>Forecast evidence, <em>scored separately from trading.</em></>}
        description="Evaluate stored forecasts against realised target observations, then replay their hypothetical trading consequences through the existing isolated NM-11 engine."
        meta={
          <>
            <span className="meta-item"><FlaskConical size={13} /> {status.data?.evaluations?.total ?? 0} evaluations</span>
            <span className="meta-item"><ShieldCheck size={13} /> NM-08 risk</span>
            <span className="meta-item"><ShieldCheck size={13} /> NM-10 execution</span>
          </>
        }
        actions={
          <>
            <Button variant="ghost" icon={<RefreshCw size={14} />} onClick={() => { evaluations.refresh(); backtests.refresh(); detail.refresh(); backtestDetail.refresh(); backtestResults.refresh(); backtestTrades.refresh(); }}>
              Refresh
            </Button>
            <Button variant="primary" icon={<Play size={14} />} loading={running} onClick={runEvaluation}>
              Run evaluation
            </Button>
          </>
        }
      />
      <Banner tone="info" title="Accuracy is not profit">
        Price, return, direction, probability and interval metrics score predictions. The replay below scores hypothetical decisions after costs, slippage and risk. Neither result is real money.
      </Banner>
      {evaluations.error && <ErrorBlock message={evaluations.error.detail} onRetry={evaluations.refresh} />}
      <div className="dashboard-grid">
        <Card className="backtest-list-card">
          <CardHeader title="Evaluation runs" subtitle="Append-only reports" action={<Badge tone="neutral">{rows.length}</Badge>} />
          {evaluations.loading ? (
            <LoadingBlock rows={4} label="Reading evaluations…" />
          ) : (
            <div className="rail-list">
              {rows.map((row) => (
                <button
                  type="button"
                  className={`rail-item ${current?.evaluation_id === row.evaluation_id ? "rail-item-active" : ""}`}
                  key={row.evaluation_id}
                  onClick={() => setSelectedId(row.evaluation_id)}
                >
                  <span className="rail-icon"><FlaskConical size={15} /></span>
                  <span>
                    <strong>{row.name || "Forecast evaluation"}</strong>
                    <small>{row.samples ?? 0} records · {timeAgo(row.created_at)}</small>
                  </span>
                  <StatusBadge value={row.status || "unknown"} />
                </button>
              ))}
            </div>
          )}
          {!rows.length && !evaluations.loading && (
            <EmptyState
              icon={<FlaskConical size={22} />}
              title="No evaluation yet"
              description="Evaluate PI-09 lifecycle outcomes or NM-06 recorded forecasts. An empty store is not evidence of accuracy."
            />
          )}
        </Card>
        <Card>
          <CardHeader title="Configuration" subtitle="The same configuration and evidence hash are persisted" />
            <div className="form-grid">
              <Field label="Evidence source">
                <SelectInput value={source} onChange={(event) => setSource(event.target.value)}>
                  {SOURCES.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
                </SelectInput>
              </Field>
              <Field label="Instrument" hint="Leave empty for the stored universe">
                <TextInput value={instrument} onChange={(event) => setInstrument(event.target.value)} placeholder="XNSE:RELIANCE" list="evaluation-instruments" />
                <datalist id="evaluation-instruments">
                  {arrayFrom(instruments.data, "instruments").map((item) => (
                    <option key={item.instrument_id} value={item.instrument_id} />
                  ))}
                </datalist>
              </Field>
              <Field label="Model / ensemble" hint="Exact model, version or ensemble id; empty means all">
                <TextInput value={modelFilter} onChange={(event) => setModelFilter(event.target.value)} placeholder="model_id or ensemble_id" list="evaluation-models" />
                <datalist id="evaluation-models">
                  {modelFilterOptions.map((item) => <option key={item} value={item} />)}
                </datalist>
              </Field>
              <Field label="Horizon" hint="No horizons are blended into one accuracy number">
                <SelectInput value={horizon} onChange={(event) => setHorizon(event.target.value)}>
                  <option value="">All recorded</option>
                  {HORIZONS.map((item) => <option key={item} value={item}>{item}</option>)}
                </SelectInput>
              </Field>
              <Field label="Start"><TextInput type="date" value={start} onChange={(event) => setStart(event.target.value)} /></Field>
              <Field label="End"><TextInput type="date" value={end} onChange={(event) => setEnd(event.target.value)} /></Field>
              <Field label="Neutral threshold" hint="Fractional move treated as neutral">
                <TextInput value={threshold} onChange={(event) => setThreshold(event.target.value)} inputMode="decimal" />
              </Field>
              <Field label="Interval confidence" hint="Compared, never assumed">
                <TextInput value={confidence} onChange={(event) => setConfidence(event.target.value)} inputMode="decimal" />
              </Field>
              <Field label="Minimum sample"><TextInput value={minSamples} onChange={(event) => setMinSamples(event.target.value)} inputMode="numeric" /></Field>
              <Field label="Walk-forward folds" hint="0 disables chronological folds">
                <TextInput value={folds} onChange={(event) => setFolds(event.target.value)} inputMode="numeric" />
              </Field>
          </div>
        </Card>
      </div>

      <div className="stack">
        {current ? (
            <Card>
              <CardHeader
                title={current.name || "Forecast evaluation"}
                subtitle={`${title(report.status || current.status || "unknown")} · ${shortId(current.evaluation_id, 20)}`}
                action={<Badge tone={report.samples ? "good" : "warn"}>{report.samples ?? 0} records</Badge>}
              />
              {detail.loading ? <LoadingBlock rows={4} label="Reading evaluation…" /> : (
                <>
                  <div className="metric-grid metric-grid-four">
                    <Metric label="Price MAE" value={metric(price.mae, 1, 2)} help={`${price.samples ?? 0} targets · error in price`} />
                    <Metric label="Direction accuracy" value={pctMetric(direction.accuracy)} help={`${direction.resolved ?? 0} resolved · policy explicit`} tone={direction.accuracy == null ? "warn" : "good"} />
                    <Metric label="Interval coverage" value={pctMetric(intervals.actual_coverage)} help={`Nominal ${pctMetric(intervals.expected_coverage)}`} tone={intervals.coverage_gap != null && intervals.coverage_gap < 0 ? "warn" : "good"} />
                    <Metric label="Brier" value={metric(probabilities.brier, 1, 4)} help={probabilities.status === "evaluated" ? "Calibrated probabilities" : "No calibrated probabilities"} />
                  </div>
                  <div className="two-col-grid">
                    <div className="detail-section">
                      <h3>Evidence and treatment</h3>
                      <div className="fact-list">
                        <div><span>Operational</span><strong>{report.provenance?.operational_forecasts ?? 0}</strong></div>
                        <div><span>Reconstructed</span><strong>{report.provenance?.research_reconstructions ?? 0}</strong></div>
                        <div><span>Model filter</span><strong>{configuredModels.join(", ") || "All"}</strong></div>
                        <div><span>Direction policy</span><strong>{direction.policy?.direction_policy || "—"}</strong></div>
                        <div><span>Uncertain</span><strong>{direction.uncertain ?? 0} · {pctMetric(direction.uncertain_rate)}</strong></div>
                        <div><span>Abstained</span><strong>{direction.abstained ?? 0} · {pctMetric(direction.abstention_rate)}</strong></div>
                        <div><span>Non-directional</span><strong>{direction.non_directional ?? 0} · {pctMetric(direction.non_directional_rate)}</strong></div>
                        <div><span>Data digest</span><strong className="mono-small">{shortId(report.data_digest, 20)}</strong></div>
                        <div><span>Report hash</span><strong className="mono-small">{shortId(report.report_hash, 20)}</strong></div>
                      </div>
                    </div>
                    <div className="detail-section">
                      <h3>Return and interval evidence</h3>
                      <div className="fact-list">
                        <div><span>Return MAE</span><strong>{metric(metrics.returns?.mean_absolute_error)}</strong></div>
                        <div><span>Return bias</span><strong>{metric(metrics.returns?.bias)}</strong></div>
                        <div><span>Over-estimate rate</span><strong>{pctMetric(metrics.returns?.overestimate_rate)}</strong></div>
                        <div><span>Coverage gap</span><strong>{metric(intervals.coverage_gap)}</strong></div>
                        <div><span>Mean interval width</span><strong>{metric(intervals.mean_width)}</strong></div>
                        <div><span>Delayed observations</span><strong>{report.leakage_audit?.delayed_observation_count ?? 0}</strong></div>
                      </div>
                    </div>
                  </div>

                  <div className="detail-section">
                    <h3>Lifecycle checkpoints</h3>
                    <DataTable
                      compact
                      rows={lifecycleRows}
                      rowKey={(row) => String(row.stage)}
                      empty={<EmptyState title="No lifecycle checkpoints" description="Only evaluated PI-09 checkpoints can be compared." />}
                      columns={[
                        { key: "stage", label: "Checkpoint", render: (row) => title(row.stage) },
                        { key: "samples", label: "Records", align: "right", render: (row) => row.price?.samples ?? 0 },
                        { key: "mae", label: "MAE", align: "right", render: (row) => metric(row.price?.mae, 1, 2) },
                        { key: "accuracy", label: "Direction", align: "right", render: (row) => pctMetric(row.direction?.accuracy) },
                        { key: "coverage", label: "Interval", align: "right", render: (row) => pctMetric(row.intervals?.actual_coverage) },
                      ]}
                    />
                    <p className="inline-meta">Later confirmation is not assumed to be better. The backend counts paired improvements and regressions on identical cycle targets.</p>
                  </div>

                  <div className="two-col-grid">
                    <div className="detail-section">
                      <h3>Model comparison</h3>
                      <DataTable
                        compact
                        rows={modelRows.slice(0, 12)}
                        rowKey={(row, index) => `${row.model_id}-${row.model_version}-${index}`}
                        empty={<EmptyState title="No eligible models" description="A model needs enough evaluated records at the same horizon." />}
                        columns={[
                          { key: "model_id", label: "Model", render: (row) => <span>{title(row.model_id)} {row.beats_baseline === false && <Badge tone="warn">below baseline</Badge>}</span> },
                          { key: "horizon", label: "Horizon" },
                          { key: "samples", label: "N", align: "right" },
                          { key: "mae", label: "MAE", align: "right", render: (row) => metric(row.mae, 1, 2) },
                          { key: "direction_accuracy", label: "Direction", align: "right", render: (row) => pctMetric(row.direction_accuracy) },
                        ]}
                      />
                    </div>
                    <div className="detail-section">
                      <h3>Market regimes</h3>
                      <DataTable
                        compact
                        rows={regimeRows.slice(0, 12)}
                        rowKey={(row) => String(row.regime)}
                        empty={<EmptyState title="No regime labels" description="Regimes come from stored evidence, not from this evaluation." />}
                        columns={[
                          { key: "regime", label: "Regime", render: (row) => title(row.regime) },
                          { key: "samples", label: "N", align: "right" },
                          { key: "mae", label: "MAE", align: "right", render: (row) => metric(row.mae, 1, 2) },
                          { key: "direction", label: "Direction", align: "right", render: (row) => pctMetric(row.direction_accuracy) },
                          { key: "caveat", label: "Evidence", render: (row) => row.sufficient ? <Badge tone="good">sufficient</Badge> : <Badge tone="warn">insufficient</Badge> },
                        ]}
                      />
                    </div>
                  </div>

                  <div className="detail-section">
                    <h3>Stored out-of-sample scorecards</h3>
                    <DataTable
                      compact
                      rows={scorecardRows.slice(0, 12)}
                      rowKey={(row, index) => `${row.model_id}-${row.horizon}-${row.instrument_id}-${index}`}
                      empty={<EmptyState title="No stored scorecards" description="Train or evaluate a PI-05/06/07/08 model to populate this evidence." />}
                      columns={[
                        { key: "model_id", label: "Model", render: (row) => title(row.model_id) },
                        { key: "instrument_id", label: "Instrument" },
                        { key: "horizon", label: "Horizon" },
                        { key: "samples", label: "N", align: "right" },
                        { key: "price_mae", label: "MAE", align: "right", render: (row) => metric(row.price_mae, 1, 2) },
                        { key: "direction_accuracy", label: "Direction", align: "right", render: (row) => pctMetric(row.direction_accuracy) },
                      ]}
                    />
                    <p className="inline-meta">These are aggregate training/test scorecards from the existing model services, not per-target forecast records. They are ranked only within a matching horizon and never promote a model.</p>
                  </div>

                  {arrayFrom(metrics, "walk_forward").length > 0 && (
                    <div className="detail-section">
                      <h3>Walk-forward windows</h3>
                      <DataTable
                        compact
                        rows={arrayFrom(metrics, "walk_forward")}
                        rowKey={(row) => String(row.fold)}
                        columns={[
                          { key: "fold", label: "Fold" },
                          { key: "test_start", label: "Test start", render: (row) => shortDate(row.test_start) },
                          { key: "test_end", label: "Test end", render: (row) => shortDate(row.test_end) },
                          { key: "samples", label: "N", align: "right", render: (row) => row.price?.samples ?? 0 },
                          { key: "mae", label: "MAE", align: "right", render: (row) => metric(row.price?.mae, 1, 2) },
                        ]}
                      />
                    </div>
                  )}

                  <div className="detail-section">
                    <h3>Known limitations</h3>
                    <ul className="plain-list">
                      {(report.limitations || []).map((item: string) => <li key={item}>{item}</li>)}
                    </ul>
                  </div>
                </>
              )}
            </Card>
          ) : (
            <Card><EmptyState icon={<FlaskConical size={24} />} title="No evaluation selected" description="Run an evaluation to inspect accuracy, calibration, lifecycle and regime evidence." /></Card>
        )}

        <Card>
          <CardHeader
            title="Historical paper-trading replay"
          subtitle="The existing NM-11 engine: isolated state, costs, slippage and NM-08 decisions"
          action={<Badge tone="neutral">{backtestRows.length} runs</Badge>}
        />
        <div className="two-col-grid">
          <div>
            <div className="form-grid">
              <Field label="Starting virtual capital"><TextInput value={capital} onChange={(event) => setCapital(event.target.value)} inputMode="decimal" /></Field>
              <Field label="Benchmark" hint="Optional canonical index or instrument"><TextInput value={benchmark} onChange={(event) => setBenchmark(event.target.value)} placeholder="XNSE:NIFTY 50" /></Field>
              <Field label="Brokerage bps"><TextInput value={brokerageBps} onChange={(event) => setBrokerageBps(event.target.value)} inputMode="decimal" /></Field>
              <Field label="Spread bps" hint="Synthetic spread, not a quoted book"><TextInput value={spreadBps} onChange={(event) => setSpreadBps(event.target.value)} inputMode="decimal" /></Field>
              <Field label="Impact bps" hint="Square-root market-impact coefficient"><TextInput value={impactBps} onChange={(event) => setImpactBps(event.target.value)} inputMode="decimal" /></Field>
              <Field label="Replay from"><TextInput type="date" value={start} onChange={(event) => setStart(event.target.value)} /></Field>
              <Field label="Replay through"><TextInput type="date" value={end} onChange={(event) => setEnd(event.target.value)} /></Field>
            </div>
            <div className="inline-warning">
              The replay uses `forecast_assisted` with the evaluation's horizon mapped to the existing strategy input. It cannot create a live or paper order, and it never mutates the wallet or ledger.
            </div>
            <div className="row-actions">
              <Button variant="primary" icon={<Target size={15} />} loading={creatingReplay} disabled={!report.evaluation_id || !replayInstruments.length || !selectedPolicy} onClick={createForecastReplay}>
                Create forecast replay
              </Button>
              <Button variant="ghost" onClick={() => navigate("backtesting")}>Open Backtesting</Button>
            </div>
          </div>
          <div>
            {selectedBacktest ? (
              <>
                <div className="metric-grid metric-grid-four">
                  <Metric label="Total return" value={backtestMetrics.total_return == null ? "—" : pct(num(backtestMetrics.total_return) * 100)} help="Net of simulated costs" tone={backtestMetrics.total_return == null ? "neutral" : num(backtestMetrics.total_return) < 0 ? "bad" : "good"} />
                  <Metric label="Max drawdown" value={backtestMetrics.max_drawdown_pct == null && backtestMetrics.max_drawdown == null ? "—" : pct(num(backtestMetrics.max_drawdown_pct ?? backtestMetrics.max_drawdown) * 100)} help="Historical replay" tone={backtestMetrics.max_drawdown_pct == null && backtestMetrics.max_drawdown == null ? "neutral" : "bad"} />
                  <Metric label="Sharpe" value={backtestMetrics.sharpe ?? "—"} help="Only when observations allow" />
                  <Metric label="Trades" value={backtestMetrics.trades ?? arrayFrom(backtestTrades.data, "trades").length} help={`Status ${title(selectedBacktest.status)}`} />
                </div>
                <div className="chart-block">
                  <div className="chart-block-head"><span>Equity curve</span><Badge tone="neutral">{curve.length ? "Isolated replay" : "No curve"}</Badge></div>
                  <LineChart data={curve} height={220} color="var(--gold)" empty="No replay curve for this run" />
                </div>
                <div className="two-col-grid">
                  <div className="detail-section">
                    <h3>Risk-adjusted replay</h3>
                    <div className="fact-list">
                      <div><span>Volatility</span><strong>{pctMetric(backtestMetrics.volatility)}</strong></div>
                      <div><span>Sortino</span><strong>{metric(backtestMetrics.sortino)}</strong></div>
                      <div><span>Calmar</span><strong>{metric(backtestMetrics.calmar)}</strong></div>
                      <div><span>Win rate</span><strong>{pctMetric(backtestMetrics.win_rate)}</strong></div>
                      <div><span>Profit factor</span><strong>{metric(backtestMetrics.profit_factor)}</strong></div>
                      <div><span>Fees / slippage</span><strong>{backtestMetrics.total_fees ?? "—"} / {backtestMetrics.total_slippage ?? "—"}</strong></div>
                    </div>
                  </div>
                  <div className="detail-section">
                    <h3>Benchmark</h3>
                    <div className="fact-list">
                      <div><span>Instrument</span><strong>{benchmarkResult.instrument_id || benchmark || "Not supplied"}</strong></div>
                      <div><span>Status</span><strong>{benchmarkResult.available ? "Comparable" : "Unavailable"}</strong></div>
                      <div><span>Return</span><strong>{pctMetric(benchmarkResult.total_return)}</strong></div>
                      <div><span>Max drawdown</span><strong>{pctMetric(benchmarkResult.max_drawdown)}</strong></div>
                      <div><span>Basis</span><strong>{benchmarkResult.basis || "—"}</strong></div>
                    </div>
                  </div>
                </div>
                <DataTable
                  compact
                  rows={arrayFrom(backtestTrades.data, "trades").slice(0, 10)}
                  rowKey={(row) => row.trade_id}
                  empty={<EmptyState title="No trades" description="A forecast with insufficient quality may correctly abstain." />}
                  columns={[
                    { key: "executed_at", label: "Executed", render: (row) => dateTime(row.executed_at) },
                    { key: "instrument_id", label: "Instrument" },
                    { key: "side", label: "Side", render: (row) => <Badge tone={row.side === "buy" ? "good" : "bad"}>{title(row.side)}</Badge> },
                    { key: "price", label: "Price", align: "right" },
                    { key: "fees", label: "Fees", align: "right" },
                  ]}
                />
              </>
            ) : (
              <EmptyState icon={<BarChart3 size={24} />} title="No replay yet" description="Create a forecast-assisted replay after an evaluation with matching instruments and historical bars." />
            )}
          </div>
        </div>
      </Card>

      <Card>
        <CardHeader title="Evaluation contract" subtitle="What the backend refuses to claim" />
        <div className="contract-list">
          <div><ShieldCheck size={15} /><span>Stored forecasts are read, never regenerated at a historical timestamp.</span></div>
          <div><ShieldCheck size={15} /><span>Reconstructed research evidence must declare its model-training and information cutoffs.</span></div>
          <div><ShieldCheck size={15} /><span>Probability metrics are withheld for uncalibrated model scores.</span></div>
          <div><ShieldCheck size={15} /><span>No model is promoted, reweighted or converted into a trading instruction here.</span></div>
        </div>
      </Card>
      </div>
    </div>
  );
}
