import { useEffect, useMemo, useState } from "react";
import {
  Activity,
  Archive,
  BrainCircuit,
  CheckCircle2,
  FlaskConical,
  Gauge,
  Pause,
  Play,
  RefreshCw,
  RotateCcw,
  ShieldCheck,
  Square,
  Target,
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
  PageHeader,
  SelectInput,
  StatusBadge,
  TextInput,
  useToast,
} from "../components/ui";
import { LineChart } from "../components/charts";

function dateInput(date: Date): string {
  return date.toISOString().slice(0, 10);
}

function isoStart(value: string): string {
  return new Date(`${value}T00:00:00Z`).toISOString();
}

function isoEnd(value: string): string {
  return new Date(`${value}T23:59:59Z`).toISOString();
}

function nextDay(value: string): string {
  const date = new Date(`${value}T00:00:00Z`);
  date.setUTCDate(date.getUTCDate() + 1);
  return dateInput(date);
}

function progressText(current: number, total: number): string {
  if (!total) return "—";
  return `${Math.min(100, Math.round((current / total) * 100))}%`;
}

export function RLTrainingPage() {
  useDocumentTitle("RL Training & Optimisation");
  const toast = useToast();
  const status = useApi<any>("/market/training/status", { refreshMs: 15_000 });
  const contracts = useApi<any>("/market/training/contracts");
  const runs = useApi<any>("/market/training/runs?limit=100", { refreshMs: 5_000 });
  const configurations = useApi<any>("/market/training/configurations?limit=100");
  const policies = useApi<any>("/market/training/policies?limit=100", { refreshMs: 15_000 });
  const rewardConfigs = useApi<any>("/market/rewards/configurations?limit=100");
  const datasets = useApi<any>("/market/training/datasets?limit=100");
  const [selectedRun, setSelectedRun] = useState("");
  const [selectedPolicy, setSelectedPolicy] = useState("");
  const [selectedConfig, setSelectedConfig] = useState("");
  const [selectedReward, setSelectedReward] = useState("");
  const [selectedPolicyIds, setSelectedPolicyIds] = useState<string[]>([]);
  const [comparison, setComparison] = useState<any>(null);
  const [validation, setValidation] = useState<any>(null);
  const [busy, setBusy] = useState("");
  const [name, setName] = useState("Controlled DQN experiment");
  const [datasetId, setDatasetId] = useState("");
  const [datasetVersion, setDatasetVersion] = useState("");
  const [featureSetVersion, setFeatureSetVersion] = useState("pi13-observation-v1");
  const [instrument, setInstrument] = useState("XNSE:ALPHA");
  const [capital, setCapital] = useState("100000");
  const [start, setStart] = useState(() => {
    const date = new Date();
    date.setUTCFullYear(date.getUTCFullYear() - 2);
    return dateInput(date);
  });
  const [trainEnd, setTrainEnd] = useState(() => {
    const date = new Date();
    date.setUTCFullYear(date.getUTCFullYear() - 1);
    return dateInput(date);
  });
  const [validationEnd, setValidationEnd] = useState(() => {
    const date = new Date();
    date.setUTCMonth(date.getUTCMonth() - 6);
    return dateInput(date);
  });
  const [end, setEnd] = useState(() => dateInput(new Date()));
  const [maxEpisodes, setMaxEpisodes] = useState("5");
  const [maxSteps, setMaxSteps] = useState("300");
  const [episodeLength, setEpisodeLength] = useState("63");
  const [batchSize, setBatchSize] = useState("32");
  const [learningRate, setLearningRate] = useState("0.001");
  const [gamma, setGamma] = useState("0.99");
  const [epsilonEnd, setEpsilonEnd] = useState("0.05");
  const [hiddenLayers, setHiddenLayers] = useState("64,32");
  const [seed, setSeed] = useState("42");
  const [checkpointInterval, setCheckpointInterval] = useState("100");
  const [validationInterval, setValidationInterval] = useState("2");
  const [maxWallSeconds, setMaxWallSeconds] = useState("600");
  const [maxMemoryMb, setMaxMemoryMb] = useState("1024");

  const runRows = arrayFrom(runs.data, "runs");
  const configRows = arrayFrom(configurations.data, "configs", "configurations");
  const policyRows = arrayFrom(policies.data, "policies");
  const rewardRows = arrayFrom(rewardConfigs.data, "configurations");
  const datasetRows = arrayFrom(datasets.data, "items");
  const currentReward = rewardRows.find((row) => row.config_id === selectedReward) || rewardRows[0];
  const datasetKey = datasetId && datasetVersion ? `${datasetId}@${datasetVersion}` : "";
  const currentDataset =
    datasetRows.find((row) => `${row.dataset_id}@${row.version}` === datasetKey) || null;

  // The dataset decides the usable history, so choosing one also moves the
  // splits inside its coverage instead of leaving stale defaults outside it.
  const applyDataset = (key: string) => {
    const row = datasetRows.find((item) => `${item.dataset_id}@${item.version}` === key);
    if (!row) return;
    setDatasetId(row.dataset_id);
    setDatasetVersion(row.version);
    const instruments: string[] = Array.isArray(row.instruments) ? row.instruments : [];
    if (instruments.length && !instruments.includes(instrument)) setInstrument(instruments[0]);
    if (!row.start_at || !row.end_at) return;
    const from = dateInput(new Date(row.start_at));
    const to = dateInput(new Date(row.end_at));
    if (!from || !to || from >= to) return;
    const span = new Date(`${to}T00:00:00Z`).getTime() - new Date(`${from}T00:00:00Z`).getTime();
    const at = (fraction: number) =>
      dateInput(new Date(new Date(`${from}T00:00:00Z`).getTime() + span * fraction));
    setStart(from);
    setEnd(to);
    setTrainEnd(at(0.6));
    setValidationEnd(at(0.8));
  };
  const currentRun = runRows.find((row) => row.run_id === selectedRun) || runRows[0];
  const currentRunDetail = useApi<any>(currentRun?.run_id ? `/market/training/runs/${encodeURIComponent(currentRun.run_id)}` : null, { refreshMs: 4_000 });
  // Manual and paused checkpoints are written at the next step boundary, after
  // the action response.  Without polling, the row never appeared until some
  // unrelated action refreshed it.
  const checkpointData = useApi<any>(currentRun?.run_id ? `/market/training/runs/${encodeURIComponent(currentRun.run_id)}/checkpoints?limit=100` : null, { refreshMs: 5_000 });
  const currentPolicy = policyRows.find((row) => row.policy_id === selectedPolicy) || policyRows[0];
  const checkpointRows = arrayFrom(checkpointData.data, "checkpoints");
  const policySignature = policyRows.map((row) => row.policy_id).join("|");
  const activeRun = currentRunDetail.data?.status === "running" || currentRunDetail.data?.status === "paused" || currentRunDetail.data?.status === "validating_policy";
  const trainingMetrics = currentRunDetail.data?.metrics || currentRun?.metrics || {};
  const rewardCurve = (trainingMetrics.reward_history || []).map((value: number, index: number) => ({
    label: `ep ${index + 1}`,
    value: num(value),
  }));
  const lossCurve = (trainingMetrics.loss_history || [])
    .map((value: number | null, index: number) => ({
      label: `step ${index + 1}`,
      value: num(value),
    }))
    .filter((row: any) => Number.isFinite(row.value));
  const actionRows = Object.entries(trainingMetrics.action_distribution || {}).map(([key, value]) => ({
    action: key,
    count: num(value),
  }));

  useEffect(() => {
    if (!selectedRun && runRows.length) setSelectedRun(runRows[0].run_id);
  }, [runRows, selectedRun]);
  useEffect(() => {
    if (!selectedReward && rewardRows.length) setSelectedReward(rewardRows[0].config_id);
  }, [rewardRows, selectedReward]);
  useEffect(() => {
    if (!selectedPolicy && policyRows.length) setSelectedPolicy(policyRows[0].policy_id);
  }, [policyRows, selectedPolicy]);
  useEffect(() => {
    // Whenever the artifact set changes, offer every comparable policy.  Keeping
    // a previous partial selection silently left "Compare selected" disabled as
    // soon as a second policy completed validation.
    const eligible = policyRows
      .filter((row) => ["validated", "tested"].includes(row.status))
      .map((row) => row.policy_id)
      .slice(0, 8);
    setSelectedPolicyIds((current) =>
      eligible.length === current.length && eligible.every((id, index) => id === current[index])
        ? current
        : eligible,
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [policySignature]);

  const buildConfiguration = useMemo(() => {
    if (!currentReward) return null;
    const layers = hiddenLayers.split(",").map((part) => Number(part.trim())).filter((value) => Number.isInteger(value) && value >= 4);
    return {
      name: name.trim(),
      algorithm: "dqn",
      feature_set_version: featureSetVersion.trim(),
      environment: {
        name: `${name.trim()} environment`,
        dataset_id: datasetId.trim(),
        dataset_version: datasetVersion.trim(),
        instrument_id: instrument.trim().toUpperCase(),
        start: isoStart(start),
        end: isoEnd(end),
        initial_capital: capital,
        max_steps: 5000,
        max_order_notional: "25000.00",
        max_position_weight: "0.50",
        execution_config: {},
      },
      reward_config: currentReward,
      splits: {
        train: { label: "train", start: isoStart(start), end: isoEnd(trainEnd) },
        validation: { label: "validation", start: isoStart(nextDay(trainEnd)), end: isoEnd(validationEnd) },
        test: { label: "test", start: isoStart(nextDay(validationEnd)), end: isoEnd(end) },
      },
      max_episodes: Number(maxEpisodes),
      max_steps: Number(maxSteps),
      episode_length: Number(episodeLength),
      batch_size: Number(batchSize),
      learning_rate: Number(learningRate),
      gamma: Number(gamma),
      epsilon_start: 1,
      epsilon_end: Number(epsilonEnd),
      epsilon_decay: 0.995,
      hidden_layers: layers.length ? layers : [64, 32],
      activation: "relu",
      optimizer: "adam",
      target_update_interval: 25,
      replay_capacity: 5000,
      checkpoint_interval: Number(checkpointInterval),
      validation_interval: Number(validationInterval),
      validation_max_transitions: 200,
      test_max_transitions: 300,
      max_wall_seconds: Number(maxWallSeconds),
      max_memory_mb: Number(maxMemoryMb),
      seed: Number(seed),
      description: "Web-created experimental DQN training run. Advisory only.",
    };
  }, [name, datasetId, datasetVersion, instrument, capital, start, trainEnd, validationEnd, end, maxEpisodes, maxSteps, episodeLength, batchSize, learningRate, gamma, epsilonEnd, hiddenLayers, seed, checkpointInterval, validationInterval, maxWallSeconds, maxMemoryMb, currentReward]);

  const runAction = async (path: string, label: string) => {
    setBusy(path);
    try {
      await apiPost(path, {});
      toast.push(label, "success");
      runs.refresh();
      currentRunDetail.refresh();
      checkpointData.refresh();
      policies.refresh();
      status.refresh();
    } catch (error: any) {
      toast.push(error?.detail || `${label} failed.`, "error");
    } finally {
      setBusy("");
    }
  };

  // Resuming a checkpoint launches a NEW run; without selecting the run the API
  // returns, the monitor kept refreshing the old (often cancelled) run and the
  // resumed one was invisible until it was found by hand in the runs table.
  const resumeCheckpoint = async (checkpointId: string) => {
    const path = `/market/training/checkpoints/${encodeURIComponent(checkpointId)}/resume`;
    setBusy(path);
    try {
      const restored = await apiPost<any>(path, {});
      const runId = restored?.run?.run_id;
      if (runId) setSelectedRun(runId);
      toast.push("Checkpoint resumed as a new isolated run.", "success");
      runs.refresh();
      currentRunDetail.refresh();
      checkpointData.refresh();
      status.refresh();
    } catch (error: any) {
      toast.push(error?.detail || "Checkpoint resume failed.", "error");
    } finally {
      setBusy("");
    }
  };

  const validateConfiguration = async () => {
    if (!buildConfiguration) {
      toast.push("A saved PI-14 reward configuration is required.", "error");
      return;
    }
    setBusy("validate");
    try {
      const result = await apiPost<any>("/market/training/validate", { config: buildConfiguration });
      setValidation(result);
      toast.push(result.valid ? "Configuration is valid." : "Configuration was refused.", result.valid ? "success" : "error");
    } catch (error: any) {
      toast.push(error?.detail || "Configuration validation failed.", "error");
    } finally {
      setBusy("");
    }
  };

  const saveAndStart = async () => {
    if (!buildConfiguration) {
      toast.push("A saved PI-14 reward configuration is required.", "error");
      return;
    }
    setBusy("start");
    try {
      const saved = await apiPost<any>("/market/training/configurations", { config: buildConfiguration });
      setSelectedConfig(saved.configuration.config_id);
      const started = await apiPost<any>(
        "/market/training/runs",
        { config_id: saved.configuration.config_id },
        undefined,
        { timeoutMs: 180_000 },
      );
      setSelectedRun(started.run.run_id);
      setSelectedPolicy(started.policy_id);
      toast.push("Training run accepted in an isolated worker.", "success");
      runs.refresh();
      configurations.refresh();
      policies.refresh();
      status.refresh();
    } catch (error: any) {
      toast.push(error?.detail || "The training run was refused.", "error");
    } finally {
      setBusy("");
    }
  };

  const startSaved = async () => {
    if (!selectedConfig) {
      toast.push("Choose a saved configuration first.", "error");
      return;
    }
    setBusy("start-saved");
    try {
      const started = await apiPost<any>(
        "/market/training/runs",
        { config_id: selectedConfig },
        undefined,
        { timeoutMs: 180_000 },
      );
      setSelectedRun(started.run.run_id);
      setSelectedPolicy(started.policy_id);
      toast.push("Saved configuration started unchanged.", "success");
      runs.refresh();
      policies.refresh();
      status.refresh();
    } catch (error: any) {
      toast.push(error?.detail || "The saved configuration was refused.", "error");
    } finally {
      setBusy("");
    }
  };

  const policyAction = async (path: string, message: string) => {
    setBusy(path);
    try {
      await apiPost(path, {}, undefined, { timeoutMs: 180_000 });
      toast.push(message, "success");
      policies.refresh();
      runs.refresh();
    } catch (error: any) {
      toast.push(error?.detail || "The policy action was refused.", "error");
    } finally {
      setBusy("");
    }
  };

  const comparePolicies = async () => {
    if (selectedPolicyIds.length < 2) {
      toast.push("Select at least two validated policies with identical conditions.", "error");
      return;
    }
    setBusy("compare");
    try {
      const result = await apiPost<any>("/market/training/compare", { policy_ids: selectedPolicyIds });
      setComparison(result.comparison);
      toast.push("Comparison completed on identical recorded conditions.", "success");
    } catch (error: any) {
      toast.push(error?.detail || "The policies are not directly comparable.", "error");
    } finally {
      setBusy("");
    }
  };

  const togglePolicySelection = (policyId: string) => {
    setSelectedPolicyIds((current) =>
      current.includes(policyId) ? current.filter((item) => item !== policyId) : [...current, policyId].slice(-8),
    );
  };

  return (
    <div className="page">
      <PageHeader
        eyebrow="Research / RL training and optimisation"
        title={<>Train experimental agents, <em>never activate them.</em></>}
        description="A bounded DQN training workspace over PI-13 historical simulation environments and PI-14 reward/evaluation contracts. Every policy is experimental and advisory. There is no activation, NM-08 bypass, NM-10 order or broker path here."
        meta={
          <>
            <span className="meta-item"><BrainCircuit size={13} /> {contracts.data?.contract_version || "PI-15"}</span>
            <span className="meta-item"><ShieldCheck size={13} /> isolated simulation</span>
            <span className="meta-item"><Activity size={13} /> {status.data?.active_runs ?? 0} active worker(s)</span>
          </>
        }
        actions={
          <>
            <Button variant="ghost" icon={<RefreshCw size={14} />} onClick={() => { status.refresh(); runs.refresh(); configurations.refresh(); policies.refresh(); currentRunDetail.refresh(); checkpointData.refresh(); }}>
              Refresh
            </Button>
            <Button variant="primary" icon={<Play size={14} />} onClick={saveAndStart} loading={busy === "start"}>
              Save & start training
            </Button>
          </>
        }
      />
      <Banner tone="info" title="Research lifecycle only">
        Training rewards measure an optimisation signal, not financial profit. Validation and test results are historical simulations.
        A TESTED or VALIDATED artifact is never promoted automatically, and no policy can control production autonomy from this workspace.
      </Banner>
      {status.error && <ErrorBlock message={status.error.detail} onRetry={status.refresh} />}
      <div className="metric-grid metric-grid-four">
        <Metric label="Active workers" value={`${status.data?.active_runs ?? 0} / ${status.data?.max_concurrent_runs ?? 1}`} help="BOUNDED CONCURRENCY" />
        <Metric label="Training runs" value={status.data?.counts?.runs ?? runRows.length} help="ISOLATED RESEARCH" />
        <Metric label="Policy artifacts" value={status.data?.counts?.policies ?? policyRows.length} help="ADVISORY ONLY" />
        <Metric label="Algorithm" value="DQN" help="DISCRETE ACTION VALUES" tone="good" />
      </div>

      <div className="dashboard-grid">
        <div className="stack">
          <Card>
            <CardHeader
              title="Training configuration"
              subtitle="Versioned algorithm, environment, chronological splits and bounded resources"
              action={<Badge tone="neutral">{configRows.length} saved</Badge>}
            />
            <div className="form-grid">
              <Field label="Agent name"><TextInput value={name} onChange={(event) => setName(event.target.value)} /></Field>
              <Field label="Algorithm">
                <SelectInput value="dqn" onChange={() => undefined}>
                  <option value="dqn">DQN · discrete buy/sell/hold/wait/no_action</option>
                </SelectInput>
              </Field>
              <Field label="Training dataset">
                <SelectInput value={currentDataset ? datasetKey : ""} onChange={(event) => applyDataset(event.target.value)}>
                  <option value="">{datasetRows.length ? "Choose a stored PI-02 dataset" : "No stored dataset versions"}</option>
                  {datasetRows.map((row) => (
                    <option value={`${row.dataset_id}@${row.version}`} key={`${row.dataset_id}@${row.version}`}>
                      {row.dataset_id} · {row.version} · {row.quality}
                    </option>
                  ))}
                </SelectInput>
                <small className="muted">
                  {currentDataset
                    ? `${currentDataset.dataset_id}@${currentDataset.version} · ${currentDataset.resolution} · ${String(currentDataset.start_at || "").slice(0, 10)} → ${String(currentDataset.end_at || "").slice(0, 10)}`
                    : "Runs load bars from an immutable PI-02 dataset version."}
                </small>
              </Field>
              <Field label="Feature-set version"><TextInput value={featureSetVersion} onChange={(event) => setFeatureSetVersion(event.target.value)} placeholder="pi13-observation-v1" /></Field>
              <Field label="Instrument">
                <TextInput value={instrument} onChange={(event) => setInstrument(event.target.value)} />
                <small className="muted">
                  {currentDataset && Array.isArray(currentDataset.instruments) && currentDataset.instruments.length
                    ? `In this dataset: ${currentDataset.instruments.join(" · ")}`
                    : "The instrument must exist inside the chosen dataset version."}
                </small>
              </Field>
              <Field label="Reward configuration">
                <SelectInput value={currentReward?.config_id || ""} onChange={(event) => setSelectedReward(event.target.value)}>
                  {rewardRows.map((row) => <option value={row.config_id} key={row.config_id}>{row.name} · {row.version}</option>)}
                </SelectInput>
              </Field>
              <Field label="Historical start"><TextInput type="date" value={start} onChange={(event) => setStart(event.target.value)} /></Field>
              <Field label="Train split ends"><TextInput type="date" value={trainEnd} onChange={(event) => setTrainEnd(event.target.value)} /></Field>
              <Field label="Validation split ends"><TextInput type="date" value={validationEnd} onChange={(event) => setValidationEnd(event.target.value)} /></Field>
              <Field label="Historical end"><TextInput type="date" value={end} onChange={(event) => setEnd(event.target.value)} /></Field>
              <Field label="Initial virtual capital (INR)"><TextInput value={capital} onChange={(event) => setCapital(event.target.value)} /></Field>
              <Field label="Random seed"><TextInput value={seed} onChange={(event) => setSeed(event.target.value)} /></Field>
              <Field label="Episodes"><TextInput value={maxEpisodes} onChange={(event) => setMaxEpisodes(event.target.value)} /></Field>
              <Field label="Maximum steps"><TextInput value={maxSteps} onChange={(event) => setMaxSteps(event.target.value)} /></Field>
              <Field label="Episode length (bars)"><TextInput value={episodeLength} onChange={(event) => setEpisodeLength(event.target.value)} /></Field>
              <Field label="Batch size"><TextInput value={batchSize} onChange={(event) => setBatchSize(event.target.value)} /></Field>
              <Field label="Learning rate"><TextInput value={learningRate} onChange={(event) => setLearningRate(event.target.value)} /></Field>
              <Field label="Discount factor"><TextInput value={gamma} onChange={(event) => setGamma(event.target.value)} /></Field>
              <Field label="Final epsilon"><TextInput value={epsilonEnd} onChange={(event) => setEpsilonEnd(event.target.value)} /></Field>
              <Field label="Hidden layers"><TextInput value={hiddenLayers} onChange={(event) => setHiddenLayers(event.target.value)} placeholder="64,32" /></Field>
              <Field label="Checkpoint interval"><TextInput value={checkpointInterval} onChange={(event) => setCheckpointInterval(event.target.value)} /></Field>
              <Field label="Validation interval"><TextInput value={validationInterval} onChange={(event) => setValidationInterval(event.target.value)} /></Field>
              <Field label="Wall limit (seconds)"><TextInput value={maxWallSeconds} onChange={(event) => setMaxWallSeconds(event.target.value)} /></Field>
              <Field label="Memory limit (MiB)"><TextInput value={maxMemoryMb} onChange={(event) => setMaxMemoryMb(event.target.value)} /></Field>
              <Field label="Saved configuration">
                <SelectInput value={selectedConfig} onChange={(event) => setSelectedConfig(event.target.value)}>
                  <option value="">New configuration</option>
                  {configRows.map((row) => <option value={row.config_id} key={row.config_id}>{row.name} · {shortId(row.config_id, 16)}</option>)}
                </SelectInput>
              </Field>
            </div>
            <div className="row-actions">
              <Button variant="secondary" icon={<CheckCircle2 size={14} />} onClick={validateConfiguration} loading={busy === "validate"}>
                Validate configuration
              </Button>
              <Button variant="primary" icon={<Play size={14} />} onClick={saveAndStart} loading={busy === "start"}>
                Save & start
              </Button>
              <Button variant="secondary" icon={<Play size={14} />} onClick={startSaved} loading={busy === "start-saved"} disabled={!selectedConfig}>
                Start saved
              </Button>
              {selectedConfig && <span className="muted">Saved config {shortId(selectedConfig, 18)} is preserved immutably.</span>}
            </div>
            {validation && (
              <div className="fact-list">
                <div><span>Validation</span><strong>{validation.valid ? "valid" : "refused"}</strong></div>
                <div><span>Config hash</span><strong>{shortId(validation.config_hash || "", 20) || "—"}</strong></div>
                <div><span>Warnings</span><strong>{validation.warnings?.join(" · ") || "none"}</strong></div>
                <div><span>Errors</span><strong>{validation.errors?.join(" · ") || "none"}</strong></div>
              </div>
            )}
            <Banner tone="info" title="Chronological isolation">
              Train ends before validation starts, and validation ends before test starts. Test data is never used for hyperparameter selection, checkpoint selection or training.
            </Banner>
          </Card>

          <Card>
            <CardHeader
              title="Training dashboard"
              subtitle="Active and historical isolated runs"
              action={<Badge tone="neutral">{runRows.length}</Badge>}
            />
            {runs.loading ? <LoadingBlock rows={4} label="Reading training runs…" /> : (
              <DataTable
                compact
                rows={runRows}
                rowKey={(row) => row.run_id}
                empty={<EmptyState icon={<FlaskConical size={24} />} title="No training runs" description="Validate a configuration and start a bounded run." />}
                columns={[
                  { key: "name", label: "Agent / run", render: (row) => <button type="button" className="link-button" onClick={() => { setSelectedRun(row.run_id); setSelectedPolicy(row.policy_id); }}><strong>{row.name}</strong><small>{shortId(row.run_id, 18)}</small></button> },
                  { key: "status", label: "Status", render: (row) => <StatusBadge value={row.status} /> },
                  { key: "progress", label: "Episodes", render: (row) => `${row.current_episode ?? 0} / ${row.total_episodes ?? 0} · ${progressText(row.current_episode, row.total_episodes)}` },
                  { key: "steps", label: "Steps", render: (row) => `${row.current_step ?? 0} / ${row.total_steps ?? 0}` },
                  { key: "reward", label: "Mean reward", render: (row) => row.metrics?.mean_reward == null ? "—" : num(row.metrics.mean_reward).toFixed(5) },
                  { key: "seed", label: "Seed", render: (row) => row.seed },
                  { key: "started", label: "Started", render: (row) => row.started_at ? dateTime(row.started_at) : "—" },
                  { key: "actions", label: "", render: (row) => <div className="row-actions"><Button size="sm" variant="quiet" onClick={() => setSelectedRun(row.run_id)}>Inspect</Button></div> },
                ]}
              />
            )}
          </Card>

          <Card>
            <CardHeader
              title="Training monitor"
              subtitle="Reward, losses, action distribution and checkpoint history"
              action={currentRunDetail.data ? <StatusBadge value={currentRunDetail.data.status} /> : undefined}
            />
            {currentRunDetail.error && <ErrorBlock message={currentRunDetail.error.detail} onRetry={currentRunDetail.refresh} />}
            {!currentRunDetail.data ? <EmptyState title="No run selected" description="Select a training run to inspect its bounded metrics." /> : (
              <>
                <div className="fact-list">
                  <div><span>Run</span><strong>{shortId(currentRunDetail.data.run_id, 22)} · {currentRunDetail.data.algorithm}</strong></div>
                  <div><span>Progress</span><strong>{currentRunDetail.data.current_episode} / {currentRunDetail.data.total_episodes} episodes · {currentRunDetail.data.current_step} / {currentRunDetail.data.total_steps} steps</strong></div>
                  <div><span>Training signature</span><strong>{shortId(currentRunDetail.data.training_signature || "", 20) || "—"}</strong></div>
                  <div><span>Reward / variance</span><strong>{trainingMetrics.mean_reward == null ? "—" : num(trainingMetrics.mean_reward).toFixed(6)} · {trainingMetrics.reward_variance == null ? "—" : num(trainingMetrics.reward_variance).toFixed(6)}</strong></div>
                  <div><span>Rolling reward</span><strong>{trainingMetrics.rolling_reward == null ? "—" : num(trainingMetrics.rolling_reward).toFixed(6)}</strong></div>
                  <div><span>Epsilon / invalid actions</span><strong>{trainingMetrics.exploration_epsilon ?? "—"} · {trainingMetrics.invalid_actions ?? 0} ({trainingMetrics.invalid_action_frequency ?? 0})</strong></div>
                  <div><span>CPU / memory</span><strong>{currentRunDetail.data.resource_usage?.cpu_percent ?? "—"}% · {currentRunDetail.data.resource_usage?.process_rss_mb ?? "—"} MiB</strong></div>
                  <div><span>Stop reason</span><strong>{currentRunDetail.data.stop_reason || "—"}</strong></div>
                </div>
                <div className="row-actions">
                  <Button size="sm" variant="secondary" icon={<Pause size={13} />} onClick={() => runAction(`/market/training/runs/${encodeURIComponent(currentRunDetail.data.run_id)}/pause`, "Pause requested at the next safe boundary.")} disabled={!activeRun || currentRunDetail.data.status === "paused"} loading={busy.includes("/pause")}>Pause</Button>
                  <Button size="sm" variant="secondary" icon={<Play size={13} />} onClick={() => runAction(`/market/training/runs/${encodeURIComponent(currentRunDetail.data.run_id)}/resume`, "Training resumed.")} disabled={currentRunDetail.data.status !== "paused"} loading={busy.includes("/resume")}>Resume</Button>
                  <Button size="sm" variant="secondary" icon={<RotateCcw size={13} />} onClick={() => runAction(`/market/training/runs/${encodeURIComponent(currentRunDetail.data.run_id)}/checkpoints`, "Manual checkpoint requested.")} disabled={!activeRun}>Checkpoint</Button>
                  <Button size="sm" variant="danger" icon={<Square size={13} />} onClick={() => runAction(`/market/training/runs/${encodeURIComponent(currentRunDetail.data.run_id)}/cancel`, "Cancellation requested.")} disabled={!activeRun}>Cancel</Button>
                  <Button size="sm" variant="ghost" onClick={() => runAction(`/market/training/runs/${encodeURIComponent(currentRunDetail.data.run_id)}/retry`, "Retry run accepted.")} disabled={!currentRunDetail.data.terminal}>Retry</Button>
                </div>
                <Banner tone="info" title="Training reward is not profitability">
                  A rising training reward means the optimiser is improving its configured signal. It is not evidence of a profitable or reliable trading strategy.
                </Banner>
                <div className="chart-block">
                  <div className="chart-block-head"><span>Episode reward</span><Badge tone="neutral">training signal</Badge></div>
                  <LineChart data={rewardCurve} height={210} format="number" color="var(--gold)" empty="Reward history appears after the first completed episode" />
                </div>
                <div className="chart-block">
                  <div className="chart-block-head"><span>Value loss (MSE)</span><Badge tone="neutral">bounded history</Badge></div>
                  <LineChart data={lossCurve} height={190} format="number" color="var(--blue)" empty="Loss history appears once training batches are available" />
                </div>
                {actionRows.length > 0 && (
                  <div className="fact-list">
                    {actionRows.map((row) => <div key={row.action}><span>{title(row.action.replace(/_/g, " "))} actions</span><strong>{row.count}</strong></div>)}
                  </div>
                )}
                {trainingMetrics.latest_validation && (
                  <div className="fact-list">
                    <div><span>Latest validation score</span><strong>{trainingMetrics.latest_validation.score}</strong></div>
                    <div><span>Validation conditions</span><strong>Identical baselines · {shortId(trainingMetrics.latest_validation.comparison_id || "", 20)}</strong></div>
                  </div>
                )}
                <DataTable
                  compact
                  rows={checkpointRows}
                  rowKey={(row) => row.checkpoint_id}
                  empty={<EmptyState title="No checkpoints yet" description="Periodic, best-validation, manual and final checkpoints appear here." />}
                  columns={[
                    { key: "kind", label: "Kind", render: (row) => <Badge tone={row.kind === "best_validation" ? "good" : "neutral"}>{title(String(row.kind).replace(/_/g, " "))}</Badge> },
                    { key: "step", label: "Step", render: (row) => `${row.step} / episode ${row.episode}` },
                    { key: "size", label: "Size", render: (row) => `${num(row.size_bytes).toLocaleString("en-IN")} B` },
                    { key: "integrity", label: "Integrity", render: (row) => shortId(row.integrity_hash || "", 18) },
                    { key: "created", label: "Created", render: (row) => dateTime(row.created_at) },
                    { key: "resume", label: "", render: (row) => <Button size="sm" variant="quiet" onClick={() => resumeCheckpoint(row.checkpoint_id)} loading={busy.includes(`/checkpoints/${row.checkpoint_id}/resume`)}>Resume</Button> },
                  ]}
                />
              </>
            )}
          </Card>

          <Card>
            <CardHeader
              title="Policy comparison"
              subtitle="Identical recorded data, environment, rewards and execution assumptions only"
              action={<Badge tone="neutral">{selectedPolicyIds.length} selected</Badge>}
            />
            <div className="row-actions">
              {policyRows.filter((row) => ["validated", "tested"].includes(row.status)).map((row) => (
                <Button key={row.policy_id} size="sm" variant={selectedPolicyIds.includes(row.policy_id) ? "primary" : "ghost"} onClick={() => togglePolicySelection(row.policy_id)}>
                  {row.name} · {row.status}
                </Button>
              ))}
              <Button variant="secondary" icon={<Target size={14} />} onClick={comparePolicies} loading={busy === "compare"} disabled={selectedPolicyIds.length < 2}>
                Compare selected
              </Button>
            </div>
            {!comparison ? <EmptyState title="No comparison yet" description="Select validated policies with matching conditions; incompatible runs are refused." /> : (
              <DataTable
                compact
                rows={comparison.results || []}
                rowKey={(row) => row.policy_id}
                empty={<EmptyState title="No comparable policies" description="Complete validation on at least two policies." />}
                columns={[
                  { key: "name", label: "Policy", render: (row) => <><div>{row.name}</div><small>{shortId(row.policy_id, 18)}</small></> },
                  { key: "status", label: "Lifecycle", render: (row) => <StatusBadge value={row.status} /> },
                  { key: "training", label: "Mean training reward", render: (row) => row.training_metrics?.mean_reward == null ? "—" : num(row.training_metrics.mean_reward).toFixed(5) },
                  { key: "validation", label: "Validation score", render: (row) => row.validation_score ?? "—" },
                  { key: "test", label: "Out-of-sample score", render: (row) => row.test_score ?? "—" },
                  { key: "return", label: "Test return", render: (row) => row.test_metrics?.financial?.total_return == null ? "—" : pct(num(row.test_metrics.financial.total_return) * 100) },
                  { key: "risk", label: "Test drawdown", render: (row) => { const value = row.test_metrics?.risk?.max_drawdown?.value; return value == null ? "—" : pct(-num(value)); } },
                  { key: "costs", label: "Trading costs", render: (row) => row.test_metrics?.trading?.total_costs ?? "—" },
                ]}
              />
            )}
            {comparison && <Banner tone="info" title="Historical comparison limits">{comparison.limitations}</Banner>}
          </Card>
        </div>

        <div className="stack">
          <Card>
            <CardHeader title="Training contracts" subtitle="Selected algorithm and bounded safety limits" action={<Badge tone="neutral">{contracts.data?.algorithm || "DQN"}</Badge>} />
            <div className="fact-list">
              <div><span>Primary algorithm</span><strong>DQN · replay + target network</strong></div>
              <div><span>Why DQN</span><strong>Discrete five-action PI-13 space and bounded numeric observations.</strong></div>
              <div><span>Action space</span><strong>{contracts.data?.actions?.join(" · ") || "buy · sell · hold · wait · no_action"}</strong></div>
              <div><span>Environment</span><strong>{contracts.data?.compatible_environments || "PI-13"}</strong></div>
              <div><span>Rewards</span><strong>{contracts.data?.rewards || "PI-14"}</strong></div>
              <div><span>Comparison baselines</span><strong>no_action · rule_based · pi12_adapter</strong></div>
              <div><span>Concurrency limit</span><strong>{contracts.data?.limits?.max_concurrent_runs ?? 1} training worker</strong></div>
              <div><span>Optimisation trials</span><strong>{contracts.data?.limits?.max_optimization_trials ?? 20} maximum</strong></div>
            </div>
          </Card>

          <Card>
            <CardHeader title="Policy artifacts" subtitle="Experimental research lifecycle; no activation control" action={<Badge tone="warn">{policyRows.length} artifacts</Badge>} />
            {policies.loading ? <LoadingBlock rows={3} label="Reading policies…" /> : policyRows.length ? (
              <div className="rail-list">
                {policyRows.slice(0, 10).map((row) => (
                  <button type="button" className={`rail-item ${currentPolicy?.policy_id === row.policy_id ? "rail-item-active" : ""}`} key={row.policy_id} onClick={() => setSelectedPolicy(row.policy_id)}>
                    <span className="rail-icon"><BrainCircuit size={15} /></span>
                    <span><strong>{row.name}</strong><small>{shortId(row.policy_id, 18)} · {row.algorithm} · seed {row.seed}</small></span>
                    <StatusBadge value={row.status} />
                  </button>
                ))}
              </div>
            ) : <EmptyState title="No policy artifacts" description="A completed training run stores its checkpoint-backed experimental policy here." />}
          </Card>

          {currentPolicy && (
            <Card>
              <CardHeader title="Artifact detail" subtitle="Versioned, reproducible, experimental" action={<Badge tone="warn">no activation</Badge>} />
              <div className="fact-list">
                <div><span>Policy ID</span><strong>{shortId(currentPolicy.policy_id, 24)}</strong></div>
                <div><span>Run</span><strong>{shortId(currentPolicy.run_id, 20)}</strong></div>
                <div><span>Algorithm / version</span><strong>{currentPolicy.algorithm} · {currentPolicy.artifact_version}</strong></div>
                <div><span>Artifact hash</span><strong>{shortId(currentPolicy.artifact_hash || "", 20) || "—"}</strong></div>
                <div><span>Environment version</span><strong>{currentPolicy.environment_version || "—"}</strong></div>
                <div><span>Reward version</span><strong>{currentPolicy.reward_version || "—"}</strong></div>
                <div><span>Dataset hash</span><strong>{shortId(currentPolicy.dataset_provenance?.data_hash || "", 20) || "—"}</strong></div>
                <div><span>Validation score</span><strong>{currentPolicy.validation_metrics?.score ?? "—"}</strong></div>
                <div><span>Out-of-sample score</span><strong>{currentPolicy.test_metrics?.score ?? "—"}</strong></div>
                <div><span>Advisory only</span><strong>{currentPolicy.advisory_only ? "yes" : "unexpected"}</strong></div>
              </div>
              <div className="row-actions">
                <Button size="sm" variant="secondary" icon={<CheckCircle2 size={13} />} onClick={() => policyAction(`/market/training/policies/${encodeURIComponent(currentPolicy.policy_id)}/validate`, "Validation completed.")} disabled={!["trained"].includes(currentPolicy.status)} loading={busy.includes("/validate")}>Validate</Button>
                <Button size="sm" variant="secondary" icon={<Gauge size={13} />} onClick={() => policyAction(`/market/training/policies/${encodeURIComponent(currentPolicy.policy_id)}/test`, "Out-of-sample test completed.")} disabled={currentPolicy.status !== "validated"} loading={busy.includes("/test")}>Out-of-sample test</Button>
                <Button size="sm" variant="ghost" icon={<Archive size={13} />} onClick={() => policyAction(`/market/training/policies/${encodeURIComponent(currentPolicy.policy_id)}/archive`, "Policy archived.")} disabled={["archived", "rejected"].includes(currentPolicy.status)}>Archive</Button>
              </div>
              {currentPolicy.validation_metrics?.baselines?.length > 0 && (
                <DataTable
                  compact
                  rows={currentPolicy.validation_metrics.baselines}
                  rowKey={(row) => row.policy_name}
                  empty={<EmptyState title="No baselines" description="Validation baselines appear after evaluation." />}
                  columns={[
                    { key: "policy", label: "Baseline", render: (row) => row.policy_name },
                    { key: "status", label: "Status", render: (row) => <StatusBadge value={row.status} /> },
                    { key: "return", label: "Return", render: (row) => row.metrics?.financial?.total_return == null ? "—" : pct(num(row.metrics.financial.total_return) * 100) },
                    { key: "reward", label: "Reward", render: (row) => num(row.reward_summary?.total_reward).toFixed(5) },
                  ]}
                />
              )}
            </Card>
          )}

          <Card>
            <CardHeader title="Resource and isolation boundaries" subtitle="Enforced by the service, not only the page" action={<Badge tone="good">sealed</Badge>} />
            <div className="fact-list">
              <div><span>Production portfolio / positions</span><strong>not reachable</strong></div>
              <div><span>Ledger / paper fills / NM-10 orders</span><strong>not written or submitted</strong></div>
              <div><span>NM-08 risk authority</span><strong>preserved; no bypass</strong></div>
              <div><span>Autonomy and operator permissions</span><strong>not changed</strong></div>
              <div><span>Broker / live trading</span><strong>not connected</strong></div>
              <div><span>Policy activation</span><strong>not available</strong></div>
              <div><span>Training DB</span><strong>{status.data?.storage?.path || "isolated research SQLite"}</strong></div>
              <div><span>Storage / checkpoints</span><strong>{status.data?.storage?.megabytes ?? 0} MiB · {status.data?.counts?.checkpoints ?? 0} records</strong></div>
            </div>
          </Card>
        </div>
      </div>
    </div>
  );
}
