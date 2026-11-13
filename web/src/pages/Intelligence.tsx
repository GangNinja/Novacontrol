import { useEffect, useMemo, useState } from "react";
import {
  ArrowRight,
  BrainCircuit,
  CalendarClock,
  Check,
  ChevronRight,
  Crosshair,
  Eye,
  Filter,
  ListChecks,
  Pencil,
  Plus,
  Radar,
  RefreshCw,
  Search,
  Sparkles,
  Target,
  TrendingUp,
  Trash2,
} from "lucide-react";
import { apiDelete, apiPost } from "../api";
import { useApi, useDocumentTitle, navigate } from "../hooks";
import {
  arrayFrom,
  dateTime,
  money,
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
  ConfirmDialog,
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
  TextInput,
  useToast,
} from "../components/ui";

const WATCHLIST_KINDS = ["custom", "discovery", "activity", "event_driven", "liquidity", "sector"];
const FORECAST_HORIZONS = ["next_session", "few_sessions", "one_week", "one_month", "three_months"];
const WATCHLIST_MEMBER_GAP_MS = 1_000;
const WATCHLIST_ERROR_BACKOFF_MS = 5_000;
const WATCHLIST_PROVIDER_CAPACITY = 40;

type WatchlistLiveRefresh = {
  phase: "paused" | "empty" | "refreshing";
  position: number;
  total: number;
  instrumentId: string;
  lastAt: string;
  error: string;
};

function scoreOf(row: any): number {
  return num(row.last_score ?? row.score ?? row.evaluation?.score ?? row.total_score ?? row.candidate_score ?? row.quality_score, 0);
}

function instrumentIdOf(row: any): string {
  return String(row.instrument_id || row.instrument?.instrument_id || row.symbol || "");
}

