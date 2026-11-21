import { useMemo, useState } from "react";
import {
  BrainCircuit,
  Check,
  Clock3,
  Play,
  RefreshCw,
  Sparkles,
  Target,
  TrendingUp,
} from "lucide-react";
import { apiGet, apiPost } from "../api";
import { useApi, useDocumentTitle } from "../hooks";
import {
  arrayFrom,
  dateTime,
  money,
  measured,
  num,
  pct,
  pctUnsigned,
  shortId,
  timeAgo,
  title,
} from "../lib";
import {
  Badge,
  Button,
  Card,
  CardHeader,
  DataTable,
  EmptyState,
  ErrorBlock,
  Field,
  LoadingBlock,
  Modal,
  PageHeader,
  SearchInput,
  SelectInput,
  StatusBadge,
  useToast,
} from "../components/ui";

const FORECAST_HORIZONS = ["5m", "15m", "20m", "1h", "1d", "3d", "5d", "30d", "90d"];
const FORECAST_ENGINES = [
  { value: "ensemble", label: "Ensemble (PI-08)" },
  { value: "gradient", label: "Gradient models" },
  { value: "lstm", label: "LSTM sequence models" },
  { value: "statistical", label: "Statistical baselines" },
];

function horizonOf(row: any): string {
  return String(row.forecast_horizon || row.horizon || "");
}

function pctText(value: any): string {
  return value == null ? "—" : pct(num(value, 0) * 100);
}

function stageLabel(value: any): string {
  const text = String(value ?? "").trim();
  const match = text.match(/^t[_-]minus[_-](\d+)$/i);
  return match ? `T−${match[1]}` : title(text);
}

