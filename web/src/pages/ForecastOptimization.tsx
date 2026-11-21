import { useState } from "react";
import type { ReactNode } from "react";
import {
  Activity,
  BadgeCheck,
  BarChart3,
  GitCompareArrows,
  Layers3,
  RefreshCw,
  ShieldQuestion,
  SlidersHorizontal,
} from "lucide-react";
import { apiPost, LOCAL_ADMIN_PROXY } from "../api";
import { useApi, useDocumentTitle } from "../hooks";
import { arrayFrom, dateTime, num, title } from "../lib";
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

const BASE = "/market/forecast-intelligence";

const VIEWS = [
  { id: "overview", label: "Performance", icon: BarChart3 },
  { id: "comparison", label: "Comparison", icon: GitCompareArrows },
  { id: "horizons", label: "Horizons", icon: Layers3 },
  { id: "reliability", label: "Reliability", icon: BadgeCheck },
  { id: "calibration", label: "Calibration", icon: SlidersHorizontal },
  { id: "disagreement", label: "Disagreement", icon: GitCompareArrows },
  { id: "drift", label: "Drift", icon: Activity },
  { id: "selection", label: "Selection", icon: ShieldQuestion },
  { id: "fallback", label: "Fallback", icon: ShieldQuestion },
] as const;

type View = (typeof VIEWS)[number]["id"];

const TASKS = [
  "return_regression",
  "price_regression",
  "direction_classification",
  "probability_estimation",
  "interval_estimation",
] as const;

function tone(value: unknown): "good" | "warn" | "bad" | "neutral" {
  const level = String(value || "").toLowerCase();
  if (["high_reliability", "selected", "calibrated", "aligned", "healthy"].includes(level)) return "good";
  if (["moderate_reliability", "conditionally_selected", "drift_unconfirmed", "watch", "warning"].includes(level)) return "warn";
  if (["low_reliability", "unreliable", "very_high", "detected", "degraded", "failed"].includes(level)) return "bad";
  if (["insufficient_data", "insufficient_evidence", "unavailable", "stale_data", "abstained"].includes(level)) return "warn";
  return "neutral";
}

function methodLabel(value: unknown): string {
  const text = String(value || "").trim();
  if (!text) return "—";
  if (text.toLowerCase() === "psi+ks") return "PSI + KS";
  return title(text);
}

function number(value: unknown, digits = 4): string {
  if (value === null || value === undefined || value === "") return "—";
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed.toFixed(digits) : "—";
}

function pct(value: unknown, digits = 2): string {
  if (value === null || value === undefined || value === "") return "—";
  const parsed = Number(value);
  return Number.isFinite(parsed) ? `${(parsed * 100).toFixed(digits)}%` : "—";
}