export function DiscoveryPage() {
  useDocumentTitle("Asset Discovery");
  const toast = useToast();
  const candidates = useApi<any>("/market/discovery/candidates?limit=200");
  const watchlists = useApi<any>("/market/watchlists");
  const status = useApi<any>("/market/discovery/status");
  const [search, setSearch] = useState("");
  const [running, setRunning] = useState(false);
  const [selected, setSelected] = useState<any | null>(null);
  const [watchlistId, setWatchlistId] = useState("");
  const [adding, setAdding] = useState(false);
  const [addOpen, setAddOpen] = useState(false);
  const rows = arrayFrom(candidates.data, "candidates");
  const lists = arrayFrom(watchlists.data, "watchlists");
  const filtered = useMemo(() => {
    const text = search.trim().toLowerCase();
    return rows.filter((row) => !text || [instrumentIdOf(row), row.instrument?.name, row.sector, row.discovery_reason, row.reason].some((value) => String(value || "").toLowerCase().includes(text)));
  }, [rows, search]);
  const lastCycleAt = status.data?.last_cycle?.finished_at || status.data?.last_cycle?.started_at || status.data?.last_cycle_at;

  const runCycle = async () => {
    setRunning(true);
    try {
      const result = await apiPost<any>("/market/discovery/cycle", { force: true });
      toast.push(result?.reason || result?.detail || `Discovery cycle completed${result?.candidates !== undefined ? ` · ${result.candidates} candidates` : ""}.`, "success");
      candidates.refresh();
      status.refresh();
    } catch (error: any) {
      toast.push(error?.detail || "The discovery cycle could not run.", "error");
    } finally {
      setRunning(false);
    }
  };

  const addMember = async () => {
    if (!selected || !watchlistId) return;
    setAdding(true);
    try {
      await apiPost(`/market/watchlists/${encodeURIComponent(watchlistId)}/members`, {
        instrument_id: instrumentIdOf(selected),
        reason: "added from asset discovery",
      });
      toast.push("Instrument added to the watchlist.", "success");
      setAddOpen(false);
      watchlists.refresh();
    } catch (error: any) {
      toast.push(error?.detail || "The instrument could not be added.", "error");
    } finally {
      setAdding(false);
    }
  };

  return (
    <div className="page">
      <PageHeader
        eyebrow="Intelligence / Discovery"
        title={<>Find the next candidate, <em>not the next rumour.</em></>}
        description="Candidates are ranked from market events, liquidity and forecast coverage by the NM-05 discovery engine. Scores are measured, not editorial."
        meta={<><span className="meta-item"><Radar size={13} /> {candidates.data?.total ?? rows.length} candidates</span><span className="meta-item"><ListChecks size={13} /> {lists.length} watchlists</span><span className="meta-item"><CalendarClock size={13} /> {dateTime(new Date().toISOString())}</span></>}
        actions={<><Button variant="ghost" icon={<RefreshCw size={14} className={candidates.refreshing ? "spin" : undefined} />} onClick={candidates.refresh}>Refresh</Button><Button variant="primary" icon={<Crosshair size={15} />} loading={running} onClick={runCycle}>Run discovery cycle</Button></>}
      />
      {candidates.error && <ErrorBlock message={candidates.error.detail} onRetry={candidates.refresh} />}
      <div className="discovery-banner">
        <div className="discovery-banner-icon"><Sparkles size={18} /></div>
        <div><strong>Discovery is a proposal surface.</strong><span>No candidate is a buy instruction. A candidate must still pass forecast, strategy, risk and explicit paper-order steps.</span></div>
        <Badge tone="info" dot>NM-05</Badge>
      </div>
      <div className="metric-grid metric-grid-four">
        <div className="mini-stat"><span>Ranked</span><strong>{rows.length}</strong><small>in the current result</small></div>
        <div className="mini-stat"><span>Above 70</span><strong>{rows.filter((row) => scoreOf(row) >= 70).length}</strong><small>high-conviction candidates</small></div>
        <div className="mini-stat"><span>Watchlist coverage</span><strong>{rows.filter((row) => row.watchlist_id || row.on_watchlist).length}</strong><small>already tracked</small></div>
        <div className="mini-stat"><span>Engine</span><strong>{title(status.data?.lifecycle || "ready")}</strong><small>{lastCycleAt ? `Last cycle ${timeAgo(lastCycleAt)}` : "Cycle history not exposed"}</small></div>
      </div>
      <Card>
        <CardHeader title="Discovery candidates" subtitle="Ranked by the discovery engine" action={<Badge tone="neutral">{filtered.length} shown</Badge>} />
        <div className="filter-row">
          <SearchInput value={search} onChange={setSearch} placeholder="Search instrument, sector or reason…" />
          <Button variant="ghost" size="sm" onClick={() => navigate("watchlists")} icon={<ListChecks size={14} />}>Manage watchlists</Button>
        </div>
        {candidates.loading ? <LoadingBlock rows={5} label="Ranking candidates…" /> : (
          <DataTable
            rows={filtered}
            rowKey={(row) => row.candidate_id || instrumentIdOf(row) || `${row.instrument_id}-${row.created_at}`}
            onRowClick={setSelected}
            empty={<EmptyState icon={<Radar size={22} />} title={rows.length ? "No candidates match" : "No candidates discovered yet"} description={rows.length ? "Adjust the search." : "Run a discovery cycle to ask the backend for a ranked set."} action={!rows.length ? <Button variant="primary" size="sm" onClick={runCycle} loading={running}>Run first cycle</Button> : undefined} />}
            columns={[
              { key: "instrument", label: "Instrument", sortable: true, sortValue: (row) => instrumentIdOf(row), render: (row) => <div className="identity-cell"><span className="symbol-mark">{String(row.symbol || row.instrument?.symbol || "?").slice(0, 2)}</span><div><strong>{row.instrument?.symbol || row.symbol || instrumentIdOf(row)}</strong><small>{row.instrument?.name || row.name || row.sector || "Candidate"}</small></div></div> },
              { key: "score", label: "Score", sortable: true, sortValue: scoreOf, render: (row) => <span className="score-pill">{scoreOf(row).toFixed(1)}<small>/100</small></span> },
              { key: "sector", label: "Sector", render: (row) => <Badge tone="neutral">{title(row.sector || "—")}</Badge> },
              { key: "reason", label: "Why it surfaced", render: (row) => <span className="clamp-cell">{row.discovery_reason || row.reason || row.explanation || row.summary || "—"}</span> },
              { key: "created_at", label: "Discovered", sortable: true, sortValue: (row) => row.first_discovered_at || row.created_at || row.discovered_at || "", render: (row) => timeAgo(row.first_discovered_at || row.created_at || row.discovered_at) },
              { key: "action", label: "", align: "right", render: (row) => <button type="button" className="row-action" onClick={(event) => { event.stopPropagation(); setSelected(row); setWatchlistId(lists[0]?.watchlist_id || ""); setAddOpen(true); }}><Plus size={14} /> Watchlist</button> },
            ]}
          />
        )}
      </Card>

      <Modal
        open={!!selected && !addOpen}
        onClose={() => setSelected(null)}
        title={selected ? `${selected.instrument?.symbol || selected.symbol || instrumentIdOf(selected)} candidate` : "Candidate"}
        description={selected ? `Discovered ${timeAgo(selected.created_at || selected.discovered_at)} · score ${scoreOf(selected).toFixed(1)}` : ""}
        footer={<><Button variant="ghost" onClick={() => setSelected(null)}>Close</Button><Button variant="primary" icon={<Plus size={14} />} onClick={() => { setWatchlistId(lists[0]?.watchlist_id || ""); setAddOpen(true); }} disabled={!lists.length}>Add to watchlist</Button></>}
      >
        {selected && (
          <div className="detail-grid">
            <div className="detail-main">
              <p className="detail-prose">{selected.reason || selected.explanation || "The discovery engine returned this candidate without a narrative."}</p>
              <div className="detail-section">
                <h3>Supporting facts</h3>
                <div className="fact-list">
                  {Object.entries(selected).filter(([key, value]) => !["candidate_id", "instrument", "reason", "explanation", "summary", "created_at", "updated_at"].includes(key) && (typeof value === "string" || typeof value === "number" || typeof value === "boolean")).slice(0, 10).map(([key, value]) => (
                    <div key={key}><span>{title(key)}</span><strong>{String(value)}</strong></div>
                  ))}
                </div>
              </div>
            </div>
            <aside className="detail-aside"><div className="detail-fact"><span>Score</span><strong>{scoreOf(selected).toFixed(1)} / 100</strong></div><div className="detail-fact"><span>Sector</span><strong>{title(selected.sector || "—")}</strong></div><div className="detail-fact"><span>Candidate ID</span><code>{shortId(selected.candidate_id || "—", 18)}</code></div><div className="detail-fact"><span>Source</span><span>{selected.source || "Discovery engine"}</span></div></aside>
          </div>
        )}
      </Modal>

      <Modal open={addOpen} onClose={() => setAddOpen(false)} title="Add to a watchlist" description="This is a persisted membership with a reason, not a local bookmark." footer={<><Button variant="ghost" onClick={() => setAddOpen(false)}>Cancel</Button><Button variant="primary" loading={adding} onClick={addMember} disabled={!watchlistId}>Add instrument</Button></>}>
        {!lists.length ? <EmptyState icon={<ListChecks size={20} />} title="Create a watchlist first" description="Watchlists are managed in the Watchlists page." action={<Button variant="primary" size="sm" onClick={() => { setAddOpen(false); navigate("watchlists"); }}>Open watchlists</Button>} /> : (
          <Field label="Watchlist">
            <SelectInput value={watchlistId} onChange={(event) => setWatchlistId(event.target.value)}>
              {lists.map((list: any) => <option value={list.watchlist_id} key={list.watchlist_id}>{list.name} · {list.entries ?? list.active_count ?? list.member_count ?? 0} members</option>)}
            </SelectInput>
          </Field>
        )}
      </Modal>
    </div>
  );
}

