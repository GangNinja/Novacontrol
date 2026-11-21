import { useId, useState } from "react";
import { money } from "../lib";

export type Point = { label: string; value: number };

function bounds(points: Point[]) {
  const values = points.map((point) => point.value);
  const min = Math.min(...values);
  const max = Math.max(...values);
  if (min === max) {
    // A flat series must not render the same axis label at every tick; that
    // makes a stored balance look like a chart with missing measurements.
    const pad = Math.abs(min) * 0.05 || 1;
    return { min: min - pad, max: max + pad };
  }
  return { min, max };
}

function tickLabel(value: number, format: "money" | "percent" | "number"): string {
  if (format === "money") {
    if (Math.abs(value) >= 10_000_000) return `₹${(value / 10_000_000).toFixed(1)}Cr`;
    if (Math.abs(value) >= 100_000) return `₹${(value / 100_000).toFixed(1)}L`;
    if (Math.abs(value) >= 1_000) return `₹${(value / 1_000).toFixed(1)}k`;
    return `₹${value.toFixed(0)}`;
  }
  if (format === "percent") return `${value.toFixed(1)}%`;
  return value.toLocaleString("en-IN");
}

export function LineChart({
  data,
  height = 260,
  format = "money",
  color = "var(--accent)",
  empty = "No measurements yet",
  className,
}: {
  data: Point[];
  height?: number;
  format?: "money" | "percent" | "number";
  color?: string;
  empty?: string;
  className?: string;
}) {
  const [hover, setHover] = useState<number | null>(null);
  const gradientId = useId().replace(/:/g, "");
  if (data.length === 0) {
    return (
      <div className={`chart-empty ${className || ""}`} style={{ height }}>
        <span>{empty}</span>
        <small>Charts appear only after the backend records real observations.</small>
      </div>
    );
  }
  const width = 760;
  const heightValue = 240;
  const pad = { left: 68, right: 16, top: 14, bottom: 30 };
  const { min, max } = bounds(data);
  const span = max - min || 1;
  const plotW = width - pad.left - pad.right;
  const plotH = heightValue - pad.top - pad.bottom;
  // A single observation is a real measurement: centre it instead of calling
  // the chart empty, which contradicted "appears after the first episode".
  const x = (index: number) =>
    pad.left + (data.length > 1 ? index / (data.length - 1) : 0.5) * plotW;
  const y = (value: number) => pad.top + (1 - (value - min) / span) * plotH;
  const line = data.map((point, index) => `${index === 0 ? "M" : "L"}${x(index)},${y(point.value)}`).join(" ");
  const area = `${line} L${x(data.length - 1)},${pad.top + plotH} L${pad.left},${pad.top + plotH} Z`;
  const grid = [0, 0.25, 0.5, 0.75, 1];
  const active = hover !== null ? data[hover] : null;
  return (
    <div className={`line-chart ${className || ""}`} style={{ height }}>
      <svg viewBox={`0 0 ${width} ${heightValue}`} preserveAspectRatio="none" onMouseLeave={() => setHover(null)} role="img" aria-label="Line chart">
        <defs>
          <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={color} stopOpacity="0.24" />
            <stop offset="100%" stopColor={color} stopOpacity="0" />
          </linearGradient>
        </defs>
        {grid.map((fraction) => {
          const value = min + span * fraction;
          return (
            <g key={fraction}>
              <line x1={pad.left} x2={width - pad.right} y1={y(value)} y2={y(value)} className="chart-grid" />
              <text x={pad.left - 12} y={y(value) + 3} textAnchor="end" className="chart-axis">
                {tickLabel(value, format)}
              </text>
            </g>
          );
        })}
        <path d={area} fill={`url(#${gradientId})`} />
        <path d={line} fill="none" stroke={color} strokeWidth="2.3" strokeLinejoin="round" strokeLinecap="round" />
        {data.map((point, index) => (
          <g key={`${point.label}-${index}`}>
            <rect
              x={x(index) - plotW / Math.max(data.length - 1, 1) / 2}
              y={pad.top}
              width={plotW / Math.max(data.length - 1, 1)}
              height={plotH}
              fill="transparent"
              onMouseEnter={() => setHover(index)}
            />
            {(hover === index || data.length === 1) && <circle cx={x(index)} cy={y(point.value)} r="4" fill={color} stroke="var(--bg)" strokeWidth="2" />}
          </g>
        ))}
        {[0, Math.floor((data.length - 1) / 2), data.length - 1].filter((value, index, list) => list.indexOf(value) === index).map((index) => (
          <text key={index} x={x(index)} y={heightValue - 8} textAnchor={index === 0 && data.length > 1 ? "start" : index === data.length - 1 && data.length > 1 ? "end" : "middle"} className="chart-axis">
            {data[index].label}
          </text>
        ))}
      </svg>
      {active && (
        <div
          className="chart-tooltip"
          style={{ left: `${Math.min(88, Math.max(9, (x(hover!) / width) * 100))}%`, top: 10 }}
        >
          <span>{active.label}</span>
          <strong>{format === "money" ? money(active.value) : format === "percent" ? `${active.value.toFixed(2)}%` : active.value.toLocaleString("en-IN")}</strong>
        </div>
      )}
    </div>
  );
}

