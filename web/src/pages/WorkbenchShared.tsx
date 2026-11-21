import { useEffect, useMemo, useState } from "react";
import {
  CalendarClock,
  Check,
  ChevronRight,
  History,
  Layers3,
  ListChecks,
  Pencil,
  Plus,
  RefreshCw,
  ShieldAlert,
  Trash2,
  X,
} from "lucide-react";
import { apiDelete, apiPost } from "../api";
import { useApi, useLocalState } from "../hooks";
import { arrayFrom, dateTime, money, num, pct, timeAgo, title } from "../lib";
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
  SearchInput,
  SelectInput,
  StatusBadge,
  TextInput,
  useToast,
} from "../components/ui";
import {
  CandlestickChart,
  type CandleBar,
  type CandleLevel,
  type CandleZone,
} from "../components/charts";

export type DeskRow = Record<string, any>;

export function symbolOf(row: any): string {
  return String(row?.symbol || row?.instrument?.symbol || row?.instrument_id?.split?.(":")?.pop?.() || "—");
}

export function nameOf(row: any): string {
  return String(row?.name || row?.instrument?.name || row?.instrument_id || "Instrument");
}

export function ScoreBar({ value, tone = "accent" }: { value: unknown; tone?: "accent" | "blue" | "danger" | "gold" }) {
  // A missing score is not a zero score: keep the meter off the page.
  const numeric = num(value, NaN);
  if (!Number.isFinite(numeric)) return <span className="muted">—</span>;
  const score = Math.max(0, Math.min(100, numeric));
  return (
    <div className={`score-meter score-meter-${tone}`} title={`${score.toFixed(1)} / 100`}>
      <span style={{ width: `${score}%` }} />
      <strong>{score.toFixed(1)}</strong>
    </div>
  );
}

export function FreshnessBadge({ value }: { value: unknown }) {
  const text = String(value || "unknown").toLowerCase();
  const tone = text === "fresh" ? "good" : text === "delayed" || text === "closed_market" || text === "stale" ? "warn" : "neutral";
  return <Badge tone={tone}>{title(text)}</Badge>;
}

export function candleBars(value: any): CandleBar[] {
  return arrayFrom(value, "bars")
    .map((row) => ({
      time: String(row.time || row.observed_at || ""),
      open: num(row.open, NaN),
      high: num(row.high, NaN),
      low: num(row.low, NaN),
      close: num(row.close, NaN),
      volume: row.volume === null || row.volume === undefined ? null : num(row.volume, 0),
      vwap20: row.vwap20 === null || row.vwap20 === undefined ? null : num(row.vwap20, NaN),
      partial: Boolean(row.partial),
    }))
    .filter((row) => row.time && [row.open, row.high, row.low, row.close].every(Number.isFinite));
}

function candleZones(value: any): CandleZone[] {
  return arrayFrom(value, "zones")
    .map((zone) => ({
      direction: String(zone.direction || ""),
      status: String(zone.status || ""),
      zone_low: num(zone.zone_low, NaN),
      zone_high: num(zone.zone_high, NaN),
      origin_time: zone.origin_time ? String(zone.origin_time) : undefined,
      label: zone.label ? String(zone.label) : undefined,
    }))
    .filter((zone) => Number.isFinite(zone.zone_low) && Number.isFinite(zone.zone_high));
}

function candleLevels(value: any): CandleLevel[] {
  return arrayFrom(value, "levels")
    .map((level) => ({
      label: String(level.label || "Level"),
      value: num(level.value, NaN),
      tone: String(level.tone || ""),
    }))
    .filter((level) => Number.isFinite(level.value));
}

function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="workbench-fact">
      <span>{label}</span>
      <strong>{children}</strong>
    </div>
  );
}

