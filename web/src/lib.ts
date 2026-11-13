export type Row = Record<string, any>;

const inrFormatter = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

const compactFormatter = new Intl.NumberFormat("en-IN", {
  notation: "compact",
  maximumFractionDigits: 1,
});

export function money(value: unknown, fallback = "—"): string {
  if (value === null || value === undefined || value === "") return fallback;
  const number = typeof value === "number" ? value : Number(String(value).replace(/,/g, ""));
  if (!Number.isFinite(number)) return fallback;
  return inrFormatter.format(number).replace("₹", "₹ ");
}

export function compact(value: unknown, fallback = "—"): string {
  if (value === null || value === undefined || value === "") return fallback;
  const number = Number(value);
  if (!Number.isFinite(number)) return fallback;
  return compactFormatter.format(number);
}

export function num(value: unknown, fallback = 0): number {
  // Number("") is 0, so an absent value must not fall through to it: that is
  // how an unmeasured quote became a confident ₹0.00 on the dashboard.
  if (value === null || value === undefined || value === "") return fallback;
  const number = typeof value === "number" ? value : Number(String(value).replace(/,/g, ""));
  return Number.isFinite(number) ? number : fallback;
}

export function horizonSlug(value: unknown): string {
  const text = String(value ?? "").toLowerCase();
  if (text.startsWith("short")) return "short";
  if (text.startsWith("medium")) return "medium";
  if (text.startsWith("long")) return "long";
  return text;
}

export function horizonApi(value: unknown): string {
  const slug = horizonSlug(value);
  if (slug === "short") return "short_term";
  if (slug === "medium") return "medium_term";
  if (slug === "long") return "long_term";
  return String(value ?? "");
}

export function pct(value: unknown, fallback = "—"): string {
  if (value === null || value === undefined || value === "") return fallback;
  const number = num(value, NaN);
  if (!Number.isFinite(number)) return fallback;
  return `${number >= 0 ? "+" : ""}${number.toFixed(2)}%`;
}

export function pctUnsigned(value: unknown, fallback = "—"): string {
  if (value === null || value === undefined || value === "") return fallback;
  const number = num(value, NaN);
  if (!Number.isFinite(number)) return fallback;
  return `${number.toFixed(2)}%`;
}

export function quantity(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  return String(value);
}