export type CandleBar = {
  time: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume?: number | null;
  vwap20?: number | null;
  partial?: boolean;
};

export type CandleZone = {
  direction: "bullish" | "bearish" | string;
  status?: string;
  zone_low: number;
  zone_high: number;
  origin_time?: string;
  label?: string;
};

export type CandleLevel = {
  label: string;
  value: number;
  tone?: "support" | "resistance" | "risk" | string;
};

function shortChartDate(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("en-IN", { day: "2-digit", month: "short" }).format(date);
}

/**
 * Candlestick + volume chart with detected zones and explicit levels.
 *
 * It is deliberately a pure SVG view: every candle comes from the backend
 * OHLCV response, and the view never interpolates a missing bar or a price.
 */
export function CandlestickChart({
  bars,
  zones = [],
  levels = [],
  height = 420,
  empty = "No validated OHLCV bars",
}: {
  bars: CandleBar[];
  zones?: CandleZone[];
  levels?: CandleLevel[];
  height?: number;
  empty?: string;
}) {
  const [hover, setHover] = useState<number | null>(null);
  const gradientId = useId().replace(/:/g, "");
  if (bars.length < 2) {
    return (
      <div className="chart-empty candle-empty" style={{ height }}>
        <span>{empty}</span>
        <small>The chart appears only when the backend has at least two usable bars.</small>
      </div>
    );
  }

  const width = 980;
  const heightValue = 420;
  const pad = { left: 62, right: 64, top: 18, bottom: 26 };
  const volumeHeight = 54;
  const priceHeight = heightValue - pad.top - pad.bottom - volumeHeight - 18;
  const plotWidth = width - pad.left - pad.right;
  const step = plotWidth / bars.length;
  const candleWidth = Math.max(2.5, Math.min(12, step * 0.58));
  const zoneValues = zones.flatMap((zone) => [zone.zone_low, zone.zone_high]);
  const levelValues = levels.map((level) => level.value);
  const allValues = [
    ...bars.flatMap((bar) => [bar.low, bar.high]),
    ...zoneValues,
    ...levelValues,
  ].filter((value) => Number.isFinite(value));
  const rawMin = Math.min(...allValues);
  const rawMax = Math.max(...allValues);
  const buffer = (rawMax - rawMin || rawMax * 0.02 || 1) * 0.045;
  const min = rawMin - buffer;
  const max = rawMax + buffer;
  const span = max - min || 1;
  const x = (index: number) => pad.left + (index + 0.5) * step;
  const y = (value: number) => pad.top + (1 - (value - min) / span) * priceHeight;
  const volumeTop = pad.top + priceHeight + 18;
  const maxVolume = Math.max(...bars.map((bar) => Number(bar.volume) || 0), 1);
  const volumeY = (value: number) => volumeTop + volumeHeight - (value / maxVolume) * volumeHeight;
  const grid = [0, 0.25, 0.5, 0.75, 1];
  const vwapPoints = bars
    .map((bar, index) => (Number.isFinite(Number(bar.vwap20)) ? `${index === 0 ? "M" : "L"}${x(index)},${y(Number(bar.vwap20))}` : ""))
    .filter(Boolean)
    .join(" ");
  const firstBar = bars[0];
  const lastBar = bars[bars.length - 1];
  const active = hover !== null ? bars[hover] : null;
  const activeX = hover !== null ? x(hover) : 0;

  const zoneStart = (zone: CandleZone) => {
    if (!zone.origin_time) return 0;
    const found = bars.findIndex((bar) => bar.time >= String(zone.origin_time));
    return found < 0 ? 0 : found;
  };

  return (
    <div className="candle-chart" style={{ height }}>
      <svg viewBox={`0 0 ${width} ${heightValue}`} preserveAspectRatio="none" role="img" aria-label="Candlestick chart with order-block zones">
        <defs>
          <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="var(--accent)" stopOpacity="0.08" />
            <stop offset="100%" stopColor="var(--accent)" stopOpacity="0" />
          </linearGradient>
        </defs>
        {zones.map((zone, index) => {
          const start = zoneStart(zone);
          const left = x(start) - step / 2;
          const right = x(bars.length - 1) + step / 2;
          const top = y(Math.max(zone.zone_high, zone.zone_low));
          const bottom = y(Math.min(zone.zone_high, zone.zone_low));
          const bullish = zone.direction === "bullish";
          return (
            <g key={`${zone.direction}-${index}-${zone.zone_low}`}>
              <rect
                x={left}
                y={top}
                width={Math.max(2, right - left)}
                height={Math.max(1, bottom - top)}
                fill={bullish ? "rgba(99,214,157,0.10)" : "rgba(255,125,141,0.10)"}
                stroke={bullish ? "rgba(99,214,157,0.35)" : "rgba(255,125,141,0.35)"}
                strokeDasharray={zone.status === "developing" ? "4 3" : undefined}
              />
              <text x={left + 4} y={top - 4} className="candle-zone-label" fill={bullish ? "var(--green)" : "var(--danger)"}>
                {zone.label || (bullish ? "Bullish OB" : "Bearish OB")}
              </text>
            </g>
          );
        })}
        {grid.map((fraction) => {
          const value = min + span * fraction;
          return (
            <g key={fraction}>
              <line x1={pad.left} x2={width - pad.right} y1={y(value)} y2={y(value)} className="chart-grid" />
              <text x={pad.left - 9} y={y(value) + 3} textAnchor="end" className="chart-axis">
                {value.toLocaleString("en-IN", { maximumFractionDigits: 2 })}
              </text>
            </g>
          );
        })}
        {levels.map((level) => (
          <g key={`${level.label}-${level.value}`}>
            <line
              x1={pad.left}
              x2={width - pad.right}
              y1={y(level.value)}
              y2={y(level.value)}
              stroke={level.tone === "risk" ? "var(--danger)" : level.tone === "support" ? "var(--blue)" : "var(--gold)"}
              strokeWidth="1"
              strokeDasharray="5 4"
              opacity="0.72"
            />
            <text x={width - pad.right + 7} y={y(level.value) + 3} className="candle-level-label" fill={level.tone === "risk" ? "var(--danger)" : level.tone === "support" ? "var(--blue)" : "var(--gold)"}>
              {level.label}
            </text>
          </g>
        ))}
        {bars.map((bar, index) => {
          const bullish = bar.close >= bar.open;
          const bodyTop = y(Math.max(bar.open, bar.close));
          const bodyHeight = Math.max(1.2, Math.abs(y(bar.open) - y(bar.close)));
          const opacity = bar.partial ? 0.55 : 1;
          return (
            <g key={`${bar.time}-${index}`} opacity={opacity}>
              <line x1={x(index)} x2={x(index)} y1={y(bar.high)} y2={y(bar.low)} stroke={bullish ? "var(--green)" : "var(--danger)"} strokeWidth="1" />
              <rect
                x={x(index) - candleWidth / 2}
                y={bodyTop}
                width={candleWidth}
                height={bodyHeight}
                rx="0.7"
                fill={bullish ? "var(--green)" : "var(--danger)"}
                stroke={bullish ? "var(--green)" : "var(--danger)"}
              />
              {bar.volume !== null && bar.volume !== undefined && (
                <rect
                  x={x(index) - candleWidth / 2}
                  y={volumeY(Number(bar.volume))}
                  width={candleWidth}
                  height={Math.max(1, volumeTop + volumeHeight - volumeY(Number(bar.volume)))}
                  fill={bullish ? "rgba(99,214,157,0.28)" : "rgba(255,125,141,0.28)"}
                />
              )}
              <rect
                x={x(index) - step / 2}
                y={pad.top}
                width={step}
                height={priceHeight}
                fill="transparent"
                onMouseEnter={() => setHover(index)}
              />
            </g>
          );
        })}
        {vwapPoints && <path d={vwapPoints} fill="none" stroke="var(--gold)" strokeWidth="1.7" strokeDasharray="4 3" opacity="0.86" />}
        <line x1={pad.left} x2={width - pad.right} y1={volumeTop + volumeHeight} y2={volumeTop + volumeHeight} className="chart-grid" />
        <text x={pad.left - 9} y={volumeTop + 9} textAnchor="end" className="chart-axis">VOL</text>
        <text x={pad.left - 9} y={volumeTop + 24} textAnchor="end" className="chart-axis">{Math.round(maxVolume).toLocaleString("en-IN")}</text>
        {[0, Math.floor((bars.length - 1) / 2), bars.length - 1].filter((value, index, list) => list.indexOf(value) === index).map((index) => (
          <text key={index} x={x(index)} y={heightValue - 8} textAnchor={index === 0 ? "start" : index === bars.length - 1 ? "end" : "middle"} className="chart-axis">
            {shortChartDate(bars[index].time)}
          </text>
        ))}
        {active && hover !== null && (
          <line x1={activeX} x2={activeX} y1={pad.top} y2={volumeTop + volumeHeight} stroke="rgba(198,212,228,0.32)" strokeDasharray="3 3" />
        )}
      </svg>
      <div className="candle-legend" aria-hidden="true">
        <span><i className="candle-swatch candle-swatch-up" /> Bullish</span>
        <span><i className="candle-swatch candle-swatch-down" /> Bearish</span>
        <span><i className="candle-swatch candle-swatch-vwap" /> VWAP 20</span>
        <span className="candle-legend-note">closed bars · source values</span>
      </div>
      {active && hover !== null && (
        <div className="chart-tooltip candle-tooltip" style={{ left: `${Math.min(86, Math.max(12, (activeX / width) * 100))}%` }}>
          <span>{new Date(active.time).toLocaleString("en-IN", { day: "2-digit", month: "short", year: "numeric" })}</span>
          <strong>{`O ${money(active.open)}  H ${money(active.high)}`}</strong>
          <strong>{`L ${money(active.low)}  C ${money(active.close)}`}</strong>
          <small>{active.volume !== null && active.volume !== undefined ? `Vol ${Number(active.volume).toLocaleString("en-IN")}` : "Volume unavailable"}{active.partial ? " · partial bar" : ""}</small>
        </div>
      )}
    </div>
  );
}