export function CandleChartCard({
  detail,
  loading,
  error,
  onRetry,
  action,
}: {
  detail: any;
  loading?: boolean;
  error?: string;
  onRetry?: () => void;
  action?: React.ReactNode;
}) {
  if (loading) {
    return (
      <Card className="workbench-chart-card">
        <LoadingBlock rows={7} label="Calculating closed-candle structure…" />
      </Card>
    );
  }
  if (error) {
    return (
      <Card className="workbench-chart-card">
        <ErrorBlock message={error} onRetry={onRetry} />
      </Card>
    );
  }
  if (!detail) {
    return (
      <Card className="workbench-chart-card">
        <EmptyState
          icon={<Layers3 size={22} />}
          title="Select an instrument"
          description="Choose a row from a board, heatmap or zone view to open the closed-candle analysis."
        />
      </Card>
    );
  }
  if (detail.analysis_available === false) {
    return (
      <Card className="workbench-chart-card">
        <CardHeader
          title={`${symbolOf(detail)} · ${nameOf(detail)}`}
          subtitle="The backend cannot build a structural view from this series."
          action={action}
        />
        <div className="workbench-unavailable">
          <ShieldAlert size={20} />
          <div>
            <strong>Analysis unavailable</strong>
            <p>{detail.reason || "No validated closed bars."}</p>
            {detail.coverage && (
              <small>
                {detail.coverage.closed_bars ?? 0} closed bars · {detail.coverage.minimum_closed_bars ?? "—"} required
              </small>
            )}
          </div>
        </div>
      </Card>
    );
  }

  const bars = candleBars(detail.chart || detail);
  const zones = candleZones(detail.chart || detail);
  const levels = candleLevels(detail.chart || detail);
  const metrics = detail.metrics || {};
  const signal = detail.signal || {};
  const structure = detail.structure || {};
  const levelsData = detail.levels || {};
  const coverage = detail.coverage || {};
  const orderBlocks = arrayFrom(detail, "order_blocks");
  const signalTone =
    signal.direction === "bullish"
      ? "good"
      : signal.direction === "bearish"
        ? "bad"
        : signal.status === "developing"
          ? "warn"
          : "neutral";

  return (
    <Card className="workbench-chart-card">
      <div className="workbench-chart-head">
        <div>
          <div className="eyebrow">
            {detail.exchange || "Market"} · {detail.asset_class || "instrument"} · {detail.timeframe_label || detail.interval || "1d"}
          </div>
          <h2>{symbolOf(detail)}</h2>
          <p>{nameOf(detail)}</p>
        </div>
        <div className="workbench-price">
          <strong>{detail.last_price === null ? "Unavailable" : money(detail.last_price)}</strong>
          {detail.change_pct !== null && detail.change_pct !== undefined ? (
            <span className={`delta ${num(detail.change_pct) >= 0 ? "positive" : "negative"}`}>{pct(detail.change_pct)}</span>
          ) : (
            <span className="muted">No change measurement</span>
          )}
          <div className="workbench-price-actions">{action}</div>
        </div>
      </div>
      <div className="workbench-source-line">
        <span><CalendarClock size={12} /> Last closed bar {timeAgo(detail.as_of)}</span>
        <span>Basis: {detail.price_basis || "stored OHLCV"}</span>
        <FreshnessBadge value={detail.freshness} />
        <Badge tone="neutral">{detail.provider || "stored"}</Badge>
        <Badge tone={signalTone}>{title(signal.status || "no setup")}</Badge>
        <span className="workbench-source-right">{coverage.closed_bars ?? bars.length} closed bars</span>
      </div>
      <div className="chart-block workbench-chart-block">
        <CandlestickChart bars={bars} zones={zones} levels={levels} height={420} empty="No stored candles for this instrument" />
      </div>
      <div className="workbench-facts">
        <Fact label="Structure">{title(structure.trend || "range")}</Fact>
        <Fact label="RSI (14)">{metrics.rsi14 === null || metrics.rsi14 === undefined ? "—" : num(metrics.rsi14).toFixed(1)}</Fact>
        <Fact label="Relative volume">{metrics.relative_volume === null || metrics.relative_volume === undefined ? "Unavailable" : `${num(metrics.relative_volume).toFixed(2)}×`}</Fact>
        <Fact label="VWAP 20">{metrics.vwap20 === null || metrics.vwap20 === undefined ? "Unavailable" : money(metrics.vwap20)}</Fact>
        <Fact label="ATR (14)">{metrics.atr14 === null || metrics.atr14 === undefined ? "Unavailable" : `${money(metrics.atr14)} · ${pct(metrics.atr_pct)}`}</Fact>
        <Fact label="Support">{levelsData.support?.price == null ? "No detected pivot" : money(levelsData.support.price)}</Fact>
        <Fact label="Resistance">{levelsData.resistance?.price == null ? "No detected pivot" : money(levelsData.resistance.price)}</Fact>
        <Fact label="Invalidation">{levelsData.invalidation == null ? "Unavailable" : money(levelsData.invalidation)}</Fact>
        <Fact label="Risk / reward">{levelsData.risk_reward == null ? "Not measurable" : `${num(levelsData.risk_reward).toFixed(2)} : 1`}</Fact>
        <Fact label="Weekly context">{title(detail.timeframe_context?.weekly?.trend || "range")} · {title(detail.timeframe_context?.alignment || "mixed")}</Fact>
      </div>
      <div className="workbench-analysis-note">
        <Badge tone={signalTone}>{title(signal.status || "none")}</Badge>
        <span>{signal.reason || "No confirmed order-block setup is currently active."}</span>
        <Badge tone="warn">Analysis only · no execution</Badge>
      </div>
      <div className="workbench-detail-grid">
        <div className="workbench-evidence">
          <div className="workbench-subhead">
            <span>Signal evidence</span>
            <Badge tone="neutral">backend calculated</Badge>
          </div>
          <ul>
            {arrayFrom(detail, "evidence").map((line, index) => (
              <li key={index}>{String(line)}</li>
            ))}
          </ul>
        </div>
        <div className="workbench-block-ledger">
          <div className="workbench-subhead">
            <span>Order-block ledger</span>
            <Badge tone="neutral">{orderBlocks.length} detected</Badge>
          </div>
          {orderBlocks.length ? (
            <div className="block-ledger-list">
              {orderBlocks.slice(-5).reverse().map((block, index) => (
                <div className={`block-ledger-row block-${block.direction}`} key={`${block.origin_time}-${index}`}>
                  <span className="block-direction">{block.direction === "bullish" ? "Bull" : "Bear"}</span>
                  <span>{money(block.zone_low)} – {money(block.zone_high)}</span>
                  <Badge tone={block.status === "invalidated" ? "bad" : block.status === "developing" ? "warn" : "good"}>{title(block.status)}</Badge>
                  <small>{timeAgo(block.confirmed_time || block.origin_time)}</small>
                </div>
              ))}
            </div>
          ) : (
            <small className="muted">No qualifying order block in the stored series.</small>
          )}
        </div>
      </div>
      <div className="workbench-disclaimer">{detail.disclaimer}</div>
    </Card>
  );
}

