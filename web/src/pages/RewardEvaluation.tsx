import { useEffect, useMemo, useState } from "react";
import {
  Activity,
  BrainCircuit,
  FlaskConical,
  Play,
  RefreshCw,
  ShieldCheck,
  SlidersHorizontal,
  XCircle,
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
  LoadingBlock,
  Metric,
  Money,
  PageHeader,
  SelectInput,
  StatusBadge,
  TextInput,
  useToast,
} from "../components/ui";

const POLICIES = ["no_action", "rule_based", "pi12_adapter", "experimental_rl"];
const COMPONENTS = [
  "profitability",
  "risk",
  "transaction_cost",
  "decision_quality",
  "forecast_utilization",
  "discipline",
  "portfolio_management",
  "execution_quality",
];

function evaluationRequestId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `reward-eval-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

type Configuration = {
  config_id: string;
  name: string;
  version: string;
  components: Record<string, number>;
  enabled: string[];
  normalization: string;
  bounds: number[];
  experimental_status?: string;
};

export function RewardEvaluationPage() {
  useDocumentTitle("Reward & Policy Evaluation");
  const toast = useToast();
  const status = useApi<any>("/market/rewards/status");
  const contracts = useApi<any>("/market/rewards/contracts");
  const configs = useApi<any>("/market/rewards/configurations?limit=100");
  const baselines = useApi<any>("/market/rewards/baselines");
  const evaluations = useApi<any>("/market/rewards/evaluations?limit=50");
  const comparisons = useApi<any>("/market/rewards/comparisons?limit=50");
  const episodes = useApi<any>("/market/rl/episodes?limit=100");
  const environments = useApi<any>("/market/rl/environments?limit=100");
  const [selectedEpisode, setSelectedEpisode] = useState("");
  const [selectedConfig, setSelectedConfig] = useState("");
  const [selectedEnv, setSelectedEnv] = useState("");
  const [selectedPolicies, setSelectedPolicies] = useState(POLICIES);
  const [saving, setSaving] = useState(false);
  const [name, setName] = useState("Experimental reward v1");
  const [profitWeight, setProfitWeight] = useState("0.2");
  const [riskWeight, setRiskWeight] = useState("0.15");
  const [decisionWeight, setDecisionWeight] = useState("0.15");
  const [normalization, setNormalization] = useState("bounded");
  const [lastEvaluation, setLastEvaluation] = useState<any>(null);
  const [activeRequestId, setActiveRequestId] = useState("");
  const [cancelling, setCancelling] = useState(false);
  const episodeRows = arrayFrom(episodes.data, "episodes");
  const configRows = arrayFrom(configs.data, "configurations") as Configuration[];
  const envRows = arrayFrom(environments.data, "environments");
  const rewardResults = useApi<any>(
    selectedEpisode ? `/market/rewards/episodes/${encodeURIComponent(selectedEpisode)}?limit=200` : null,
  );
  const rewardRows = arrayFrom(rewardResults.data, "rewards");
  const currentConfig = configRows.find((row) => row.config_id === selectedConfig) || configRows[0];
  const currentEnv = envRows.find((row) => row.environment_id === selectedEnv) || envRows[0];
  const evaluationRows = lastEvaluation?.results || arrayFrom(evaluations.data, "evaluations");
  const comparisonRows = arrayFrom(comparisons.data, "comparisons");

  useEffect(() => {
    if (!selectedConfig && configRows.length) setSelectedConfig(configRows[0].config_id);
  }, [configRows, selectedConfig]);
  useEffect(() => {
    if (!selectedEnv && envRows.length) setSelectedEnv(envRows[0].environment_id);
  }, [envRows, selectedEnv]);
  useEffect(() => {
    if (!selectedEpisode && episodeRows.length) setSelectedEpisode(episodeRows[0].episode_id);
  }, [episodeRows, selectedEpisode]);

  const latestReward = useMemo(() => rewardRows[rewardRows.length - 1], [rewardRows]);
  const totalComponents = useMemo(() => {
    const totals: Record<string, number> = {};
    for (const row of rewardRows) {
      for (const [key, value] of Object.entries(row.components || {})) {
        totals[key] = num(totals[key]) + num((value as any)?.contribution);
      }
    }
    return totals;
  }, [rewardRows]);

  const createConfiguration = async () => {
    setSaving(true);
    try {
      const created = await apiPost<any>("/market/rewards/configurations", {
        name,
        version: "experimental-1",
        components: {
          profitability: Number(profitWeight),
          risk: Number(riskWeight),
          decision_quality: Number(decisionWeight),
          transaction_cost: 0.1,
          forecast_utilization: 0.1,
          discipline: 0.1,
          portfolio_management: 0.1,
          execution_quality: 0.1,
        },
        enabled: COMPONENTS,
        normalization,
        experimental: true,
        description: "Web-created experimental configuration; not financially validated.",
      });
      setSelectedConfig(created.configuration.config_id);
      toast.push("Experimental reward configuration saved.", "success");
      configs.refresh();
      status.refresh();
    } catch (error: any) {
      toast.push(error?.detail || "The configuration was refused.", "error");
    } finally {
      setSaving(false);
    }
  };

  const calculateEpisode = async () => {
    if (!selectedEpisode) return;
    setSaving(true);
    try {
      const result = await apiPost<any>(
        `/market/rewards/episodes/${encodeURIComponent(selectedEpisode)}/calculate`,
        { config_id: currentConfig?.config_id || "" },
        undefined,
        { timeoutMs: 120_000 },
      );
      toast.push(`Calculated ${result.summary.reward_count} reward results.`, "success");
      rewardResults.refresh();
      status.refresh();
    } catch (error: any) {
      toast.push(error?.detail || "Reward calculation failed.", "error");
    } finally {
      setSaving(false);
    }
  };

  const runEvaluation = async () => {
    if (!currentEnv) {
      toast.push("Create an isolated RL environment first.", "error");
      return;
    }
    const requestId = evaluationRequestId();
    setActiveRequestId(requestId);
    setSaving(true);
    try {
      const environment = Object.fromEntries(
        Object.entries(currentEnv).filter(
          ([key]) =>
            !["config_hash", "contract_version", "simulated", "advisory_only", "production_authority"].includes(key),
        ),
      );
      const result = await apiPost<any>(
        "/market/rewards/evaluations",
        {
          name: `${currentConfig?.name || "Reward"} comparison`,
          environment,
          policies: selectedPolicies,
          reward_config: currentConfig,
          split: "all",
          max_transitions: Math.min(Number(environment.max_steps || 252), 500),
          request_id: requestId,
        },
        undefined,
        { timeoutMs: 180_000 },
      );
      setLastEvaluation(result);
      toast.push(
        result.cancelled
          ? "Policy comparison cancelled; partial evidence was persisted."
          : "Policy comparison completed on the configured historical window.",
        result.cancelled ? "info" : "success",
      );
      evaluations.refresh();
      comparisons.refresh();
      status.refresh();
    } catch (error: any) {
      toast.push(error?.detail || "Policy evaluation failed.", "error");
    } finally {
      setSaving(false);
      setActiveRequestId("");
    }
  };

  const cancelEvaluation = async () => {
    if (!activeRequestId) return;
    setCancelling(true);
    try {
      await apiPost("/market/rewards/evaluations/cancel", { request_id: activeRequestId });
      toast.push("Cancellation requested; the comparison stops at its next simulation step.", "info");
    } catch (error: any) {
      toast.push(error?.detail || "The cancellation request was refused.", "error");
    } finally {
      setCancelling(false);
    }
  };

  const togglePolicy = (policy: string) => {
    setSelectedPolicies((current) =>
      current.includes(policy) ? current.filter((item) => item !== policy) : [...current, policy],
    );
  };

  return (
    <div className="page">
      <PageHeader
        eyebrow="Research / Reward and policy evaluation"
        title={<>Reward attribution, <em>not a profit score.</em></>}
        description="Experimental reward components and chronological policy comparisons over the isolated PI-13 simulation. Every result is historical simulation only; no policy can be promoted or activated from this page."
        meta={
          <>
            <span className="meta-item"><BrainCircuit size={13} /> {contracts.data?.contract_version || "PI-14"}</span>
            <span className="meta-item"><ShieldCheck size={13} /> simulated only</span>
            <span className="meta-item"><FlaskConical size={13} /> {configRows.length} configuration(s)</span>
          </>
        }
        actions={
          <>
            <Button
              variant="ghost"
              icon={<RefreshCw size={14} />}
              onClick={() => {
                status.refresh();
                configs.refresh();
                evaluations.refresh();
                comparisons.refresh();
                rewardResults.refresh();
              }}
            >
              Refresh
            </Button>
            <Button variant="primary" icon={<SlidersHorizontal size={14} />} onClick={createConfiguration} loading={saving}>
              Save configuration
            </Button>
          </>
        }
      />
      <Banner tone="info" title="Historical simulation and experimental evaluation">
        Components are transparent and weight-configurable; the defaults are experimental, not financially validated.
        A profitable action is not automatically a good decision, and a loss is not automatically a bad one.
        Results are not guaranteed future performance.
      </Banner>
      {status.error && <ErrorBlock message={status.error.detail} onRetry={status.refresh} />}
      <div className="metric-grid metric-grid-four">
        <Metric label="Reward configurations" value={status.data?.storage?.reward_configurations ?? configRows.length} help="VERSIONED · IMMUTABLE" />
        <Metric label="Reward results" value={status.data?.storage?.reward_results ?? 0} help="APPEND-ONLY" />
        <Metric label="Policy evaluations" value={status.data?.storage?.policy_evaluations ?? 0} help="CHRONOLOGICAL" />
        <Metric label="Production authority" value={status.data?.production_authority ? "unexpected" : "none"} help="ADVISORY ONLY" tone="good" />
      </div>

      <div className="dashboard-grid">
        <div className="stack">
          <Card>
            <CardHeader
              title="Reward configuration"
              subtitle="Enabled components, weights, normalization and experimental status"
              action={<Badge tone="warn">{currentConfig?.experimental_status || "experimental"}</Badge>}
            />
            <div className="form-grid">
              <Field label="Configuration name"><TextInput value={name} onChange={(event) => setName(event.target.value)} /></Field>
              <Field label="Normalization">
                <SelectInput value={normalization} onChange={(event) => setNormalization(event.target.value)}>
                  <option value="bounded">bounded [-1, 1]</option>
                  <option value="none">none</option>
                </SelectInput>
              </Field>
              <Field label="Profitability weight"><TextInput value={profitWeight} onChange={(event) => setProfitWeight(event.target.value)} /></Field>
              <Field label="Risk weight"><TextInput value={riskWeight} onChange={(event) => setRiskWeight(event.target.value)} /></Field>
              <Field label="Decision-quality weight"><TextInput value={decisionWeight} onChange={(event) => setDecisionWeight(event.target.value)} /></Field>
              <Field label="Saved configuration">
                <SelectInput value={currentConfig?.config_id || ""} onChange={(event) => setSelectedConfig(event.target.value)}>
                  {configRows.map((row) => <option value={row.config_id} key={row.config_id}>{row.name} · {row.version}</option>)}
                </SelectInput>
              </Field>
            </div>
            {currentConfig && (
              <div className="fact-list">
                <div><span>Configuration</span><strong>{currentConfig.name} · {currentConfig.version}</strong></div>
                <div><span>Version hash</span><strong>{shortId(currentConfig.config_id, 22)}</strong></div>
                <div><span>Enabled components</span><strong>{currentConfig.enabled?.join(" · ") || "—"}</strong></div>
                <div><span>Normalization / bounds</span><strong>{currentConfig.normalization} · [{currentConfig.bounds?.join(", ")}]</strong></div>
                <div><span>Weights</span><strong>{Object.entries(currentConfig.components || {}).map(([key, value]) => `${key} ${value}`).join(" · ")}</strong></div>
                <div><span>Status</span><strong>Experimental defaults; no learned normalizer and no financial validation claim.</strong></div>
              </div>
            )}
          </Card>

          <Card>
            <CardHeader
              title="Reward analysis"
              subtitle="Per-transition attribution from stored PI-13 transitions"
              action={<Badge tone="neutral">{rewardRows.length} result(s)</Badge>}
            />
            <div className="row-actions">
              <SelectInput value={selectedEpisode} onChange={(event) => setSelectedEpisode(event.target.value)}>
                <option value="">Select an episode</option>
                {episodeRows.map((row) => <option value={row.episode_id} key={row.episode_id}>{shortId(row.episode_id, 22)} · {row.status}</option>)}
              </SelectInput>
              <Button variant="secondary" icon={<Play size={14} />} onClick={calculateEpisode} disabled={!selectedEpisode} loading={saving}>
                Calculate rewards
              </Button>
            </div>
            {rewardResults.error && <ErrorBlock message={rewardResults.error.detail} onRetry={rewardResults.refresh} />}
            {!selectedEpisode ? (
              <EmptyState title="No episode selected" description="Choose a completed PI-13 episode to inspect its reward attribution." />
            ) : rewardResults.loading ? (
              <LoadingBlock rows={4} label="Reading reward results…" />
            ) : (
              <>
                <div className="metric-grid metric-grid-four">
                  <Metric label="Latest total reward" value={latestReward ? num(latestReward.total).toFixed(6) : "—"} help={latestReward ? `transition ${shortId(latestReward.transition_id, 18)}` : "not calculated"} />
                  <Metric label="Results" value={rewardRows.length} help="BOUNDED · EPISODE" />
                  <Metric label="Missing components" value={latestReward?.missing_components?.length ?? "—"} help="EXPLICIT, NOT SILENT" />
                  <Metric label="Result hash" value={latestReward ? shortId(latestReward.result_hash, 12) : "—"} help="REPRODUCIBLE" />
                </div>
                <div className="fact-list">
                  {Object.entries(totalComponents).map(([key, value]) => (
                    <div key={key}><span>{title(key.replace(/_/g, " "))} contribution</span><strong>{num(value).toFixed(6)}</strong></div>
                  ))}
                  {latestReward?.penalties && Object.entries(latestReward.penalties).map(([key, value]) => (
                    <div key={key}><span>Penalty · {title(key.replace(/_/g, " "))}</span><strong>{num(value).toFixed(6)}</strong></div>
                  ))}
                </div>
                <DataTable
                  compact
                  rows={rewardRows.slice().reverse()}
                  rowKey={(row) => row.reward_id}
                  empty={<EmptyState title="No reward results" description="Calculate rewards for this episode." />}
                  columns={[
                    { key: "transition", label: "Transition", render: (row) => shortId(row.transition_id, 20) },
                    { key: "total", label: "Total", render: (row) => num(row.total).toFixed(6) },
                    { key: "profitability", label: "Profit", render: (row) => num(row.components?.profitability?.contribution).toFixed(4) },
                    { key: "risk", label: "Risk", render: (row) => num(row.components?.risk?.contribution).toFixed(4) },
                    { key: "decision", label: "Decision", render: (row) => num(row.components?.decision_quality?.contribution).toFixed(4) },
                    { key: "forecast", label: "Forecast", render: (row) => num(row.components?.forecast_utilization?.contribution).toFixed(4) },
                    { key: "execution", label: "Execution", render: (row) => num(row.components?.execution_quality?.contribution).toFixed(4) },
                    { key: "penalties", label: "Penalties", render: (row) => Object.entries(row.penalties || {}).map(([key, value]) => `${key}:${num(value).toFixed(2)}`).join(" ") || "none" },
                  ]}
                />
              </>
            )}
          </Card>

          <Card>
            <CardHeader
              title="Policy evaluation"
              subtitle="Identical historical conditions, chronological data and isolated simulated books"
              action={<Badge tone="warn">historical simulation</Badge>}
            />
            <div className="form-grid">
              <Field label="Isolated environment">
                <SelectInput value={currentEnv?.environment_id || ""} onChange={(event) => setSelectedEnv(event.target.value)}>
                  {envRows.map((row) => <option value={row.environment_id} key={row.environment_id}>{row.name} · {row.dataset_version}</option>)}
                </SelectInput>
              </Field>
              <Field label="Policies">
                <div className="row-actions">
                  {POLICIES.map((policy) => (
                    <Button
                      key={policy}
                      size="sm"
                      variant={selectedPolicies.includes(policy) ? "primary" : "ghost"}
                      onClick={() => togglePolicy(policy)}
                    >
                      {title(policy.replace(/_/g, " "))}
                    </Button>
                  ))}
                </div>
              </Field>
            </div>
            <div className="row-actions">
              <Button variant="primary" icon={<FlaskConical size={14} />} onClick={runEvaluation} loading={saving} disabled={!currentEnv}>
                Run comparison
              </Button>
              {activeRequestId && (
                <Button variant="danger" icon={<XCircle size={14} />} onClick={cancelEvaluation} loading={cancelling}>
                  Cancel evaluation
                </Button>
              )}
              {currentEnv && <span className="muted">{currentEnv.instrument_id} · {String(currentEnv.start).slice(0, 10)} → {String(currentEnv.end).slice(0, 10)} · ₹{currentEnv.initial_capital} virtual</span>}
            </div>
            {!evaluationRows.length ? (
              <EmptyState title="No policy evaluations" description="Run the baselines against one isolated environment to compare policy behavior." />
            ) : (
              <DataTable
                compact
                rows={evaluationRows}
                rowKey={(row) => row.evaluation_id}
                empty={<EmptyState title="No results" description="Run a comparison." />}
                columns={[
                  { key: "policy", label: "Policy", render: (row) => <><div>{title(String(row.policy_name || "").replace(/_/g, " "))}</div><small>{row.policy_version || row.baseline_type || "baseline"}</small></> },
                  { key: "status", label: "Status", render: (row) => <StatusBadge value={row.status} /> },
                  { key: "return", label: "Total return", render: (row) => row.metrics?.financial?.total_return == null ? "—" : pct(num(row.metrics.financial.total_return) * 100) },
                  { key: "drawdown", label: "Max drawdown", render: (row) => { const value = row.metrics?.risk?.max_drawdown?.value; return value == null ? "—" : pct(-num(value)); } },
                  { key: "sharpe", label: "Sharpe", render: (row) => row.metrics?.financial?.sharpe ?? "insufficient" },
                  { key: "sortino", label: "Sortino", render: (row) => row.metrics?.financial?.sortino ?? "insufficient" },
                  { key: "win", label: "Win rate", render: (row) => row.metrics?.trading?.win_rate == null ? "—" : pct(num(row.metrics.trading.win_rate) * 100) },
                  { key: "profit_factor", label: "Profit factor", render: (row) => row.metrics?.trading?.profit_factor ?? "—" },
                  { key: "costs", label: "Trading costs", render: (row) => row.metrics?.trading?.total_costs ?? "—" },
                  { key: "turnover", label: "Turnover", render: (row) => row.metrics?.trading?.turnover ?? "—" },
                  { key: "actions", label: "Actions", render: (row) => Object.entries(row.metrics?.decision?.action_distribution || {}).map(([key, value]) => `${key}:${num(value)}`).join(" · ") },
                  { key: "reward", label: "Episode reward", render: (row) => num(row.reward_summary?.total_reward).toFixed(6) },
                  { key: "transitions", label: "Transitions", render: (row) => row.transitions ?? 0 },
                  { key: "at", label: "Evaluation date", render: (row) => row.created_at ? dateTime(row.created_at) : "—" },
                ]}
              />
            )}
          </Card>
        </div>

        <div className="stack">
          <Card>
            <CardHeader title="Baseline catalogue" subtitle="Every policy uses the same data, capital and simulation assumptions" action={<Badge tone="neutral">{baselines.data?.policies?.length ?? POLICIES.length}</Badge>} />
            <div className="fact-list">
              {(baselines.data?.policies || []).map((policy: any) => (
                <div key={policy.name}><span>{title(policy.name.replace(/_/g, " "))}</span><strong>{policy.version} · {policy.trained ? "trained" : "untrained"}</strong></div>
              ))}
              <div><span>Shared conditions</span><strong>Same dataset · period · capital · costs · universe · risk constraints</strong></div>
              <div><span>Policy sharing</span><strong>No mutable simulated portfolio is shared between runs.</strong></div>
              <div><span>Limitations</span><strong>Short samples cannot establish generalization; a single backtest is not proof.</strong></div>
            </div>
          </Card>
          <Card>
            <CardHeader title="Evaluation conditions" subtitle="Dataset, split and version evidence" action={<Badge tone="neutral">identical</Badge>} />
            <div className="fact-list">
              <div><span>Dataset version</span><strong>{lastEvaluation?.dataset?.dataset_version || currentEnv?.dataset_version || "—"}</strong></div>
              <div><span>Split</span><strong>{lastEvaluation?.dataset?.split || "all (explicit window)"}</strong></div>
              <div><span>Bars / hash</span><strong>{lastEvaluation?.dataset?.bars ?? currentEnv?.max_steps ?? "—"} · {shortId(lastEvaluation?.dataset?.bars_hash || "", 16) || "—"}</strong></div>
              <div><span>Initial virtual capital</span><strong><Money value={currentEnv?.initial_capital || "0"} /></strong></div>
              <div><span>Simulation</span><strong>NM-10 pricing functions · next-bar open · fees and slippage</strong></div>
              <div><span>Conditions fingerprint</span><strong>{shortId(lastEvaluation?.condition_fingerprint || "", 20) || "—"}</strong></div>
            </div>
          </Card>
          <Card>
            <CardHeader title="Isolation boundaries" subtitle="Enforced by the service, not only by this page" />
            <div className="fact-list">
              <div><span>Production portfolio / positions</span><strong>not modified</strong></div>
              <div><span>Ledger / paper fills / NM-10 orders</span><strong>not written or submitted</strong></div>
              <div><span>NM-08 risk authority</span><strong>preserved; reward risk is diagnostic</strong></div>
              <div><span>Autonomy / operator permissions</span><strong>not changed</strong></div>
              <div><span>Policy promotion or activation</span><strong>not available</strong></div>
              <div><span>Live trading</span><strong>not enabled</strong></div>
            </div>
          </Card>
          <Card>
            <CardHeader title="Recent comparisons" subtitle="Persisted baseline runs, newest first" action={<Badge tone="neutral">{comparisonRows.length}</Badge>} />
            {comparisons.loading ? <LoadingBlock rows={3} label="Reading comparisons…" /> : (
              <div className="rail-list">
                {comparisonRows.slice(0, 8).map((row) => (
                  <div className="rail-item" key={row.comparison_id}>
                    <span className="rail-icon"><Activity size={15} /></span>
                    <span>
                      <strong>{row.config?.name || shortId(row.comparison_id, 20)}</strong>
                      <small>{dateTime(row.created_at)} · {row.results?.length || 0} policies · {row.dataset?.split || "all"}</small>
                    </span>
                    <Badge tone="neutral">simulated</Badge>
                  </div>
                ))}
                {!comparisonRows.length && <EmptyState title="No comparisons" description="Run a policy comparison to persist its conditions and results." />}
              </div>
            )}
          </Card>
        </div>
      </div>
    </div>
  );
}