export function WatchlistsPage() {
  useDocumentTitle("Watchlists");
  const toast = useToast();
  const lists = useApi<any>("/market/watchlists");
  const instruments = useApi<any>("/market/instruments?limit=200");
  const status = useApi<any>("/market/status", { refreshMs: 15_000 });
  const plan = status.data?.market_session?.refresh || {};
  const live = plan.phase === "live";
  const refreshMs = Math.max(60_000, Number(plan.interval_ms) || 15 * 60_000);
  const [selectedId, setSelectedId] = useState("");
  const [createOpen, setCreateOpen] = useState(false);
  const [name, setName] = useState("");
  const [kind, setKind] = useState("custom");
  const [description, setDescription] = useState("");
  const [addOpen, setAddOpen] = useState(false);
  const [instrumentId, setInstrumentId] = useState("");
  const [reason, setReason] = useState("");
  const [saving, setSaving] = useState(false);
  const [renameTarget, setRenameTarget] = useState<any | null>(null);
  const [renameName, setRenameName] = useState("");
  const [removeTarget, setRemoveTarget] = useState<any | null>(null);
  const [liveRefresh, setLiveRefresh] = useState<WatchlistLiveRefresh>({
    phase: "paused",
    position: 0,
    total: 0,
    instrumentId: "",
    lastAt: "",
    error: "",
  });
  const rows = arrayFrom(lists.data, "watchlists");
  const detail = useApi<any>(selectedId ? `/market/watchlists/${encodeURIComponent(selectedId)}?limit=200` : null);
  const current = rows.find((list) => list.watchlist_id === selectedId) || rows[0];
  const currentId = selectedId || rows[0]?.watchlist_id || "";
  const members = useMemo(() => arrayFrom(detail.data, "members"), [detail.data]);
  const memberIds = useMemo(
    () => members
      .map((row) => String(row.instrument_id || row.instrument?.instrument_id || ""))
      .filter(Boolean),
    [members],
  );
  const memberKey = memberIds.join("|");

  useEffect(() => {
    if (!selectedId && rows.length) setSelectedId(rows[0].watchlist_id);
  }, [rows, selectedId]);

  useEffect(() => {
    if (!live || !currentId || !memberIds.length) {
      setLiveRefresh({
        phase: live ? "empty" : "paused",
        position: 0,
        total: memberIds.length,
        instrumentId: "",
        lastAt: "",
        error: "",
      });
      return;
    }

    let cancelled = false;
    let index = 0;
    let timer: number | undefined;
    let backoff = false;

    const refreshMember = async () => {
      const instrumentId = memberIds[index % memberIds.length];
      setLiveRefresh((previous) => ({
        ...previous,
        phase: "refreshing",
        position: (index % memberIds.length) + 1,
        total: memberIds.length,
        instrumentId,
        error: "",
      }));
      try {
        const report = await apiPost<any>("/market/data/ingest", {
          instrument_ids: [instrumentId],
          intervals: ["1m"],
          // The live chart owns quotes. The watchlist pass spends its whole
          // provider budget on the one-minute series, one member at a time.
          include_quotes: false,
          trigger: "watchlist-live",
        });
        if (cancelled) return;
        const failure = report?.ok
          ? ""
          : report?.errors?.[0] || "the provider did not return a complete refresh";
        // A single unsupported or unavailable member must not block the rest
        // of the list. Advance, surface the failure, and let the pass back off.
        index += 1;
        backoff = Boolean(failure);
        setLiveRefresh((previous) => ({
          ...previous,
          position: (index % memberIds.length) + 1,
          lastAt: failure ? previous.lastAt : new Date().toISOString(),
          error: failure,
        }));
        if (index % memberIds.length === 0) detail.refresh();
      } catch (error: any) {
        if (cancelled) return;
        backoff = true;
        setLiveRefresh((previous) => ({
          ...previous,
          error: error?.detail || error?.message || "The watchlist refresh was refused.",
        }));
      }
      if (!cancelled) {
        const passComplete = index > 0 && index % memberIds.length === 0;
        timer = window.setTimeout(
          () => void refreshMember(),
          backoff
            ? WATCHLIST_ERROR_BACKOFF_MS
            : passComplete
              ? refreshMs
              : WATCHLIST_MEMBER_GAP_MS,
        );
      }
    };

    void refreshMember();
    return () => {
      cancelled = true;
      if (timer) window.clearTimeout(timer);
    };
  }, [currentId, live, memberKey, refreshMs]); // detail.refresh is intentionally excluded

  const createList = async () => {
    setSaving(true);
    try {
      const body = await apiPost<any>("/market/watchlists", { name, kind, description });
      toast.push("Watchlist created.", "success");
      setCreateOpen(false);
      setName("");
      setDescription("");
      lists.refresh();
      if (body?.watchlist_id) setSelectedId(body.watchlist_id);
    } catch (error: any) {
      toast.push(error?.detail || "The watchlist could not be created.", "error");
    } finally { setSaving(false); }
  };

  const renameList = async () => {
    if (!renameTarget || !renameName.trim()) return;
    setSaving(true);
    try {
      const body = await apiPost<any>(`/market/watchlists/${encodeURIComponent(renameTarget.watchlist_id)}/rename`, { name: renameName.trim() });
      toast.push("Watchlist renamed; its id and membership history are unchanged.", "success");
      setRenameTarget(null);
      setRenameName("");
      lists.refresh();
      detail.refresh();
      if (body?.watchlist_id) setSelectedId(body.watchlist_id);
    } catch (error: any) {
      toast.push(error?.detail || "The watchlist could not be renamed.", "error");
    } finally { setSaving(false); }
  };

  const addInstrument = async () => {
    if (!currentId || !instrumentId) return;
    setSaving(true);
    try {
      await apiPost(`/market/watchlists/${encodeURIComponent(currentId)}/members`, { instrument_id: instrumentId, reason });
      toast.push("Instrument added.", "success");
      setAddOpen(false);
      setInstrumentId("");
      setReason("");
      detail.refresh();
      lists.refresh();
    } catch (error: any) {
      toast.push(error?.detail || "The instrument could not be added.", "error");
    } finally { setSaving(false); }
  };

  const removeInstrument = async () => {
    if (!removeTarget || !currentId) return;
    setSaving(true);
    try {
      await apiDelete(`/market/watchlists/${encodeURIComponent(currentId)}/members/${encodeURIComponent(instrumentIdOf(removeTarget))}?reason=removed%20from%20the%20desk`);
      toast.push("Instrument removed; its membership history is retained.", "success");
      setRemoveTarget(null);
      detail.refresh();
      lists.refresh();
    } catch (error: any) {
      toast.push(error?.detail || "The instrument could not be removed.", "error");
    } finally { setSaving(false); }
  };

  return (
    <div className="page">
      <PageHeader
        eyebrow="Intelligence / Watchlists"
        title={<>A deliberate shortlist, <em>not an endless feed.</em></>}
        description="Watchlists persist memberships and reasons in the backend. Removal keeps history rather than silently forgetting a decision."
        meta={<><span className="meta-item"><ListChecks size={13} /> {rows.length} lists</span><span className="meta-item"><Target size={13} /> {detail.data?.total ?? 0} members in view</span></>}
        actions={<><Button variant="ghost" icon={<RefreshCw size={14} />} onClick={() => { lists.refresh(); detail.refresh(); }}>Refresh</Button><Button variant="primary" icon={<Plus size={15} />} onClick={() => setCreateOpen(true)}>New watchlist</Button></>}
      />
      {lists.error && <ErrorBlock message={lists.error.detail} onRetry={lists.refresh} />}
      {current && memberIds.length > 0 && (
        <div className="notice-strip" title={plan.provider_note || undefined}>
          <Target size={16} />
          <span>
            <strong>{live ? "15-minute watchlist refresh." : "Live watchlist refresh is paused."}</strong>{" "}
            {live
              ? liveRefresh.phase === "refreshing"
                ? `Refreshing ${liveRefresh.instrumentId} (${liveRefresh.position}/${liveRefresh.total}) one member at a time at the provider's 1 request/s pace.`
                : "Starting the next member at the provider's 1 request/s pace."
              : `The market session is ${title(status.data?.market_session?.state || plan.phase || "unknown")}; no provider requests are made.`}
            {live && liveRefresh.lastAt ? ` Last completed ${timeAgo(liveRefresh.lastAt)}.` : ""}
            {live && memberIds.length > WATCHLIST_PROVIDER_CAPACITY
              ? ` Each pass uses the provider's 1 request/s pace; the next pass starts after the refresh interval.`
              : ""}
          </span>
          <Badge tone={live ? "good" : "neutral"}>{live ? "15m" : "Paused"}</Badge>
        </div>
      )}
      {liveRefresh.error && (
        <div className="inline-warning">
          {liveRefresh.error} The pass backs off for five seconds before retrying.
        </div>
      )}
      <div className="split-layout watchlist-layout">
        <Card className="list-rail">
          <CardHeader title="Your lists" subtitle="Saved in NovaMarket" />
          {lists.loading ? <LoadingBlock rows={4} label="Reading watchlists…" /> : rows.length ? (
            <div className="rail-list">
              {rows.map((list: any) => (
                <button type="button" className={`rail-item ${currentId === list.watchlist_id ? "rail-item-active" : ""}`} key={list.watchlist_id} onClick={() => setSelectedId(list.watchlist_id)}>
                  <span className="rail-icon"><ListChecks size={15} /></span>
                  <span><strong>{list.name || "Untitled"}</strong><small>{title(list.kind || "custom")} · {list.entries ?? list.active_count ?? list.member_count ?? 0} members</small></span>
                  <ChevronRight size={14} />
                </button>
              ))}
            </div>
          ) : <EmptyState title="No watchlists" description="Create a list to track candidates across discovery and forecasts." action={<Button size="sm" variant="primary" onClick={() => setCreateOpen(true)}>Create watchlist</Button>} />}
        </Card>
        <div className="stack">
          {current ? (
            <Card>
              <CardHeader title={current.name || "Watchlist"} subtitle={current.description || "Persisted membership and ranking"} action={<div className="row-actions"><Badge tone="neutral">{detail.data?.total ?? current.entries ?? current.member_count ?? 0} members</Badge><Button size="sm" variant="ghost" icon={<Pencil size={14} />} onClick={() => { setRenameTarget(current); setRenameName(current.name || ""); }}>Rename</Button></div>} />
              <div className="filter-row">
                <div className="inline-meta"><span className="meta-item"><i className={`health-dot health-good`} /> Active</span><span className="meta-item">Kind {title(current.kind || "custom")}</span></div>
                <Button size="sm" variant="primary" icon={<Plus size={14} />} onClick={() => setAddOpen(true)}>Add instrument</Button>
              </div>
              {detail.loading ? <LoadingBlock rows={4} label="Reading members…" /> : detail.error ? <ErrorBlock message={detail.error.detail} onRetry={detail.refresh} /> : (
                <DataTable
                  rows={arrayFrom(detail.data, "members")}
                  rowKey={(row) => row.entry_id || instrumentIdOf(row)}
                  empty={<EmptyState icon={<Search size={20} />} title="This list is empty" description="Add an instrument or let discovery propose one." />}
                  columns={[
                    { key: "instrument", label: "Instrument", render: (row) => <div className="identity-cell"><span className="symbol-mark">{(row.instrument?.symbol || row.instrument_id || "?").slice(0, 2)}</span><div><strong>{row.instrument?.symbol || row.instrument_id}</strong><small>{row.instrument?.name || "Instrument"}</small></div></div> },
                    { key: "status", label: "Status", render: (row) => <StatusBadge value={row.status || "active"} /> },
                    { key: "score", label: "Score", render: (row) => <span className="score-pill">{scoreOf(row).toFixed(1)}</span>, sortable: true, sortValue: scoreOf },
                    { key: "reason", label: "Reason", render: (row) => <span className="clamp-cell">{row.reason || row.note || "—"}</span> },
                    { key: "added_at", label: "Added", render: (row) => timeAgo(row.added_at || row.created_at) },
                    { key: "action", label: "", align: "right", render: (row) => <button type="button" className="row-action row-action-danger" onClick={() => setRemoveTarget(row)}><Trash2 size={13} /> Remove</button> },
                  ]}
                />
              )}
            </Card>
          ) : <Card><EmptyState icon={<ListChecks size={22} />} title="Create your first watchlist" description="A watchlist is a persisted filter for the candidates you want to keep close." action={<Button variant="primary" onClick={() => setCreateOpen(true)}>New watchlist</Button>} /></Card>}
          {detail.data && (
            <Card>
              <CardHeader title="Membership contract" subtitle="What the backend keeps" />
              <div className="contract-list">
                <div><Check size={15} /><span>Members have explicit status and reason fields.</span></div>
                <div><Check size={15} /><span>Removal is a recorded state change, not an edited row.</span></div>
                <div><Check size={15} /><span>Watchlists never place an order or move funds.</span></div>
              </div>
            </Card>
          )}
        </div>
      </div>

      <Modal open={createOpen} onClose={() => setCreateOpen(false)} title="Create a watchlist" description="Give the list a durable purpose so its membership can be reviewed later." footer={<><Button variant="ghost" onClick={() => setCreateOpen(false)}>Cancel</Button><Button variant="primary" loading={saving} disabled={!name.trim()} onClick={createList}>Create list</Button></>}>
        <div className="form-grid">
          <Field label="Name"><TextInput value={name} onChange={(event) => setName(event.target.value)} placeholder="e.g. Energy transition" /></Field>
          <Field label="Kind"><SelectInput value={kind} onChange={(event) => setKind(event.target.value)}>{WATCHLIST_KINDS.map((item) => <option key={item} value={item}>{title(item)}</option>)}</SelectInput></Field>
        </div>
        <Field label="Description" hint="Optional context retained with the list."><TextInput value={description} onChange={(event) => setDescription(event.target.value)} placeholder="Why this list exists" /></Field>
      </Modal>
      <Modal open={addOpen} onClose={() => setAddOpen(false)} title={`Add to ${current?.name || "watchlist"}`} description="The backend will validate the instrument identity and persist the membership." footer={<><Button variant="ghost" onClick={() => setAddOpen(false)}>Cancel</Button><Button variant="primary" loading={saving} disabled={!instrumentId} onClick={addInstrument}>Add instrument</Button></>}>
        <Field label="Instrument">
          <SelectInput value={instrumentId} onChange={(event) => setInstrumentId(event.target.value)}>
            <option value="">Choose an instrument…</option>
            {arrayFrom(instruments.data, "items").map((item) => <option key={item.instrument_id} value={item.instrument_id}>{item.symbol || item.instrument_id} · {item.name || item.asset_class}</option>)}
          </SelectInput>
        </Field>
        <Field label="Reason" hint="Optional but useful for later review."><TextInput value={reason} onChange={(event) => setReason(event.target.value)} placeholder="Why this belongs here" /></Field>
      </Modal>
      <Modal open={!!renameTarget} onClose={() => { setRenameTarget(null); setRenameName(""); }} title="Rename watchlist" description="The stable watchlist id and every membership record are retained; only the displayed name changes." footer={<><Button variant="ghost" onClick={() => { setRenameTarget(null); setRenameName(""); }}>Cancel</Button><Button variant="primary" loading={saving} disabled={!renameName.trim() || renameName.trim() === renameTarget?.name} onClick={renameList}>Save name</Button></>}>
        <Field label="Name" hint="The backend refuses a blank name or a duplicate list name."><TextInput value={renameName} onChange={(event) => setRenameName(event.target.value)} placeholder="e.g. Energy transition" /></Field>
      </Modal>
      <ConfirmDialog open={!!removeTarget} title="Remove this instrument?" description="The membership moves to removed and its history remains in the backend. This does not affect any portfolio." confirmLabel="Remove membership" busy={saving} onConfirm={removeInstrument} onClose={() => setRemoveTarget(null)} />
    </div>
  );
}