/* --------------------------------------------------------------- watchlists */

function useWatchlists(open: boolean) {
  return useApi<any>(open ? "/market/tradingwatch/watchlists" : null);
}

export function WatchlistButton({ instrumentId, label = "Watchlist" }: { instrumentId?: string; label?: string }) {
  const toast = useToast();
  const [open, setOpen] = useState(false);
  const lists = useWatchlists(open);
  const [selected, setSelected] = useState("");
  const [newName, setNewName] = useState("");
  const [saving, setSaving] = useState(false);
  const rows = arrayFrom(lists.data, "watchlists");

  useEffect(() => {
    if (!selected && rows.length) setSelected(String(rows[0].watchlist_id || ""));
  }, [rows, selected]);

  const add = async () => {
    if (!instrumentId) return;
    setSaving(true);
    try {
      let watchlistId = selected;
      if (!watchlistId || newName.trim()) {
        const created = await apiPost<any>("/market/watchlists", {
          name: newName.trim() || `Desk ${new Date().toLocaleDateString("en-IN")}`,
          kind: "custom",
          description: "Created from the OrderBlock / TradingWatch desk",
        });
        watchlistId = String(created?.watchlist_id || "");
        if (!watchlistId) throw new Error("The backend did not return a watchlist id.");
      }
      await apiPost(`/market/watchlists/${encodeURIComponent(watchlistId)}/members`, {
        instrument_id: instrumentId,
        reason: "added from the trading workbench",
      });
      toast.push(`${symbolOf({ instrument_id: instrumentId })} added to the watchlist.`, "success");
      setOpen(false);
      setNewName("");
      lists.refresh();
    } catch (error: any) {
      toast.push(error?.detail || error?.message || "The instrument could not be added.", "error");
    } finally {
      setSaving(false);
    }
  };

  return (
    <>
      <Button variant="ghost" size="sm" icon={<ListChecks size={14} />} onClick={() => setOpen(true)} disabled={!instrumentId}>
        {label}
      </Button>
      <Modal
        open={open}
        onClose={() => setOpen(false)}
        title="Add to a watchlist"
        description="Membership is persisted by the existing NM-05 watchlist service. No candidate or signal is executed."
        footer={
          <>
            <Button variant="ghost" onClick={() => setOpen(false)}>Cancel</Button>
            <Button variant="primary" loading={saving} onClick={add} disabled={!instrumentId || (!selected && !newName.trim())}>
              Add instrument
            </Button>
          </>
        }
      >
        {lists.loading ? (
          <LoadingBlock rows={3} label="Reading persisted watchlists…" />
        ) : lists.error ? (
          <ErrorBlock message={lists.error.detail} onRetry={lists.refresh} />
        ) : (
          <div className="stack">
            <div className="inline-note"><ListChecks size={14} /> {instrumentId || "No instrument selected"}</div>
            {rows.length > 0 && (
              <Field label="Existing watchlist">
                <SelectInput value={selected} onChange={(event) => setSelected(event.target.value)}>
                  {rows.map((row: any) => (
                    <option key={row.watchlist_id} value={row.watchlist_id}>
                      {row.name} · {row.member_count ?? row.active_count ?? 0} members
                    </option>
                  ))}
                </SelectInput>
              </Field>
            )}
            <Field label={rows.length ? "Or create a new watchlist (optional)" : "Create a watchlist"}>
              <TextInput
                value={newName}
                onChange={(event) => setNewName(event.target.value)}
                placeholder="e.g. Breakout shortlist"
              />
            </Field>
            {!rows.length && <p className="field-hint">The new list is created and the selected instrument is added in one step.</p>}
          </div>
        )}
      </Modal>
    </>
  );
}

