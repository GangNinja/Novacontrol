/**
 * NovaMarket's only backend access point.
 *
 * The browser never talks to the database and never derives financial values.
 * Every number rendered by the UI comes from one of these responses.
 */

export type Json = any;

const TOKEN_KEY = "novamarket.apiToken";
const configuredBase = (import.meta.env.VITE_API_BASE_URL || "").replace(/\/$/, "");
export const API_BASE = configuredBase;

export class ApiError extends Error {
  status: number;
  detail: string;
  body: Json;

  constructor(message: string, status: number, body: Json = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
    this.detail =
      typeof body?.detail === "string"
        ? body.detail
        : typeof body?.message === "string"
          ? body.message
          : message;
  }
}

export function getToken(): string {
  try {
    return window.sessionStorage.getItem(TOKEN_KEY) || "";
  } catch {
    return "";
  }
}

export function setToken(token: string): void {
  try {
    if (token.trim()) window.sessionStorage.setItem(TOKEN_KEY, token.trim());
    else window.sessionStorage.removeItem(TOKEN_KEY);
  } catch {
    /* session storage can be unavailable in hardened browsers */
  }
}

function endpoint(path: string, params?: Record<string, string | number | boolean | null | undefined>): string {
  const url = `${API_BASE}${path.startsWith("/") ? path : `/${path}`}`;
  if (!params) return url;
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    query.set(key, String(value));
  }
  const suffix = query.toString();
  return suffix ? `${url}?${suffix}` : url;
}

function describe(status: number, body: Json): string {
  if (typeof body?.detail === "string") return body.detail;
  if (body?.detail && typeof body.detail === "object" && typeof body.detail.message === "string") return body.detail.message;
  if (typeof body?.message === "string") return body.message;
  if (status === 401) return "Authentication is required. Add your API token in Settings.";
  if (status === 403) return "This action is not permitted.";
  if (status === 404) return "That record no longer exists.";
  if (status === 422) return "The backend rejected the request fields.";
  if (status === 429) return "Too many requests. Wait a moment and try again.";
  if (status >= 500) return "The NovaMarket backend could not complete the request.";
  return `Request failed (${status}).`;
}

async function request<T>(
  method: string,
  path: string,
  options: {
    params?: Record<string, string | number | boolean | null | undefined>;
    body?: Json;
    signal?: AbortSignal;
    timeoutMs?: number;
    operatorToken?: string;
  } = {},
): Promise<T> {
  const token = getToken();
  const headers: Record<string, string> = { Accept: "application/json" };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (token) headers.Authorization = `Bearer ${token}`;
  if (options.operatorToken?.trim()) headers["X-Autonomy-Operator-Token"] = options.operatorToken.trim();

  const timeout = AbortSignal.timeout(options.timeoutMs ?? 25_000);
  const signal = options.signal
    ? AbortSignal.any([options.signal, timeout])
    : timeout;

  let response: Response;
  try {
    response = await fetch(endpoint(path, options.params), {
      method,
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      signal,
      credentials: "omit",
    });
  } catch (error: any) {
    if (error?.name === "AbortError") throw error;
    if (error?.name === "TimeoutError") {
      throw new ApiError("The NovaMarket backend did not answer in time.", 0);
    }
    throw new ApiError(
      "Cannot reach the NovaMarket backend. Check the API origin and that the service is running.",
      0,
    );
  }

  const text = await response.text();
  let body: Json = null;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = text;
    }
  }
  if (!response.ok) {
    if (response.status === 401) {
      window.dispatchEvent(new CustomEvent("novamarket:unauthorized"));
    }
    throw new ApiError(describe(response.status, body), response.status, body);
  }
  return body as T;
}

export function apiGet<T = Json>(
  path: string,
  params?: Record<string, string | number | boolean | null | undefined>,
  signal?: AbortSignal,
  options: { operatorToken?: string } = {},
): Promise<T> {
  return request<T>("GET", path, { params, signal, operatorToken: options.operatorToken });
}

export function apiPost<T = Json>(
  path: string,
  body: Json = {},
  signal?: AbortSignal,
  options: { timeoutMs?: number; operatorToken?: string } = {},
): Promise<T> {
  return request<T>("POST", path, {
    body,
    signal,
    timeoutMs: options.timeoutMs,
    operatorToken: options.operatorToken,
  });
}

export function apiPut<T = Json>(
  path: string,
  body: Json = {},
  signal?: AbortSignal,
  options: { timeoutMs?: number; operatorToken?: string } = {},
): Promise<T> {
  return request<T>("PUT", path, {
    body,
    signal,
    timeoutMs: options.timeoutMs,
    operatorToken: options.operatorToken,
  });
}

export function apiDelete<T = Json>(
  path: string,
  signal?: AbortSignal,
  options: { operatorToken?: string } = {},
): Promise<T> {
  return request<T>("DELETE", path, { signal, operatorToken: options.operatorToken });
}

export function newIdempotencyKey(prefix = "web"): string {
  const random =
    typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `${prefix}-${random}`;
}
