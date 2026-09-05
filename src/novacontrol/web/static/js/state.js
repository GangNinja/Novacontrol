/* ── Application State ─────────────────────────────────────── */

const state = {
  lastText: "",
  lastQuery: "",
  lastBuildGoal: "",
  lastPreviewId: "",
  lastExploredTopic: "",
  recognition: null,
  voiceMode: null,
  voiceChatActive: false,
  pendingSpeak: false,
  lastRaw: null,
  // Server-minted approval token for the last previewed desktop action.
  // Mirrors _APPROVAL_TTL_SECONDS (300s) in application.py: { command, token, mintedAt }
  approval: null,
};

const APPROVAL_TTL_MS = 5 * 60 * 1000;

/* ── Desktop approval token ───────────────────────────────── */

// Remember the token from a freshly planned, not-yet-approved action so the
// sidebar's "Approve And Run" can execute it without re-planning.
function rememberApproval(data) {
  const approval = data && data.approval;
  if (!data || data.status !== "waiting_for_approval" || !approval || approval.approved || !approval.token) return;
  state.approval = { command: data.command, token: approval.token, mintedAt: Date.now() };
}

// Return the stored token when it still approves this exact command, else null.
function approvalFor(command) {
  const pending = state.approval;
  if (!pending || pending.command !== command) return null;
  if (Date.now() - pending.mintedAt > APPROVAL_TTL_MS) {
    state.approval = null; // server-side expiry; force a fresh plan next time
    return null;
  }
  return pending.token;
}

// Drop the stored approval once its single-use token has been consumed.
function clearApproval(command) {
  if (state.approval && state.approval.command === command) state.approval = null;
}

/* ── API / HTTP ───────────────────────────────────────────── */

function authHeaders() {
  const token = byId("tokenInput").value.trim();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function requestJson(path, payload) {
  const options = payload
    ? {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify(payload),
      }
    : { headers: authHeaders() };
  const response = await fetch(path, options);
  const text = await response.text();
  const data = text ? JSON.parse(text) : {};
  if (!response.ok) {
    throw new Error(data.detail || response.statusText);
  }
  state.lastRaw = data;
  byId("cliOutput").textContent = JSON.stringify(data, null, 2);
  return data;
}

function setStatus(text) {
  byId("statusText").textContent = text;
}

/* ── Answer text extraction ────────────────────────────────── */

// Extract the primary user-facing text from any response shape. This is the
// single owner of chat-answer text policy; speech, rendering, and history all
// consume it so they can never disagree about which field is the answer.
//
// Precedence (first non-empty value wins):
//   1. A bare string is returned as-is.
//   2. /ask envelopes nest the payload under `payload`; unwrap when it's an object.
//   3. Inside the payload: message -> answer -> content -> overview -> summary.
//   4. Top-level fields (flat bodies, or envelope-level summaries):
//      summary -> answer -> content -> overview -> message.
//
// The result is trimmed; leaked internal routing text ("Payload:"/"Intent:")
// is replaced with a friendly line; an empty result returns `fallback`.
function extractAnswerText(value, fallback = "") {
  if (typeof value === "string") return value.trim() || fallback;
  if (!value) return fallback;
  const payload = value.payload && typeof value.payload === "object" ? value.payload : value;
  const fields = ["message", "answer", "content", "overview", "summary"];
  for (const field of fields) {
    const hit = payload[field];
    if (typeof hit === "string" && hit.trim()) return sanitizeAnswerText(hit);
  }
  for (const field of fields) {
    const hit = value[field];
    if (typeof hit === "string" && hit.trim()) return sanitizeAnswerText(hit);
  }
  return fallback;
}

function sanitizeAnswerText(text) {
  const clean = String(text).trim();
  if (clean.includes("Payload:") || clean.includes("Intent:")) {
    return "I routed this through NovaControl and prepared a clear result.";
  }
  return clean;
}

// Speech fallbacks beyond the shared extractor: non-answer shapes still need
// something speakable (learning scope, readiness, or a neutral default).
function textForSpeech(value) {
  if (!value) return "";
  const primary = extractAnswerText(value);
  if (primary) return primary;
  if (value.training_scope) return value.training_scope;
  if (value.ready !== undefined) return value.ready ? "Ready" : "Needs attention";
  return "Result ready.";
}
