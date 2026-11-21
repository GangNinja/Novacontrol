import { useEffect, useMemo, useState } from "react";
import {
  Activity,
  CalendarClock,
  ChevronLeft,
  ChevronRight,
  Gauge,
  History,
  Info,
  Layers3,
  ListChecks,
  RefreshCw,
  ShieldAlert,
  Target,
  TrendingDown,
  TrendingUp,
} from "lucide-react";
import { navigate, useApi } from "../hooks";
import { arrayFrom, money, num, pct, shortDate, signTone, timeAgo, title } from "../lib";
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
  Metric,
  PageHeader,
  SearchInput,
  SelectInput,
  TextInput,
} from "../components/ui";
import { CandlestickChart, HBar, type CandleLevel } from "../components/charts";
import {
  CandleChartCard,
  FreshnessBadge,
  ScoreBar,
  SourceLine,
  WatchlistButton,
  WatchlistPanel,
  blockStatusTone,
  candleBars,
  nameOf,
  symbolOf,
  useDeskInstrument,
} from "./WorkbenchShared";

type Row = Record<string, any>;

const sectorOf = (row: Row) => String(row?.sector || "Unclassified");
const directionOf = (row: Row) => {
  const declared = String(row?.directional_bias || "");
  if (declared) return declared;
  // An unmeasured change carries no direction; it must not read as "bullish".
  const change = num(row?.change_pct, NaN);
  return Number.isFinite(change) ? (change >= 0 ? "bullish" : "bearish") : "unknown";
};

function heatClass(value: unknown): string {
  const number = num(value, NaN);
  if (!Number.isFinite(number)) return "heatmap-tile-neutral";
  if (number >= 2) return "heatmap-tile-up-strong";
  if (number > 0) return "heatmap-tile-up";
  if (number <= -2) return "heatmap-tile-down-strong";
  if (number < 0) return "heatmap-tile-down";
  return "heatmap-tile-neutral";
}

function zoneType(zone: Row): string {
  return String(zone?.zone_type || "").toLowerCase();
}

function formatZoneValue(zone: Row): string {
  if (zone?.zone_low != null || zone?.zone_high != null) {
    return `${money(zone.zone_low)} – ${money(zone.zone_high)}`;
  }
  return money(zone?.price);
}

function ZoneBoard({
  zones,
  selected,
  onSelect,
}: {
  zones: Row[];
  selected: string;
  onSelect: (id: string) => void;
}) {
  const [type, setType] = useState("all");
  const rows = useMemo(
    () => zones.filter((row) => type === "all" || zoneType(row) === type),
    [zones, type],
  );
  const types = Array.from(new Set(zones.map(zoneType).filter(Boolean))).sort();

  return (
    <Card>
      <CardHeader
        eyebrow="Calculated support, resistance and block areas"
        title={<span className="workbench-board-title"><Layers3 size={17} /> Zone board</span>}
        subtitle="Every zone is a backend calculation from stored bars; method and status are shown, not inferred in the browser."
        action={
          <SelectInput value={type} onChange={(event) => setType(event.target.value)} aria-label="Filter zone type">
            <option value="all">All zones</option>
            {types.map((value) => <option key={value} value={value}>{title(value)}</option>)}
          </SelectInput>
        }
      />
      <DataTable
        compact
        rows={rows.slice(0, 40)}
        rowKey={(row, index) => `${row.instrument_id}-${row.zone_type}-${row.price ?? row.zone_low}-${index}`}
        onRowClick={(row) => onSelect(String(row.instrument_id))}
        columns={[
          {
            key: "instrument",
            label: "Instrument",
            render: (row: Row) => (
              <div className="workbench-inline-identity">
                <strong>{symbolOf(row)}</strong>
                <small>{sectorOf(row)}</small>
              </div>
            ),
          },
          { key: "zone_type", label: "Zone", render: (row: Row) => <Badge tone={zoneType(row).includes("order") ? "accent" : "neutral"}>{title(row.zone_type || "level")}</Badge> },
          { key: "price", label: "Area", align: "right", render: (row: Row) => <span className="mono">{formatZoneValue(row)}</span> },
          { key: "distance_pct", label: "Distance", align: "right", render: (row: Row) => pct(row.distance_pct) },
          { key: "status", label: "Status", render: (row: Row) => <Badge tone={row.status === "confirmed" || row.status === "detected" ? "good" : row.status === "developing" ? "warn" : "neutral"}>{title(row.status || "unknown")}</Badge> },
          { key: "method", label: "Method", render: (row: Row) => <span className="workbench-method">{row.method || "—"}</span> },
          { key: "freshness", label: "As of", align: "right", render: (row: Row) => <small>{timeAgo(row.as_of)}</small> },
        ]}
        empty={<EmptyState icon={<Target size={20} />} title="No matching zones" description="The selected zone type has no calculated levels in the current sample." />}
      />
      <div className="inline-note">
        <ShieldAlert size={14} /> Zones are structural levels, not orders. No zone is sent to the risk engine or paper book automatically.
      </div>
    </Card>
  );
}