export function WatchlistPanel({
  selectedInstrumentId,
  selectedInstrumentLabel,
}: {
  selectedInstrumentId?: string;
  selectedInstrumentLabel?: string;
}) {
  const toast = useToast();
  const lists = useApi<any>("/market/tradingwatch/watchlists", { refreshMs: 60_000 });
  const rows = arrayFrom(lists.data, "watchlists");
  const [selectedId, setSelectedId] = useState("");
  const [createOpen, setCreateOpen] = useState(false);
  const [renameOpen, setRenameOpen] = useState(false);
  const [addOpen, setAddOpen] = useState(false);
  const [name, setName] = useState("");
  const [addId, setAddId] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (selectedId && rows.some((row: any) => row.watchlist_id === selectedId)) return;
    if (rows[0]?.watchlist_id) setSelectedId(String(rows[0].watchlist_id));
  }, [rows, selectedId]);

  const current = rows.find((row: any) => row.watchlist_id === selectedId) || rows[0];
  const members = arrayFrom(current, "members");
  const currentId = String(current?.watchlist_id || "");

  const refresh = () => {
    lists.refresh();
  };

  const createList = async () => {
    setSaving(true);
    try {
      const created = await apiPost<any>("/market/watchlists", {
        name: name.trim(),
        kind: "custom",
        description: "Created from TradingWatch",
      });
      if (created?.watchlist_id) setSelectedId(String(created.watchlist_id));
      setName("");
      setCreateOpen(false);
      toast.push("Watchlist created.", "success");
      refresh();
    } catch (error: any) {
      toast.push(error?.detail || "The watchlist could not be created.", "error");
    } finally {
      setSaving(false);
    }
  };

  const renameList = async () => {
    if (!currentId) return;
    setSaving(true);
    try {
      await apiPost(`/market/watchlists/${encodeURIComponent(currentId)}/rename`, { name: name.trim() });
      setName("");
      setRenameOpen(false);
      toast.push("Watchlist renamed; its id and history are unchanged.", "success");
      refresh();
    } catch (error: any) {
      toast.push(error?.detail || "The watchlist could not be renamed.", "error");
    } finally {
      setSaving(false);
    }
  };

  const addMember = async () => {
    if (!currentId || !addId.trim()) return;
    setSaving(true);
    try {
      await apiPost(`/market/watchlists/${encodeURIComponent(currentId)}/members`, {
        instrument_id: addId.trim(),
        reason: "added from TradingWatch",
      });
      setAddOpen(false);
      setAddId("");
      toast.push("Instrument added to the watchlist.", "success");
      refresh();
    } catch (error: any) {
      toast.push(error?.detail || "The instrument could not be added.", "error");
    } finally {
      setSaving(false);
    }
  };

  const removeMember = async (row: any) => {
    if (!currentId) return;
    setSaving(true);
    try {
      await apiDelete(`/market/watchlists/${encodeURIComponent(currentId)}/members/${encodeURIComponent(String(row.instrument_id || ""))}?reason=removed%20from%20TradingWatch`);
      toast.push("Instrument removed; membership history is retained.", "success");
      refresh();
    } catch (error: any) {
      toast.push(error?.detail || "The instrument could not be removed.", "error");
    } finally {
      setSaving(false);
    }
  };

  return (
    <Card className="watchlist-panel">
      <CardHeader
        title="Watchlists"
        subtitle="Persisted NM-05 memberships, priced from the backend's latest stored observation"
        action={
          <div className="row-actions">
            <Button variant="ghost" size="sm" icon={<RefreshCw size={13} className={lists.refreshing ? "spin" : undefined} />} onClick={refresh}>Refresh</Button>
            <Button variant="primary" size="sm" icon={<Plus size={13} />} onClick={() => { setName(""); setCreateOpen(true); }}>New</Button>
          </div>
        }
      />
      {lists.error && <ErrorBlock message={lists.error.detail} onRetry={lists.refresh} />}
      {lists.loading ? (
        <LoadingBlock rows={5} label="Reading watchlists and latest stored prices…" />
      ) : rows.length ? (
        <div className="watchlist-desk-grid">
          <div className="watchlist-rail">
            {rows.map((row: any) => (
              <button
                type="button"
                key={row.watchlist_id}
                className={`watchlist-rail-item ${row.watchlist_id === currentId ? "active" : ""}`}
                onClick={() => setSelectedId(String(row.watchlist_id))}
              >
                <span><ListChecks size={14} /></span>
                <div><strong>{row.name || "Untitled"}</strong><small>{title(row.kind || "custom")} · {row.member_count ?? row.members?.length ?? 0}</small></div>
                <ChevronRight size={13} />
              </button>
            ))}
          </div>
          <div className="watchlist-desk-main">
            <div className="watchlist-desk-head">
              <div>
                <strong>{current?.name || "Watchlist"}</strong>
                <span>{current?.description || "No description"}</span>
              </div>
              <div className="row-actions">
                <Button variant="ghost" size="sm" icon={<Pencil size={13} />} onClick={() => { setName(String(current?.name || "")); setRenameOpen(true); }}>Rename</Button>
                <Button variant="secondary" size="sm" icon={<Plus size={13} />} onClick={() => { setAddId(selectedInstrumentId || ""); setAddOpen(true); }}>Add</Button>
              </div>
            </div>
            {selectedInstrumentId && (
              <div className="watchlist-selection-hint">
                <span>Selected on this desk: <strong>{selectedInstrumentLabel || symbolOf({ instrument_id: selectedInstrumentId })}</strong></span>
                <Button size="sm" variant="ghost" onClick={() => { setAddId(selectedInstrumentId); setAddOpen(true); }}>Add selected</Button>
              </div>
            )}
            <DataTable
              compact
              rows={members}
              rowKey={(row: any) => row.entry_id || row.instrument_id}
              empty={<EmptyState icon={<ListChecks size={20} />} title="This watchlist is empty" description="Add the instrument selected on either desk, or enter a canonical instrument id." action={<Button size="sm" variant="primary" onClick={() => setAddOpen(true)}>Add instrument</Button>} />}
              columns={[
                {
                  key: "instrument",
                  label: "Instrument",
                  render: (row: any) => (
                    <div className="identity-cell">
                      <span className="symbol-mark">{symbolOf(row).slice(0, 2)}</span>
                      <div><strong>{symbolOf(row)}</strong><small>{nameOf(row)}</small></div>
                    </div>
                  ),
                },
                {
                  key: "price",
                  label: "Latest",
                  align: "right",
                  render: (row: any) => {
                    const market = row.market || {};
                    return market.available && market.price !== null && market.price !== undefined
                      ? <span className="mono">{money(market.price)}</span>
                      : <span className="muted">Unavailable</span>;
                  },
                },
                {
                  key: "change",
                  label: "Change",
                  align: "right",
                  render: (row: any) => {
                    const value = row.market?.change_pct;
                    return value === null || value === undefined ? <span className="muted">—</span> : <span className={`delta ${num(value) >= 0 ? "positive" : "negative"}`}>{pct(value)}</span>;
                  },
                },
                {
                  key: "basis",
                  label: "Basis / freshness",
                  render: (row: any) => (
                    <div className="watchlist-basis">
                      <span>{row.market?.price_basis || "No stored price"}</span>
                      <small>{row.market?.as_of ? timeAgo(row.market.as_of) : "—"} · {title(row.market?.freshness || "missing")}</small>
                    </div>
                  ),
                },
                {
                  key: "status",
                  label: "Membership",
                  render: (row: any) => <StatusBadge value={row.status || "active"} />,
                },
                {
                  key: "action",
                  label: "",
                  align: "right",
                  render: (row: any) => (
                    <button type="button" className="row-action row-action-danger" disabled={saving} onClick={() => removeMember(row)}>
                      <Trash2 size={13} /> Remove
                    </button>
                  ),
                },
              ]}
            />
            <div className="watchlist-foot">
              <span><History size={12} /> Removals retain membership history in SQLite.</span>
              <span>Price basis: latest stored close or reconciled quote.</span>
            </div>
          </div>
        </div>
      ) : (
        <EmptyState
          icon={<ListChecks size={22} />}
          title="No watchlists yet"
          description="Create a list here, then add instruments from OrderBlock or TradingWatch."
          action={<Button size="sm" variant="primary" onClick={() => { setName(""); setCreateOpen(true); }}>Create watchlist</Button>}
        />
      )}
      <Modal
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        title="Create watchlist"
        description="The list is persisted by NM-05 and is immediately reusable from both desks."
        footer={<><Button variant="ghost" onClick={() => setCreateOpen(false)}>Cancel</Button><Button variant="primary" loading={saving} onClick={createList} disabled={!name.trim()}>Create</Button></>}
      >
        <Field label="Name"><TextInput value={name} onChange={(event) => setName(event.target.value)} placeholder="e.g. Momentum focus" /></Field>
      </Modal>
      <Modal
        open={renameOpen}
        onClose={() => setRenameOpen(false)}
        title="Rename watchlist"
        description="The stable id and all membership history stay intact."
        footer={<><Button variant="ghost" onClick={() => setRenameOpen(false)}>Cancel</Button><Button variant="primary" loading={saving} onClick={renameList} disabled={!name.trim()}>Rename</Button></>}
      >
        <Field label="Name"><TextInput value={name} onChange={(event) => setName(event.target.value)} /></Field>
      </Modal>
      <Modal
        open={addOpen}
        onClose={() => setAddOpen(false)}
        title="Add instrument"
        description="Use the canonical MIC:SYMBOL identity. The backend rejects unknown instruments."
        footer={<><Button variant="ghost" onClick={() => setAddOpen(false)}>Cancel</Button><Button variant="primary" loading={saving} onClick={addMember} disabled={!addId.trim()}>Add</Button></>}
      >
        <Field label="Canonical instrument id" hint="Example: XNSE:RELIANCE">
          <TextInput value={addId} onChange={(event) => setAddId(event.target.value)} placeholder="XNSE:RELIANCE" />
        </Field>
      </Modal>
    </Card>
  );
}

