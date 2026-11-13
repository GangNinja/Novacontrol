import { useEffect, useMemo, useState } from "react";
import {
  Activity,
  CalendarClock,
  ChevronRight,
  Gauge,
  Layers3,
  ListChecks,
  RefreshCw,
  ShieldAlert,
  Target,
  TrendingDown,
  TrendingUp,
} from "lucide-react";
import { navigate, useApi } from "../hooks";
import { arrayFrom, money, num, pct, shortDate, timeAgo, title } from "../lib";
import {
  Badge,
  Button,
  Card,
  CardHeader,
  DataTable,
  EmptyState,
  ErrorBlock,
  LoadingBlock,
  Metric,
  PageHeader,
  SearchInput,
  SelectInput,
} from "../components/ui";
import { HBar } from "../components/charts";
import {
  CandleChartCard,
  FreshnessBadge,
  ScoreBar,
  SourceLine,
  WatchlistButton,
  blockStatusTone,
  nameOf,
  signalTone,
  symbolOf,
  useDeskInstrument,
} from "./WorkbenchShared";

type Row = Record<string, any>;

const sectorOf = (row: Row) => String(row?.sector || "Unclassified");

function zoneText(zone: any): string {
  if (!zone) return "No entry zone";
  if (Array.isArray(zone) && zone.length >= 2) return `${money(zone[0])} – ${money(zone[1])}`;
  if (typeof zone === "object") {
    const low = zone.zone_low ?? zone.low;
    const high = zone.zone_high ?? zone.high;
    if (low !== undefined || high !== undefined) return `${money(low)} – ${money(high)}`;
  }
  return "No entry zone";
}

function changeClass(value: unknown): string {
  return num(value, 0) >= 0 ? "positive" : "negative";
}

const buildupTone = (value: unknown): "good" | "bad" | "neutral" => {
  const label = String(value || "");
  if (label === "long_buildup" || label === "short_covering") return "good";
  if (label === "short_buildup" || label === "long_unwinding") return "bad";
  return "neutral";
};

const mappingTone = (value: unknown): "good" | "warn" | "info" => {
  const label = String(value || "");
  if (label === "mapped") return "good";
  if (label === "index_underlying") return "info";
  return "warn";
};

function Board({
  tone,
  rows,
  selected,
  onSelect,
}: {
  tone: "bullish" | "bearish";
  rows: Row[];
  selected: string;
  onSelect: (id: string) => void;
}) {
  const [showAll, setShowAll] = useState(false);
  const visible = showAll ? rows : rows.slice(0, 10);
  const bullish = tone === "bullish";
  const Icon = bullish ? TrendingUp : TrendingDown;
  const columns = [
    {
      key: "rank",
      label: "#",
      width: "42px",
      render: (row: Row) => <span className="workbench-rank">{row._rank}</span>,
    },
    {
      key: "instrument",
      label: "Instrument",
      render: (row: Row) => (
        <div className={`identity-cell ${selected === row.instrument_id ? "identity-cell-selected" : ""}`}>
          <span className="symbol-mark">{symbolOf(row).slice(0, 2)}</span>
          <div>
            <strong>{symbolOf(row)}</strong>
            <small>{nameOf(row)}</small>
            <span className="workbench-row-meta">
              {sectorOf(row)} · RV {row.relative_volume == null ? "—" : `${num(row.relative_volume).toFixed(2)}×`} ·
              VWAP {title(row.vwap_relation || "n/a")}
            </span>
          </div>
        </div>
      ),
    },
    {
      key: "last_price",
      label: "Last",
      align: "right" as const,
      render: (row: Row) => <span className="mono">{money(row.last_price)}</span>,
    },
    {
      key: "change_pct",
      label: "Change",
      align: "right" as const,
      render: (row: Row) => (
        <span className={`delta ${changeClass(row.change_pct)}`}>{pct(row.change_pct)}</span>
      ),
    },
    {
      key: "directional_strength",
      label: "Strength",
      render: (row: Row) => (
        <ScoreBar value={row.directional_strength} tone={bullish ? "accent" : "danger"} />
      ),
    },
    {
      key: "order_block_status",
      label: "Order block",
      render: (row: Row) => (
        <div className="workbench-block-cell">
          <Badge tone={blockStatusTone(row.order_block_status)}>{title(row.order_block_status || "none")}</Badge>
          <small>{title(row.order_block_direction || "—")} · {zoneText(row.order_block_zone)}</small>
        </div>
      ),
    },
    {
      key: "freshness",
      label: "As of",
      align: "right" as const,
      render: (row: Row) => (
        <div className="workbench-asof">
          <FreshnessBadge value={row.freshness} />
          <small>{timeAgo(row.as_of)}</small>
        </div>
      ),
    },
  ];

  return (
    <Card className={`workbench-board workbench-board-${tone}`}>
      <CardHeader
        eyebrow={`${bullish ? "Long-side" : "Short-side"} directional ranking`}
        title={
          <span className="workbench-board-title">
            <Icon size={17} /> {bullish ? "Bullish board" : "Bearish board"}
          </span>
        }
        subtitle={
          bullish
            ? "Closed-bar strength, structure and order-block context ranked high to low."
            : "Weakest names first. A high bearish score is a short-side candidate, not an execution instruction."
        }
        action={<Badge tone={bullish ? "good" : "bad"}>{rows.length} ranked</Badge>}
      />
      <DataTable
        compact
        rows={visible.map((row, index) => ({ ...row, _rank: index + 1 }))}
        rowKey={(row) => String(row.instrument_id)}
        onRowClick={(row) => onSelect(String(row.instrument_id))}
        columns={columns}
        empty={
          <EmptyState
            icon={bullish ? <TrendingUp size={20} /> : <TrendingDown size={20} />}
            title="No matching setup"
            description="Clear or relax the filters. The board only contains instruments with validated closed bars."
          />
        }
      />
      {rows.length > 10 && (
        <div className="workbench-board-foot">
          <span className="muted">{visible.length} of {rows.length} ranked rows</span>
          <Button size="sm" variant="ghost" onClick={() => setShowAll((current) => !current)}>
            {showAll ? "Show top 10" : `Show all ${rows.length}`}
          </Button>
        </div>
      )}
    </Card>
  );
}

