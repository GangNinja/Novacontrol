/* ── Application State ─────────────────────────────────────── */

const state = {
  lastText: "",
  lastQuery: "",
  lastBuildGoal: "",
  lastExploredTopic: "",
  recognition: null,
  voiceMode: null,
  voiceChatActive: false,
  pendingSpeak: false,
  lastRaw: null,
  // Settings → "Auto-approve & run": a freshly planned command executes
  // immediately (same tokened flow — the plan preview still renders).
  autoApproveRun: false,
  // Server-minted approval token for the last previewed desktop action.
  // Mirrors _APPROVAL_TTL_SECONDS (300s) in application.py: { command, token, mintedAt }
  approval: null,
  // Server-side improvement preview id for the last generated preview. Goal-bound
  // like the approval token: { goal, previewId, mintedAt } — Approve And Apply
  // reuses it instead of asking the server to re-derive the preview.
  preview: null,
};

const APPROVAL_TTL_MS = 5 * 60 * 1000;
// Same session horizon as the approval token: a preview kept open past this is
// dropped client-side, so a stale apply never silently targets old changes.
const PREVIEW_TTL_MS = 5 * 60 * 1000;

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

/* ── Improvement preview token ────────────────────────────── */

// Remember the preview id of a freshly generated, not-yet-approved improvement
// preview so the sidebar's "Approve And Apply" can reuse it instead of asking
// the server to re-derive the preview on every click. An approve response
// carries its preview back already applied, so it is deliberately not remembered.
function rememberPreview(data) {
  const preview = data && data.preview;
  if (!data || !preview || !preview.id) return;
  const approval = data.approval;
  if (approval && approval.approved) return;
  state.preview = { goal: data.goal || "", previewId: preview.id, mintedAt: Date.now() };
}

// Return the stored preview id when it still matches this exact goal, else "".
// "" means "no reusable preview" — the caller falls back to a fresh preview.
function previewFor(goal) {
  const pending = state.preview;
  if (!pending || !goal || pending.goal !== goal) return "";
  if (Date.now() - pending.mintedAt > PREVIEW_TTL_MS) {
    state.preview = null; // stale — force a fresh preview next time
    return "";
  }
  return pending.previewId;
}

// Drop the stored preview once it has been applied.
function clearPreview(goal) {
  if (state.preview && state.preview.goal === goal) state.preview = null;
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
//   2. /ask envelopes carry the handler payload under the `data` key; unwrap it
//      when it is an object (never probe for `payload` nesting again).
//   3. Inside the data payload: message -> answer -> content -> overview -> summary.
//      `content` here is REACHABLE, not vestigial: the /ask agent fallback
//      (_handle_agent -> AgentResponse.to_dict(), route "agent") is the one
//      handler that ships a content-only payload {task_id, agent_name, role,
//      status, content, metadata, brain_mode} with NO message. The scan hits
//      data.content before the top-level envelope summary, so removing this
//      branch would drop every agent answer to the generic "I completed the
//      request." fallback. Keep it while that handler speaks `content`.
//   4. Top-level fields (flat bodies, or envelope-level summaries):
//      summary -> answer -> content -> overview -> message.
//
// The result is trimmed; leaked internal routing text ("Payload:"/"Intent:")
// is replaced with a friendly line; an empty result returns `fallback`.
function extractAnswerText(value, fallback = "") {
  if (typeof value === "string") return value.trim() || fallback;
  if (!value) return fallback;
  const payload = value.data && typeof value.data === "object" ? value.data : value;
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