function Heatmap({
  heatmap,
  selected,
  onSelect,
  onSector,
}: {
  heatmap: any;
  selected: string;
  onSelect: (id: string) => void;
  onSector: (sector: string) => void;
}) {
  const sectors = arrayFrom(heatmap, "sectors");
  return (
    <Card>
      <CardHeader
        eyebrow="Sector and instrument participation"
        title="Market heatmap"
        subtitle={heatmap?.note || "Each tile is an analysed instrument with a stored bar."}
        action={
          <div className="workbench-legend">
            <span><i className="heatmap-swatch heatmap-tile-up-strong" /> Strong up</span>
            <span><i className="heatmap-swatch heatmap-tile-down-strong" /> Strong down</span>
            <span><i className="heatmap-swatch heatmap-tile-neutral" /> Flat / unavailable</span>
          </div>
        }
      />
      {heatmap?.sector_reference?.as_of && (
        <div className="inline-note">
          <Info size={14} /> Sector classification: {heatmap.sector_reference.source || "NSE snapshot"} · effective {heatmap.sector_reference.as_of}.
        </div>
      )}
      {heatmap?.sector_available === false && (
        <div className="inline-note inline-note-warn">
          <ShieldAlert size={14} /> Sector metadata is unavailable in the stored instrument registry; the backend groups tiles honestly as Unclassified.
        </div>
      )}
      {sectors.length ? (
        <div className="heatmap-sectors">
          {sectors.map((sector: Row) => {
            const items = arrayFrom(sector, "instruments");
            return (
              <section className="heatmap-sector" key={sector.sector}>
                <button type="button" className="heatmap-sector-head" onClick={() => onSector(String(sector.sector))}>
                  <div>
                    <strong>{sector.sector || "Unclassified"}</strong>
                    <span>{sector.count ?? items.length} instruments · avg {pct(sector.average_change_pct)}</span>
                  </div>
                  <ChevronRight size={14} />
                </button>
                <div className="heatmap-grid">
                  {items.map((item: Row) => (
                    <button
                      type="button"
                      key={item.instrument_id}
                      className={`heatmap-tile ${heatClass(item.change_pct)} ${selected === item.instrument_id ? "heatmap-tile-selected" : ""}`}
                      onClick={() => onSelect(String(item.instrument_id))}
                      title={`${symbolOf(item)} · ${nameOf(item)} · ${pct(item.change_pct)} · momentum ${item.momentum_score ?? "—"}`}
                    >
                      <strong>{symbolOf(item)}</strong>
                      <span>{pct(item.change_pct)}</span>
                      <small>M {item.momentum_score == null ? "—" : num(item.momentum_score).toFixed(0)} · RV {item.relative_volume == null ? "—" : `${num(item.relative_volume).toFixed(1)}×`}</small>
                    </button>
                  ))}
                </div>
              </section>
            );
          })}
        </div>
      ) : (
        <EmptyState icon={<Activity size={22} />} title="No heatmap cells" description="No analysable instrument is available for this session." />
      )}
    </Card>
  );
}