export function ForecastsPage() {
  useDocumentTitle("Forecasts");
  const toast = useToast();
  const [engine, setEngine] = useState("gradient");
  const [family, setFamily] = useState("");
  const [search, setSearch] = useState("");
  const [horizon, setHorizon] = useState("");
  const [createOpen, setCreateOpen] = useState(false);
  const [instrumentId, setInstrumentId] = useState("");
  const [forecastHorizon, setForecastHorizon] = useState("1d");
  const [saving, setSaving] = useState(false);
  const [selected, setSelected] = useState<any | null>(null);
  const [trainOpen, setTrainOpen] = useState(false);
  const [trainInstrument, setTrainInstrument] = useState("");
  const [trainHorizon, setTrainHorizon] = useState("1d");
  const [training, setTraining] = useState(false);
  const [report, setReport] = useState<any | null>(null);
  const [explain, setExplain] = useState<any | null>(null);
  const [explaining, setExplaining] = useState(false);
  const [ensembleResult, setEnsembleResult] = useState<any | null>(null);
  const [lifecycleInstrument, setLifecycleInstrument] = useState("");
  const [lifecycleHorizon, setLifecycleHorizon] = useState("20m");
  const [lifecycleCreating, setLifecycleCreating] = useState(false);
  const [lifecycleRunning, setLifecycleRunning] = useState(false);
  const [selectedCycle, setSelectedCycle] = useState<any | null>(null);
  const [cycleDetail, setCycleDetail] = useState<any | null>(null);
  const [cycleDetailLoading, setCycleDetailLoading] = useState(false);
  const [outlookInstrument, setOutlookInstrument] = useState("");
  const [outlookResult, setOutlookResult] = useState<any | null>(null);
  const [outlookAccuracy, setOutlookAccuracy] = useState<any | null>(null);
  const [outlookLoading, setOutlookLoading] = useState(false);
  const [outlookPerformanceLoading, setOutlookPerformanceLoading] = useState(false);

  const forecastPath = `/market/forecasts?limit=200&engine=${encodeURIComponent(engine)}${family && engine === "gradient" ? `&model_id=${encodeURIComponent(family)}` : ""}`;
  const forecasts = useApi<any>(forecastPath);
  const models = useApi<any>(`/market/forecasts/models?engine=${encodeURIComponent(engine)}`);
  const status = useApi<any>("/market/forecasts/status");
  const instruments = useApi<any>("/market/instruments?limit=200");
  const cycles = useApi<any>("/market/forecasts/cycles?limit=50");
  const scheduler = useApi<any>("/market/forecasts/scheduler");
  const outlookStatus = useApi<any>("/market/forecasts/horizons/status");
  const rows = arrayFrom(forecasts.data, "forecasts");
  const modelRows = arrayFrom(models.data, "models");
  const cycleRows = arrayFrom(cycles.data, "cycles");
  const autoPlan = scheduler.data?.auto_plan;
  const autoPlanState = autoPlan
    ? `${autoPlan.enabled ? "Enabled" : "Disabled"} · ${autoPlan.horizon || "horizon unset"}`
    : "unavailable";
  const autoPlanSelection = autoPlan
    ? `${num(autoPlan.selected, 0)} selected of ${num(autoPlan.considered, 0)}`
    : "—";
  const autoPlanResult = autoPlan
    ? `${num((autoPlan.created || []).length, 0)} created · ${num((autoPlan.skipped || []).length, 0)} skipped · ${num((autoPlan.errors || []).length, 0)} errors`
    : "—";
  const backends = engine === "lstm"
    ? (status.data?.lstm?.backends || {})
    : engine === "gradient"
      ? (status.data?.gradient?.backends || {})
      : {};

  const filtered = useMemo(() => {
    const text = search.toLowerCase().trim();
    return rows.filter((row) => {
      const matchesText = !text || [row.instrument_id, row.instrument?.symbol, row.model_id, row.predicted_direction]
        .some((value) => String(value || "").toLowerCase().includes(text));
      return matchesText && (!horizon || horizonOf(row) === horizon);
    });
  }, [rows, search, horizon]);

  const create = async () => {
    setSaving(true);
    try {
      const body = await apiPost<any>("/market/forecasts", {
        instrument_id: instrumentId,
        horizon: forecastHorizon,
        engine: engine === "statistical" ? "statistical" : engine === "lstm" ? "lstm" : engine === "ensemble" ? "ensemble" : (family || "gradient"),
      });
      if (engine === "ensemble") setEnsembleResult(body);
      toast.push("Forecast generated and stored with its model identity.", "success");
      setCreateOpen(false);
      forecasts.refresh();
    } catch (error: any) {
      toast.push(error?.detail || "The forecast could not be generated.", "error");
    } finally { setSaving(false); }
  };

  const train = async () => {
    if (engine === "ensemble") {
      toast.push("Ensemble weights are evidence-derived; there is no separate ensemble training step.", "error");
      return;
    }
    setTraining(true);
    try {
      const trainEngine = engine === "lstm" ? "lstm" : "gradient";
      const body = await apiPost<any>("/market/forecasts/train", {
        instrument_id: trainInstrument,
        horizon: trainHorizon,
        engine: trainEngine,
      });
      setReport(body);
      const label = trainEngine === "lstm" ? "LSTM" : "Gradient";
      toast.push(
        body?.selected_model
          ? `${label} evaluation stored; ${body.selected_model} selected on validation.`
          : `${label} evaluation stored; no family was eligible.`,
        body?.selected_model ? "success" : "error",
      );
      setTrainOpen(false);
      forecasts.refresh();
      models.refresh();
    } catch (error: any) {
      toast.push(error?.detail || "Model training could not run.", "error");
    } finally { setTraining(false); }
  };

  const showExplanation = async (row: any) => {
    const id = String(row.forecast_id || "");
    if (!id.startsWith("gbf_") && !id.startsWith("lstm_") && !id.startsWith("ens_") && !id.startsWith("ensfb_")) return;
    setExplaining(true);
    try {
      setExplain(await apiGet<any>(`/market/forecasts/${encodeURIComponent(row.forecast_id)}/explain`));
    } catch (error: any) {
      toast.push(error?.detail || "The explanation could not be produced.", "error");
    } finally { setExplaining(false); }
  };

  const scheduleLifecycle = async () => {
    if (!lifecycleInstrument) return;
    setLifecycleCreating(true);
    try {
      const detail = await apiPost<any>("/market/forecasts/cycles", {
        instrument_id: lifecycleInstrument,
        horizon: lifecycleHorizon,
      });
      setSelectedCycle(detail.cycle || detail);
      setCycleDetail(detail);
      toast.push("Forecast lifecycle scheduled with durable checkpoints.", "success");
      cycles.refresh();
      scheduler.refresh();
    } catch (error: any) {
      toast.push(error?.detail || "The lifecycle could not be scheduled.", "error");
    } finally { setLifecycleCreating(false); }
  };

  const generateOutlook = async () => {
    if (!outlookInstrument) return;
    setOutlookLoading(true);
    try {
      const body = await apiPost<any>("/market/forecasts/horizons", {
        instrument_id: outlookInstrument,
        horizons: ["1d", "3d", "5d", "30d", "90d"],
      });
      setOutlookResult(body);
      setOutlookAccuracy(null);
      const generated = num(body.generated, 0);
      const failed = num(body.failed, 0);
      const abstained = num(body.abstained, 0);
      toast.push(
        `${generated} horizon forecast(s) generated; ${abstained} abstained; ${failed} failed independently.`,
        failed ? "error" : generated ? "success" : "info",
      );
      forecasts.refresh();
    } catch (error: any) {
      toast.push(error?.detail || "The horizon outlook could not be generated.", "error");
    } finally { setOutlookLoading(false); }
  };

  const loadOutlookLatest = async () => {
    if (!outlookInstrument) return;
    setOutlookLoading(true);
    try {
      setOutlookResult(await apiGet<any>(`/market/forecasts/horizons/${encodeURIComponent(outlookInstrument)}`));
      setOutlookAccuracy(null);
    } catch (error: any) {
      toast.push(error?.detail || "The stored horizon outlook could not be read.", "error");
    } finally { setOutlookLoading(false); }
  };

  const loadOutlookPerformance = async () => {
    setOutlookPerformanceLoading(true);
    try {
      const suffix = outlookInstrument ? `instrument_id=${encodeURIComponent(outlookInstrument)}&` : "";
      setOutlookAccuracy(await apiGet<any>(`/market/forecasts/horizons/performance?${suffix}limit=1000`));
    } catch (error: any) {
      toast.push(error?.detail || "The horizon performance could not be read.", "error");
    } finally { setOutlookPerformanceLoading(false); }
  };

  const inspectCycle = async (cycle: any) => {
    const id = String(cycle.cycle_id || "");
    if (!id) return;
    setSelectedCycle(cycle);
    setCycleDetailLoading(true);
    try {
      setCycleDetail(await apiGet<any>(`/market/forecasts/cycles/${encodeURIComponent(id)}`));
    } catch (error: any) {
      toast.push(error?.detail || "The lifecycle could not be read.", "error");
      setCycleDetail(null);
    } finally { setCycleDetailLoading(false); }
  };

  const runDueCheckpoints = async () => {
    setLifecycleRunning(true);
    try {
      const result = await apiPost<any>("/market/forecasts/scheduler/tick", { limit: 5 });
      toast.push(`${(result.executed || []).length} due checkpoint(s) processed; failures remain visible in the timeline.`, "success");
      cycles.refresh();
      scheduler.refresh();
      if (selectedCycle?.cycle_id) await inspectCycle(selectedCycle);
    } catch (error: any) {
      toast.push(error?.detail || "The scheduler tick failed.", "error");
    } finally { setLifecycleRunning(false); }
  };

  return (
    <div className="page">
      <PageHeader
        eyebrow="Intelligence / Forecasts"
        title={<>Point estimates, <em>with the uncertainty left in.</em></>}
        description="Statistical baselines, XGBoost, LightGBM, LSTM sequences and the evidence-weighted PI-08 ensemble share the same PI-01 contract. Every record names its model versions, weights and data cutoff."
        meta={<><span className="meta-item"><TrendingUp size={13} /> {rows.length} stored</span><span className="meta-item"><BrainCircuit size={13} /> {modelRows.length} model records</span></>}
        actions={<>
          <Button variant="ghost" icon={<RefreshCw size={14} />} onClick={() => { forecasts.refresh(); models.refresh(); status.refresh(); }}>Refresh</Button>
          <Button variant="secondary" icon={<Target size={15} />} disabled={engine === "ensemble"} title={engine === "ensemble" ? "Ensemble weights come from constituent validation evidence; there is no ensemble fitting step." : undefined} onClick={() => { setTrainInstrument(instrumentId); setTrainOpen(true); }}>Train &amp; evaluate</Button>
          <Button variant="primary" icon={<Sparkles size={15} />} onClick={() => setCreateOpen(true)}>Generate forecast</Button>
        </>}
      />
      {forecasts.error && <ErrorBlock message={forecasts.error.detail} onRetry={forecasts.refresh} />}
      <div className="notice-strip">
        <BrainCircuit size={16} />
        <span>
          <strong>{engine === "ensemble" ? "Evidence-weighted ensemble." : engine === "gradient" ? "Experimental gradient models." : engine === "lstm" ? "Experimental LSTM sequence models." : "Statistical baselines."}</strong>{" "}
          {engine === "ensemble"
            ? "Only validation-evidenced models may contribute. Weights are inverse relative validation error, smoothed and capped; disagreement, calibration and fallback stay explicit."
            : engine === "gradient"
            ? "XGBoost and LightGBM are research candidates. They are not automatically promoted, and a baseline may still be preferable."
            : engine === "lstm"
              ? "LSTM consumes chronological PI-04 feature windows. Its intervals are validation-residual based, its classifier probabilities are withheld, and a simpler model may still win."
              : "Last-value, random-walk, moving-average, exponential-smoothing and ARIMA baselines remain the comparison floor."}
        </span>
        <Badge tone={engine === "gradient" || engine === "lstm" ? "warn" : "info"}>{engine === "ensemble" ? "PI-08" : engine === "gradient" ? "PI-06" : engine === "lstm" ? "PI-07" : "PI-05"}</Badge>
      </div>

      <Card>
        <CardHeader
          title="Swing and long-term outlook"
          subtitle="1d / 3d / 5d count verified sessions; 30d / 90d count calendar days. Every point estimate still comes from the existing PI-08 ensemble."
          action={<Badge tone={outlookResult?.partial ? "warn" : "info"}>{outlookResult ? `${num(outlookResult.generated ?? outlookResult.available_horizons, 0)} / 5 horizons` : "PI-10"}</Badge>}
        />
        <div className="filter-row">
          <SelectInput value={outlookInstrument} onChange={(event) => setOutlookInstrument(event.target.value)} aria-label="Outlook instrument">
            <option value="">Choose an instrument…</option>
            {arrayFrom(instruments.data, "items").map((item) => <option key={item.instrument_id} value={item.instrument_id}>{item.symbol || item.instrument_id} · {item.exchange || item.mic}</option>)}
          </SelectInput>
          <Button variant="primary" icon={<Target size={15} />} loading={outlookLoading} disabled={!outlookInstrument} onClick={generateOutlook}>Generate five horizons</Button>
          <Button variant="secondary" icon={<RefreshCw size={14} />} disabled={!outlookInstrument || outlookLoading} onClick={loadOutlookLatest}>Load latest stored</Button>
          <Button variant="ghost" loading={outlookPerformanceLoading} onClick={loadOutlookPerformance}>Outcome metrics</Button>
        </div>
        {outlookStatus.error && <ErrorBlock message={outlookStatus.error.detail} onRetry={outlookStatus.refresh} />}
        <div className="notice-strip">
          <Clock3 size={16} />
          <span>
            Swing horizons use the next verified trading sessions. Long horizons use elapsed calendar days and the first verified close at or after that date. Missing fundamentals, sector mappings or unverifiable targets stay explicit; they are never filled with invented values.
          </span>
          <Badge tone="neutral">forecast only</Badge>
        </div>
        {outlookResult ? (
          <>
            <DataTable
              rows={outlookResult.results || []}
              rowKey={(row: any) => row.horizon}
              empty={<EmptyState title="No horizon results" description="The instrument has no supported observations at this cutoff." />}
              columns={[
                { key: "horizon", label: "Horizon", render: (row: any) => <div><Badge tone={row.family === "swing" ? "info" : "neutral"}>{row.horizon}</Badge><small className="table-sub">{title(row.family)} · {row.profile?.target_rule}</small></div> },
                { key: "status", label: "Status", render: (row: any) => <StatusBadge value={row.status} /> },
                { key: "direction", label: "Direction", render: (row: any) => {
                  const value = String(row.forecast?.predicted_direction || row.forecast?.direction || "uncertain");
                  return <Badge tone={value === "up" ? "good" : value === "down" ? "bad" : "neutral"}>{title(value)}</Badge>;
                } },
                { key: "price", label: "Point estimate", render: (row: any) => row.forecast ? <div><strong>{money(row.forecast.predicted_price)}</strong><small className="table-sub">return {pctText(row.forecast.predicted_return)}</small></div> : "—" },
                { key: "range", label: "Return range", render: (row: any) => row.forecast ? `${pctText(row.forecast.lower_return)} → ${pctText(row.forecast.upper_return)}` : "—" },
                { key: "uncertainty", label: "Uncertainty", render: (row: any) => row.forecast ? <Badge tone={String(row.forecast.forecast_uncertainty || row.forecast.uncertainty).toLowerCase() === "high" ? "bad" : "warn"}>{title(row.forecast.forecast_uncertainty || row.forecast.uncertainty || "unknown")}</Badge> : "—" },
                { key: "target", label: "Target", render: (row: any) => row.expected_target_at ? <span className="mono-small">{dateTime(row.expected_target_at)}</span> : <span className="clamp-cell">{row.target_error || "unavailable"}</span> },
              ]}
            />
            <div className="fact-list">
              <div><span>Cross-horizon alignment</span><strong>{title(outlookResult.cross_horizon?.alignment || "insufficient")}</strong></div>
              <div><span>Short vs long divergence</span><strong>{outlookResult.cross_horizon?.short_long_divergence ? "Divergent outlook" : "No directional divergence"}</strong></div>
              <div><span>Sector context</span><strong>{outlookResult.context?.sector?.available ? outlookResult.context.sector.instrument_id : "unavailable"}</strong></div>
              <div><span>Benchmark context</span><strong>{outlookResult.context?.benchmark?.available ? outlookResult.context.benchmark.instrument_id : "unavailable"}</strong></div>
              <div><span>Point-in-time events</span><strong>{num(outlookResult.context?.counts?.news_model_eligible, 0)} verified of {num(outlookResult.context?.counts?.news_point_in_time, 0)}</strong></div>
              <div><span>Fundamental snapshots</span><strong>{num(outlookResult.context?.counts?.fundamentals_model_eligible, 0)} eligible</strong></div>
            </div>
            {(outlookResult.context?.notes || []).length > 0 && (
              <div className="inline-warning">{(outlookResult.context.notes || []).join(" · ")}</div>
            )}
            {outlookResult.cross_horizon?.short_long_divergence && (
              <div className="inline-warning">Short and long horizons disagree directionally. This is reported as multi-timeframe evidence, not forced into one direction.</div>
            )}
            <div className="inline-warning">These are forecasts with uncertainty, not guaranteed returns. They cannot place an order; NM-08 remains the risk authority and NM-10 the paper-execution authority.</div>
          </>
        ) : (
          <EmptyState icon={<TrendingUp size={22} />} title="No horizon outlook loaded" description="Generate the five horizons or load the latest stored forecasts for an instrument. No forecast is fabricated when evidence is missing." />
        )}
        {outlookAccuracy && (
          <div className="detail-section">
            <h3>Outcome metrics · separated by horizon</h3>
            <DataTable
              rows={outlookAccuracy.by_horizon || []}
              rowKey={(row: any) => row.horizon}
              empty={<EmptyState title="No evaluated outcomes" description="A horizon appears only after a target-time observation is recorded." />}
              columns={[
                { key: "horizon", label: "Horizon", render: (row: any) => <Badge tone={row.family === "swing" ? "info" : "neutral"}>{row.horizon}</Badge> },
                { key: "sample", label: "Samples", render: (row: any) => <span>{num(row.metrics?.sample_size, 0)}{!row.metrics?.reliable && <Badge tone="warn">small sample</Badge>}</span> },
                { key: "mae", label: "Price MAE / RMSE", render: (row: any) => row.metrics?.price_mae == null ? "—" : `${money(row.metrics.price_mae)} / ${money(row.metrics.price_rmse)}` },
                { key: "bias", label: "Bias", render: (row: any) => row.metrics?.price_bias == null ? "—" : money(row.metrics.price_bias) },
                { key: "direction", label: "Direction accuracy", render: (row: any) => row.metrics?.direction_accuracy == null ? "—" : pct(row.metrics.direction_accuracy * 100) },
                { key: "f1", label: "Macro F1", render: (row: any) => row.metrics?.macro_f1 == null ? "—" : num(row.metrics.macro_f1, 0).toFixed(3) },
                { key: "coverage", label: "Interval coverage", render: (row: any) => row.metrics?.interval_coverage == null ? "—" : pct(row.metrics.interval_coverage * 100) },
              ]}
            />
            <div className="inline-warning">{outlookAccuracy.combined_accuracy_refused}. Historical evaluation is not realized portfolio profit or a guarantee.</div>
          </div>
        )}
      </Card>

      {engine === "ensemble" && ensembleResult && (
        <Card>
          <CardHeader
            title={ensembleResult.abstained ? "Ensemble abstained" : ensembleResult.status === "fallback" ? "Statistical fallback" : "Latest ensemble forecast"}
            subtitle={ensembleResult.abstained ? "No forced direction was produced" : "Only models that actually participated are listed"}
            action={<Badge tone={ensembleResult.abstained ? "bad" : ensembleResult.status === "fallback" ? "warn" : "good"}>{title(ensembleResult.status || "unknown")}</Badge>}
          />
          {ensembleResult.abstained ? (
            <div className="inline-warning">{(ensembleResult.reasons || []).join(" ") || "The ensemble withheld a forecast because its validation conditions were not met."}</div>
          ) : (
            <>
              {ensembleResult.status === "fallback" && (
                <div className="inline-warning">Fallback to <strong>{ensembleResult.ensemble?.fallback_model_id}</strong> ({ensembleResult.ensemble?.fallback_model_version}). This is a single constituent baseline, not an ensemble blend.</div>
              )}
              <div className="fact-list">
                <div><span>Point estimate</span><strong>{money(ensembleResult.predicted_price)}</strong></div>
                <div><span>Expected return</span><strong>{pctText(ensembleResult.predicted_return)}</strong></div>
                <div><span>Direction</span><strong>{title(ensembleResult.predicted_direction || "uncertain")}</strong></div>
                <div><span>Interval</span><strong>{money(ensembleResult.lower_price)} → {money(ensembleResult.upper_price)}</strong></div>
                <div><span>Calibration</span><strong>{title(ensembleResult.ensemble?.calibration?.status || "uncalibrated")}</strong></div>
                <div><span>Disagreement</span><strong>{pctText(ensembleResult.ensemble?.disagreement?.weighted_return_dispersion)}</strong></div>
              </div>
              <DataTable
                rows={ensembleResult.ensemble?.constituents || []}
                rowKey={(row: any) => `${row.model_id}-${row.version}`}
                empty={<EmptyState title="No constituent contributions" description="The fallback path carries one model identity, not a weighted blend." />}
                columns={[
                  { key: "model", label: "Constituent", render: (row: any) => <span className="mono-small">{row.model_id} <small>v{row.version}</small></span> },
                  { key: "family", label: "Family", render: (row: any) => <Badge tone="neutral">{title(row.family || "—")}</Badge> },
                  { key: "weight", label: "Weight", render: (row: any) => pctText(row.weight) },
                  { key: "return", label: "Return", render: (row: any) => pctText(row.predicted_return) },
                  { key: "interval", label: "Constituent range", render: (row: any) => `${pctText(row.lower_return)} → ${pctText(row.upper_return)}` },
                ]}
              />
            </>
          )}
          {(ensembleResult.failures || []).length > 0 && (
            <div className="inline-warning">Isolated constituent failures: {(ensembleResult.failures || []).join(" · ")}</div>
          )}
        </Card>
      )}

      <Card>
        <CardHeader
          title="Forecast lifecycle"
          subtitle="One stable cycle, immutable T−20 / T−5 / T−1 versions and the target-time outcome"
          action={<Badge tone={scheduler.error ? "bad" : scheduler.loading ? "neutral" : scheduler.data?.running ? "good" : "warn"}>{scheduler.error ? "scheduler unavailable" : scheduler.loading ? "scheduler reading" : scheduler.data?.running ? "scheduler running" : "scheduler idle"}</Badge>}
        />
        <div className="filter-row">
          <SelectInput value={lifecycleInstrument} onChange={(event) => setLifecycleInstrument(event.target.value)} aria-label="Lifecycle instrument">
            <option value="">Choose an instrument…</option>
            {arrayFrom(instruments.data, "items").map((item) => <option key={item.instrument_id} value={item.instrument_id}>{item.symbol || item.instrument_id} · {item.exchange || item.mic}</option>)}
          </SelectInput>
          <SelectInput value={lifecycleHorizon} onChange={(event) => setLifecycleHorizon(event.target.value)} aria-label="Lifecycle horizon">
            {FORECAST_HORIZONS.map((item) => <option key={item} value={item}>{item}</option>)}
          </SelectInput>
          <Button variant="primary" icon={<Clock3 size={15} />} loading={lifecycleCreating} disabled={!lifecycleInstrument} onClick={scheduleLifecycle}>Schedule lifecycle</Button>
          <Button variant="secondary" icon={<Play size={14} />} loading={lifecycleRunning} onClick={runDueCheckpoints}>Run due checkpoints</Button>
        </div>
        {scheduler.loading ? <LoadingBlock rows={2} label="Reading scheduler state…" /> : scheduler.error ? (
          <ErrorBlock message={scheduler.error.detail} onRetry={scheduler.refresh} />
        ) : (
          <div className="fact-list">
            <div><span>Scheduler state</span><strong>{title(scheduler.data?.running ? "running" : "idle")}</strong></div>
            <div><span>Auto-planner</span><strong>{autoPlanState}</strong></div>
            <div><span>Last auto plan</span><strong>{autoPlanSelection}</strong></div>
            <div><span>Auto-plan result</span><strong>{autoPlanResult}</strong></div>
            <div><span>Tracked cycles</span><strong>{num(scheduler.data?.cycles, 0)}</strong></div>
            <div><span>Missed checkpoints</span><strong>{num((scheduler.data?.missed_checkpoints || []).length, 0)}</strong></div>
            <div><span>Pending evaluations</span><strong>{num((scheduler.data?.pending_evaluations || []).length, 0)}</strong></div>
            <div><span>Last scheduler tick</span><strong>{scheduler.data?.last_tick ? timeAgo(scheduler.data.last_tick) : "not recorded"}</strong></div>
          </div>
        )}
        {cycles.loading ? <LoadingBlock rows={3} label="Reading forecast cycles…" /> : cycles.error ? (
          <ErrorBlock message={cycles.error.detail} onRetry={cycles.refresh} />
        ) : (
          <DataTable
            rows={cycleRows}
            rowKey={(row: any) => row.cycle_id}
            onRowClick={inspectCycle}
            empty={<EmptyState icon={<Clock3 size={22} />} title="No scheduled lifecycles" description="Schedule a cycle to create T−20, T−5, T−1 and target-time checkpoints. Market-session and calendar coverage are validated before creation." />}
            columns={[
              { key: "instrument", label: "Instrument", render: (row: any) => <div className="identity-cell"><span className="symbol-mark">{(row.instrument_id || "?").split(":").pop()?.slice(0, 2)}</span><div><strong>{row.instrument_id}</strong><small>{row.exchange} · {row.resolution}</small></div></div> },
              { key: "horizon", label: "Horizon", render: (row: any) => <Badge tone="neutral">{row.horizon}</Badge> },
              { key: "target", label: "Target", render: (row: any) => <span className="mono-small">{dateTime(row.target_at)}</span> },
              { key: "state", label: "Cycle", render: (row: any) => <StatusBadge value={row.state} /> },
              { key: "policy", label: "Target rule", render: (row: any) => <span className="clamp-cell">{title(row.target_rule || "—")}</span> },
            ]}
          />
        )}
        {selectedCycle && (
          <div className="detail-section">
            <h3>Checkpoint timeline · {selectedCycle.cycle_id ? shortId(selectedCycle.cycle_id, 18) : "cycle"}</h3>
            {cycleDetailLoading ? <LoadingBlock rows={3} label="Reading lifecycle evidence…" /> : cycleDetail && (
              <>
                <div className="checkpoint-timeline">
                  <DataTable
                    rows={cycleDetail.checkpoints || []}
                    rowKey={(row: any) => row.checkpoint_id}
                    empty={<EmptyState title="No checkpoints" description="This cycle has no durable checkpoint records." />}
                    columns={[
                      { key: "label", label: "Checkpoint", width: "20%", render: (row: any) => <strong>{row.label}</strong> },
                      { key: "scheduled", label: "Scheduled", width: "20%", render: (row: any) => dateTime(row.scheduled_at) },
                      { key: "executed", label: "Executed", width: "23%", render: (row: any) => row.executed_at ? <span>{dateTime(row.executed_at)}{row.late && <Badge tone="warn">late</Badge>}</span> : "—" },
                      { key: "status", label: "Status", width: "13%", render: (row: any) => <StatusBadge value={row.status} /> },
                      { key: "reason", label: "Evidence", render: (row: any) => <span className="clamp-cell">{[...new Set(row.reasons || [])].join(" · ") || "—"}</span> },
                    ]}
                  />
                </div>
                <h3>Preserved versions</h3>
                <DataTable
                  rows={cycleDetail.forecasts || []}
                  rowKey={(row: any) => row.forecast_id}
                  empty={<EmptyState title="No forecast versions yet" description="A version appears only after a checkpoint obtains a fresh observation and a valid ensemble output." />}
                  columns={[
                    { key: "stage", label: "Version", render: (row: any) => <Badge tone="neutral">{stageLabel(row.lifecycle_stage)}</Badge> },
                    { key: "price", label: "Point estimate", render: (row: any) => <div><strong>{money(row.predicted_price)}</strong><small className="table-sub">return {pctText(row.predicted_return)}</small></div> },
                    { key: "direction", label: "Direction", render: (row: any) => <Badge tone={String(row.predicted_direction || row.direction).toLowerCase() === "up" ? "good" : String(row.predicted_direction || row.direction).toLowerCase() === "down" ? "bad" : "neutral"}>{title(row.predicted_direction || row.direction || "uncertain")}</Badge> },
                    { key: "uncertainty", label: "Uncertainty", render: (row: any) => <span>{title(row.forecast_uncertainty || row.uncertainty || "—")} · {pctText(row.uncertainty_width)}</span> },
                    { key: "model", label: "Ensemble", render: (row: any) => <span className="mono-small">{row.model_id} <small>v{row.model_version}</small></span> },
                    { key: "observed", label: "Data observed", render: (row: any) => dateTime(row.data_observed_at || row.underlying_data_timestamp || row.data_received_timestamp) },
                  ]}
                />
                {(cycleDetail.revisions || []).length > 0 && <div className="inline-warning">Revisions: {(cycleDetail.revisions || []).map((item: any) => `${stageLabel(item.previous_stage)}→${stageLabel(item.current_stage)} ${title(item.classification)}`).join(" · ")}</div>}
                <h3>Target outcome</h3>
                <DataTable
                  rows={cycleDetail.outcomes || []}
                  rowKey={(row: any) => row.outcome_id}
                  empty={<EmptyState title="Outcome pending" description="The target observation has not been validated. No later price is substituted silently." />}
                  columns={[
                    { key: "stage", label: "Version", render: (row: any) => <Badge tone="neutral">{stageLabel(row.stage)}</Badge> },
                    { key: "actual", label: "Actual", render: (row: any) => <div><strong>{money(row.actual_price)}</strong><small className="table-sub">return {pctText(row.actual_return)}</small></div> },
                    { key: "error", label: "Price error", render: (row: any) => <span>{money(row.absolute_price_error)} · {pctText(row.percentage_price_error)}</span> },
                    { key: "direction", label: "Direction", render: (row: any) => <Badge tone={row.direction_correct ? "good" : "bad"}>{row.direction_correct ? "correct" : "incorrect"} · {title(row.actual_direction)}</Badge> },
                    { key: "interval", label: "Interval", render: (row: any) => <Badge tone={row.inside_interval ? "good" : "warn"}>{row.inside_interval ? "inside" : "outside"}</Badge> },
                    { key: "delay", label: "Observation delay", render: (row: any) => row.observation_delay_seconds == null ? "—" : `${num(row.observation_delay_seconds, 0).toFixed(0)}s` },
                  ]}
                />
              </>
            )}
          </div>
        )}
      </Card>

      <Card>
        <CardHeader title="Forecast records" subtitle="Filter by engine and family, then inspect the same contract" action={<Badge tone="neutral">{filtered.length} shown</Badge>} />
        <div className="filter-row">
          <SearchInput value={search} onChange={setSearch} placeholder="Search instrument, family or direction…" />
          <SelectInput value={engine} onChange={(event) => { setEngine(event.target.value); setFamily(""); setHorizon(""); }} aria-label="Forecast engine">
            {FORECAST_ENGINES.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
          </SelectInput>
          {engine === "gradient" && (
            <SelectInput value={family} onChange={(event) => setFamily(event.target.value)} aria-label="Gradient family">
              <option value="">XGBoost and LightGBM</option>
              <option value="xgboost">XGBoost only</option>
              <option value="lightgbm">LightGBM only</option>
            </SelectInput>
          )}
          <SelectInput value={horizon} onChange={(event) => setHorizon(event.target.value)} aria-label="Forecast horizon">
            <option value="">All horizons</option>
            {FORECAST_HORIZONS.map((item) => <option key={item} value={item}>{item}</option>)}
          </SelectInput>
        </div>
        {forecasts.loading ? <LoadingBlock rows={5} label="Reading forecast history…" /> : (
          <DataTable
            rows={filtered}
            rowKey={(row) => row.forecast_id || `${row.instrument_id}-${row.generated_at}`}
            onRowClick={setSelected}
            empty={<EmptyState icon={<TrendingUp size={22} />} title={rows.length ? "No forecasts match" : "No forecasts for this engine"} description={rows.length ? "Adjust the search, family or horizon." : "Train a gradient model or generate a statistical baseline first."} action={!rows.length ? <Button variant="primary" size="sm" onClick={() => setCreateOpen(true)}>Generate forecast</Button> : undefined} />}
            columns={[
              { key: "instrument", label: "Instrument", render: (row) => <div className="identity-cell"><span className="symbol-mark">{(row.instrument?.symbol || row.instrument_id || "?").slice(0, 2)}</span><div><strong>{row.instrument?.symbol || row.instrument_id}</strong><small>{title(horizonOf(row) || "horizon")}</small></div></div>, sortable: true, sortValue: (row) => row.instrument_id || "" },
              { key: "predicted_price", label: "Point estimate", sortable: true, sortValue: (row) => num(row.predicted_price, 0), render: (row) => <div><strong>{money(row.predicted_price)}</strong><small className="table-sub">return {pctText(row.predicted_return)}</small></div> },
              { key: "direction", label: "Direction", render: (row) => <Badge tone={String(row.predicted_direction).toLowerCase() === "up" ? "good" : String(row.predicted_direction).toLowerCase() === "down" ? "bad" : "neutral"}>{title(row.predicted_direction || "uncertain")}</Badge> },
              { key: "interval", label: "Range", render: (row) => <span className="range-cell">{money(row.lower_price)}<i>→</i>{money(row.upper_price)}</span> },
              { key: "uncertainty", label: "Uncertainty", render: (row) => <Badge tone={String(row.forecast_uncertainty).toLowerCase() === "high" ? "bad" : String(row.forecast_uncertainty).toLowerCase() === "medium" ? "warn" : "good"}>{title(row.forecast_uncertainty || "—")}</Badge> },
              { key: "model_id", label: "Model", render: (row) => <span className="mono-small">{row.model_id || "—"} <small>v{row.model_version || "?"}</small></span> },
              { key: "created_at", label: "Created", render: (row) => timeAgo(row.forecast_generation_timestamp || row.generated_at), sortable: true, sortValue: (row) => row.forecast_generation_timestamp || row.generated_at || "" },
              { key: "status", label: "Validity", render: (row) => <StatusBadge value={row.forecast_validity || "pending"} /> },
            ]}
          />
        )}
      </Card>

      {report && (
        <Card>
          <CardHeader title={engine === "lstm" ? "Latest LSTM evaluation" : "Latest gradient evaluation"} subtitle="Validation selects; the test window is reported once" action={<Badge tone={report.selected_model ? "good" : "bad"}>{report.selected_model || "no selection"}</Badge>} />
          <div className="fact-list">
            <div><span>Preferred overall</span><strong>{title(report.preferred_overall || "insufficient data")}</strong></div>
            <div><span>Selected version</span><code>{report.selected_version || "—"}</code></div>
            <div><span>Selection metric</span><strong>{title(report.selection_metric || "validation price MAE")}</strong></div>
            <div><span>Test used for selection</span><strong>{report.test_used_for_selection ? "YES (invalid)" : "No"}</strong></div>
          </div>
          <DataTable
            rows={Object.entries(report.families || {}).map(([name, value]: [string, any]) => ({ name, ...value }))}
            rowKey={(row) => row.name}
            empty={<EmptyState title="No families evaluated" description="The run reported no eligible family." />}
            columns={[
              { key: "family", label: "Family", render: (row) => <Badge tone={row.name === report.selected_model ? "good" : "neutral"}>{row.name}</Badge> },
              { key: "status", label: "Suitability", render: (row) => <StatusBadge value={row.status || "unknown"} /> },
              { key: "validation", label: "Validation MAE", render: (row) => row.validation?.price_mae == null ? "—" : money(row.validation.price_mae) },
              { key: "test", label: "Test MAE", render: (row) => row.test?.price_mae == null ? "—" : money(row.test.price_mae) },
              { key: "walkForward", label: "Walk-forward MAE", render: (row) => {
                const values = (row.walk_forward || [])
                  .filter((fold: any) => fold.status === "evaluated" && fold.metrics?.price_mae != null)
                  .map((fold: any) => fold.metrics.price_mae);
                return values.length ? `${values.length} folds · ${money(Math.min(...values))}–${money(Math.max(...values))}` : "—";
              } },
              { key: "direction", label: "Test direction", render: (row) => row.test?.directional_accuracy == null ? "—" : pct(row.test.directional_accuracy * 100) },
              { key: "reason", label: "Decision", render: (row) => <span className="clamp-cell">{row.reason || "—"}</span> },
            ]}
          />
          {report.baselines && Object.keys(report.baselines).length > 0 && (
            <div className="fact-list">
              <div><span>Best statistical baseline</span><strong>{String(report.preferred_overall || "").startsWith("baseline:") ? "Preferable on the same test rows" : "Compared on the same test rows"}</strong></div>
              {Object.entries(report.baselines).map(([name, value]: [string, any]) => <div key={name}><span>{title(name)} price MAE</span><strong>{value?.price_mae == null ? "—" : money(value.price_mae)}</strong></div>)}
            </div>
          )}
        </Card>
      )}

      <div className="two-col-grid">
        <Card>
          <CardHeader title="Model suitability" subtitle="Trained artifacts and their current state" action={<Badge tone="neutral">{modelRows.length} records</Badge>} />
          <div className="model-list">
            {modelRows.slice(0, 6).map((model: any) => (
              <div key={`${model.model_id}-${model.version}-${(model.supported_horizons || []).join("-")}`}>
                <span className="model-dot" />
                <strong>{model.model_id}</strong>
                <small>{model.state} · {model.version}</small>
              </div>
            ))}
          </div>
          {!modelRows.length && <EmptyState title={`No ${engine === "ensemble" ? "ensemble" : engine === "lstm" ? "LSTM" : "gradient"} models yet`} description={engine === "ensemble" ? "Generate an ensemble forecast after at least two constituent models have validation evidence." : engine === "lstm" ? "Run Train & evaluate to fit a bounded CPU LSTM on chronological PI-04 feature windows." : "Run Train & evaluate to fit XGBoost and LightGBM on PI-04 features."} />}
          <div className="contract-list">
            {engine === "ensemble" ? (
              <>
                <div><Check size={15} /><span>Eligibility: validation evidence, supported horizon, usable artifact and freshness are checked before contribution.</span></div>
                <div><Check size={15} /><span>Weights: inverse relative validation error, smoothed across configurations and capped; test rows never set a weight.</span></div>
                <div><Check size={15} /><span>Only calibrated constituent probabilities are aggregated; otherwise probabilities are withheld.</span></div>
              </>
            ) : engine === "lstm" ? (
              <>
                <div><Check size={15} /><span>Backend: PyTorch {backends.lstm?.available ? `CPU available (${backends.lstm.version})` : "unavailable"}</span></div>
                <div><Check size={15} /><span>Input: chronological feature windows with explicit missing masks and training-only scaling.</span></div>
              </>
            ) : (
              <>
                <div><Check size={15} /><span>Backend: XGBoost {backends.xgboost?.available ? `available (${backends.xgboost.version})` : "unavailable"}</span></div>
                <div><Check size={15} /><span>Backend: LightGBM {backends.lightgbm?.available ? `available (${backends.lightgbm.version})` : "unavailable"}</span></div>
              </>
            )}
            <div><Check size={15} /><span>{engine === "ensemble" ? "Conformal intervals are validation-fitted and do not guarantee market movement." : "Probabilities are withheld; residual intervals are not guarantees."}</span></div>
          </div>
        </Card>
        <Card>
          <CardHeader title="How to read this" subtitle="A forecast is evidence, not an instruction" />
          <div className="contract-list">
            <div><Check size={15} /><span>PI-05 baselines remain the comparison floor; sophistication is not an edge.</span></div>
            <div><Check size={15} /><span>Feature importance is global; it does not explain every forecast.</span></div>
            <div><Check size={15} /><span>Walk-forward folds refit on expanding windows; their MAE range is shown separately from the single test block.</span></div>
            <div><Check size={15} /><span>No model is automatically promoted and none can place an order.</span></div>
            <div><Check size={15} /><span>Every gradient or LSTM forecast retains its artifact version and feature schema.</span></div>
          </div>
        </Card>
      </div>

      <Modal open={createOpen} onClose={() => setCreateOpen(false)} title="Generate a forecast" description="The backend uses only observations at or before the cutoff and refuses when the engine has no versioned model." footer={<><Button variant="ghost" onClick={() => setCreateOpen(false)}>Cancel</Button><Button variant="primary" loading={saving} disabled={!instrumentId} onClick={create}>Generate and store</Button></>}>
        <div className="form-grid">
          <Field label="Instrument"><SelectInput value={instrumentId} onChange={(event) => setInstrumentId(event.target.value)}><option value="">Choose an instrument…</option>{arrayFrom(instruments.data, "items").map((item) => <option key={item.instrument_id} value={item.instrument_id}>{item.symbol || item.instrument_id} · {item.name || item.asset_class}</option>)}</SelectInput></Field>
          <Field label="Horizon"><SelectInput value={forecastHorizon} onChange={(event) => setForecastHorizon(event.target.value)}>{FORECAST_HORIZONS.map((item) => <option key={item} value={item}>{item}</option>)}</SelectInput></Field>
        </div>
        <div className="inline-warning">{engine === "ensemble" ? "The ensemble runs only registered, validation-evidenced constituents for this instrument and horizon. It may abstain; a single eligible model is labelled as a fallback, never as a blend." : engine === "gradient" ? "Gradient forecasts require a trained, versioned model for this horizon; no family is silently substituted." : engine === "lstm" ? "LSTM forecasts require a trained, versioned sequence model for this horizon; no other engine is silently substituted." : "Statistical baselines do not emit calibrated probabilities; only intervals are shown."}</div>
      </Modal>

      <Modal open={trainOpen} onClose={() => setTrainOpen(false)} title={engine === "lstm" ? "Train and evaluate LSTM sequence models" : "Train and evaluate gradient models"} description="Chronological PI-02 splits and PI-04 features. Validation selects the configuration; the test window is reported once." footer={<><Button variant="ghost" onClick={() => setTrainOpen(false)}>Cancel</Button><Button variant="primary" loading={training} disabled={!trainInstrument} onClick={train}>Run evaluation</Button></>}>
        <div className="form-grid">
          <Field label="Instrument"><SelectInput value={trainInstrument} onChange={(event) => setTrainInstrument(event.target.value)}><option value="">Choose an instrument…</option>{arrayFrom(instruments.data, "items").map((item) => <option key={item.instrument_id} value={item.instrument_id}>{item.symbol || item.instrument_id} · {item.name || item.asset_class}</option>)}</SelectInput></Field>
          <Field label="Horizon"><SelectInput value={trainHorizon} onChange={(event) => setTrainHorizon(event.target.value)}>{FORECAST_HORIZONS.map((item) => <option key={item} value={item}>{item}</option>)}</SelectInput></Field>
        </div>
        <div className="inline-warning">{engine === "lstm" ? "This fits bounded CPU LSTM candidates with early stopping, checkpoint restoration and training-only preprocessing. It does not promote them or change any risk or execution setting." : "This fits two bounded families and their classifiers. It does not promote them or change any risk or execution setting."}</div>
      </Modal>

      <Modal open={!!selected} onClose={() => setSelected(null)} title={selected ? `${selected.instrument?.symbol || selected.instrument_id} · ${title(horizonOf(selected))}` : "Forecast"} description={selected ? `Model ${selected.model_id || "—"} · ${dateTime(selected.forecast_generation_timestamp || selected.generated_at)}` : ""} footer={<><Button variant="ghost" onClick={() => setSelected(null)}>Close</Button>{selected && /^(gbf_|lstm_|ens_|ensfb_)/.test(String(selected.forecast_id || "")) && <Button variant="secondary" loading={explaining} onClick={() => showExplanation(selected)}>Explain forecast</Button>}</>}>
        {selected && <div className="forecast-detail">
          <div className="forecast-band"><div><span>Lower</span><strong>{money(selected.lower_price)}</strong></div><div className="forecast-band-center"><span>Point estimate</span><strong>{money(selected.predicted_price)}</strong></div><div><span>Upper</span><strong>{money(selected.upper_price)}</strong></div></div>
          {selected.metadata?.phase === "PI-08" && (
            <div className="inline-warning">
              {selected.metadata?.fallback
                ? `Fallback to ${selected.metadata?.fallback_model_id} (${selected.metadata?.fallback_model_version}): ${selected.metadata?.fallback_reason}. This is not an ensemble blend.`
                : `Evidence-weighted ensemble · ${selected.metadata?.methodology_version || "PI-08"} · config ${shortId(selected.metadata?.config_id, 14)}`}
            </div>
          )}
          <div className="detail-grid"><div className="detail-main"><div className="fact-list">
            <div><span>Predicted return</span><strong>{pctText(selected.predicted_return)}</strong></div>
            <div><span>Direction</span><strong>{title(selected.predicted_direction || "uncertain")}</strong></div>
            <div><span>Probability up</span><strong>{selected.p_up == null ? "withheld" : pctUnsigned(num(selected.p_up, 0) * 100)}</strong></div>
            <div><span>Probability down</span><strong>{selected.p_down == null ? "withheld" : pctUnsigned(num(selected.p_down, 0) * 100)}</strong></div>
            <div><span>Uncertainty</span><strong>{title(selected.forecast_uncertainty || "—")}</strong></div>
            <div><span>Data quality</span><strong>{title(selected.data_quality || "—")}</strong></div>
          </div></div><aside className="detail-aside">
            <div className="detail-fact"><span>Interval width</span><strong>{pctText(selected.uncertainty_width)}</strong></div>
            <div className="detail-fact"><span>Target</span><strong>{dateTime(selected.forecast_target_timestamp || selected.target_at)}</strong></div>
            <div className="detail-fact"><span>Data observed</span><strong>{dateTime(selected.underlying_data_timestamp || selected.data_observed_at)}</strong></div>
            <div className="detail-fact"><span>Validity</span><StatusBadge value={selected.forecast_validity || "unknown"} /></div>
            <div className="detail-fact"><span>Forecast ID</span><code>{shortId(selected.forecast_id, 18)}</code></div>
            {selected.sequence_length != null && <div className="detail-fact"><span>Sequence length</span><strong>{selected.sequence_length} observations</strong></div>}
            {selected.metadata?.disagreement?.weighted_return_dispersion != null && <div className="detail-fact"><span>Disagreement</span><strong>{pctText(selected.metadata.disagreement.weighted_return_dispersion)}</strong></div>}
            {selected.metadata?.calibration?.status && <div className="detail-fact"><span>Calibration</span><strong>{title(selected.metadata.calibration.status)}</strong></div>}
          </aside></div>
          {(selected.metadata?.constituents || []).length > 0 && (
            <div className="detail-section">
              <h3>Model contribution</h3>
              <DataTable
                rows={selected.metadata.constituents}
                rowKey={(row: any) => `${row.model_id}-${row.version}`}
                columns={[
                  { key: "model", label: "Model", render: (row: any) => <span className="mono-small">{row.model_id} <small>v{row.version}</small></span> },
                  { key: "family", label: "Family", render: (row: any) => <Badge tone="neutral">{title(row.family || "—")}</Badge> },
                  { key: "weight", label: "Weight", render: (row: any) => pctText(row.weight) },
                  { key: "return", label: "Return", render: (row: any) => pctText(row.predicted_return) },
                ]}
              />
            </div>
          )}
        </div>}
      </Modal>

<Modal open={!!explain} onClose={() => setExplain(null)} title="Forecast explanation" description={explain ? `${explain.method} · ${explain.model_id} ${explain.model_version}` : ""} footer={<Button variant="secondary" onClick={() => setExplain(null)}>Close</Button>}>
        {explain && explain.engine === "ensemble" && <div className="detail-grid">
          <div className="detail-main"><div className="detail-section"><h3>Eligibility decisions</h3><div className="fact-list">
            {(explain.eligibility || []).map((item: any) => <div key={`${item.model_id}-${item.version}`}><span>{item.model_id} v{item.version}</span><strong>{item.eligible ? `included · ${pctText(item.weight)}` : "excluded"}</strong></div>)}
          </div></div></div>
          <aside className="detail-aside">
            <div className="detail-fact"><span>Config</span><code>{shortId(explain.config_id, 14)}</code></div>
            <div className="detail-fact"><span>Fallback</span><strong>{explain.fallback ? title(explain.fallback_reason || "yes") : "No"}</strong></div>
          </aside>
          <div className="detail-main"><ul className="contract-list">{(explain.limitations || []).map((item: string) => <li key={item}>{item}</li>)}</ul></div>
        </div>}
        {explain && explain.engine !== "ensemble" && <div className="detail-grid">
          <div className="detail-main">
            <div className="detail-section"><h3>{explain.feature_importance ? "Global sequence feature importance" : "Local contributions"}</h3><div className="fact-list">
              {explain.feature_importance
                ? (explain.feature_importance || []).slice(0, 10).map((item: any) => <div key={item.name}><span>{item.name}</span><strong>{measured(item.mae_increase, 6)}</strong></div>)
                : (explain.local_contributions || []).slice(0, 10).map((item: any) => <div key={item.feature}><span>{item.feature}</span><strong>{item.contribution == null ? "—" : `${num(item.contribution, 0) >= 0 ? "+" : ""}${num(item.contribution, 0).toFixed(6)}`}</strong></div>)}
            </div></div>
          </div>
          <aside className="detail-aside">
            <div className="detail-fact"><span>Global method</span><strong>{explain.feature_importance ? "Validation occlusion" : "Gain importance"}</strong></div>
            <div className="detail-fact"><span>{explain.feature_importance ? "Sequence length" : "Base value"}</span><strong>{explain.feature_importance ? explain.sequence_length ?? "—" : explain.base_value == null ? "—" : num(explain.base_value, 0).toFixed(6)}</strong></div>
            {!explain.feature_importance && <div className="detail-fact"><span>Prediction</span><strong>{pctText(explain.prediction)}</strong></div>}
          </aside>
          <div className="detail-main"><ul className="contract-list">{(explain.limitations || (explain.feature_importance ? ["Global importance is not a per-forecast attribution.", "Classifier probabilities are uncalibrated and withheld."] : [])).map((item: string) => <li key={item}>{item}</li>)}</ul></div>
        </div>}
      </Modal>
    </div>
  );
}