export function Sparkline({ data, color = "var(--accent)" }: { data: number[]; color?: string }) {
  if (data.length < 2) return <div className="sparkline-empty" />;
  const width = 140;
  const height = 36;
  const min = Math.min(...data);
  const max = Math.max(...data);
  const span = max - min || 1;
  const points = data.map((value, index) => `${(index / (data.length - 1)) * width},${height - ((value - min) / span) * (height - 5) - 2}`).join(" ");
  return (
    <svg viewBox={`0 0 ${width} ${height}`} className="sparkline" aria-hidden="true">
      <polyline points={points} fill="none" stroke={color} strokeWidth="2" strokeLinecap="round" />
    </svg>
  );
}

export type Segment = { label: string; value: number; color: string };

export function DonutChart({
  segments,
  totalLabel,
  totalValue,
  size = 156,
}: {
  segments: Segment[];
  totalLabel?: string;
  totalValue?: string;
  size?: number;
}) {
  const total = segments.reduce((sum, segment) => sum + segment.value, 0);
  const radius = 54;
  const circumference = 2 * Math.PI * radius;
  let offset = 0;
  return (
    <div className="donut-wrap" style={{ width: size, height: size }}>
      <svg viewBox="0 0 140 140" className="donut" role="img" aria-label="Allocation chart">
        <circle cx="70" cy="70" r={radius} fill="none" stroke="var(--line)" strokeWidth="12" />
        {total > 0 && segments.map((segment) => {
          const length = (segment.value / total) * circumference;
          const stroke = (
            <circle
              key={segment.label}
              cx="70"
              cy="70"
              r={radius}
              fill="none"
              stroke={segment.color}
              strokeWidth="12"
              strokeLinecap="butt"
              strokeDasharray={`${Math.max(length - 2, 0)} ${circumference - Math.max(length - 2, 0)}`}
              strokeDashoffset={-offset}
              transform="rotate(-90 70 70)"
            />
          );
          offset += length;
          return stroke;
        })}
      </svg>
      <div className="donut-center">
        <strong>{totalValue || "—"}</strong>
        <span>{totalLabel || "Total"}</span>
      </div>
    </div>
  );
}