export function TradingWatchPage() {
  const [selected, setSelected] = useDeskInstrument();
  const [search, setSearch] = useState("");
  const [direction, setDirection] = useState("all");
  const [sector, setSector] = useState("all");
  const [sortMode, setSortMode] = useState("momentum");

  const [replayId, setReplayId] = useState("");
  const [replayDate, setReplayDate] = useState("");
  const [replayInterval, setReplayInterval] = useState("1d");
  const [cursor, setCursor] = useState(0);
  const [playing, setPlaying] = useState(false);

  const watch = useApi<any>("/market/tradingwatch?interval=1d&limit=50", { refreshMs: 120_000 });
  const detail = useApi<any>(
    selected ? `/market/orderblock/${encodeURIComponent(selected)}?interval=1d` : null,
    { refreshMs: 120_000 },
  );

  const data = watch.data;
  const momentum = arrayFrom(data, "momentum");
  const zones = arrayFrom(data, "zones");
  const instruments = arrayFrom(data, "instruments");
  const flow = data?.market_flow || {};
  const heatmap = data?.heatmap || {};
  const options = data?.options || {};
  const futures = data?.futures || {};
  const coverage = data?.coverage || {};
  const session = data?.session || {};

  const sectors = useMemo(() => {
    const names = new Set<string>();
    [...momentum, ...instruments].forEach((row) => names.add(sectorOf(row)));
    return Array.from(names).sort((left, right) => left.localeCompare(right));
  }, [momentum, instruments]);

  useEffect(() => {
    if (selected) return;
    const first = momentum[0] || instruments[0];
    if (first?.instrument_id) setSelected(String(first.instrument_id));
  }, [selected, momentum, instruments, setSelected]);

  useEffect(() => {
    if (replayId) return;
    const first = selected || momentum[0]?.instrument_id || instruments[0]?.instrument_id;
    if (first) setReplayId(String(first));
  }, [replayId, selected, momentum, instruments]);

  const momentumRows = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return momentum
      .filter((row) => {
        if (needle && !`${symbolOf(row)} ${nameOf(row)} ${sectorOf(row)}`.toLowerCase().includes(needle)) return false;
        if (direction !== "all" && directionOf(row) !== direction) return false;
        if (sector !== "all" && sectorOf(row) !== sector) return false;
        return true;
      })
      .sort((left, right) => {
        if (sortMode === "change") return num(right.change_pct) - num(left.change_pct);
        if (sortMode === "volume") return num(right.relative_volume) - num(left.relative_volume);
        if (sortMode === "symbol") return symbolOf(left).localeCompare(symbolOf(right));
        return num(right.momentum_score) - num(left.momentum_score);
      });
  }, [momentum, search, direction, sector, sortMode]);

  const replayPath = replayId
    ? `/market/tradingwatch/replay/${encodeURIComponent(replayId)}?interval=${encodeURIComponent(replayInterval)}&limit=180${replayDate ? `&end_date=${encodeURIComponent(replayDate)}` : ""}`
    : null;
  const replay = useApi<any>(replayPath);

  const replayBars = arrayFrom(replay.data, "bars");
  const replayPoints = arrayFrom(replay.data, "points");
  const maxCursor = Math.max(0, replayBars.length - 1);
  const point = replayPoints[Math.min(cursor, Math.max(0, replayPoints.length - 1))] || {};
  const shownBars = replayBars.slice(0, Math.min(cursor + 1, replayBars.length));

  useEffect(() => {
    setCursor(Math.max(0, replayBars.length - 1));
    setPlaying(false);
  }, [replay.data?.instrument_id, replay.data?.end_date, replay.data?.interval, replayBars.length]);

  useEffect(() => {
    if (playing && cursor >= maxCursor) setPlaying(false);
  }, [playing, cursor, maxCursor]);

  useEffect(() => {
    if (!playing) return;
    const timer = window.setInterval(() => {
      setCursor((current) => Math.min(maxCursor, current + 1));
    }, 600);
    return () => window.clearInterval(timer);
  }, [playing, maxCursor]);

  const replayLevels: CandleLevel[] = [];
  if (point.vwap20 != null && Number.isFinite(num(point.vwap20, NaN))) {
    replayLevels.push({ label: "VWAP 20", value: num(point.vwap20), tone: "support" });
  }
  if (point.sma20 != null && Number.isFinite(num(point.sma20, NaN))) {
    replayLevels.push({ label: "SMA 20", value: num(point.sma20), tone: "resistance" });
  }

  const breadth = flow.breadth || {};
  const volumeContext = flow.volume_context || {};
  const distribution = flow.momentum_distribution || {};
  const sectorParticipation = arrayFrom(flow, "sector_participation");
  const indexContext = flow.index_context || {};
  const optionFields = Array.isArray(options.fields_unavailable) ? options.fields_unavailable : [];
  const optionRows: Row[] = arrayFrom(options, "rows");
  const futuresRows: Row[] = arrayFrom(futures, "rows");
  const optionIv = (side: Row | null | undefined) => {
    const value = side?.implied_volatility_pct;
    return value == null ? "—" : `${num(value).toFixed(1)}%`;
  };
  const optionOi = (side: Row | null | undefined) => {
    if (side?.open_interest == null) return "—";
    return Number(side.open_interest).toLocaleString("en-IN");
  };
  const optionDelta = (side: Row | null | undefined) => {
    if (side?.change_in_open_interest == null) return "—";
    const value = num(side.change_in_open_interest);
    return `${value > 0 ? "+" : ""}${value.toLocaleString("en-IN")}`;
  };
  const distributionMax = Math.max(1, num(distribution.bullish) + num(distribution.neutral) + num(distribution.bearish));
  const selectedLabel = selected ? symbolOf({ instrument_id: selected }) : "";

  const togglePlay = () => {
    if (!playing && cursor >= maxCursor) setCursor(0);
    setPlaying((current) => !current);
  };

  const refreshAll = () => {
    watch.refresh();
    detail.refresh();
    replay.refresh();
  };

  return (
    <div className="page workbench-page">
      <PageHeader
        eyebrow="NM-14 · market intelligence desk"
        title="TradingWatch"
        description="Momentum, price zones, market breadth, sector participation and historical replay over the same validated OHLCV store used by OrderBlock."
        meta={
          <>
            <Badge tone={data?.live ? "good" : "warn"} dot>{data?.live ? "Live feed" : "Stored OHLCV"}</Badge>
            <Badge tone={coverage.complete ? "good" : "warn"}>{coverage.complete ? "Full scan" : coverage.truncated ? "Limited scan" : "Partial scan"}</Badge>
            <span>{data?.timeframe_label || "Daily"} momentum</span>
            <span>{coverage.analysed ?? 0} analysed · {coverage.scanned ?? 0} scanned · {coverage.failed ?? 0} failed</span>
          </>
        }
        actions={
          <>
            <Button variant="ghost" icon={<RefreshCw size={15} className={watch.refreshing ? "spin" : undefined} />} onClick={refreshAll}>
              Refresh
            </Button>
            <Button variant="primary" icon={<Target size={15} />} onClick={() => navigate("orderblock")}>
              OrderBlock desk
            </Button>
          </>
        }
      />

      <div className="workbench-data-strip">
        <span><CalendarClock size={13} /> Generated {timeAgo(data?.generated_at)}</span>
        <span>Session: {title(session.state || "unknown")} · {session.label || "NSE/BSE session not verified"}</span>
        <span>Basis: {data?.price_basis || "last closed OHLCV bar"}</span>
        <Badge tone={data?.live ? "good" : "warn"}>{data?.live ? "Live" : "Not live"}</Badge>
        <span className="workbench-strip-right">
          {coverage.sample_only ? "Partial coverage · " : ""}
          {coverage.scanned ?? 0} / {coverage.eligible ?? coverage.instruments_with_bars ?? 0} eligible · {coverage.instruments_with_bars ?? 0} with bars
        </span>
      </div>

      {watch.loading && !data ? (
        <Card><LoadingBlock rows={8} label="Reading momentum, zones and market breadth…" /></Card>
      ) : watch.error && !data ? (
        <Card><ErrorBlock message={watch.error.detail} onRetry={watch.refresh} /></Card>
      ) : (
        <>
          <Card>
            <CardHeader
              eyebrow="Directional movement and participation"
              title={<span className="workbench-board-title"><TrendingUp size={17} /> Momentum radar</span>}
              subtitle="Momentum combines trend, RSI and relative volume on closed bars. It is a ranked observation, not a buy or sell instruction."
              action={<Badge tone="neutral">{momentumRows.length} instruments</Badge>}
            />
            <div className="workbench-filterbar workbench-filterbar-inline">
              <SearchInput value={search} onChange={setSearch} placeholder="Search momentum radar" />
              <SelectInput value={direction} onChange={(event) => setDirection(event.target.value)} aria-label="Filter momentum direction">
                <option value="all">All directions</option>
                <option value="bullish">Bullish</option>
                <option value="bearish">Bearish</option>
              </SelectInput>
              <SelectInput value={sector} onChange={(event) => setSector(event.target.value)} aria-label="Filter momentum sector">
                <option value="all">All sectors</option>
                {sectors.map((name) => <option key={name} value={name}>{name}</option>)}
              </SelectInput>
              <SelectInput value={sortMode} onChange={(event) => setSortMode(event.target.value)} aria-label="Sort momentum radar">
                <option value="momentum">Sort: momentum score</option>
                <option value="change">Sort: daily change</option>
                <option value="volume">Sort: relative volume</option>
                <option value="symbol">Sort: symbol A–Z</option>
              </SelectInput>
              <Button size="sm" variant="ghost" onClick={() => { setSearch(""); setDirection("all"); setSector("all"); }}>Clear</Button>
            </div>
            <DataTable
              compact
              rows={momentumRows.map((row, index) => ({ ...row, _rank: index + 1 }))}
              rowKey={(row) => String(row.instrument_id)}
              onRowClick={(row) => setSelected(String(row.instrument_id))}
              columns={[
                { key: "rank", label: "#", width: "42px", render: (row: Row) => <span className="workbench-rank">{row._rank}</span> },
                {
                  key: "instrument",
                  label: "Instrument",
                  render: (row: Row) => (
                    <div className={`identity-cell ${selected === row.instrument_id ? "identity-cell-selected" : ""}`}>
                      <span className="symbol-mark">{symbolOf(row).slice(0, 2)}</span>
                      <div>
                        <strong>{symbolOf(row)}</strong>
                        <small>{nameOf(row)}</small>
                        <span className="workbench-row-meta">{sectorOf(row)} · {data?.timeframe_label || "Daily"} · {row.provider || "stored"}</span>
                      </div>
                    </div>
                  ),
                },
                { key: "momentum_score", label: "Momentum", width: "160px", render: (row: Row) => <ScoreBar value={row.momentum_score} /> },
                {
                  key: "direction",
                  label: "Direction",
                  render: (row: Row) => (
                    <div className="workbench-block-cell">
                      <Badge tone={directionOf(row) === "bullish" ? "good" : directionOf(row) === "bearish" ? "bad" : "neutral"}>{title(directionOf(row))}</Badge>
                      <small><span className={`delta ${signTone(row.change_pct)}`}>{pct(row.change_pct)}</span></small>
                    </div>
                  ),
                },
                { key: "relative_volume", label: "Rel vol", align: "right", render: (row: Row) => row.relative_volume == null ? "—" : `${num(row.relative_volume).toFixed(2)}×` },
                { key: "rsi14", label: "RSI", align: "right", render: (row: Row) => row.rsi14 == null ? "—" : num(row.rsi14).toFixed(1) },
                { key: "order_block_status", label: "Block", render: (row: Row) => <Badge tone={blockStatusTone(row.order_block_status)}>{title(row.order_block_status || "none")}</Badge> },
                { key: "freshness", label: "Freshness", align: "right", render: (row: Row) => <div className="workbench-asof"><FreshnessBadge value={row.freshness} /><small>{timeAgo(row.as_of)}</small></div> },
              ]}
              empty={<EmptyState icon={<TrendingDown size={20} />} title="No momentum rows" description="Relax the direction or sector filter. Only instruments with validated closed bars are ranked." />}
            />
          </Card>

          <section className="workbench-detail-section">
            <div className="workbench-section-heading">
              <div>
                <div className="eyebrow">Shared instrument context</div>
                <h2>{selectedLabel || "Select an instrument"}</h2>
                <p>The same canonical instrument and closed-bar calculation used by OrderBlock.</p>
              </div>
              {selected && (
                <div className="row-actions">
                  <WatchlistButton instrumentId={selected} />
                  <Button size="sm" variant="secondary" icon={<ChevronRight size={14} />} onClick={() => navigate("orderblock")}>
                    Open in OrderBlock
                  </Button>
                </div>
              )}
            </div>
            <CandleChartCard
              detail={detail.data}
              loading={detail.loading}
              error={detail.error?.detail}
              onRetry={detail.refresh}
            />
          </section>

          <Heatmap heatmap={heatmap} selected={selected} onSelect={setSelected} onSector={setSector} />

          <div className="workbench-split">
            <ZoneBoard zones={zones} selected={selected} onSelect={setSelected} />
            <Card>
              <CardHeader
                eyebrow="Market-wide participation"
                title={<span className="workbench-board-title"><Activity size={17} /> Market flow</span>}
                subtitle="Breadth and momentum distribution over the analysed sample."
                action={<Badge tone="neutral">{breadth.breadth_ratio_pct == null ? "Unavailable" : `${num(breadth.breadth_ratio_pct).toFixed(1)}% breadth`}</Badge>}
              />
              <div className="workbench-metric-grid">
                <Metric label="Advancing" value={breadth.advancing ?? "—"} tone="good" />
                <Metric label="Declining" value={breadth.declining ?? "—"} tone="bad" />
                <Metric label="Avg relative volume" value={volumeContext.average_relative_volume == null ? "—" : `${num(volumeContext.average_relative_volume).toFixed(2)}×`} help={`${volumeContext.instruments_with_volume ?? 0} with volume`} />
                <Metric label="Momentum bullish" value={distribution.bullish ?? "—"} help={`${distribution.neutral ?? 0} neutral · ${distribution.bearish ?? 0} bearish`} />
              </div>
              <div className="workbench-bars">
                <HBar label="Advancing" value={num(breadth.advancing)} max={Math.max(1, num(breadth.advancing) + num(breadth.declining))} display={String(breadth.advancing ?? "—")} />
                <HBar label="Declining" value={num(breadth.declining)} max={Math.max(1, num(breadth.advancing) + num(breadth.declining))} display={String(breadth.declining ?? "—")} color="var(--danger)" />
                <HBar label="Momentum ≥ 55" value={num(distribution.bullish)} max={distributionMax} display={`${distribution.bullish ?? 0} of ${distributionMax}`} />
                <HBar label="Momentum ≤ 45" value={num(distribution.bearish)} max={distributionMax} display={`${distribution.bearish ?? 0} of ${distributionMax}`} color="var(--danger)" />
              </div>
              <div className={`inline-note ${indexContext.available ? "" : "inline-note-warn"}`}>
                <Gauge size={14} />
                {indexContext.available
                  ? `Index context: ${(indexContext.instruments || []).map((item: Row) => `${item.symbol} ${pct(item.change_pct)}`).join(" · ")}`
                  : indexContext.reason || "No index instrument with a stored series is available in this sample."}
              </div>
              <div className="workbench-subhead">
                <span>Sector participation</span>
                <Badge tone="neutral">{sectorParticipation.length} sectors</Badge>
              </div>
              <div className="flow-sector-list">
                {sectorParticipation.slice(0, 7).map((item: Row) => (
                  <button type="button" className="flow-sector-row" key={item.sector} onClick={() => setSector(String(item.sector))}>
                    <span>{item.sector || "Unclassified"}</span>
                    <strong className={signTone(item.average_change_pct)}>{pct(item.average_change_pct)}</strong>
                    <small>{item.count ?? 0} names · M {item.average_momentum_score == null ? "—" : num(item.average_momentum_score).toFixed(0)}</small>
                  </button>
                ))}
              </div>
              <p className="workbench-note">{volumeContext.note || flow.coverage_note}</p>
            </Card>
          </div>

          <Card className={options.supported ? "" : "workbench-blocked-card"}>
            <CardHeader
              eyebrow="Derivatives coverage"
              title="Index options"
              subtitle={
                options.supported
                  ? "Official NSE F&O bhavcopy: strike, open interest, change in open interest, volume and model IV."
                  : "Strike-wise calls, puts, open interest and expiry require an official option-chain source."
              }
              action={<Badge tone={options.supported ? "good" : "bad"}>{options.status || "BLOCKED"}</Badge>}
            />
            {options.supported ? (
              <>
                <div className="workbench-data-strip">
                  <span>{options.symbol} · expiry {shortDate(options.expiry)}</span>
                  <span>Underlying {money(options.underlying_price)}</span>
                  <span>PCR {options.pcr == null ? "—" : num(options.pcr).toFixed(2)}</span>
                  <span>{optionRows.length} strikes</span>
                  <Badge tone="neutral">EOD {shortDate(options.trade_date)}</Badge>
                </div>
                <DataTable
                  compact
                  rows={optionRows}
                  rowKey={(row) => String(row.strike)}
                  columns={[
                    { key: "strike", label: "Strike", align: "right", render: (row: Row) => money(row.strike) },
                    { key: "call_oi", label: "Call OI", align: "right", render: (row: Row) => optionOi(row.CE) },
                    { key: "call_chg", label: "Call Δ OI", align: "right", render: (row: Row) => <span className={signTone(row.CE?.change_in_open_interest)}>{optionDelta(row.CE)}</span> },
                    { key: "call_iv", label: "Call IV", align: "right", render: (row: Row) => optionIv(row.CE) },
                    { key: "put_iv", label: "Put IV", align: "right", render: (row: Row) => optionIv(row.PE) },
                    { key: "put_oi", label: "Put OI", align: "right", render: (row: Row) => optionOi(row.PE) },
                    { key: "put_chg", label: "Put Δ OI", align: "right", render: (row: Row) => <span className={signTone(row.PE?.change_in_open_interest)}>{optionDelta(row.PE)}</span> },
                  ]}
                  empty={<EmptyState icon={<ShieldAlert size={20} />} title="No strikes" description="The official snapshot contains no rows for this expiry." />}
                />
                <p className="workbench-note">
                  {options.iv_method} OI and premiums are provider values. {options.disclaimer}
                </p>
              </>
            ) : (
              <div className="workbench-blocked">
                <div className="workbench-blocked-mark"><ShieldAlert size={22} /></div>
                <div>
                  <strong>{options.reason || "No official option-chain source is configured."}</strong>
                  <p>{options.next_requirement || "No strike, open-interest or implied-volatility values are generated."}</p>
                  <div className="workbench-unavailable-tags">
                    {optionFields.map((field: string) => <span key={field}>{title(field)}</span>)}
                  </div>
                </div>
              </div>
            )}
          </Card>

          <Card className={futures.supported ? "" : "workbench-blocked-card"}>
            <CardHeader
              eyebrow="Official NSE F&O · futures"
              title="Index & stock futures"
              subtitle={
                futures.supported
                  ? "Expiry-wise provider values from the same official bhavcopy; no synthetic futures are generated."
                  : "Index and stock futures require the official NSE F&O contract file."
              }
              action={<Badge tone={futures.supported ? "good" : "bad"}>{futures.status || "BLOCKED"}</Badge>}
            />
            {futures.supported ? (
              <>
                <div className="workbench-data-strip">
                  <span>{futures.symbol} · {futures.expiries?.length ?? 0} expiries</span>
                  <span>{futures.contracts ?? 0} contracts · {futures.underlying_count ?? 0} underlyings</span>
                  <span>OI {futures.contract?.open_interest == null ? "—" : Number(futures.contract.open_interest).toLocaleString("en-IN")}</span>
                  <Badge tone="neutral">EOD {shortDate(futures.trade_date)}</Badge>
                </div>
                <DataTable
                  compact
                  rows={futuresRows}
                  rowKey={(row) => String(row.expiry)}
                  columns={[
                    { key: "expiry", label: "Expiry", render: (row: Row) => shortDate(row.expiry) },
                    { key: "close", label: "Close", align: "right", render: (row: Row) => money(row.close) },
                    { key: "oi", label: "Open interest", align: "right", render: (row: Row) => row.open_interest == null ? "—" : Number(row.open_interest).toLocaleString("en-IN") },
                    { key: "change", label: "Δ Open interest", align: "right", render: (row: Row) => <span className={signTone(row.change_in_open_interest)}>{row.change_in_open_interest == null ? "—" : `${num(row.change_in_open_interest) > 0 ? "+" : ""}${Number(row.change_in_open_interest).toLocaleString("en-IN")}`}</span> },
                    { key: "volume", label: "Volume", align: "right", render: (row: Row) => row.volume == null ? "—" : Number(row.volume).toLocaleString("en-IN") },
                    { key: "lot", label: "Lot size", align: "right", render: (row: Row) => row.lot_size == null ? "—" : Number(row.lot_size).toLocaleString("en-IN") },
                  ]}
                  empty={<EmptyState icon={<ShieldAlert size={20} />} title="No futures" description="The official snapshot contains no futures rows." />}
                />
                <p className="workbench-note">{futures.note || "Expiry, open interest and volume are provider values. No futures IV is reported."}</p>
              </>
            ) : (
              <div className="workbench-blocked">
                <div className="workbench-blocked-mark"><ShieldAlert size={22} /></div>
                <div>
                  <strong>{futures.reason || "No official futures source is configured."}</strong>
                  <p>{futures.next_requirement || "No futures contract, open-interest or volume value is generated."}</p>
                </div>
              </div>
            )}
          </Card>

          <Card>
            <CardHeader
              eyebrow="Historical bars · no look-ahead"
              title={<span className="workbench-board-title"><History size={17} /> Market replay</span>}
              subtitle={replay.data?.methodology?.timeframe || "Daily stored bars; weekly replay aggregates the same daily series."}
              action={<Badge tone="info">Historical only</Badge>}
            />
            <div className="replay-toolbar">
              <Field label="Instrument">
                <SelectInput value={replayId} onChange={(event) => { setReplayId(event.target.value); setCursor(0); }}>
                  {[...instruments, ...momentum].filter((row, index, list) => row.instrument_id && list.findIndex((item) => item.instrument_id === row.instrument_id) === index).map((row) => (
                    <option key={row.instrument_id} value={row.instrument_id}>{symbolOf(row)} · {nameOf(row)}</option>
                  ))}
                </SelectInput>
              </Field>
              <Field label="Cutoff date" hint="Leave blank for the latest stored bar.">
                <TextInput type="date" value={replayDate} onChange={(event) => { setReplayDate(event.target.value); setCursor(0); }} />
              </Field>
              <Field label="Timeframe">
                <SelectInput value={replayInterval} onChange={(event) => { setReplayInterval(event.target.value); setCursor(0); }}>
                  <option value="1d">Daily (stored)</option>
                  <option value="1wk">Weekly (derived from daily)</option>
                </SelectInput>
              </Field>
              <Button size="sm" variant="ghost" icon={<RefreshCw size={13} className={replay.refreshing ? "spin" : undefined} />} onClick={replay.refresh}>Reload</Button>
            </div>
            {replay.error ? (
              <ErrorBlock message={replay.error.detail} onRetry={replay.refresh} />
            ) : replay.loading ? (
              <LoadingBlock rows={6} label="Loading historical bars and cursor-safe indicators…" />
            ) : replay.data && replay.data.ok === false ? (
              <div className="workbench-unavailable"><ShieldAlert size={20} /><div><strong>Replay unavailable</strong><p>{replay.data.reason}</p></div></div>
            ) : replayBars.length > 0 ? (
              <>
                <div className="replay-meta">
                  <span><CalendarClock size={13} /> Cutoff {shortDate(replay.data?.end_date)}</span>
                  <span>{replay.data?.timeframe_label || title(replayInterval)} · {replayBars.length} bars</span>
                  <Badge tone="info">Historical · no look-ahead</Badge>
                  <span>{replay.data?.coverage?.look_ahead_guard ? "Cursor calculation guard active" : "Guard not reported"}</span>
                </div>
                <div className="replay-controls">
                  <Button size="sm" variant="ghost" onClick={() => { setPlaying(false); setCursor(0); }}>Start</Button>
                  <Button size="sm" variant="secondary" icon={<ChevronLeft size={14} />} onClick={() => { setPlaying(false); setCursor((current) => Math.max(0, current - 1)); }} aria-label="Previous bar" />
                  <Button size="sm" variant="secondary" icon={<ChevronRight size={14} />} onClick={() => { setPlaying(false); setCursor((current) => Math.min(maxCursor, current + 1)); }} aria-label="Next bar" />
                  <Button size="sm" variant={playing ? "primary" : "ghost"} onClick={togglePlay}>{playing ? "Pause" : "Play"}</Button>
                  <input
                    className="replay-range"
                    type="range"
                    min={0}
                    max={maxCursor}
                    value={Math.min(cursor, maxCursor)}
                    onChange={(event) => { setPlaying(false); setCursor(Number(event.target.value)); }}
                    aria-label="Replay cursor"
                  />
                  <span className="replay-position">{cursor + 1} / {replayBars.length}</span>
                  <span className="muted">{shortDate(shownBars[shownBars.length - 1]?.time || shownBars[shownBars.length - 1]?.observed_at)}</span>
                </div>
                <div className="replay-chart">
                  <CandlestickChart
                    bars={candleBars({ bars: shownBars })}
                    levels={replayLevels}
                    height={420}
                    empty="At least two bars are required for replay."
                  />
                </div>
                <div className="replay-metrics">
                  <Metric label="Bar close" value={shownBars[shownBars.length - 1] ? money(shownBars[shownBars.length - 1].close) : "—"} help={point.time ? shortDate(point.time) : "Cursor"} />
                  <Metric label="SMA 20" value={point.sma20 == null ? "—" : money(point.sma20)} help="Cursor prefix only" />
                  <Metric label="RSI 14" value={point.rsi14 == null ? "—" : num(point.rsi14).toFixed(1)} help="Cursor prefix only" />
                  <Metric label="VWAP 20" value={point.vwap20 == null ? "—" : money(point.vwap20)} help="Cursor prefix only" />
                  <Metric label="Relative volume" value={point.relative_volume == null ? "—" : `${num(point.relative_volume).toFixed(2)}×`} help="Prior 20 bars" />
                  <Metric label="ATR 14" value={point.atr14 == null ? "—" : money(point.atr14)} help={`${point.inputs ?? 0} input bars`} />
                </div>
                <div className="inline-note">
                  <History size={14} /> Cursor {cursor + 1} calculates indicators from solely the visible prefix. This is historical replay, not a live price.
                </div>
              </>
            ) : (
              <EmptyState icon={<History size={22} />} title="No historical bars" description="Choose an instrument or a date with at least two stored bars." />
            )}
          </Card>

          <WatchlistPanel selectedInstrumentId={selected} selectedInstrumentLabel={selectedLabel} />
          <SourceLine data={data} />
          <p className="workbench-disclaimer">{data?.disclaimer}</p>
        </>
      )}

      {watch.error && data && <ErrorBlock message={`Refresh failed: ${watch.error.detail}`} onRetry={watch.refresh} />}
    </div>
  );
}
