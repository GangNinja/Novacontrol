import { useEffect, useMemo, useState } from "react";
import {
  Activity,
  AlertTriangle,
  Brain,
  FlaskConical,
  RefreshCw,
  ShieldCheck,
  TrendingUp,
} from "lucide-react";
import { apiPost } from "../api";
import { useApi, useDocumentTitle } from "../hooks";
import { arrayFrom, dateTime, num, pct, shortId, title } from "../lib";
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
  Metric,
  PageHeader,
  SelectInput,
  StatusBadge,
  TextInput,
  useToast,
} from "../components/ui";

function reassessmentWindow(row: any) {
  const folds: any[] = row?.result?.folds || [];
  const windows: any[] = folds.length
    ? folds
    : row?.config?.windows?.length
      ? row.config.windows
      : row?.config?.start
        ? [row.config]
        : [];
  if (!windows.length) return "—";
  const first = windows[0];
  const last = windows[windows.length - 1];
  const range = `${String(first.start || "").slice(0, 10)} → ${String(last.end || "").slice(0, 10)}`;
  return windows.length > 1 ? `${range} (${windows.length} walk-forward windows)` : range;
}

function reassessmentEvidence(row: any) {
  const folds: any[] = row?.result?.folds || [];
  if (folds.length) {
    const sufficient = folds.filter((fold: any) => fold.evaluation?.sufficient).length;
    return `${sufficient}/${folds.length} folds sufficient`;
  }
  const result = row?.result || {};
  if (result.sample_size === undefined) return "—";
  return `${result.sample_size} transitions · ${result.sufficient ? "sufficient" : "below the minimum"}`;
}

function EvidenceScope({ row }: { row: any }) {
  if (row?.out_of_sample === true) return <Badge tone="good">out-of-sample</Badge>;
  if (row?.in_sample) return <Badge tone="warn">in-sample</Badge>;
  return <Badge tone="neutral">unverified</Badge>;
}

const REGIMES = [
  "strong_up",
  "weak_up",
  "strong_down",
  "weak_down",
  "sideways",
  "high_volatility",
  "low_volatility",
  "breakout",
  "breakdown",
  "potential_reversal",
  "uncertain",
];

const SAMPLE_REFERENCE = JSON.stringify(
  Array.from({ length: 20 }, (_, index) => ({
    timestamp: `2025-01-${String(index + 1).padStart(2, "0")}T00:00:00Z`,
    returns: 0.001,
    volatility: 0.01,
    volume: 100,
    features: { rsi_14: 50, return_20: 0.002 },
  })),
  null,
  2,
);

const SAMPLE_CURRENT = JSON.stringify(
  Array.from({ length: 20 }, (_, index) => ({
    timestamp: `2025-02-${String(index + 1).padStart(2, "0")}T00:00:00Z`,
    returns: 0.03,
    volatility: 0.05,
    volume: 240,
    features: { rsi_14: 78, return_20: 0.08 },
  })),
  null,
  2,
);

const SAMPLE_DEGRADATION = JSON.stringify(
  Array.from({ length: 10 }, (_, index) => ({
    timestamp: `2025-03-${String(index + 1).padStart(2, "0")}T00:00:00Z`,
    metrics: {
      return: index < 8 ? 0.01 : -0.03,
      max_drawdown: index < 8 ? 0.02 : 0.08,
      volatility: index < 8 ? 0.01 : 0.05,
      sharpe: index < 8 ? 1.2 : 0.4,
      transaction_costs: 100,
    },
    action_distribution: { buy: 4, hold: 5, sell: 1 },
  })),
  null,
  2,
);

function parseArray(value: string): any[] {
  const parsed = JSON.parse(value);
  if (!Array.isArray(parsed)) throw new Error("Expected a JSON array.");
  return parsed;
}

function tone(status: string): "good" | "warn" | "bad" | "neutral" | "info" {
  if (["suitable", "no_drift", "none", "normal_variation", "completed"].includes(status)) return "good";
  if (["insufficient_evidence", "stale_evaluation", "not_evaluated", "data_unavailable"].includes(status)) return "warn";
  if (["unsuitable", "persistent_degradation", "concept_drift", "both", "policy_unsuitable"].includes(status)) return "bad";
  return "info";
}