/* ------------------------------------------------------------------ helpers */

export const DESK_SELECTION_KEY = "novamarket.deskInstrument";

/**
 * The selected instrument is shared by OrderBlock and TradingWatch. Keeping it
 * in local storage means the two hash routes agree after a reload, while the
 * canonical MIC:SYMBOL id remains the only identity passed to the backend.
 */
export function useDeskInstrument(): [string, (value: string) => void] {
  return useLocalState<string>(DESK_SELECTION_KEY, "");
}

export function blockStatusTone(status: unknown): "good" | "warn" | "bad" | "neutral" {
  const value = String(status || "").toLowerCase();
  if (value === "confirmed" || value === "tested" || value === "active") return "good";
  if (value === "developing" || value === "partial") return "warn";
  if (value === "invalidated" || value === "blocked") return "bad";
  return "neutral";
}

export function signalTone(value: unknown): "good" | "bad" | "warn" | "neutral" {
  const text = String(value || "").toLowerCase();
  if (text === "bullish") return "good";
  if (text === "bearish") return "bad";
  if (text === "developing") return "warn";
  return "neutral";
}

export function SourceLine({ data }: { data: any }) {
  if (!data) return null;
  return (
    <div className="workbench-source-line">
      <span><CalendarClock size={12} /> Generated {dateTime(data.generated_at)}</span>
      <span>{data.data_state === "stored_ohlcv" ? "Stored validated OHLCV" : title(data.data_state || "data")}</span>
      <FreshnessBadge value={data.instruments?.[0]?.freshness || "unknown"} />
      <span className="workbench-source-right">{data.coverage?.analysed ?? 0} analyzed · {data.coverage?.catalogue_total ?? 0} catalogue</span>
    </div>
  );
}

export { Check, X, SearchInput, useMemo };