export function ForecastsPage() {
  useDocumentTitle("Forecasts");
  const toast = useToast();
  const forecasts = useApi<any>("/market/forecasts?limit=200");
  const instruments = useApi<any>("/market/instruments?limit=200");
  const [search, setSearch] = useState("");
  const [horizon, setHorizon] = useState("");
  const [createOpen, setCreateOpen] = useState(false);
  const [instrumentId, setInstrumentId] = useState("");
  const [forecastHorizon, setForecastHorizon] = useState("next_session");
  const [saving, setSaving] = useState(false);
  const [selected, setSelected] = useState<any | null>(null);
  const rows = arrayFrom(forecasts.data, "forecasts");
  const filtered = useMemo(() => {
    const text = search.toLowerCase().trim();
    return rows.filter((row) => (!text || [row.instrument_id, row.instrument?.symbol, row.model_id].some((value) => String(value || "").toLowerCase().includes(text))) && (!horizon || row.horizon === horizon));
  }, [rows, search, horizon]);

  const create = async () => {
    setSaving(true);
    try {
      await apiPost("/market/forecasts", { instrument_id: instrumentId, horizon: forecastHorizon });
      toast.push("Forecast generated and stored by the backend.", "success");
      setCreateOpen(false);
      forecasts.refresh();
    } catch (error: any) {
      toast.push(error?.detail || "The forecast could not be generated.", "error");
    } finally { setSaving(false); }
  };

  return (
    <div className="page">
      <PageHeader
        eyebrow="Intelligence / Forecasts"
        title={<>Probabilities, <em>with the uncertainty left in.</em></>}
        description="Every forecast is a stored range with a model identity and data cutoff. It is a simulation input, never a guaranteed price."
        meta={<><span className="meta-item"><TrendingUp size={13} /> {forecasts.data?.total ?? rows.length} stored</span><span className="meta-item"><BrainCircuit size={13} /> {new Set(rows.map((row) => row.model_id).filter(Boolean)).size} models in view</span></>}
        actions={<><Button variant="ghost" icon={<RefreshCw size={14} />} onClick={forecasts.refresh}>Refresh</Button><Button variant="primary" icon={<Sparkles size={15} />} onClick={() => setCreateOpen(true)}>Generate forecast</Button></>}
      />
      {forecasts.error && <ErrorBlock message={forecasts.error.detail} onRetry={forecasts.refresh} />}
      <div className="notice-strip"><BrainCircuit size={16} /><span><strong>Uncertainty is part of the answer.</strong> A point estimate without its interval and confidence is not shown as a trading instruction.</span><Badge tone="warn">Simulation input</Badge></div>
      <Card>
        <CardHeader title="Stored forecasts" subtitle="Newest first, with model provenance" action={<Badge tone="neutral">{filtered.length} shown</Badge>} />
        <div className="filter-row">
          <SearchInput value={search} onChange={setSearch} placeholder="Search instrument or model…" />
          <SelectInput value={horizon} onChange={(event) => setHorizon(event.target.value)} aria-label="Forecast horizon">
            <option value="">All horizons</option>
            {FORECAST_HORIZONS.map((item) => <option key={item} value={item}>{title(item)}</option>)}
          </SelectInput>
        </div>
        {forecasts.loading ? <LoadingBlock rows={5} label="Reading forecast history…" /> : (
          <DataTable
            rows={filtered}
            rowKey={(row) => row.forecast_id || `${row.instrument_id}-${row.created_at}`}
            onRowClick={setSelected}
            empty={<EmptyState icon={<TrendingUp size={22} />} title={rows.length ? "No forecasts match" : "No forecasts yet"} description={rows.length ? "Adjust the search or horizon." : "Generate one from an instrument with enough history for the backend model."} action={!rows.length ? <Button variant="primary" size="sm" onClick={() => setCreateOpen(true)}>Generate forecast</Button> : undefined} />}
            columns={[
              { key: "instrument", label: "Instrument", render: (row) => <div className="identity-cell"><span className="symbol-mark">{(row.instrument?.symbol || row.instrument_id || "?").slice(0, 2)}</span><div><strong>{row.instrument?.symbol || row.instrument_id}</strong><small>{title(row.horizon || "horizon")}</small></div></div>, sortable: true, sortValue: (row) => row.instrument_id || "" },
              { key: "predicted_price", label: "Point estimate", sortable: true, sortValue: (row) => num(row.predicted_price, 0), render: (row) => <div><strong>{money(row.predicted_price)}</strong><small className="table-sub">return {pct(num(row.predicted_return, 0) * 100)}</small></div> },
              { key: "interval", label: "Range", render: (row) => <span className="range-cell">{money(row.lower_price)}<i>→</i>{money(row.upper_price)}</span> },
              { key: "uncertainty", label: "Uncertainty", render: (row) => <Badge tone={String(row.uncertainty).toLowerCase() === "high" ? "bad" : String(row.uncertainty).toLowerCase() === "medium" ? "warn" : "good"}>{title(row.uncertainty || "—")}</Badge> },
              { key: "model_id", label: "Model", render: (row) => <span className="mono-small">{row.model_id || "—"} <small>v{row.model_version || "?"}</small></span> },
              { key: "created_at", label: "Created", render: (row) => timeAgo(row.created_at), sortable: true, sortValue: (row) => row.created_at || "" },
              { key: "status", label: "Status", render: (row) => <StatusBadge value={row.status || "pending"} /> },
            ]}
          />
        )}
      </Card>
      <div className="two-col-grid">
        <Card><CardHeader title="How to read this" subtitle="A forecast is a probability, not a promise." /><div className="contract-list"><div><Check size={15} /><span>Point estimate is the model's centre, not a guaranteed close.</span></div><div><Check size={15} /><span>The interval makes the uncertainty visible; it can miss.</span></div><div><Check size={15} /><span>Model version and data cutoff are retained for evaluation.</span></div><div><Check size={15} /><span>Outcomes are evaluated later against actual observations.</span></div></div></Card>
        <Card><CardHeader title="Coverage" subtitle="Models in the current result" /><div className="model-list">{Array.from(new Set(rows.map((row) => row.model_id).filter(Boolean))).slice(0, 8).map((model) => <div key={model}><span className="model-dot" /><strong>{model}</strong><small>{rows.filter((row) => row.model_id === model).length} forecasts</small></div>)}</div>{!rows.length && <EmptyState title="No model coverage yet" description="Generate a forecast to populate this view." />}</Card>
      </div>

      <Modal open={createOpen} onClose={() => setCreateOpen(false)} title="Generate a forecast" description="The backend will use its validated historical window and refuse if the instrument lacks enough data." footer={<><Button variant="ghost" onClick={() => setCreateOpen(false)}>Cancel</Button><Button variant="primary" loading={saving} disabled={!instrumentId} onClick={create}>Generate and store</Button></>}>
        <div className="form-grid">
          <Field label="Instrument"><SelectInput value={instrumentId} onChange={(event) => setInstrumentId(event.target.value)}><option value="">Choose an instrument…</option>{arrayFrom(instruments.data, "items").map((item) => <option key={item.instrument_id} value={item.instrument_id}>{item.symbol || item.instrument_id} · {item.name || item.asset_class}</option>)}</SelectInput></Field>
          <Field label="Horizon"><SelectInput value={forecastHorizon} onChange={(event) => setForecastHorizon(event.target.value)}>{FORECAST_HORIZONS.map((item) => <option key={item} value={item}>{title(item)}</option>)}</SelectInput></Field>
        </div>
        <div className="inline-warning">A forecast is generated only from data the backend has already ingested. No future price is read.</div>
      </Modal>

      <Modal open={!!selected} onClose={() => setSelected(null)} title={selected ? `${selected.instrument?.symbol || selected.instrument_id} · ${title(selected.horizon)}` : "Forecast"} description={selected ? `Model ${selected.model_id || "—"} · ${dateTime(selected.created_at)}` : ""} footer={<Button variant="secondary" onClick={() => setSelected(null)}>Close</Button>}>
        {selected && <div className="forecast-detail">
          <div className="forecast-band"><div><span>Lower</span><strong>{money(selected.lower_price)}</strong></div><div className="forecast-band-center"><span>Point estimate</span><strong>{money(selected.predicted_price)}</strong></div><div><span>Upper</span><strong>{money(selected.upper_price)}</strong></div></div>
          <div className="detail-grid"><div className="detail-main"><div className="fact-list"><div><span>Predicted return</span><strong>{pct(num(selected.predicted_return, 0) * 100)}</strong></div><div><span>Probability up</span><strong>{pctUnsigned(num(selected.p_up, 0) * 100)}</strong></div><div><span>Probability down</span><strong>{pctUnsigned(num(selected.p_down, 0) * 100)}</strong></div><div><span>Probability sideways</span><strong>{pctUnsigned(num(selected.p_sideways, 0) * 100)}</strong></div><div><span>Regime</span><strong>{title(selected.regime || "—")}</strong></div><div><span>Data quality</span><strong>{title(selected.data_quality || "—")}</strong></div></div></div><aside className="detail-aside"><div className="detail-fact"><span>Uncertainty</span><Badge tone={String(selected.uncertainty).toLowerCase() === "high" ? "bad" : "warn"}>{title(selected.uncertainty || "—")}</Badge></div><div className="detail-fact"><span>Interval width</span><strong>{pct(num(selected.uncertainty_width, 0) * 100)}</strong></div><div className="detail-fact"><span>Target</span><strong>{dateTime(selected.target_at)}</strong></div><div className="detail-fact"><span>Cutoff</span><strong>{dateTime(selected.data_cutoff)}</strong></div><div className="detail-fact"><span>Forecast ID</span><code>{shortId(selected.forecast_id, 18)}</code></div></aside></div>
        </div>}
      </Modal>
    </div>
  );
}
