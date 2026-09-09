/* ── Recent Activity (Command Center timeline) ────────────── */

// The timeline is SERVER-fed, not localStorage-fed: /activity seeds it once on
// load, and completion events arriving over the single /events/stream channel
// append live. That means actions started from ANY client (CLI, GUI, another
// tab) appear here, and the feed survives a browser profile reset — the
// previous localStorage copy could do neither.

const ACTIVITY_LIMIT = 25;
const ACTIVITY_PILL = { command: "ok", research: "", learn: "warn" };

// Newest-first entries already shaped like the server's journal records
// ({type, title, detail, at}); at is epoch millis, matching timeAgo().
let activityEntries = [];

// Seed from /activity (server journal, newest first). Called once at init and
// again whenever the EventSource (re)opens, so a reconnect backfills any gap
// without polling — the /activity GET is the only timeline request.
async function loadActivityFromServer() {
  let data;
  try {
    data = await requestJson("/activity");
  } catch (_) { /* timeline stays live-only */ return; }
  if (data && Array.isArray(data.activity)) {
    activityEntries = data.activity.slice(0, ACTIVITY_LIMIT);
    renderActivityFeed();
  }
}

// Append one completion from the /events/stream channel (event type
// "<family>.completed", payload = {type, title, detail, at}). The SSE payload
// already carries the journal record shape, so no per-client recording exists
// anymore — recordActivity was the localStorage path and is gone.
function recordActivityFromEvent(data) {
  const type = String(data.type || "").split(".")[0] === "explore" ? "research" : String(data.type || "").split(".")[0];
  const family = String(data.type || "").split(".")[0];
  if (!["command", "explore", "learn"].includes(family)) return;
  const title = String(data.title || "");
  if (!title) return;
  const detail = String(data.detail || "");
  const at = Number(data.at) || Date.now();
  // De-dupe: seeding races the stream (the completing tab gets its own event
  // for an action the seed may already include).
  if (activityEntries.some((e) => e.type === type && e.title === title && e.detail === detail && Math.abs(e.at - at) < 5000)) return;
  activityEntries.unshift({ type, title, detail, at });
  activityEntries = activityEntries.slice(0, ACTIVITY_LIMIT);
  renderActivityFeed();
}

function timeAgo(at) {
  const seconds = Math.max(0, Math.round((Date.now() - at) / 1000));
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return new Date(at).toLocaleDateString();
}

function renderActivityFeed() {
  const feed = byId("homeActivity");
  if (!feed) return;
  clearNode(feed);
  feed.classList.toggle("empty-state", activityEntries.length === 0);
  if (!activityEntries.length) {
    const hint = el("div", "empty-hint");
    hint.appendChild(el("span", "empty-hint-icon", "\u26A1"));
    hint.appendChild(document.createTextNode(" Executed commands, research, and learning cycles will appear here"));
    feed.appendChild(hint);
    return;
  }
  activityEntries.slice(0, ACTIVITY_LIMIT).forEach((entry) => {
    const row = el("div", "activity-item");
    row.appendChild(el("span", `pill activity-type ${ACTIVITY_PILL[entry.type] || ""}`.trim(), label(entry.type || "activity")));
    const body = el("div", "activity-body");
    body.appendChild(el("strong", "", entry.title || label(entry.type)));
    if (entry.detail) body.appendChild(el("span", "", entry.detail));
    row.appendChild(body);
    row.appendChild(el("time", "activity-time", timeAgo(entry.at)));
    feed.appendChild(row);
  });
}
