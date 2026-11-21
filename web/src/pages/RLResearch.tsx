import { useEffect, useMemo, useState } from "react";
import {
  Activity,
  BrainCircuit,
  FlaskConical,
  Play,
  Plus,
  RefreshCw,
  RotateCcw,
  ShieldCheck,
  Square,
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
import { LineChart } from "../components/charts";

const ACTIONS = ["buy", "sell", "hold", "wait", "no_action"];

export function RLResearchPage() {
  useDocumentTitle("RL Research");
  const toast = useToast();
  const status = useApi<any>("/market/rl/status");
  const contracts = useApi<any>("/market/rl/contracts");
  const health = useApi<any>("/market/rl/health");
  const environments = useApi<any>("/market/rl/environments?limit=100");
  const episodes = useApi<any>("/market/rl/episodes?limit=100");
  const [selectedEnv, setSelectedEnv] = useState("");
  const [selectedEpisode, setSelectedEpisode] = useState("");
  const detail = useApi<any>(selectedEpisode ? `/market/rl/episodes/${encodeURIComponent(selectedEpisode)}` : null);
  const transitions = useApi<any>(selectedEpisode ? `/market/rl/episodes/${encodeURIComponent(selectedEpisode)}/transitions?limit=200` : null);
  const [saving, setSaving] = useState(false);
  const [action, setAction] = useState("hold");
  const [notional, setNotional] = useState("10000");
  const [fraction, setFraction] = useState("");
  const [name, setName] = useState("Daily swing research");
  const [datasetId, setDatasetId] = useState("");
  const [datasetVersion, setDatasetVersion] = useState("");
  const [instrument, setInstrument] = useState("XNSE:ALPHA");
  const [start, setStart] = useState(() => { const date = new Date(); date.setFullYear(date.getFullYear() - 1); return date.toISOString().slice(0, 10); });
  const [end, setEnd] = useState(() => new Date().toISOString().slice(0, 10));
  const [capital, setCapital] = useState("100000");
  const [maxSteps, setMaxSteps] = useState("252");
  const envRows = arrayFrom(environments.data, "environments");
  const episodeRows = arrayFrom(episodes.data, "episodes");
  const transitionRows = arrayFrom(transitions.data, "transitions", "experiences");
  const currentEpisode = detail.data?.episode || episodeRows.find((row) => row.episode_id === selectedEpisode) || detail.data;
  const observation = currentEpisode?.observation || detail.data?.observation;
  const portfolio = currentEpisode?.portfolio || detail.data?.portfolio || {};
  const summary = currentEpisode?.summary || detail.data?.summary || {};
  const drawdown = portfolio.drawdown ?? summary.max_drawdown;
  const currentEnv = envRows.find((row) => row.environment_id === selectedEnv) || envRows[0];
  const isLive = ["running", "created"].includes(String(currentEpisode?.status));

  useEffect(() => {
    if (!selectedEnv && envRows.length) setSelectedEnv(envRows[0].environment_id);
  }, [envRows, selectedEnv]);
  useEffect(() => {
    if (!selectedEpisode && episodeRows.length) setSelectedEpisode(episodeRows[0].episode_id);
  }, [episodeRows, selectedEpisode]);

  const curve = useMemo(() => {
    const rows = transitionRows.map((row) => ({
      label: String(row.timestamp || "").slice(5, 10),
      value: num(row.portfolio_after?.equity, NaN),
    })).filter((row) => Number.isFinite(row.value));
    return rows;
  }, [transitionRows]);

  const createEnvironment = async () => {
    if (!datasetId.trim() || !datasetVersion.trim() || !instrument.trim()) {
      toast.push("Dataset id, version and instrument are required.", "error");
      return;
    }
    setSaving(true);
    try {
      const created = await apiPost<any>("/market/rl/environments", {
        name,
        dataset_id: datasetId.trim(),
        dataset_version: datasetVersion.trim(),
        instrument_id: instrument.trim().toUpperCase(),
        start: new Date(`${start}T00:00:00Z`).toISOString(),
        end: new Date(`${end}T00:00:00Z`).toISOString(),
        initial_capital: capital,
        max_steps: Number(maxSteps),
      });
      setSelectedEnv(created.environment_id);
      toast.push("Isolated RL environment created.", "success");
      environments.refresh();
      status.refresh();
    } catch (error: any) {
      toast.push(error?.detail || "The RL environment was refused.", "error");
    } finally { setSaving(false); }
  };

  const startEpisode = async () => {
    if (!currentEnv) return;
    setSaving(true);
    try {
      const started = await apiPost<any>(`/market/rl/environments/${encodeURIComponent(currentEnv.environment_id)}/episodes`, {});
      setSelectedEpisode(started.episode.episode_id);
      toast.push("Simulated episode started.", "success");
      episodes.refresh();
      status.refresh();
    } catch (error: any) {
      toast.push(error?.detail || "The episode could not start.", "error");
    } finally { setSaving(false); }
  };

  const step = async () => {
    if (!selectedEpisode) return;
    setSaving(true);
    try {
      const payload: any = { action_type: action };
      if (action === "buy" || action === "sell") {
        payload.instrument_id = currentEnv?.instrument_id || instrument;
        if (fraction.trim()) payload.size_fraction = fraction.trim();
        else if (notional.trim()) payload.notional = notional.trim();
      }
      const result = await apiPost<any>(`/market/rl/episodes/${encodeURIComponent(selectedEpisode)}/step`, payload);
      toast.push(`Transition ${result.transition_count} recorded${result.terminated ? " · episode terminated" : result.truncated ? " · episode truncated" : ""}.`, "success");
      detail.refresh();
      transitions.refresh();
      episodes.refresh();
      health.refresh();
    } catch (error: any) {
      toast.push(error?.detail || "The simulated action was refused.", "error");
    } finally { setSaving(false); }
  };

  const resetEpisode = async () => {
    if (!selectedEpisode) return;
    setSaving(true);
    try {
      await apiPost(`/market/rl/episodes/${encodeURIComponent(selectedEpisode)}/reset`, {});
      toast.push("Episode reset; its previous experiences were cleared.", "success");
      detail.refresh();
      transitions.refresh();
      episodes.refresh();
    } catch (error: any) {
      toast.push(error?.detail || "The episode could not be reset.", "error");
    } finally { setSaving(false); }
  };

  const cancelEpisode = async () => {
    if (!selectedEpisode) return;
    setSaving(true);
    try {
      await apiPost(`/market/rl/episodes/${encodeURIComponent(selectedEpisode)}/cancel`, {});
      toast.push("Episode cancelled. Production books were not touched.", "success");
      detail.refresh();
      episodes.refresh();
    } catch (error: any) {
      toast.push(error?.detail || "The episode could not be cancelled.", "error");
    } finally { setSaving(false); }
  };

  return (
    <div className="page">
      <PageHeader
        eyebrow="Research / Reinforcement learning"
        title={<>Historical RL research, <em>isolated by construction.</em></>}
        description="A point-in-time market environment with its own simulated cash, position and experience database. It cannot submit NM-10 orders, mutate production portfolios, change autonomy or promote a policy."
        meta={<><span className="meta-item"><BrainCircuit size={13} /> {status.data?.contract_version || "PI-13"}</span><span className="meta-item"><ShieldCheck size={13} /> simulated only</span><span className="meta-item"><Activity size={13} /> {status.data?.active_environments ?? 0} live episode(s)</span></>}
        actions={<><Button variant="ghost" icon={<RefreshCw size={14} />} onClick={() => { status.refresh(); health.refresh(); environments.refresh(); episodes.refresh(); detail.refresh(); transitions.refresh(); }}>Refresh</Button><Button variant="primary" icon={<Plus size={15} />} onClick={createEnvironment} loading={saving}>New environment</Button></>}
      />
      <Banner tone="info" title="Experimental research — not trading performance">
        Observations use completed historical bars only and decisions fill no earlier than the next bar’s open. Rewards are an interface with a zero total until PI-14 defines a policy. No policy is promoted automatically.
      </Banner>
      {status.error && <ErrorBlock message={status.error.detail} onRetry={status.refresh} />}
      <div className="metric-grid metric-grid-four">
        <Metric label="Simulated equity" value={<Money value={portfolio.equity} />} help="VIRTUAL BOOK · INR" tone={summary.total_return == null ? "neutral" : num(summary.total_return) < 0 ? "bad" : "good"} />
        <Metric label="Virtual cash" value={<Money value={portfolio.cash} />} help="1 NC = ₹1" />
        <Metric label="Simulated P&L" value={<Money value={summary.realized_pnl} />} help={`Unrealized ${portfolio.unrealized_pnl ?? "—"}`} />
        <Metric label="Drawdown" value={drawdown == null ? "—" : pct(num(drawdown) * 100)} help="Peak-to-current, simulated" tone={drawdown == null ? "neutral" : "bad"} />
      </div>
      <div className="dashboard-grid">
        <div className="stack">
          <Card>
            <CardHeader title="Environment configuration" subtitle="Immutable dataset window, observation and simulation settings" action={<Badge tone="neutral">{envRows.length} saved</Badge>} />
            <div className="form-grid">
              <Field label="Environment name"><TextInput value={name} onChange={(event) => setName(event.target.value)} /></Field>
              <Field label="Instrument"><TextInput value={instrument} onChange={(event) => setInstrument(event.target.value)} placeholder="XNSE:ALPHA" /></Field>
              <Field label="PI-02 dataset id"><TextInput value={datasetId} onChange={(event) => setDatasetId(event.target.value)} placeholder="dataset id" /></Field>
              <Field label="Dataset version"><TextInput value={datasetVersion} onChange={(event) => setDatasetVersion(event.target.value)} placeholder="immutable version" /></Field>
              <Field label="Historical start"><TextInput type="date" value={start} onChange={(event) => setStart(event.target.value)} /></Field>
              <Field label="Historical end"><TextInput type="date" value={end} onChange={(event) => setEnd(event.target.value)} /></Field>
              <Field label="Initial virtual capital (INR)"><TextInput value={capital} onChange={(event) => setCapital(event.target.value)} /></Field>
              <Field label="Max steps"><TextInput value={maxSteps} onChange={(event) => setMaxSteps(event.target.value)} /></Field>
            </div>
            <div className="row-actions">
              <Button variant="secondary" onClick={createEnvironment} loading={saving}>Create environment</Button>
              <Button variant="primary" icon={<Play size={14} />} onClick={startEpisode} disabled={!currentEnv} loading={saving}>Start isolated episode</Button>
            </div>
            {currentEnv && (
              <div className="fact-list">
                <div><span>Environment</span><strong>{currentEnv.name} · {shortId(currentEnv.environment_id, 18)}</strong></div>
                <div><span>Dataset</span><strong>{currentEnv.dataset_id} @ {currentEnv.dataset_version}</strong></div>
                <div><span>Window</span><strong>{String(currentEnv.start).slice(0, 10)} → {String(currentEnv.end).slice(0, 10)}</strong></div>
                <div><span>Observation / action</span><strong>{currentEnv.feature_profile} · long-only BUY/SELL/HOLD/WAIT/NO_ACTION</strong></div>
                <div><span>Simulation</span><strong>Next-bar open · configured fees · bounded liquidity</strong></div>
                <div><span>Version</span><strong>{currentEnv.environment_version} · {shortId(currentEnv.config_hash, 16)}</strong></div>
              </div>
            )}
          </Card>
          <Card>
            <CardHeader title="Episode monitor" subtitle="Simulated portfolio and transition cursor" action={currentEpisode ? <StatusBadge value={currentEpisode.status} /> : undefined} />
            {detail.error && <ErrorBlock message={detail.error.detail} onRetry={detail.refresh} />}
            {!currentEpisode ? <EmptyState icon={<FlaskConical size={24} />} title="No episode selected" description="Create an environment and start an isolated historical episode." /> : (
              <>
                <div className="fact-list">
                  <div><span>Episode</span><strong>{shortId(currentEpisode.episode_id, 24)} · {currentEpisode.status}</strong></div>
                  <div><span>Simulation timestamp</span><strong>{currentEpisode.current_timestamp ? dateTime(currentEpisode.current_timestamp) : "—"}</strong></div>
                  <div><span>Steps / transitions</span><strong>{currentEpisode.step_count ?? currentEpisode.steps ?? 0} / {currentEpisode.transition_count ?? 0}</strong></div>
                  <div><span>Feature version</span><strong>{observation?.provenance?.feature_set_version || "unavailable"}</strong></div>
                  <div><span>Position</span><strong>{portfolio.positions?.length ? `${portfolio.positions[0].quantity} @ ${portfolio.positions[0].average_entry_price}` : "flat"}</strong></div>
                  <div><span>Exposure / fees</span><strong>{portfolio.exposure ?? "0"} · {portfolio.fees_paid ?? "0"} INR</strong></div>
                </div>
                <div className="row-actions">
                  <SelectInput value={action} onChange={(event) => setAction(event.target.value)}>
                    {ACTIONS.map((item) => <option value={item} key={item}>{title(item)}</option>)}
                  </SelectInput>
                  {["buy", "sell"].includes(action) && <>
                    <TextInput value={notional} onChange={(event) => setNotional(event.target.value)} placeholder="notional INR" aria-label="Action notional" />
                    <TextInput value={fraction} onChange={(event) => setFraction(event.target.value)} placeholder="or size fraction" aria-label="Action size fraction" />
                  </>}
                  <Button variant="primary" icon={<Play size={14} />} onClick={step} disabled={!isLive} loading={saving}>Step simulation</Button>
                  <Button variant="secondary" icon={<RotateCcw size={14} />} onClick={resetEpisode} loading={saving}>Reset</Button>
                  <Button variant="danger" icon={<Square size={14} />} onClick={cancelEpisode} disabled={!isLive} loading={saving}>Cancel</Button>
                </div>
                <Banner tone="info" title="Every action is simulated">RL orders have no proposal id, order id or portfolio id. They never reach NM-10, NM-08, the wallet, ledger or an autonomy controller.</Banner>
                {curve.length > 1 && <div className="chart-block"><div className="chart-block-head"><span>Simulated equity curve</span><Badge tone="neutral">Research, not live</Badge></div><LineChart data={curve} height={220} color="var(--gold)" empty="No transitions yet" /></div>}
              </>
            )}
          </Card>
          <Card>
            <CardHeader title="Transition history" subtitle="Bounded experience records with action, outcome and provenance" action={<Badge tone="neutral">{transitionRows.length}</Badge>} />
            <DataTable compact rows={transitionRows.slice().reverse()} rowKey={(row) => row.transition_id} empty={<EmptyState title="No transitions" description="Step an episode to record its first observation-action-outcome transition." />} columns={[
              { key: "sequence", label: "Seq", render: (row) => `#${row.sequence}` },
              { key: "timestamp", label: "Observed", render: (row) => String(row.timestamp || "").slice(0, 16).replace("T", " ") },
              { key: "action", label: "Action", render: (row) => <Badge tone={row.action?.action_type === "buy" ? "good" : row.action?.action_type === "sell" ? "bad" : "neutral"}>{title(row.action?.action_type || "—")}</Badge> },
              { key: "execution", label: "Execution", render: (row) => <><div>{row.execution?.status || "—"}</div><small>{row.execution?.reason || row.execution?.price_assumption}</small></> },
              { key: "reward", label: "Reward interface", render: (row) => <span title={JSON.stringify(row.reward?.components || {})}>{row.reward?.total ?? "0"} · {row.reward?.interface_only ? "interface only" : "policy"}</span> },
              { key: "next", label: "Next observation", render: (row) => shortId(row.next_observation?.observation_id || "—", 18) },
            ]} />
          </Card>
        </div>
        <div className="stack">
          <Card>
            <CardHeader title="Observation snapshot" subtitle="Timestamped, point-in-time, feature-versioned" action={observation ? <Badge tone="neutral">{observation.quality?.status || "unknown"}</Badge> : undefined} />
            {observation ? <>
              <div className="fact-list">
                <div><span>Timestamp</span><strong>{dateTime(observation.timestamp)}</strong></div>
                <div><span>Close / volume</span><strong>{observation.market?.close} · {observation.market?.volume}</strong></div>
                <div><span>1d / 20d return</span><strong>{observation.market?.return_1 ?? "—"} / {observation.market?.return_20 ?? "—"}</strong></div>
                <div><span>Volatility / relative volume</span><strong>{observation.market?.volatility_20 ?? "—"} / {observation.market?.relative_volume_20 ?? "—"}</strong></div>
                <div><span>Forecast</span><strong>{observation.forecasts?.direction || "unavailable"} · {observation.forecasts?.validity || "—"}</strong></div>
                <div><span>Missing / imputed / invalid</span><strong>{observation.quality?.missing?.length ?? 0} / {observation.quality?.imputed?.length ?? 0} / {observation.quality?.invalid?.length ?? 0}</strong></div>
              </div>
              <pre className="json-preview">{JSON.stringify({ features: observation.features, forecasts: observation.forecasts, quality: observation.quality, provenance: observation.provenance }, null, 2)}</pre>
            </> : <EmptyState title="No observation" description="The current observation appears after an episode starts." />}
          </Card>
          <Card>
            <CardHeader title="Environment health" subtitle="Bounded execution and storage" action={<Badge tone={health.data?.healthy === false ? "bad" : "neutral"}>{health.data?.healthy === false ? "attention" : "bounded"}</Badge>} />
            <div className="fact-list">
              <div><span>Step latency</span><strong>{health.data?.step_latency_ms ?? 0} ms avg</strong></div>
              <div><span>Observation latency</span><strong>{health.data?.observation_latency_ms ?? 0} ms avg</strong></div>
              <div><span>Active / limit</span><strong>{health.data?.active_environments ?? 0} / {health.data?.max_active_environments ?? 0}</strong></div>
              <div><span>Experience rows</span><strong>{health.data?.storage?.experiences ?? 0}</strong></div>
              <div><span>Database size</span><strong>{health.data?.storage?.size_bytes ?? 0} bytes</strong></div>
              <div><span>Last error</span><strong>{health.data?.last_error || "none"}</strong></div>
            </div>
          </Card>
          <Card>
            <CardHeader title="Episode list" subtitle="Newest first" action={<Badge tone="neutral">{episodeRows.length}</Badge>} />
            {episodes.loading ? <LoadingBlock rows={4} label="Reading episodes…" /> : <div className="rail-list">{episodeRows.map((row) => <button type="button" className={`rail-item ${selectedEpisode === row.episode_id ? "rail-item-active" : ""}`} key={row.episode_id} onClick={() => setSelectedEpisode(row.episode_id)}><span className="rail-icon"><FlaskConical size={15} /></span><span><strong>{shortId(row.episode_id, 22)}</strong><small>{row.environment_id} · {row.transition_count ?? 0} transitions</small></span><StatusBadge value={row.status} /></button>)}</div>}
            {!episodeRows.length && !episodes.loading && <EmptyState title="No episodes" description="Start one from a saved environment." />}
          </Card>
          <Card>
            <CardHeader title="Version contracts" subtitle="Observation, action, reward and experience schemas" />
            <div className="fact-list">
              <div><span>Observation</span><strong>{contracts.data?.observation_schema_version || "—"}</strong></div>
              <div><span>Action</span><strong>{contracts.data?.action_schema_version || "—"}</strong></div>
              <div><span>Reward</span><strong>{contracts.data?.reward_interface_version || "—"}</strong></div>
              <div><span>Experience</span><strong>{contracts.data?.experience_schema_version || "—"}</strong></div>
              <div><span>Policy promotion</span><strong>not available in PI-13</strong></div>
              <div><span>Algorithm</span><strong>reset/step, algorithm-independent</strong></div>
            </div>
          </Card>
        </div>
      </div>
    </div>
  );
}