function countFor(counts: any, kind: string): number {
  return Number(counts?.by_kind?.[kind] ?? counts?.[kind] ?? 0);
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

function Table({ rows, columns, empty }: { rows: any[]; columns: Column<any>[]; empty: string }) {
  return (
    <DataTable
      rows={rows}
      columns={columns}
      rowKey={(row, index) =>
        String(
          row.recommendation_id ||
            row.selection_id ||
            row.fallback_id ||
            row.consistency_id ||
            row.calibration_id ||
            row.assessment_id ||
            row.reliability_id ||
            row.drift_id ||
            row.degradation_id ||
            row.model_id ||
            row.forecast_id ||
            index,
        )
      }
      empty={<EmptyState title="No evidence" description={empty} />}
    />
  );
}

function modelColumns(showTest = true): Column<any>[] {
  const columns: Column<any>[] = [
    { key: "model_id", label: "Model", render: (row) => <strong>{row.model_id || "—"}</strong> },
    { key: "version", label: "Version", render: (row) => row.version || "—" },
    { key: "family", label: "Family", render: (row) => title(row.family) },
    { key: "status", label: "Evidence", render: (row) => {
      // Registry rows describe a model's `state` (e.g. eligible); evaluation
      // rows carry `status`. Neither may render as an empty badge.
      const evidence = row.status || row.state || "";
      return evidence ? <Badge tone={tone(evidence)}>{title(evidence)}</Badge> : "—";
    } },
    { key: "samples", label: "Samples", align: "right", render: (row) => row.samples ?? row.validation_samples ?? "—" },
    { key: "mae", label: "Validation MAE", align: "right", render: (row) => number(row.selection_metrics?.mae ?? row.metrics?.mae) },
    { key: "accuracy", label: "Direction", align: "right", render: (row) => pct(row.metrics?.directional_accuracy ?? row.selection_metrics?.directional_accuracy) },
    { key: "coverage", label: "Coverage", align: "right", render: (row) => pct(row.metrics?.interval_coverage ?? row.selection_metrics?.interval_coverage) },
    { key: "ece", label: "Calibration error", align: "right", render: (row) => number(row.metrics?.calibration_error ?? row.selection_metrics?.calibration_error) },
    { key: "suitability", label: "Suitability", render: (row) => <Badge tone={tone(row.suitability?.suitability)}>{title(row.suitability?.suitability || (showTest ? "unavailable" : "not_assessed"))}</Badge> },
  ];
  if (showTest) {
    columns.push({
      key: "test_mae",
      label: "Test MAE (monitoring)",
      align: "right",
      render: (row) => number(row.performance_metrics?.mae ?? row.test?.mae),
    });
  }
  return columns;
}

export function ForecastOptimizationPage() {
  useDocumentTitle("Forecast Optimization");
  const toast = useToast();
  const [view, setView] = useState<View>("overview");
  const [instrument, setInstrument] = useState("");
  const [horizon, setHorizon] = useState("5d");
  const [task, setTask] = useState<string>("return_regression");
  const [operatorToken, setOperatorToken] = useState("");
  const [busy, setBusy] = useState("");
  const [revision, setRevision] = useState(0);
  // Left empty on purpose: prefilled synthetic distributions produced a
  // confident "high drift" result that was never evidence from this system.
  const [referenceValues, setReferenceValues] = useState("");
  const [currentValues, setCurrentValues] = useState("");

  const status = useApi<any>(`${BASE}/status`, { refreshMs: 30_000, refreshKey: revision });
  const config = useApi<any>(`${BASE}/config`, { refreshKey: revision });
  const registry = useApi<any>(
    instrument.trim() ? `${BASE}/registry?instrument_id=${encodeURIComponent(instrument.trim())}&horizon=${horizon}` : null,
    { refreshKey: revision },
  );
  const overview = useApi<any>(
    instrument.trim() ? `${BASE}/overview?instrument_id=${encodeURIComponent(instrument.trim())}&horizon=${horizon}` : null,
    { refreshKey: revision },
  );
  const comparison = useApi<any>(
    instrument.trim() && view === "comparison"
      ? `${BASE}/comparison/${encodeURIComponent(instrument.trim())}?horizon=${horizon}&task=${task}`
      : null,
    { refreshKey: revision },
  );
  const horizons = useApi<any>(
    instrument.trim() && view === "horizons"
      ? `${BASE}/horizons/${encodeURIComponent(instrument.trim())}?task=${task}`
      : null,
    { refreshKey: revision },
  );
  const history = useApi<any>(
    instrument.trim() && view === "selection"
      ? `${BASE}/history?instrument_id=${encodeURIComponent(instrument.trim())}&horizon=${horizon}&kind=selection&limit=100`
      : null,
    { refreshKey: revision },
  );

  const [results, setResults] = useState<Record<string, any>>({});
  const state = status.data || {};
  const evaluation = overview.data?.evaluation || {};
  const models = arrayFrom(overview.data, "models");
  const registryRows = arrayFrom(registry.data, "models");
  const comparisonRows = arrayFrom(comparison.data, "models");
  const historyRows = arrayFrom(history.data, "records");
  const latest = state.latest || {};
  const policy = config.data?.config || {};

  async function run(action: string, path: string, body: Record<string, unknown> = {}) {
    if (!instrument.trim()) {
      toast.push("Enter an instrument id first, for example XNSE:RELIANCE.", "error");
      return;
    }
    setBusy(action);
    try {
      const response = await apiPost(
        `${BASE}/${path}`,
        { instrument_id: instrument.trim(), horizon, task, ...body },
        undefined,
        { operatorToken: operatorToken.trim() },
      );
      setResults((previous) => ({ ...previous, [action]: response }));
      toast.push(`${title(action)} recorded. No model, weight, risk decision or order was changed.`, "success");
      setRevision((value) => value + 1);
    } catch (error: any) {
      toast.push(
        `${title(action)} refused: ${error?.detail?.message || error?.detail || String(error?.message || error)}`,
        "error",
      );
    } finally {
      setBusy("");
    }
  }

  function renderView() {
    const result = results[view === "overview" ? "analyze" : view];
    if (view === "overview") {
      if (!instrument.trim()) {
        return (
          <Card>
            <EmptyState
              icon={<BarChart3 size={22} />}
              title="Enter an instrument to begin"
              description="Model evidence is scoped to one instrument and horizon. Type a MIC-qualified symbol such as XBOM:ADANIENT above; nothing is evaluated until you do."
            />
          </Card>
        );
      }
      if (overview.loading) return <LoadingBlock label="Reading chronological forecast evidence" />;
      if (overview.error) return <ErrorBlock message={overview.error.message} onRetry={overview.refresh} />;
      return (
        <div className="stack">
          <div className="metric-grid">
            <Metric label="Evidence status" value={title(evaluation.data_status || "unavailable")} />
            <Metric label="Models" value={evaluation.model_count ?? models.length} />
            <Metric label="Evaluations" value={countFor(state.counts, "evaluation")} />
            <Metric label="Selection evidence" value="Validation only" />
          </div>
          <Banner tone="info" title="Advisory optimization">
            PI-23 reads existing PI-05 to PI-11 evidence. Test scorecards are monitoring evidence only and never
            choose a model. Missing or stale evidence is reported, never treated as zero error.
          </Banner>
          <Card>
            <CardHeader title="Model performance" subtitle="Chronological evaluation with validation-only selection evidence." />
            <Table rows={models} columns={modelColumns()} empty="Enter an instrument to read existing model evidence." />
          </Card>
          <Card>
            <CardHeader title="Full analysis" subtitle="Run evaluation, selection, calibration, drift, reliability, weights and fallback together." />
            <Button variant="primary" icon={<Activity size={15} />} loading={busy === "analyze"} onClick={() => run("analyze", "analyze")}>
              Run full analysis
            </Button>
          </Card>
          <Card>
            <CardHeader title="Model evidence registry" subtitle="Known models for this scope; availability is not quality." />
            <Table rows={registryRows} columns={modelColumns(false)} empty="No registry entries exist for this scope." />
          </Card>
        </div>
      );
    }
    if (view === "comparison") {
      if (comparison.loading) return <LoadingBlock label="Comparing eligible models" />;
      if (comparison.error) return <ErrorBlock message={comparison.error.message} onRetry={comparison.refresh} />;
      return (
        <Card>
          <CardHeader title="Model comparison" subtitle="One instrument, horizon and task. Selection ranks validation evidence only." />
          <Table rows={comparisonRows} columns={modelColumns()} empty="No compatible model evidence was found." />
        </Card>
      );
    }
    if (view === "horizons") {
      if (horizons.loading) return <LoadingBlock label="Evaluating multi-horizon evidence" />;
      if (horizons.error) return <ErrorBlock message={horizons.error.message} onRetry={horizons.refresh} />;
      const rows = arrayFrom(horizons.data, "horizons");
      return (
        <div className="stack">
          <Card>
            <CardHeader title="Horizon optimization" subtitle="Each horizon keeps its own objective; disagreement is not automatically a contradiction." />
            <Table
              rows={rows}
              columns={[
                { key: "horizon", label: "Horizon", render: (row) => <strong>{title(row.horizon)}</strong> },
                { key: "family", label: "Family", render: (row) => title(row.family) },
                { key: "best_model", label: "Best model", render: (row) => row.selection?.selected_model_id || row.selection?.selected_model?.model_id || "—" },
                { key: "suitability", label: "Selection", render: (row) => <Badge tone={tone(row.selection?.outcome)}>{title(row.selection?.outcome || row.status || "unavailable")}</Badge> },
                { key: "samples", label: "Samples", align: "right", render: (row) => row.samples ?? "—" },
                { key: "mae", label: "Validation MAE", align: "right", render: (row) => number(row.metrics?.mae) },
              ]}
              empty="No horizon evidence is available for this instrument."
            />
          </Card>
          <Card>
            <CardHeader title="Cross-horizon consistency" subtitle="Run a consistency assessment to compare current forecasts." />
            <Button variant="secondary" icon={<RefreshCw size={15} />} loading={busy === "consistency"} onClick={() => run("consistency", "consistency", { instrument_id: instrument.trim() })}>
              Assess consistency
            </Button>
            {results.consistency?.pairs && <Table rows={results.consistency.pairs} columns={[
              { key: "short_horizon", label: "Short" },
              { key: "long_horizon", label: "Long" },
              { key: "state", label: "State", render: (row) => <Badge tone={tone(row.state)}>{title(row.state)}</Badge> },
              { key: "interpretation", label: "Interpretation", render: (row) => <small className="muted">{row.interpretation}</small> },
            ]} empty="No horizon pair has compatible current forecasts." />}
            {results.consistency ? (
              <Facts rows={[
                { label: "Consistency status", value: <Badge tone={tone(results.consistency.status)}>{title(results.consistency.status)}</Badge> },
                { label: "Current pairs", value: Array.isArray(results.consistency.pairs) ? results.consistency.pairs.length : 0 },
                { label: "Interpretation", value: <small className="muted">{results.consistency.interpretation || "—"}</small> },
              ]} />
            ) : null}
          </Card>
        </div>
      );
    }
    if (view === "reliability") {
      return (
        <div className="stack">
          <Card>
            <CardHeader title="Reliability assessment" subtitle="Categorical evidence, never a probability or a composite score." />
            <Button variant="primary" icon={<BadgeCheck size={15} />} loading={busy === "reliability"} onClick={() => run("reliability", "reliability")}>
              Assess reliability
            </Button>
          </Card>
          {result?.assessments && (
            <Card>
              <CardHeader title="Model reliability" subtitle={result.policy_version} />
              <Table rows={result.assessments} columns={[
                { key: "model_id", label: "Model", render: (row) => <strong>{row.model_id}</strong> },
                { key: "classification", label: "Classification", render: (row) => <Badge tone={tone(row.classification)}>{title(row.classification)}</Badge> },
                { key: "health", label: "Health", render: (row) => <Badge tone={tone(row.health)}>{title(row.health)}</Badge> },
                { key: "drift_detected", label: "Drift", render: (row) => (row.drift_detected ? <Badge tone="warn">Detected</Badge> : "None") },
                { key: "sample_count", label: "Samples", align: "right" },
                { key: "calibration_status", label: "Calibration", render: (row) => title(row.calibration_status) },
              ]} empty="No models were assessable." />
            </Card>
          )}
        </div>
      );
    }
    if (view === "calibration") {
      return (
        <div className="stack">
          <Card>
            <CardHeader title="Forecast calibration" subtitle="Fit on the earliest chronological segment, evaluate once on a later holdout." />
            <Button variant="primary" icon={<SlidersHorizontal size={15} />} loading={busy === "calibration"} onClick={() => run("calibration", "calibration", { method: "auto" })}>
              Fit and evaluate calibration
            </Button>
          </Card>
          {result && (
            <Card>
              <CardHeader title="Calibration result" subtitle={result.calibration_version} />
              <Facts rows={[
                { label: "Status", value: <Badge tone={tone(result.status)}>{title(result.status)}</Badge> },
                { label: "Method", value: title(result.method) },
                { label: "Samples", value: result.samples },
                { label: "Fitted on", value: result.fitted_on || "—" },
                { label: "Evaluated on", value: result.evaluation_on || "—" },
                { label: "ECE improvement", value: number(result.ece_improvement) },
                { label: "Applied to forecasts", value: result.applied_to_existing_forecasts ? "Yes" : "No" },
              ]} />
              {result.before && (
                <Table rows={[{ ...result.before, split_label: "Before (holdout)" }, { ...result.after, split_label: "After (holdout)" }]} columns={[
                  { key: "split_label", label: "Split" },
                  { key: "brier", label: "Brier", align: "right", render: (row) => number(row.brier) },
                  { key: "calibration_error", label: "ECE", align: "right", render: (row) => number(row.calibration_error) },
                  { key: "samples", label: "Samples", align: "right" },
                ]} empty="No probability evidence" />
              )}
            </Card>
          )}
        </div>
      );
    }
    if (view === "disagreement") {
      return (
        <div className="stack">
          <Card>
            <CardHeader title="Model disagreement" subtitle="Compatible current forecasts only; stale and missing models are named." />
            <Button variant="primary" icon={<GitCompareArrows size={15} />} loading={busy === "disagreement"} onClick={() => run("disagreement", "disagreement")}>
              Measure disagreement
            </Button>
          </Card>
          {result && (
            <Card>
              <CardHeader title="Disagreement evidence" subtitle={`State: ${title(result.state)}`} />
              <div className="metric-grid">
                <Metric label="Models" value={result.participant_count ?? result.participants?.length ?? 0} />
                <Metric label="Dispersion" value={number(result.measurements?.return_dispersion)} />
                <Metric label="Direction split" value={pct(result.measurements?.direction_split, 0)} />
                <Metric label="Conservative action" value={title(result.recommended_action || "review")} />
              </div>
              {Array.isArray(result.participants) && (
                <Table rows={result.participants} columns={[
                  { key: "model_id", label: "Model", render: (row) => <strong>{row.model_id}</strong> },
                  { key: "predicted_return", label: "Predicted return", align: "right", render: (row) => pct(row.predicted_return) },
                  { key: "direction", label: "Direction", render: (row) => title(row.direction) },
                  { key: "target_at", label: "Target", render: (row) => dateTime(row.target_at) },
                ]} empty="No compatible forecasts" />
              )}
            </Card>
          )}
        </div>
      );
    }
    if (view === "drift") {
      return (
        <div className="stack">
          <Card>
            <CardHeader title="Drift and degradation" subtitle="Explicit feature distributions, or chronological residual behaviour when none are supplied." />
            <div className="form-grid">
              <Field label="Reference feature values" hint="Comma-separated numeric sample for the reference window.">
                <TextInput value={referenceValues} onChange={(event) => setReferenceValues(event.target.value)} />
              </Field>
              <Field label="Current feature values" hint="Comma-separated numeric sample for the current window.">
                <TextInput value={currentValues} onChange={(event) => setCurrentValues(event.target.value)} />
              </Field>
            </div>
            <div className="button-row">
              <Button
                variant="primary"
                icon={<Activity size={15} />}
                loading={busy === "drift"}
                onClick={() => {
                  const sample = (text: string) =>
                    text.split(",").map((value) => Number(value.trim())).filter((value) => Number.isFinite(value));
                  const body: Record<string, unknown> = {};
                  if (referenceValues.trim()) body.reference_features = { feature: sample(referenceValues) };
                  if (currentValues.trim()) body.current_features = { feature: sample(currentValues) };
                  run("drift", "drift", body);
                }}
              >
                Run drift assessment
              </Button>
              <Button variant="secondary" loading={busy === "degradation"} onClick={() => run("degradation", "degradation")}>
                Assess degradation
              </Button>
            </div>
          </Card>
          {result?.drift_id && (
            <Card>
              <CardHeader title="Drift result" subtitle={`Confirmed severity: ${title(result.severity || "none")} · per-window severity is listed below`} />
              <Facts rows={[
                { label: "Status", value: <Badge tone={tone(result.status)}>{title(result.status)}</Badge> },
                { label: "Detected (this window)", value: result.window_detected ? "Yes" : "No" },
                { label: "Confirmed (persisted)", value: result.detected ? "Yes" : "No" },
                { label: "Persistence", value: `${result.detected_windows ?? 0} of ${result.evidence?.persistence_required ?? "—"} consecutive windows` },
                { label: "Automatic deactivation", value: "No" },
                { label: "Evidence records", value: result.evidence?.records ?? 0 },
              ]} />
              {result.review ? <p className="muted">{result.review}</p> : null}
              <Table rows={arrayFrom(result, "events")} columns={[
                { key: "feature", label: "Feature", render: (row) => row.feature || "residual behaviour" },
                { key: "method", label: "Method", render: (row) => methodLabel(row.method) },
                { key: "statistic", label: "Statistic", align: "right", render: (row) => number(row.statistic ?? row.psi ?? row.ks_statistic) },
                { key: "severity", label: "Severity", render: (row) => <Badge tone={tone(row.severity)}>{title(row.severity)}</Badge> },
              ]} empty="No drift event was confirmed." />
            </Card>
          )}
          {results.degradation?.degradation_id && (
            <Card>
              <CardHeader title="Degradation result" subtitle={title(results.degradation.health || results.degradation.status)} />
              <Facts rows={[
                { label: "Health", value: <Badge tone={tone(results.degradation.health)}>{title(results.degradation.health)}</Badge> },
                { label: "Baseline", value: results.degradation.baseline_samples ?? "—" },
                { label: "Current", value: results.degradation.current_samples ?? "—" },
              ]} />
              {results.degradation.detail || results.degradation.reason ? (
                <p className="muted">{results.degradation.detail || results.degradation.reason}</p>
              ) : null}
            </Card>
          )}
        </div>
      );
    }
    if (view === "selection") {
      return (
        <div className="stack">
          <Card>
            <CardHeader title="Validation-only selection" subtitle="A recommendation, never a promotion or production weight change." />
            <Button variant="primary" icon={<ShieldQuestion size={15} />} loading={busy === "selection"} onClick={() => run("selection", "select")}>
              Recommend a model
            </Button>
          </Card>
          {result?.selection_id && (
            <Card>
              <CardHeader title="Selection result" subtitle={`${result.selection_policy_version} · ${title(result.outcome)}`} />
              <div className="metric-grid">
                <Metric label="Outcome" value={title(result.outcome)} />
                <Metric label="Selected" value={result.selected_model_id || "None"} />
                <Metric label="Samples" value={result.sample_count ?? 0} />
                <Metric label="Test used" value={result.test_used_for_selection ? "Yes" : "No"} />
              </div>
              <Table rows={arrayFrom(result, "alternatives")} columns={[
                { key: "model_id", label: "Model" },
                { key: "selection_score", label: "Validation score", align: "right", render: (row) => number(row.selection_score) },
                { key: "score_direction", label: "Ranking", render: (row) => title(row.score_direction) },
                { key: "samples", label: "Samples", align: "right" },
                { key: "guardrails_passed", label: "Guardrails", render: (row) => (row.guardrails_passed ? "Passed" : "Missed") },
              ]} empty="No eligible alternatives" />
            </Card>
          )}
          <Card>
            <CardHeader title="Selection history" subtitle="Append-only decisions with their evidence." />
            <Table rows={historyRows} columns={[
              { key: "created_at", label: "Created", render: (row) => dateTime(row.created_at) },
              { key: "horizon", label: "Horizon" },
              { key: "outcome", label: "Outcome", render: (row) => <Badge tone={tone(row.outcome)}>{title(row.outcome)}</Badge> },
              { key: "selected_model_id", label: "Selected", render: (row) => row.selected_model_id || "—" },
              { key: "sample_count", label: "Samples", align: "right" },
            ]} empty="No selection history for this scope." />
          </Card>
        </div>
      );
    }
    return (
      <div className="stack">
        <Card>
          <CardHeader title="Fallback and abstention" subtitle="Why a preferred forecast was or was not used. Abstention is a valid outcome." />
          <Button variant="primary" icon={<ShieldQuestion size={15} />} loading={busy === "fallback"} onClick={() => run("fallback", "fallback")}>
            Evaluate fallback
          </Button>
        </Card>
        {result?.fallback_id && (
          <Card>
            <CardHeader title="Fallback result" subtitle={title(result.outcome)} />
            <Facts rows={[
              { label: "Outcome", value: <Badge tone={tone(result.outcome)}>{title(result.outcome)}</Badge> },
              { label: "Selected", value: result.selected?.model_id || "None" },
              { label: "Abstained", value: result.abstained ? "Yes" : "No" },
              { label: "New forecast created", value: result.new_forecast_created ? "Yes" : "No" },
            ]} />
            {result.reasons?.length > 0 && (
              <ul className="list">
                {result.reasons.map((reason: string) => <li key={reason}>{reason}</li>)}
              </ul>
            )}
          </Card>
        )}
      </div>
    );
  }

  return (
    <div className="page">
      <PageHeader
        title="Forecast Optimization"
        description="Predictive intelligence and forecast optimization over existing PI-05 to PI-11 evidence. Advisory only; NM-08 owns risk and NM-10 owns paper execution."
        actions={
          <Button variant="secondary" icon={<RefreshCw size={15} />} onClick={() => setRevision((value) => value + 1)}>
            Refresh
          </Button>
        }
      />
      <div className="toolbar">
        <Field label="Instrument">
          <TextInput value={instrument} onChange={(event) => setInstrument(event.target.value)} placeholder="XNSE:RELIANCE" />
        </Field>
        <Field label="Horizon">
          <SelectInput value={horizon} onChange={(event) => setHorizon(event.target.value)}>
            {["5m", "15m", "20m", "1h", "1d", "3d", "5d", "30d", "90d"].map((item) => <option key={item} value={item}>{item}</option>)}
          </SelectInput>
        </Field>
        <Field label="Task">
          <SelectInput value={task} onChange={(event) => setTask(event.target.value)}>
            {TASKS.map((item) => <option key={item} value={item}>{title(item)}</option>)}
          </SelectInput>
        </Field>
        <Field label="Operator token" hint={LOCAL_ADMIN_PROXY ? "The local admin proxy already authorizes this session." : "Required for selection, calibration, evaluation and analysis. Sent only with this request."}>
          <TextInput type="password" value={operatorToken} onChange={(event) => setOperatorToken(event.target.value)} placeholder={LOCAL_ADMIN_PROXY ? "Local admin session active" : undefined} autoComplete="off" />
        </Field>
      </div>
      <div className="tabs" role="tablist" aria-label="Forecast optimization views">
        {VIEWS.map((item) => {
          const Icon = item.icon;
          return (
            <button
              key={item.id}
              type="button"
              role="tab"
              aria-selected={view === item.id}
              className={`tab ${view === item.id ? "tab-active" : ""}`}
              onClick={() => setView(item.id)}
            >
              <Icon size={15} /> {item.label}
            </button>
          );
        })}
      </div>
      <div className="layout-two-column">
        <div className="stack">
          {renderView()}
        </div>
        <div className="stack">
          <Card>
            <CardHeader title="Authority" subtitle="Bounded advisory surface." />
            {status.loading ? <LoadingBlock label="Reading PI-23 status" /> : status.error ? <ErrorBlock message={status.error.message} onRetry={status.refresh} /> : (
              <Facts rows={[
                { label: "Phase", value: state.phase || "PI-23" },
                { label: "Risk authority", value: state.authority?.risk_authority || "NM-08" },
                { label: "Execution authority", value: state.authority?.execution_authority || "NM-10" },
                { label: "Automatic promotion", value: state.authority?.automatic_promotion ? "Enabled" : "Disabled" },
                { label: "Production weight mutation", value: state.authority?.production_weight_mutation ? "Enabled" : "Disabled" },
                { label: "Paper only", value: state.authority?.paper_only ? "Yes" : "No" },
              ]} />
            )}
          </Card>
          <Card>
            <CardHeader title="Evidence" subtitle="Latest persisted records and thresholds." />
            <Facts rows={[
              { label: "Evaluations", value: countFor(state.counts, "evaluation") },
              { label: "Selections", value: countFor(state.counts, "selection") },
              { label: "Calibrations", value: countFor(state.counts, "calibration") },
              { label: "Drift assessments", value: countFor(state.counts, "drift") },
              { label: "Reliability", value: countFor(state.counts, "reliability") },
              { label: "Last selection", value: latest.selection?.created_at ? dateTime(latest.selection.created_at) : "—" },
              { label: "Min selection samples", value: policy.min_selection_samples ?? "—" },
              { label: "Drift PSI threshold", value: number(policy.drift_psi_threshold, 2) },
            ]} />
          </Card>
          {state.evidence_errors?.length > 0 && (
            <Banner tone="warn" title="Some evidence was unavailable">
              {state.evidence_errors.map((item: string) => <div key={item}>{item}</div>)}
            </Banner>
          )}
          <Banner tone="info" title="No financial mutation">
            This workspace never promotes a model, overwrites ensemble weights, creates a risk decision, submits an
            order or changes a ledger entry.
          </Banner>
        </div>
      </div>
    </div>
  );
}