export function dateTime(value: unknown, fallback = "—"): string {
  if (!value) return fallback;
  const date = new Date(String(value));
  if (Number.isNaN(date.getTime())) return fallback;
  return new Intl.DateTimeFormat("en-IN", {
    day: "2-digit",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

export function shortDate(value: unknown, fallback = "—"): string {
  if (!value) return fallback;
  const date = new Date(String(value));
  if (Number.isNaN(date.getTime())) return fallback;
  return new Intl.DateTimeFormat("en-IN", {
    day: "2-digit",
    month: "short",
    year: "numeric",
  }).format(date);
}

export function timeAgo(value: unknown): string {
  if (!value) return "—";
  const date = new Date(String(value));
  if (Number.isNaN(date.getTime())) return "—";
  const seconds = Math.round((date.getTime() - Date.now()) / 1000);
  const abs = Math.abs(seconds);
  const formatter = new Intl.RelativeTimeFormat("en", { numeric: "auto" });
  if (abs < 45) return "just now";
  if (abs < 3600) return formatter.format(Math.round(seconds / 60), "minute");
  if (abs < 86_400) return formatter.format(Math.round(seconds / 3600), "hour");
  if (abs < 2_592_000) return formatter.format(Math.round(seconds / 86_400), "day");
  return shortDate(value);
}

export function title(value: unknown): string {
  return String(value ?? "")
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

export function shortId(value: unknown, length = 10): string {
  const text = String(value ?? "");
  if (!text) return "—";
  return text.length > length ? `${text.slice(0, length)}…` : text;
}

export function arrayFrom(body: any, ...keys: string[]): Row[] {
  if (Array.isArray(body)) return body;
  if (!body) return [];
  for (const key of keys) {
    if (Array.isArray(body[key])) return body[key];
  }
  if (Array.isArray(body.items)) return body.items;
  if (Array.isArray(body.data)) return body.data;
  return [];
}

export function objectFrom(body: any, ...keys: string[]): Row {
  if (!body || typeof body !== "object") return {};
  for (const key of keys) {
    if (body[key] && typeof body[key] === "object" && !Array.isArray(body[key])) {
      return body[key];
    }
  }
  return body;
}

export function first(body: any, ...keys: string[]): any {
  if (!body) return undefined;
  for (const key of keys) {
    if (body[key] !== undefined && body[key] !== null) return body[key];
  }
  return undefined;
}

export function quoteValue(quote: any): number | null {
  if (!quote) return null;
  const source = quote.quote || quote.observation || quote.data || quote;
  // An unavailable quote carries no observation at all. Rendering its absence
  // as zero would invent a price, so refuse the value explicitly.
  if (source.usable === false || source.status === "unavailable") return null;
  const selected = source.selected && typeof source.selected === "object" ? source.selected : null;
  const value =
    selected?.price ??
    selected?.close ??
    source.price ??
    source.close ??
    source.last ??
    source.value ??
    source.amount ??
    quote.price ??
    quote.close;
  const parsed = num(value, NaN);
  return Number.isFinite(parsed) ? parsed : null;
}

export function quoteTime(quote: any): string {
  if (!quote) return "";
  const source = quote.quote || quote.observation || quote.data || quote;
  return (
    source.observed_at ||
    source.as_of ||
    source.timestamp ||
    source.at ||
    quote.observed_at ||
    quote.as_of ||
    ""
  );
}

export function quoteChange(quote: any): number | null {
  if (!quote) return null;
  const source = quote.quote || quote.observation || quote.data || quote;
  const value = source.change_pct ?? source.change_percent ?? source.changePct ?? quote.change_pct;
  const parsed = num(value, NaN);
  return Number.isFinite(parsed) ? parsed : null;
}

export function valuationPoints(body: any): { label: string; value: number }[] {
  return arrayFrom(body, "valuations")
    .map((row) => ({
      label: String(row.at || row.created_at || ""),
      value: num(row.total_value ?? row.value, NaN),
    }))
    .filter((point) => Number.isFinite(point.value));
}

export function barsFrom(body: any): Row[] {
  return arrayFrom(body, "bars", "items", "observations");
}

export function barPoints(body: any): { label: string; value: number }[] {
  return barsFrom(body)
    .map((row) => ({
      label: String(row.at || row.timestamp || row.observed_at || row.date || ""),
      value: num(row.close ?? row.price ?? row.value, NaN),
    }))
    .filter((point) => Number.isFinite(point.value))
    // The bars endpoint returns newest-first when asked for the latest window.
    // Charts must always plot oldest-to-newest regardless of transport order.
    .sort((left, right) => left.label.localeCompare(right.label));
}

export function statusTone(status: unknown): "good" | "warn" | "bad" | "neutral" | "info" {
  const value = String(status ?? "").toLowerCase();
  if (["ok", "healthy", "active", "running", "completed", "promoted", "approved", "passed", "available", "executed", "filled", "open", "validated"].includes(value)) return "good";
  if (["degraded", "paused", "pending", "pending_approval", "queued", "created", "under_review", "draft", "warning", "warn", "stale", "partial", "waiting_for_approval", "idle"].includes(value)) return "warn";
  if (["failed", "error", "blocked", "rejected", "cancelled", "canceled", "suspended", "critical", "unhealthy", "offline", "expired", "violated"].includes(value)) return "bad";
  if (["running", "active", "live"].includes(value)) return "info";
  return "neutral";
}

export function severityTone(value: unknown): "good" | "warn" | "bad" | "neutral" | "info" {
  const text = String(value ?? "").toLowerCase();
  if (["critical", "high", "error"].includes(text)) return "bad";
  if (["warning", "warn", "medium"].includes(text)) return "warn";
  if (["info", "low"].includes(text)) return "info";
  return "neutral";
}

export function initials(value: string): string {
  return value
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase())
    .join("");
}

export function humanBytes(value: unknown): string {
  const bytes = Number(value);
  if (!Number.isFinite(bytes) || bytes <= 0) return "—";
  const units = ["B", "KB", "MB", "GB"];
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  return `${(bytes / 1024 ** index).toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
}