export function OrderBlockPage() {
  const [selected, setSelected] = useDeskInstrument();
  const [search, setSearch] = useState("");
  const [sector, setSector] = useState("all");
  const [setup, setSetup] = useState("all");
  const [sortMode, setSortMode] = useState("strength");

  const board = useApi<any>("/market/orderblock?interval=1d&limit=50", { refreshMs: 120_000 });
  const detail = useApi<any>(
    selected ? `/market/orderblock/${encodeURIComponent(selected)}?interval=1d` : null,
    { refreshMs: 120_000 },
  );

  const data = board.data;
  const boards = data?.boards || {};
  const bullish = arrayFrom(boards, "bullish");
  const bearish = arrayFrom(boards, "bearish");
  const instruments = arrayFrom(data, "instruments");
  const liquidity = arrayFrom(data, "liquidity");
  const sectorFlow = arrayFrom(data, "sector_flow");
  const sectorReference = data?.sector_reference || {};
  const xray = data?.market_xray || {};
  const coverage = data?.coverage || {};
  const fno = data?.fno || {};
  const session = data?.session || {};
  const methodology = data?.methodology || {};

  const sectors = useMemo(() => {
    const names = new Set<string>();
    [...instruments, ...bullish, ...bearish].forEach((row) => names.add(sectorOf(row)));
    return Array.from(names).sort((left, right) => left.localeCompare(right));
  }, [instruments, bullish, bearish]);

  useEffect(() => {
    if (selected) return;
    const first =
      [...bullish, ...bearish].find((row) => row.order_block_status === "confirmed") ||
      bullish[0] ||
      bearish[0] ||
      instruments[0];
    if (first?.instrument_id) setSelected(String(first.instrument_id));
  }, [selected, bullish, bearish, instruments, setSelected]);

  const filterAndSort = (rows: Row[]) => {
    const needle = search.trim().toLowerCase();
    const filtered = rows.filter((row) => {
      if (needle && !`${symbolOf(row)} ${nameOf(row)} ${sectorOf(row)}`.toLowerCase().includes(needle)) {
        return false;
      }
      if (sector !== "all" && sectorOf(row) !== sector) return false;
      if (setup !== "all" && String(row.order_block_status || "none") !== setup) return false;
      return true;
    });
    return filtered.sort((left, right) => {
      if (sortMode === "change") return num(right.change_pct) - num(left.change_pct);
      if (sortMode === "volume") return num(right.relative_volume) - num(left.relative_volume);
      if (sortMode === "reward") return num(right.risk_reward) - num(left.risk_reward);
      if (sortMode === "symbol") return symbolOf(left).localeCompare(symbolOf(right));
      return (
        num(right.directional_strength) - num(left.directional_strength) ||
        num(right.momentum_score) - num(left.momentum_score)
      );
    });
  };

  const bullishRows = useMemo(
    () => filterAndSort(bullish),
    [bullish, search, sector, setup, sortMode],
  );
  const bearishRows = useMemo(
    () => filterAndSort(bearish),
    [bearish, search, sector, setup, sortMode],
  );

  const advancing = num(xray.advancing);
  const declining = num(xray.declining);
  const breadthMax = Math.max(1, advancing + declining);
  const indexContext = xray.index_context || {};
  const fnoRequired = Array.isArray(fno.required) ? fno.required : [];
  const fnoCoverage = fno.coverage || {};
  const fnoRows = arrayFrom(fno, "rows");

  const refreshAll = () => {
    board.refresh();
    detail.refresh();
  };

  return (
    <div className="page workbench-page">
      <PageHeader
        eyebrow="NM-14 · directional structure desk"
        title="OrderBlock"
        description="Closed-candle order-block analysis, liquidity structure and market breadth from the existing NovaMarket market-data service. Signals are analysis only; risk approval remains a separate gate."
        meta={
          <>
            <Badge tone={data?.live ? "good" : "warn"} dot>{data?.live ? "Live feed" : "Stored OHLCV"}</Badge>
            <Badge tone={coverage.complete ? "good" : "warn"}>
              {coverage.complete ? "Full scan" : coverage.truncated ? "Limited scan" : "Partial scan"}
            </Badge>
            <span>{data?.timeframe_label || "Daily"} structure · weekly context</span>
            <span>{coverage.analysed ?? 0} analysed of {coverage.eligible ?? coverage.instruments_with_bars ?? 0} eligible</span>
          </>
        }
        actions={
          <>
            <Button variant="ghost" icon={<RefreshCw size={15} className={board.refreshing ? "spin" : undefined} />} onClick={refreshAll}>
              Refresh
            </Button>
            <Button variant="primary" icon={<ListChecks size={15} />} onClick={() => navigate("tradingwatch")}>
              TradingWatch desk
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
          {coverage.scanned ?? 0} scanned / {coverage.eligible ?? coverage.instruments_with_bars ?? 0} eligible
          {" · "}{coverage.failed ?? 0} failed · {coverage.catalogue_total ?? 0} catalogue
        </span>
      </div>

      <div className="workbench-filterbar">
        <SearchInput value={search} onChange={setSearch} placeholder="Search symbol, name or sector" />
        <SelectInput value={sector} onChange={(event) => setSector(event.target.value)} aria-label="Filter by sector">
          <option value="all">All sectors</option>
          {sectors.map((name) => <option key={name} value={name}>{name}</option>)}
        </SelectInput>
        <SelectInput value={setup} onChange={(event) => setSetup(event.target.value)} aria-label="Filter by order-block status">
          <option value="all">All block states</option>
          <option value="confirmed">Confirmed</option>
          <option value="developing">Developing</option>
          <option value="none">No active setup</option>
        </SelectInput>
        <SelectInput value={sortMode} onChange={(event) => setSortMode(event.target.value)} aria-label="Sort boards">
          <option value="strength">Sort: directional strength</option>
          <option value="change">Sort: daily change</option>
          <option value="volume">Sort: relative volume</option>
          <option value="reward">Sort: risk / reward</option>
          <option value="symbol">Sort: symbol A–Z</option>
        </SelectInput>
        <Button size="sm" variant="ghost" onClick={() => { setSearch(""); setSector("all"); setSetup("all"); }}>
          Clear filters
        </Button>
      </div>

      {board.loading && !data ? (
        <Card><LoadingBlock rows={8} label="Ranking closed-candle structure across the eligible stored-bar catalogue…" /></Card>
      ) : board.error && !data ? (
        <Card><ErrorBlock message={board.error.detail} onRetry={board.refresh} /></Card>
      ) : (
        <>
          <div className="workbench-board-grid">
            <Board tone="bullish" rows={bullishRows} selected={selected} onSelect={setSelected} />
            <Board tone="bearish" rows={bearishRows} selected={selected} onSelect={setSelected} />
          </div>

          <Card className={fno.supported ? "" : "workbench-blocked-card"}>
            <CardHeader
              eyebrow="Official NSE F&O · futures scanner"
              title="F&O scanner"
              subtitle={
                fno.supported
                  ? "Official expiry-wise futures contracts; equity mappings and derived buildup labels are reported separately."
                  : "The scanner only reports official contracts. Equity setups are never relabelled as futures."
              }
              action={<Badge tone={fno.status === "AVAILABLE" ? "good" : fno.status === "STALE" ? "warn" : "bad"}>{fno.status || "BLOCKED"}</Badge>}
            />
            {fno.supported ? (
              <>
                <div className="workbench-data-strip">
                  <span>{fnoCoverage.contracts_registered ?? 0} contracts registered</span>
                  <span>{fnoCoverage.contracts_scanned ?? 0} unexpired</span>
                  <span>{fnoCoverage.index_contracts ?? 0} index · {fnoCoverage.stock_contracts ?? 0} stock</span>
                  <span>{fnoCoverage.mapped_contracts ?? 0} mapped · {fnoCoverage.unmapped_contracts ?? 0} unmapped</span>
                  <span>{fnoCoverage.expired_excluded ?? 0} expired excluded</span>
                  <Badge tone="neutral">EOD {shortDate(fno.trade_date)}</Badge>
                  {fno.stale_days > 0 && <Badge tone="warn">{fno.stale_days}d old</Badge>}
                </div>
                <DataTable
                  compact
                  rows={fnoRows.slice(0, 40)}
                  rowKey={(row: Row) => String(row.contract_id || `${row.symbol}-${row.expiry}`)}
                  defaultSort={{ key: "open_interest", direction: "desc" }}
                  empty={<EmptyState icon={<ShieldAlert size={20} />} title="No futures contracts" description="The official snapshot contains no unexpired contract." />}
                  columns={[
                    { key: "symbol", label: "Contract", render: (row: Row) => <div className="workbench-inline-identity"><strong>{row.symbol}</strong><small>{row.is_front ? "Front · " : ""}{row.contract_name || row.instrument_type}</small></div> },
                    { key: "expiry", label: "Expiry", render: (row: Row) => <span className="mono">{shortDate(row.expiry)}</span> },
                    { key: "instrument_type", label: "Type", render: (row: Row) => <Badge tone={row.instrument_type === "IDF" ? "info" : "neutral"}>{row.instrument_type || "FUT"}</Badge> },
                    { key: "close", label: "Close", align: "right", render: (row: Row) => money(row.close) },
                    { key: "price_change_pct", label: "Price Δ", align: "right", render: (row: Row) => <span className={changeClass(row.price_change_pct)}>{pct(row.price_change_pct)}</span> },
                    { key: "open_interest", label: "Open interest", align: "right", render: (row: Row) => Number(row.open_interest ?? 0).toLocaleString("en-IN") },
                    { key: "change_in_open_interest", label: "Δ OI", align: "right", render: (row: Row) => <span className={changeClass(row.change_in_open_interest)}>{Number(row.change_in_open_interest ?? 0).toLocaleString("en-IN")}</span> },
                    { key: "oi_change_pct", label: "OI Δ %", align: "right", render: (row: Row) => pct(row.oi_change_pct) },
                    { key: "volume", label: "Volume", align: "right", render: (row: Row) => Number(row.volume ?? 0).toLocaleString("en-IN") },
                    { key: "lot_size", label: "Lot", align: "right", render: (row: Row) => Number(row.lot_size ?? 0).toLocaleString("en-IN") },
                    { key: "buildup", label: "Buildup", render: (row: Row) => <Badge tone={buildupTone(row.buildup)}>{title(row.buildup || "unclassified")}</Badge> },
                    { key: "mapping_status", label: "Equity mapping", render: (row: Row) => <Badge tone={mappingTone(row.mapping_status)}>{title(row.mapping_status || "unmapped")}</Badge> },
                  ]}
                />
                <p className="workbench-note">
                  {fno.note} {fno.reason} Showing {Math.min(fnoRows.length, 40)} of {fnoRows.length} unexpired contracts; the API returns every valid row.
                </p>
              </>
            ) : (
              <div className="workbench-blocked">
                <div className="workbench-blocked-mark"><ShieldAlert size={22} /></div>
                <div>
                  <strong>{fno.reason || "No official futures contract market is available."}</strong>
                  <p>Equity setups are not relabelled as futures or options signals. No synthetic coverage is created.</p>
                  <div className="workbench-blocked-metrics">
                    <span>{fnoCoverage.contracts_registered ?? 0} contracts registered</span>
                    <span>{fnoCoverage.contracts_scanned ?? 0} scanned</span>
                    <span>{fnoCoverage.equities_scanned ?? coverage.analysed ?? 0} equities analysed</span>
                  </div>
                  {fnoRequired.length > 0 && (
                    <ul className="workbench-required">
                      {fnoRequired.map((requirement: string) => <li key={requirement}>{requirement}</li>)}
                    </ul>
                  )}
                </div>
              </div>
            )}
          </Card>

          <section className="workbench-detail-section">
            <div className="workbench-section-heading">
              <div>
                <div className="eyebrow">Instrument detail</div>
                <h2>{selected ? symbolOf({ instrument_id: selected }) : "Select an instrument"}</h2>
                <p>Closed candles, confirmed and developing blocks, VWAP, liquidity levels and weekly context.</p>
              </div>
              {selected && (
                <div className="row-actions">
                  <WatchlistButton instrumentId={selected} />
                  <Button size="sm" variant="secondary" icon={<ChevronRight size={14} />} onClick={() => navigate("tradingwatch")}>
                    Open in TradingWatch
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

          <div className="workbench-split">
            <Card className="workbench-xray">
              <CardHeader
                eyebrow="Breadth over the analysed sample"
                title={<span className="workbench-board-title"><Activity size={17} /> Market X-Ray</span>}
                subtitle="Participation is measured only on instruments with validated closed bars."
                action={<Badge tone={signalTone(xray.direction)}>{title(xray.direction || "unknown")}</Badge>}
              />
              <div className="workbench-metric-grid">
                <Metric label="Advancing" value={xray.advancing ?? "—"} tone="good" />
                <Metric label="Declining" value={xray.declining ?? "—"} tone="bad" />
                <Metric label="Unchanged" value={xray.unchanged ?? "—"} />
                <Metric label="Breadth" value={xray.breadth_ratio_pct == null ? "—" : `${num(xray.breadth_ratio_pct).toFixed(1)}%`} tone="accent" />
                <Metric label="Avg. change" value={pct(xray.average_change_pct)} tone={num(xray.average_change_pct) >= 0 ? "good" : "bad"} />
                <Metric label="A / D ratio" value={xray.advance_decline_ratio ?? "—"} help={`${xray.sample_size ?? 0} instruments`} />
              </div>
              <div className="workbench-bars">
                <HBar label="Advancing participation" value={advancing} max={breadthMax} display={String(xray.advancing ?? "—")} />
                <HBar label="Declining participation" value={declining} max={breadthMax} display={String(xray.declining ?? "—")} color="var(--danger)" />
              </div>
              <div className="workbench-participation">
                <span>Bullish trend <strong>{xray.bullish_trend_count ?? "—"}</strong> · {pct(xray.bullish_participation_pct)}</span>
                <span>Bearish trend <strong>{xray.bearish_trend_count ?? "—"}</strong> · {pct(xray.bearish_participation_pct)}</span>
              </div>
              <div className={`inline-note ${indexContext.available ? "" : "inline-note-warn"}`}>
                <Gauge size={14} />
                {indexContext.available
                  ? `Index context: ${(indexContext.instruments || []).map((item: Row) => `${item.symbol} ${pct(item.change_pct)}`).join(" · ")}`
                  : indexContext.reason || "No index instrument with a stored series is in this scan."}
              </div>
            </Card>

            <Card>
              <CardHeader
                eyebrow="Sector participation"
                title="Sector flow"
                subtitle={`Average change, momentum, volume activity and directional participation by sector.${sectorReference.as_of ? ` Classification: ${sectorReference.source || "NSE snapshot"} · effective ${sectorReference.as_of}.` : ""}`}
                action={<Badge tone={sectorFlow.some((row) => row.sector_available) ? "good" : "warn"}>{sectorFlow.length} groups</Badge>}
              />
              <DataTable
                compact
                rows={sectorFlow}
                rowKey={(row) => String(row.sector)}
                columns={[
                  {
                    key: "sector",
                    label: "Sector",
                    render: (row: Row) => (
                      <div className="workbench-sector-cell">
                        <strong>{row.sector || "Unclassified"}</strong>
                        <small>{row.count ?? 0} instruments</small>
                      </div>
                    ),
                  },
                  { key: "average_change_pct", label: "Avg", align: "right", render: (row: Row) => <span className={`delta ${changeClass(row.average_change_pct)}`}>{pct(row.average_change_pct)}</span> },
                  { key: "average_momentum_score", label: "Momentum", width: "150px", render: (row: Row) => <ScoreBar value={row.average_momentum_score} /> },
                  { key: "volume_activity", label: "RV", align: "right", render: (row: Row) => row.volume_activity == null ? "—" : `${num(row.volume_activity).toFixed(2)}×` },
                  {
                    key: "participation",
                    label: "Bull / bear",
                    render: (row: Row) => (
                      <span className="workbench-participation-cell">
                        <span className="positive">{num(row.bullish_participation_pct).toFixed(0)}%</span>
                        <span className="muted">/</span>
                        <span className="negative">{num(row.bearish_participation_pct).toFixed(0)}%</span>
                      </span>
                    ),
                  },
                ]}
                empty={<EmptyState title="No sector rows" description="Sector flow is unavailable without analysable instruments." />}
              />
              {!sectorFlow.some((row) => row.sector_available) && (
                <div className="inline-note inline-note-warn">
                  <ShieldAlert size={14} /> Stored instruments carry no sector metadata, so the backend honestly groups them as Unclassified.
                </div>
              )}
            </Card>
          </div>

          <Card>
            <CardHeader
              eyebrow="Calculated price structure"
              title={<span className="workbench-board-title"><Layers3 size={17} /> Liquidity board</span>}
              subtitle="Nearest calculated pivots and liquidity areas. Status and method identify detected, estimated or unavailable levels."
              action={<Badge tone="neutral">{liquidity.length} levels</Badge>}
            />
            <DataTable
              compact
              rows={liquidity.slice(0, 30)}
              rowKey={(row) => `${row.instrument_id}-${row.kind}-${row.price}`}
              onRowClick={(row) => setSelected(String(row.instrument_id))}
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
                { key: "kind", label: "Area", render: (row: Row) => title(row.kind || "level") },
                { key: "price", label: "Price", align: "right", render: (row: Row) => <span className="mono">{money(row.price)}</span> },
                { key: "distance_pct", label: "Distance", align: "right", render: (row: Row) => pct(row.distance_pct) },
                { key: "side", label: "Side", render: (row: Row) => <Badge tone={row.side === "support" ? "good" : "warn"}>{title(row.side || "level")}</Badge> },
                { key: "status", label: "Status", render: (row: Row) => <Badge tone={row.status === "detected" ? "good" : row.status === "estimated" ? "warn" : "neutral"}>{title(row.status || "unknown")}</Badge> },
                { key: "method", label: "Method", render: (row: Row) => <span className="workbench-method">{row.method || "—"}</span> },
              ]}
              empty={<EmptyState icon={<Target size={20} />} title="No calculated levels" description="The backend could not derive a pivot or liquidity level from the sample." />}
            />
          </Card>

          <SourceLine data={data} />

          <details className="market-methodology">
            <summary>Calculation methodology and data limits</summary>
            <div className="methodology-grid">
              {Object.entries(methodology).slice(0, 12).map(([key, value]) => (
                <div key={key}>
                  <h4>{title(key)}</h4>
                  <p>{typeof value === "string" ? value : JSON.stringify(value)}</p>
                </div>
              ))}
            </div>
            <p className="workbench-disclaimer">{data?.disclaimer}</p>
          </details>
        </>
      )}

      {board.error && data && <ErrorBlock message={`Refresh failed: ${board.error.detail}`} onRetry={board.refresh} />}
    </div>
  );
}