export function AdaptiveIntelligencePage() {
  useDocumentTitle("Adaptive Intelligence");
  const toast = useToast();
  const dashboard = useApi<any>("/market/adaptive/dashboard", { refreshMs: 30_000 });
  const status = useApi<any>("/market/adaptive/status", { refreshMs: 30_000 });
  const contracts = useApi<any>("/market/adaptive/contracts");
  const changes = useApi<any>("/market/adaptive/changes?limit=100", { refreshMs: 30_000 });
  const drift = useApi<any>("/market/adaptive/drift?limit=100", { refreshMs: 30_000 });
  const degradation = useApi<any>("/market/adaptive/degradation?limit=100", { refreshMs: 30_000 });
  const suitability = useApi<any>("/market/adaptive/suitability?limit=100", { refreshMs: 30_000 });
  const regimeEvaluations = useApi<any>("/market/adaptive/regime/evaluations?limit=100", { refreshMs: 30_000 });
  const recommendations = useApi<any>("/market/adaptive/recommendations?limit=100", { refreshMs: 30_000 });
  const retraining = useApi<any>("/market/adaptive/retraining?limit=100", { refreshMs: 30_000 });
  const reassessments = useApi<any>("/market/adaptive/reassessments?limit=100", { refreshMs: 30_000 });
  const policies = useApi<any>("/market/training/policies?limit=100");

  const [instrument, setInstrument] = useState("XNSE:ALPHA");
  const [policyId, setPolicyId] = useState("");
  const [regime, setRegime] = useState("strong_up");
  const [referenceJson, setReferenceJson] = useState(SAMPLE_REFERENCE);
  const [currentJson, setCurrentJson] = useState(SAMPLE_CURRENT);
  const [observationsJson, setObservationsJson] = useState(SAMPLE_DEGRADATION);
  const [busy, setBusy] = useState("");
  const [latestSuitability, setLatestSuitability] = useState<any>(null);
  const [latestDegradation, setLatestDegradation] = useState<any>(null);
  const [latestChange, setLatestChange] = useState<any>(null);
  const [latestDrift, setLatestDrift] = useState<any>(null);
  const [latestRecommendation, setLatestRecommendation] = useState<any>(null);
  const [trainingStart, setTrainingStart] = useState("");
  const [trainingEnd, setTrainingEnd] = useState("");
  const [reassessmentStart, setReassessmentStart] = useState("");
  const [reassessmentEnd, setReassessmentEnd] = useState("");
  const [allowInSample, setAllowInSample] = useState(false);
  const [latestReassessment, setLatestReassessment] = useState<any>(null);
  const [reassessmentError, setReassessmentError] = useState("");

  const policyRows = arrayFrom(policies.data, "policies");
  const suitabilityRows = arrayFrom(suitability.data, "items");
  const regimeEvaluationRows = arrayFrom(regimeEvaluations.data, "evaluations");
  const changeRows = arrayFrom(changes.data, "items");
  const driftRows = arrayFrom(drift.data, "items");
  const degradationRows = arrayFrom(degradation.data, "items");
  const recommendationRows = arrayFrom(recommendations.data, "items");
  const retrainingRows = arrayFrom(retraining.data, "items");
  const reassessmentRows = arrayFrom(reassessments.data, "items");
  const currentRegime = dashboard.data?.latest_regime?.analysis || dashboard.data?.latest_regime || null;
  const selectedPolicy = policyRows.find((row: any) => row.policy_id === policyId) || policyRows[0];

  useEffect(() => {
    if (!policyId && policyRows.length) setPolicyId(policyRows[0].policy_id);
  }, [policyId, policyRows]);

  const regimeSignals = useMemo(
    () => Object.entries(currentRegime?.evidence || {}).slice(0, 8),
    [currentRegime],
  );

  const run = async (key: string, action: () => Promise<void>) => {
    setBusy(key);
    try {
      await action();
    } catch (error: any) {
      toast.push(error?.detail || error?.message || "The research request was refused.", "error");
    } finally {
      setBusy("");
    }
  };

  const analyzeRegime = () =>
    run("regime", async () => {
      const result = await apiPost<any>("/market/adaptive/regime/analyze", { instrument_id: instrument });
      toast.push(
        result.status === "DATA_UNAVAILABLE"
          ? "No PI-04 regime is available yet; uncertainty was preserved."
          : `Regime analyzed: ${title(result.primary_regime || "uncertain")}.`,
        result.status === "DATA_UNAVAILABLE" ? "error" : "success",
      );
      dashboard.refresh();
    });

  const assess = () =>
    run("suitability", async () => {
      if (!selectedPolicy) throw new Error("Select a PI-15 experimental policy first.");
      const result = await apiPost<any>("/market/adaptive/suitability/assess", {
        policy_id: selectedPolicy.policy_id,
        regime,
      });
      setLatestSuitability(result.suitability);
      const suitabilityStatus = result.suitability?.status || "unknown";
      toast.push(
        suitabilityStatus === "not_evaluated"
          ? "No sufficient stored regime evaluation exists for this policy and regime yet."
          : `Suitability: ${title(suitabilityStatus)}.`,
        ["not_evaluated", "insufficient_evidence", "stale_evaluation"].includes(suitabilityStatus)
          ? "error"
          : "success",
      );
      suitability.refresh();
    });

  const detectChange = () =>
    run("change", async () => {
      const result = await apiPost<any>("/market/adaptive/changes/detect", {
        instrument_id: instrument,
        reference: parseArray(referenceJson),
        current: parseArray(currentJson),
      });
      setLatestChange(result.change);
      toast.push(`Market change: ${title(result.change?.status || "unknown")}.`, "success");
      changes.refresh();
    });

  const detectDrift = (kind: "data" | "concept") =>
    run("drift", async () => {
      const result = await apiPost<any>("/market/adaptive/drift/detect", {
        scope: instrument,
        kind,
        as_of: new Date().toISOString(),
        reference: parseArray(referenceJson),
        current: parseArray(currentJson),
      });
      setLatestDrift(result.drift);
      toast.push(`${title(kind)} drift: ${title(result.drift?.status || "unknown")}.`, "success");
      drift.refresh();
    });

  const createAndRunReassessment = () =>
    run("reassessment", async () => {
      setReassessmentError("");
      try {
        if (!selectedPolicy) throw new Error("Select a PI-15 experimental policy first.");
        if (!reassessmentStart || !reassessmentEnd) {
          throw new Error("Choose a historical reassessment window.");
        }
        const created = await apiPost<any>("/market/adaptive/reassessments", {
          policy_id: selectedPolicy.policy_id,
          regime,
          reason: "explicit workspace reassessment",
          start: `${reassessmentStart}T00:00:00Z`,
          end: `${reassessmentEnd}T23:59:59Z`,
          allow_in_sample: allowInSample || undefined,
        });
        const reassessmentId = created.reassessment?.reassessment_id;
        if (!reassessmentId) throw new Error("The reassessment request was refused.");
        setLatestReassessment(created.reassessment);
        const result = await apiPost<any>(
          `/market/adaptive/reassessments/${encodeURIComponent(reassessmentId)}/run`,
          {},
        );
        setLatestReassessment(result.reassessment || created.reassessment);
        const runStatus = result.reassessment?.status || "unknown";
        toast.push(
          result.ok
            ? `Reassessment ${title(runStatus)}; no policy was trained or activated.`
            : result.reason || "Reassessment failed.",
          result.ok ? "success" : "error",
        );
      } catch (error: any) {
        setReassessmentError(
          error?.detail || error?.message || "The reassessment was refused.",
        );
        throw error;
      } finally {
        reassessments.refresh();
        regimeEvaluations.refresh();
        status.refresh();
      }
    });

  const monitorDegradation = () =>
    run("degradation", async () => {
      if (!selectedPolicy) throw new Error("Select a PI-15 experimental policy first.");
      const observations = parseArray(observationsJson);
      const result = await apiPost<any>("/market/adaptive/degradation/monitor", {
        policy_id: selectedPolicy.policy_id,
        observations,
      });
      setLatestDegradation(result.degradation);
      toast.push(`Degradation: ${title(result.degradation?.status || "unknown")}.`, "success");
      degradation.refresh();
    });

  const generateRecommendation = () =>
    run("recommendation", async () => {
      if (!selectedPolicy) throw new Error("Select a PI-15 experimental policy first.");
      const result = await apiPost<any>("/market/adaptive/recommendations", {
        policy_id: selectedPolicy.policy_id,
        scope: instrument,
        suitability: latestSuitability,
        degradation: latestDegradation,
        drift: latestDrift,
        changes: latestChange ? [latestChange] : [],
      });
      setLatestRecommendation(result.recommendation);
      toast.push(`Recommendation: ${title(result.recommendation?.action || "unknown")}.`, "success");
      recommendations.refresh();
    });

  const requestRetraining = () =>
    run("retraining", async () => {
      if (!latestRecommendation) throw new Error("Generate an adaptation recommendation first.");
      if (!trainingStart || !trainingEnd) throw new Error("Choose a historical training window.");
      const result = await apiPost<any>("/market/adaptive/retraining", {
        policy_id: latestRecommendation.affected_policy,
        recommendation_id: latestRecommendation.recommendation_id,
        training_start: `${trainingStart}T00:00:00Z`,
        training_end: `${trainingEnd}T23:59:59Z`,
        required_regimes: [regime],
      });
      toast.push("Retraining recommendation recorded; no training was started.", "success");
      retraining.refresh();
      void result;
    });

  return (
    <div className="page">
      <PageHeader
        eyebrow="Research / Adaptive intelligence"
        title={<>Market regimes, <em>evidence and drift.</em></>}
        description="Assess experimental policy suitability across PI-04 regimes, detect meaningful market and concept drift, and record advisory reassessment or retraining recommendations. Nothing here activates a policy, changes autonomy, or reaches NM-08/NM-10."
        meta={
          <>
            <span className="meta-item"><Brain size={13} /> {contracts.data?.contract_version || "PI-16"}</span>
            <span className="meta-item"><ShieldCheck size={13} /> advisory research only</span>
            <span className="meta-item"><Activity size={13} /> {status.data?.counts?.recommendations ?? 0} recommendation(s)</span>
          </>
        }
        actions={
          <Button
            variant="ghost"
            icon={<RefreshCw size={14} />}
            onClick={() => {
              dashboard.refresh();
              changes.refresh();
              drift.refresh();
              degradation.refresh();
              suitability.refresh();
              regimeEvaluations.refresh();
              recommendations.refresh();
              retraining.refresh();
              reassessments.refresh();
            }}
          >
            Refresh
          </Button>
        }
      />
      <Banner tone="info" title="Experimental research intelligence">
        Suitability and adaptation outputs are evidence summaries, not production authorization. A SUITABLE result is not
        permission to trade, and no recommendation can activate an experimental policy.
      </Banner>
      {status.error && <ErrorBlock message={status.error.detail} onRetry={status.refresh} />}
      <div className="metric-grid metric-grid-four">
        <Metric label="Regime evaluations" value={status.data?.counts?.regime_evaluations ?? 0} help="PI-14 EVIDENCE" />
        <Metric label="Drift events" value={status.data?.counts?.drift ?? 0} help="FUTURE OUTCOMES EXCLUDED" tone="warn" />
        <Metric label="Degradation alerts" value={status.data?.counts?.degradation ?? 0} help="PERSISTENCE-GATED" tone="warn" />
        <Metric label="Production authority" value={status.data?.production_authority ? "unexpected" : "none"} help="ADVISORY ONLY" tone="good" />
      </div>

      <div className="dashboard-grid">
        <div className="stack">
          <Card>
            <CardHeader
              title="Market regime dashboard"
              subtitle="PI-04 primary and secondary classifications, confidence and supporting evidence"
              action={<StatusBadge value={currentRegime?.primary_regime || "uncertain"} />}
            />
            <div className="row-actions">
              <TextInput value={instrument} onChange={(event) => setInstrument(event.target.value.toUpperCase())} placeholder="Instrument" />
              <Button variant="primary" icon={<TrendingUp size={14} />} onClick={analyzeRegime} loading={busy === "regime"}>
                Analyze regime
              </Button>
            </div>
            {currentRegime ? (
              <>
                <div className="fact-list">
                  <div><span>Primary classification</span><strong>{title(currentRegime.primary_regime || "uncertain")}</strong></div>
                  <div><span>Secondary conditions</span><strong>{currentRegime.secondary_conditions?.map(title).join(" · ") || "none"}</strong></div>
                  <div><span>Confidence</span><strong>{currentRegime.confidence == null ? "not calibrated / unavailable" : pct(num(currentRegime.confidence) * 100)}</strong></div>
                  <div><span>Detection timestamp</span><strong>{dateTime(currentRegime.source_timestamp || currentRegime.as_of)}</strong></div>
                  <div><span>Source / version</span><strong>{currentRegime.source || "PI-04"} · {currentRegime.regime_version || currentRegime.method_version || "—"}</strong></div>
                  <div><span>Quality</span><strong>{title(currentRegime.quality || "partial")}</strong></div>
                </div>
                {regimeSignals.length > 0 && (
                  <DataTable
                    compact
                    rows={regimeSignals}
                    rowKey={(row: any) => row[0]}
                    columns={[
                      { key: "feature", label: "Supporting feature", render: (row: any) => title(String(row[0])) },
                      { key: "value", label: "Value", render: (row: any) => num(row[1]).toFixed(4) },
                    ]}
                  />
                )}
              </>
            ) : (
              <EmptyState title="No regime analysis yet" description="Run a PI-04 regime analysis for an instrument with stored feature evidence." />
            )}
          </Card>

          <Card>
            <CardHeader title="Stored regime evaluations" subtitle="Regime-specific PI-14/PI-15 evidence used by suitability" action={<Badge tone="neutral">{regimeEvaluationRows.length}</Badge>} />
            <DataTable
              compact
              rows={regimeEvaluationRows}
              rowKey={(row: any) => row.regime_evaluation_id}
              empty={<EmptyState title="No regime evaluations" description="Record an evaluation from stored evidence or run an explicit reassessment." />}
              columns={[
                { key: "policy", label: "Policy", render: (row: any) => shortId(row.policy_id, 16) },
                { key: "regime", label: "Regime", render: (row: any) => title(row.regime) },
                { key: "sample", label: "Sample", render: (row: any) => row.sample_size },
                { key: "sufficient", label: "Evidence", render: (row: any) => row.sufficient ? "sufficient" : "insufficient" },
                { key: "scope", label: "Scope", render: (row: any) => <EvidenceScope row={row} /> },
                { key: "at", label: "Evaluated", render: (row: any) => dateTime(row.evaluated_at) },
              ]}
            />
          </Card>

          <Card>
            <CardHeader title="Policy suitability" subtitle="Reuses the latest sufficient stored PI-14 regime evaluation; no manual metrics or fresh timestamp" action={<Badge tone="warn">research only</Badge>} />
            <div className="form-grid">
              <Field label="Experimental policy">
                <SelectInput value={selectedPolicy?.policy_id || ""} onChange={(event) => setPolicyId(event.target.value)}>
                  {policyRows.map((row: any) => <option key={row.policy_id} value={row.policy_id}>{row.name} · {row.status}</option>)}
                </SelectInput>
              </Field>
              <Field label="Regime">
                <SelectInput value={regime} onChange={(event) => setRegime(event.target.value)}>
                  {REGIMES.map((item) => <option key={item} value={item}>{title(item)}</option>)}
                </SelectInput>
              </Field>
            </div>
            <div className="row-actions">
              <Button variant="primary" icon={<FlaskConical size={14} />} onClick={assess} loading={busy === "suitability"} disabled={!selectedPolicy}>
                Assess suitability
              </Button>
              {latestSuitability && <StatusBadge value={latestSuitability.status} />}
            </div>
            {latestSuitability && (
              <div className="fact-list">
                <div><span>Status</span><strong>{title(latestSuitability.status)}</strong></div>
                <div><span>Score</span><strong>{latestSuitability.suitability_score == null ? "—" : num(latestSuitability.suitability_score).toFixed(4)}</strong></div>
                <div><span>Sample</span><strong>{latestSuitability.sample_size} / minimum {latestSuitability.minimum_sample}</strong></div>
                <div><span>Hard flags</span><strong>{latestSuitability.hard_flags?.join(" · ") || "none"}</strong></div>
                <div><span>Limitations</span><strong>{latestSuitability.limitations?.join(" · ") || "none recorded"}</strong></div>
              </div>
            )}
            <DataTable
              compact
              rows={suitabilityRows}
              rowKey={(row: any) => row.assessment_id}
              empty={<EmptyState title="No suitability assessments" description="Assess a policy against a regime to create evidence." />}
              columns={[
                { key: "policy", label: "Policy", render: (row: any) => shortId(row.policy_id, 16) },
                { key: "regime", label: "Regime", render: (row: any) => title(row.regime) },
                { key: "status", label: "Status", render: (row: any) => <StatusBadge value={row.status} /> },
                { key: "score", label: "Score", render: (row: any) => row.suitability_score == null ? "—" : num(row.suitability_score).toFixed(3) },
                { key: "sample", label: "Sample", render: (row: any) => row.sample_size },
                { key: "at", label: "Evaluated", render: (row: any) => dateTime(row.evaluated_at) },
              ]}
            />
          </Card>

          <Card>
            <CardHeader title="Market-condition and drift monitoring" subtitle="Normal variation is separated from persistent change; no future outcome enters drift" action={<Badge tone="warn">PSI · KS · JS · PH · CUSUM</Badge>} />
            <div className="form-grid">
              <Field label="Reference observations (JSON)"><textarea className="input" rows={8} value={referenceJson} onChange={(event) => setReferenceJson(event.target.value)} /></Field>
              <Field label="Current observations (JSON)"><textarea className="input" rows={8} value={currentJson} onChange={(event) => setCurrentJson(event.target.value)} /></Field>
            </div>
            <div className="row-actions">
              <Button variant="secondary" onClick={detectChange} loading={busy === "change"}>Detect market change</Button>
              <Button variant="secondary" onClick={() => detectDrift("data")} loading={busy === "drift"}>Detect data drift</Button>
              <Button variant="secondary" onClick={() => detectDrift("concept")} loading={busy === "drift"}>Detect concept drift</Button>
            </div>
            {latestChange && (
              <div className="fact-list">
                <div><span>Change status</span><strong>{title(latestChange.status)}</strong></div>
                <div><span>Signals</span><strong>{latestChange.signals?.map((item: any) => title(item.name)).join(" · ") || "none"}</strong></div>
                <div><span>Evidence strength</span><strong>{title(latestChange.evidence_strength)}</strong></div>
              </div>
            )}
            {latestDrift && (
              <div className="fact-list">
                <div><span>Drift status</span><strong>{title(latestDrift.status)}</strong></div>
                <div><span>Affected features</span><strong>{latestDrift.affected_features?.join(" · ") || "none"}</strong></div>
                <div><span>Detection method</span><strong>PSI / KS / Jensen-Shannon</strong></div>
              </div>
            )}
            <DataTable
              compact
              rows={driftRows}
              rowKey={(row: any) => row.drift_id}
              empty={<EmptyState title="No drift results" description="Run a data or concept drift detection over two observation windows." />}
              columns={[
                { key: "scope", label: "Scope", render: (row: any) => row.scope },
                { key: "status", label: "Status", render: (row: any) => <StatusBadge value={row.status} /> },
                { key: "features", label: "Features", render: (row: any) => row.affected_features?.join(", ") || "—" },
                { key: "at", label: "Detected", render: (row: any) => dateTime(row.detected_at) },
              ]}
            />
          </Card>

          <Card>
            <CardHeader title="Policy degradation" subtitle="Persistence-gated rolling metrics; one losing window is not degradation" action={<Badge tone="warn">{contracts.data?.thresholds?.degradation_persistence_windows ?? 2} windows</Badge>} />
            <Field label="Observation windows (JSON array)">
              <textarea className="input" rows={8} value={observationsJson} onChange={(event) => setObservationsJson(event.target.value)} />
            </Field>
            <div className="row-actions">
              <Button variant="primary" onClick={monitorDegradation} loading={busy === "degradation"} disabled={!selectedPolicy}>
                Monitor degradation
              </Button>
            </div>
            {latestDegradation && (
              <div className="fact-list">
                <div><span>Status</span><strong>{title(latestDegradation.status)}</strong></div>
                <div><span>Triggers</span><strong>{latestDegradation.triggers?.map((item: any) => title(item.name)).join(" · ") || "none"}</strong></div>
                <div><span>Evidence strength</span><strong>{title(latestDegradation.evidence_strength)}</strong></div>
              </div>
            )}
            <DataTable
              compact
              rows={degradationRows}
              rowKey={(row: any) => row.degradation_id}
              empty={<EmptyState title="No degradation assessments" description="Provide at least the configured minimum of rolling observation windows." />}
              columns={[
                { key: "policy", label: "Policy", render: (row: any) => shortId(row.policy_id, 16) },
                { key: "status", label: "Status", render: (row: any) => <StatusBadge value={row.status} /> },
                { key: "triggers", label: "Triggers", render: (row: any) => row.triggers?.map((item: any) => item.name).join(", ") || "—" },
                { key: "at", label: "Detected", render: (row: any) => dateTime(row.detected_at) },
              ]}
            />
          </Card>
        </div>

        <div className="stack">
          <Card>
            <CardHeader title="Adaptation recommendations" subtitle="Advisory actions with evidence, cooldown and hysteresis" action={<Badge tone="warn">never executes</Badge>} />
            <Button variant="primary" icon={<AlertTriangle size={14} />} onClick={generateRecommendation} loading={busy === "recommendation"} disabled={!selectedPolicy}>
              Generate recommendation
            </Button>
            {latestRecommendation && (
              <div className="fact-list">
                <div><span>Recommendation</span><strong>{title(latestRecommendation.action)}</strong></div>
                <div><span>Next step</span><strong>{latestRecommendation.recommended_next_action}</strong></div>
                <div><span>Evidence strength</span><strong>{title(latestRecommendation.evidence_strength)}</strong></div>
                <div><span>Hysteresis</span><strong>{latestRecommendation.hysteresis?.suppressed ? "suppressed a low-evidence switch" : "no suppression"}</strong></div>
                <div><span>Policy activation</span><strong>unavailable</strong></div>
              </div>
            )}
            <DataTable
              compact
              rows={recommendationRows}
              rowKey={(row: any) => row.recommendation_id}
              empty={<EmptyState title="No recommendations" description="Generate one after suitability, drift or degradation evidence exists." />}
              columns={[
                { key: "action", label: "Action", render: (row: any) => <Badge tone={tone(row.action)}>{title(row.action)}</Badge> },
                { key: "policy", label: "Policy", render: (row: any) => shortId(row.affected_policy, 16) },
                { key: "strength", label: "Evidence", render: (row: any) => title(row.evidence_strength) },
                { key: "status", label: "Status", render: (row: any) => <StatusBadge value={row.status} /> },
                { key: "at", label: "Detected", render: (row: any) => dateTime(row.detected_at) },
              ]}
            />
          </Card>

          <Card>
            <CardHeader title="Retraining recommendations" subtitle="A suggested window and validation conditions; training stays an explicit operator action" action={<Badge tone="warn">not started</Badge>} />
            <div className="form-grid">
              <Field label="Training window start"><TextInput type="date" value={trainingStart} onChange={(event) => setTrainingStart(event.target.value)} /></Field>
              <Field label="Training window end"><TextInput type="date" value={trainingEnd} onChange={(event) => setTrainingEnd(event.target.value)} /></Field>
            </div>
            <Button variant="secondary" onClick={requestRetraining} loading={busy === "retraining"} disabled={!latestRecommendation}>
              Record retraining recommendation
            </Button>
            <DataTable
              compact
              rows={retrainingRows}
              rowKey={(row: any) => row.retraining_id}
              empty={<EmptyState title="No retraining recommendations" description="Generate one from a supported adaptation recommendation." />}
              columns={[
                { key: "policy", label: "Policy", render: (row: any) => shortId(row.policy_id, 16) },
                { key: "reason", label: "Reason", render: (row: any) => row.reason },
                { key: "start", label: "Window", render: (row: any) => `${String(row.suggested_training_window?.start || "").slice(0, 10)} → ${String(row.suggested_training_window?.end || "").slice(0, 10)}` },
                { key: "regimes", label: "Regimes", render: (row: any) => row.required_regime_coverage?.map(title).join(", ") || "—" },
                { key: "started", label: "Started", render: (row: any) => row.training_started ? "unexpected" : "no" },
              ]}
            />
          </Card>

          <Card>
            <CardHeader title="Reassessment history" subtitle="Explicit bounded PI-14 evaluations outside the policy's training and selection data; valid evidence is reused when versions match" action={<Badge tone="neutral">{reassessmentRows.length}</Badge>} />
            <div className="form-grid">
              <Field label="Reassessment start"><TextInput type="date" value={reassessmentStart} onChange={(event) => setReassessmentStart(event.target.value)} /></Field>
              <Field label="Reassessment end"><TextInput type="date" value={reassessmentEnd} onChange={(event) => setReassessmentEnd(event.target.value)} /></Field>
            </div>
            <label className="toggle-label">
              <input
                type="checkbox"
                checked={allowInSample}
                onChange={(event) => setAllowInSample(event.target.checked)}
              />
              Record even when the window is in-sample (flagged; never used by suitability)
            </label>
            <Button
              variant="secondary"
              onClick={createAndRunReassessment}
              loading={busy === "reassessment"}
              disabled={!selectedPolicy}
            >
              Create and run reassessment
            </Button>
            {reassessmentError && (
              <Banner tone="bad" title="Reassessment refused">
                {reassessmentError}
              </Banner>
            )}
            {latestReassessment && (
              <div className="fact-list">
                <div><span>Status</span><strong>{title(latestReassessment.status)}</strong></div>
                <div><span>Scope</span><strong><EvidenceScope row={latestReassessment} /></strong></div>
                <div><span>Window</span><strong>{reassessmentWindow(latestReassessment)}</strong></div>
                <div><span>Evidence</span><strong>{reassessmentEvidence(latestReassessment)}</strong></div>
                <div><span>Policy activation</span><strong>unavailable</strong></div>
              </div>
            )}
            <DataTable
              compact
              rows={reassessmentRows}
              rowKey={(row: any) => row.reassessment_id}
              empty={<EmptyState title="No reassessments" description="Choose a historical window and create an explicit bounded reassessment; no policy is trained or activated." />}
              columns={[
                { key: "policy", label: "Policy", render: (row: any) => shortId(row.policy_id, 16) },
                { key: "regime", label: "Regime", render: (row: any) => title(row.regime) },
                { key: "status", label: "Status", render: (row: any) => <StatusBadge value={row.status} /> },
                { key: "cached", label: "Reused", render: (row: any) => row.cached ? "yes" : "no" },
                { key: "scope", label: "Scope", render: (row: any) => <EvidenceScope row={row} /> },
                { key: "at", label: "Created", render: (row: any) => dateTime(row.created_at) },
              ]}
            />
          </Card>

          <Card>
            <CardHeader title="Detected changes" subtitle="Normal variation, temporary anomaly or potential/sustained change" action={<Badge tone="neutral">{changeRows.length}</Badge>} />
            <DataTable
              compact
              rows={changeRows}
              rowKey={(row: any) => row.change_id}
              empty={<EmptyState title="No change events" description="Detect a change over chronological reference and current windows." />}
              columns={[
                { key: "instrument", label: "Instrument", render: (row: any) => row.instrument_id },
                { key: "status", label: "Status", render: (row: any) => <StatusBadge value={row.status} /> },
                { key: "signals", label: "Signals", render: (row: any) => row.signal_count },
                { key: "at", label: "Detected", render: (row: any) => dateTime(row.detected_at) },
              ]}
            />
          </Card>
        </div>
      </div>
    </div>
  );
}