export function AllocationBars({ segments }: { segments: Segment[] }) {
  const total = segments.reduce((sum, segment) => sum + segment.value, 0);
  return (
    <div className="allocation-bars">
      {segments.map((segment) => {
        const share = total > 0 ? (segment.value / total) * 100 : 0;
        return (
          <div className="allocation-bar" key={segment.label}>
            <div className="allocation-label">
              <span><i style={{ background: segment.color }} />{segment.label}</span>
              <strong>{share.toFixed(1)}%</strong>
            </div>
            <div className="allocation-track"><span style={{ width: `${share}%`, background: segment.color }} /></div>
          </div>
        );
      })}
    </div>
  );
}

export function HBar({ label, value, max, display, color = "var(--accent)" }: { label: string; value: number; max: number; display: string; color?: string }) {
  const width = max > 0 ? Math.min(100, (Math.abs(value) / max) * 100) : 0;
  return (
    <div className="hbar">
      <div className="hbar-label"><span>{label}</span><strong>{display}</strong></div>
      <div className="hbar-track"><span style={{ width: `${width}%`, background: color }} /></div>
    </div>
  );
}

export function DrawdownChart({ data }: { data: Point[] }) {
  return <LineChart data={data} format="percent" color="var(--danger)" empty="No drawdown series yet" />;
}
