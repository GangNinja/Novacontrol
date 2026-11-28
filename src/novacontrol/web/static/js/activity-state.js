/* ── Extra 4 · Central AI activity state ─────────────────────
 *
 * ONE adapter turns real NovaControl runtime events into the four orb
 * placements. It is a presentation consumer, not a planner: it never starts,
 * stops or steers work.
 *
 * Sources (all existing):
 *   • the app-wide `/events/stream` SSE bus (intent/decision/plan/task/tool/
 *     verification/recovery/explore/command events);
 *   • `run()`'s local request lifecycle (a request this tab started);
 *   • Web Speech events from `voice.js` (microphone acquired, speech captured,
 *     speech output started/ended, failure);
 *   • the real `/status` tracked-task snapshot.
 *
 * Four scopes read the same activity store: main, explore, voice, task. An
 * activity may feed several scopes; each scope resolves its own view, so an
 * unrelated NovaMarket operation cannot appear as Explore research and an idle
 * microphone cannot appear as Listening.
 *
 * Staleness rules: terminal events settle after a short dwell; an activity
 * with no update for two minutes (five for a task) becomes UNKNOWN instead of
 * pretending it is still running; a closed SSE transport says so. Out-of-order
 * frames are rejected when they carry an older explicit sequence, and progress
 * frames received after a terminal frame for the same activity are ignored.
 */
(function () {
  "use strict";

  const SCOPES = ["main", "explore", "voice", "task"];
  const SETTLE_MS = 2400;
  const STALE_MS = 120000;
  const TASK_STALE_MS = 300000;
  const DEDUPE_MS = 700;
  const MAX_ACTIVITIES = 48;
  const MAX_SEEN = 240;

  const DEFAULTS = {
    main: {
      state: "idle",
      label: "Ready",
      description: "NovaControl is idle — send a request or start a voice session.",
      source: "system",
    },
    explore: {
      state: "idle",
      label: "Research idle",
      description: "No research is running.",
      source: "explore",
    },
    voice: {
      state: "idle",
      label: "Voice idle",
      description: "The microphone is off.",
      source: "voice",
    },
    task: {
      state: "idle",
      label: "No active task",
      description: "Start a request to see its execution state here.",
      source: "tasks",
    },
  };

  const DEFAULT_ORB = {
    idle: "breathing",
    breathing: "breathing",
    listening: "listening",
    thinking: "weaving",
    searching: "searching",
    planning: "shaping",
    connecting: "connecting",
    working: "working",
    solving: "solving",
    recovering: "solving",
    composing: "composing",
    waiting: "breathing",
    completed: "breathing",
    cancelled: "breathing",
    error: "error",
    unknown: "breathing",
  };

  const TERMINAL = new Set(["completed", "failed", "cancelled"]);
  const TERMINAL_TASK_STATUS = new Set(["completed", "failed", "cancelled", "deleted"]);

  let seq = 0;
  let localSeq = 0;
  const activities = new Map();
  const localRuns = new Map();
  const subscribers = new Set();
  const lastPublished = new Map();
  const seen = new Map();
  let voiceActivityId = null;
  let transport = "connected";
  let transportDetail = "";
  let taskSnapshot = [];
  let settleTimer = null;

  function now() {
    return Date.now();
  }

  function orbState(state) {
    return DEFAULT_ORB[state] || "breathing";
  }

  function clampText(value, limit = 180) {
    const text = String(value == null ? "" : value).replace(/\s+/g, " ").trim();
    return text.length > limit ? text.slice(0, limit - 1) + "…" : text;
  }

  function makeActivity(id, state, label, description, meta = {}) {
    const stamp = now();
    return {
      id,
      state,
      orb: orbState(state),
      label: clampText(label, 60) || "Working",
      description: clampText(description, 220),
      source: meta.source || "system",
      taskId: meta.taskId || "",
      sessionId: meta.sessionId || "",
      correlationId: meta.correlationId || "",
      status: meta.status || state,
      error: meta.error || "",
      scopes: new Set(meta.scopes || ["main"]),
      local: Boolean(meta.local),
      priority: Number(meta.priority) || 0,
      startedAt: stamp,
      updatedAt: stamp,
      seq: ++seq,
      lastEventSeq: null,
      terminal: Boolean(meta.terminal),
      stale: false,
    };
  }

  function defaultFor(scope) {
    const base = DEFAULTS[scope] || DEFAULTS.main;
    return {
      ...base,
      orb: orbState(base.state),
      scope,
      taskId: "",
      sessionId: "",
      correlationId: "",
      status: base.state,
      error: "",
      updatedAt: 0,
      terminal: false,
      stale: false,
      local: false,
    };
  }

  function trimActivities() {
    if (activities.size <= MAX_ACTIVITIES) return;
    const ordered = [...activities.values()].sort((a, b) => a.updatedAt - b.updatedAt);
    while (ordered.length > MAX_ACTIVITIES) {
      const old = ordered.shift();
      if (!old) break;
      activities.delete(old.id);
      if (voiceActivityId === old.id) voiceActivityId = null;
    }
  }

  function scheduleSettle() {
    // One shared timer is enough: every publish re-resolves all four scopes.
    // Keep the handle in a variable rather than attaching a property to it —
    // browsers return a NUMBER from setTimeout, and writing a property onto a
    // primitive throws in strict mode (this interrupted a live chat run once).
    if (settleTimer != null) return;
    settleTimer = setTimeout(() => {
      settleTimer = null;
      publish(true);
    }, SETTLE_MS + 60);
  }

  function isFresh(activity, maxAge) {
    return activity && now() - activity.updatedAt < maxAge;
  }

  function pickLatest(scope, options = {}) {
    // A genuinely live microphone is the primary activity while it is open.
    // Processing/speaking do NOT get this override: the request lifecycle that
    // follows a captured phrase must be able to take the main orb over.
    if (scope === "main") {
      const voice = activities.get("voice:local");
      if (
        voice &&
        (voice.state === "listening" || voice.state === "error") &&
        !voice.terminal &&
        isFresh(voice, STALE_MS) &&
        voice.scopes.has("main")
      ) {
        return voice;
      }
    }
    const allowTaskSnapshot = options.taskSnapshot !== false;
    let latest = null;
    for (const activity of activities.values()) {
      if (!activity.scopes.has(scope)) continue;
      if (!isFresh(activity, activity.state === "unknown" ? SETTLE_MS : Math.max(STALE_MS, SETTLE_MS))) continue;
      if (activity.stale && activity.state !== "unknown") continue;
      if (activity.terminal && now() - activity.updatedAt > SETTLE_MS) continue;
      // Several events can arrive inside the same millisecond (a fast local
      // response and its first SSE frame). The monotonic sequence is the
      // tiebreaker, so a later event cannot lose to loop order.
      if (
        !latest ||
        activity.updatedAt > latest.updatedAt ||
        (activity.updatedAt === latest.updatedAt && activity.seq > latest.seq)
      ) {
        latest = activity;
      }
    }
    if (latest) return latest;
    if (scope === "task" && allowTaskSnapshot) {
      const active = taskSnapshot.find((task) => task && !TERMINAL_TASK_STATUS.has(String(task.status || "").toLowerCase()));
      if (active) {
        return {
          ...defaultFor("task"),
          state: "working",
          orb: orbState("working"),
          label: "Task in progress",
          description: clampText(active.title || "A tracked task is running.", 220),
          source: "tasks",
          taskId: active.id || "",
          status: active.status || "running",
          terminal: false,
          updatedAt: now(),
        };
      }
    }
    return defaultFor(scope);
  }

  function viewFor(scope) {
    if (scope === "main" && transport !== "connected") {
      const live = pickLatest("main");
      if (!live || live.state === "idle") {
        return {
          ...defaultFor("main"),
          state: "unknown",
          orb: orbState("unknown"),
          label: "Activity feed reconnecting",
          description: transportDetail || "The live activity channel is not connected. Orb states may be stale.",
          source: "transport",
          status: "unavailable",
          updatedAt: now(),
        };
      }
    }
    return pickLatest(scope);
  }

  function signature(state) {
    return [state.state, state.label, state.description, state.taskId, state.source, state.status].join("|");
  }

  function publish(force = false) {
    SCOPES.forEach((scope) => {
      const view = viewFor(scope);
      const sig = signature(view);
      if (!force && lastPublished.get(scope) === sig) return;
      lastPublished.set(scope, sig);
      subscribers.forEach((fn) => {
        try {
          fn(scope, view);
        } catch (error) {
          if (window.console && window.console.warn) window.console.warn("NovaActivity subscriber failed", error);
        }
      });
    });
  }

  function scopeListFor(type, payload) {
    if (type.indexOf("explore.") === 0) return ["explore", "main"];
    if (type.indexOf("command.") === 0) return ["task", "main"];
    if (
      type.indexOf("tool.") === 0 ||
      type.indexOf("verification.") === 0 ||
      type.indexOf("recovery.") === 0 ||
      type.indexOf("task.") === 0
    ) {
      return ["task", "main"];
    }
    if (type === "learn.completed" || type === "goal.executed" || type === "agents.completed") {
      return ["task", "main"];
    }
    return ["main"];
  }

  function describeEvent(type, payload) {
    if (type === "task.started") {
      const kind = clampText(payload.kind || payload.agent || "request", 40);
      const conversational = /ask|chat|conversation|research/i.test(kind);
      return {
        state: conversational ? "thinking" : "working",
        label: conversational ? "Thinking" : "Task started",
        description: conversational ? "Understanding the request and choosing how to answer." : `Running a ${kind} task.`,
      };
    }
    if (type === "task.paused") return { state: "waiting", label: "Paused", description: "The task is paused." };
    if (type === "task.resumed") return { state: "working", label: "Resumed", description: "The task is running again." };
    if (type === "intent.detected") {
      const intent = clampText(payload.intent || "request", 40);
      return { state: "thinking", label: "Understanding", description: `Classified the request as ${intent}.` };
    }
    if (type === "context.resolved") {
      return { state: "thinking", label: "Resolving context", description: "Reading conversation and workspace context." };
    }
    if (type === "decision.created") {
      const route = clampText(payload.route || payload.decision_type || "a route", 40);
      return { state: "thinking", label: "Choosing an approach", description: `Selected ${route}.` };
    }
    if (type === "plan.created") {
      const count = Array.isArray(payload.steps) ? payload.steps.length : 0;
      return { state: "planning", label: "Planning", description: count ? `Built a plan with ${count} step${count === 1 ? "" : "s"}.` : "Building a plan." };
    }
    if (type === "tool.selected") {
      return { state: "connecting", label: "Selecting a tool", description: `Selected ${clampText(payload.tool || "a tool", 60)}.` };
    }
    if (type === "tool.started") {
      return { state: "working", label: "Working", description: `Running ${clampText(payload.tool || "a tool", 60)}.` };
    }
    if (type === "tool.completed") {
      return { state: "solving", label: "Checking the result", description: `${clampText(payload.tool || "Tool", 60)} finished${payload.status ? ` (${clampText(payload.status, 24)})` : ""}.` };
    }
    if (type === "tool.failed") {
      return { state: "error", label: "Tool failed", description: clampText(payload.error || "A tool reported a failure.", 200) };
    }
    if (type === "verification.started") {
      return { state: "solving", label: "Evaluating", description: "Verifying what the action actually produced." };
    }
    if (type === "verification.completed") {
      const failed = /fail|error|denied/i.test(String(payload.status || ""));
      return {
        state: failed ? "error" : "solving",
        label: failed ? "Verification failed" : "Result verified",
        description: payload.status ? `Verification status: ${clampText(payload.status, 40)}.` : "The result was checked.",
      };
    }
    if (type === "recovery.started") {
      const attempt = Number(payload.attempt) || 0;
      const ceiling = Number(payload.ceiling) || 0;
      return {
        state: "recovering",
        label: "Recovering",
        description: attempt && ceiling ? `Recovery attempt ${attempt} of ${ceiling}.` : "Trying a recovery path.",
      };
    }
    if (type === "recovery.completed") {
      return { state: "working", label: "Recovered", description: payload.outcome ? `Recovery outcome: ${clampText(payload.outcome, 60)}.` : "Recovery finished; resuming work." };
    }
    if (type === "task.completed") {
      return { state: "completed", label: "Completed", description: payload.route ? `Finished as ${clampText(payload.route, 40)}.` : "The task completed." };
    }
    if (type === "task.failed") {
      return { state: "error", label: "Failed", description: clampText(payload.error || "The task failed.", 200) };
    }
    if (type === "task.cancelled") {
      return { state: "cancelled", label: "Cancelled", description: "The task was cancelled." };
    }
    if (type === "explore.progress") {
      const step = String(payload.step || "");
      const detail = clampText(payload.detail || "", 180);
      const map = {
        cached: { state: "composing", label: "Using cached research", description: detail || "Returning a cached research result." },
        planning: { state: "thinking", label: "Planning research", description: detail || "Choosing search queries." },
        searching: { state: "searching", label: "Searching", description: detail || "Searching sources." },
        sources_found: { state: "solving", label: "Reading sources", description: detail || "Reading the sources that were found." },
        searching_videos: { state: "searching", label: "Searching videos", description: detail || "Looking for related videos." },
        videos_found: { state: "solving", label: "Checking videos", description: detail || "Reviewing related videos." },
        synthesizing: { state: "composing", label: "Composing", description: detail || "Writing the research answer." },
        complete: { state: "completed", label: "Research complete", description: detail || "The research report is ready." },
        retrying: { state: "recovering", label: "Retrying search", description: detail || "The first search came back empty." },
      };
      return map[step] || { state: "working", label: "Researching", description: detail || "Research is running." };
    }
    if (type === "explore.completed") {
      return { state: "completed", label: "Research complete", description: clampText(payload.detail || "The research report is ready.", 180) };
    }
    if (type === "command.progress") {
      return { state: "working", label: "Running action", description: clampText(payload.detail || payload.step || "Executing an approved action.", 180) };
    }
    if (type === "command.completed") {
      return { state: "completed", label: "Action complete", description: clampText(payload.detail || "The approved action finished.", 180) };
    }
    if (type === "learn.completed" || type === "goal.executed") {
      return { state: "completed", label: "Completed", description: "The operation finished." };
    }
    if (type === "runtime.stopped") {
      return { state: "unknown", label: "Runtime stopped", description: "NovaControl is no longer running." };
    }
    return null;
  }

  function ingest(type, payload, meta = {}) {
    const eventType = String(type || "").trim();
    if (!eventType) return null;
    const data = payload && typeof payload === "object" ? payload : {};
    const stamp = now();
    const dedupeKey = [
      eventType,
      data.correlation_id,
      data.task_id,
      data.step,
      data.detail,
      data.tool,
      data.status,
    ].join("|");
    const lastSeen = seen.get(dedupeKey);
    if (lastSeen && stamp - lastSeen < DEDUPE_MS) return null;
    seen.set(dedupeKey, stamp);
    while (seen.size > MAX_SEEN) seen.delete(seen.keys().next().value);

    const description = describeEvent(eventType, data);
    if (!description) return null;

    const correlation = String(data.correlation_id || data.task_id || data.session_id || `event:${eventType}`);
    let activity = activities.get(correlation);
    // Only a genuinely new task may reopen a terminal activity. A late
    // progress frame for a finished correlation is stale, never a new run.
    const isStart = eventType === "task.started";
    if (!activity) {
      activity = makeActivity(correlation, description.state, description.label, description.description, {
        source: eventType.split(".")[0],
        taskId: data.task_id || "",
        sessionId: data.session_id || "",
        correlationId: data.correlation_id || "",
        scopes: scopeListFor(eventType, data),
      });
      activities.set(correlation, activity);
    } else if (activity.terminal && !isStart) {
      // A late frame from a finished activity must not reopen it.
      return null;
    }

    if (meta.seq != null) {
      const numeric = Number(meta.seq);
      if (Number.isFinite(numeric) && activity.lastEventSeq != null && numeric < activity.lastEventSeq) return null;
      if (Number.isFinite(numeric)) activity.lastEventSeq = numeric;
    }

    // Late terminal frames after a newer activity for the same task are ignored.
    if (data.task_id) activity.taskId = String(data.task_id);
    if (data.session_id) activity.sessionId = String(data.session_id);
    if (data.correlation_id) activity.correlationId = String(data.correlation_id);
    activity.scopes = new Set(scopeListFor(eventType, data));
    activity.state = description.state;
    activity.orb = orbState(description.state);
    activity.label = clampText(description.label, 60) || "Working";
    activity.description = clampText(description.description, 220);
    activity.source = eventType.split(".")[0];
    activity.status = String(data.status || description.state);
    activity.error = description.state === "error" ? activity.description : "";
    activity.updatedAt = stamp;
    activity.seq = ++seq;
    activity.terminal = TERMINAL.has(description.state);
    activity.stale = false;

    if (activity.terminal) scheduleSettle();
    trimActivities();
    publish();
    return activity;
  }

  function upsertLocal(id, state, label, description, scopes, meta = {}) {
    const existing = activities.get(id);
    const stamp = now();
    if (!existing) {
      const activity = makeActivity(id, state, label, description, {
        source: meta.source || "local",
        scopes,
        local: true,
        priority: meta.priority || 2,
        taskId: meta.taskId || "",
      });
      activities.set(id, activity);
      publish();
      return activity;
    }
    existing.state = state;
    existing.orb = orbState(state);
    existing.label = clampText(label, 60);
    existing.description = clampText(description, 220);
    existing.status = state;
    existing.error = state === "error" ? existing.description : "";
    existing.updatedAt = stamp;
    existing.seq = ++seq;
    existing.terminal = TERMINAL.has(state);
    existing.stale = false;
    existing.scopes = new Set(scopes);
    publish();
    return existing;
  }

  function runStart(type, options = {}) {
    const family = String(type || "request");
    const id = `local:${family}:${++localSeq}`;
    localRuns.set(id, { type: family, startedAt: now() });
    const label = family === "explore" ? "Starting research" : family === "command" ? "Preparing action" : "Thinking";
    const description = clampText(options.loadingText || "The request is being processed.", 220);
    const scopes = family === "explore" ? ["main", "explore"] : ["main", "task"];
    upsertLocal(id, family === "explore" ? "searching" : family === "command" ? "working" : "thinking", label, description, scopes, { local: true });
    lastPublished.delete("main");
    lastPublished.delete(scopes[1]);
    publish(true);
    return id;
  }

  function finishLocal(id, state, label, description, scopes) {
    if (!id) return;
    localRuns.delete(id);
    const activity = activities.get(id);
    if (!activity) return;
    activity.state = state;
    activity.orb = orbState(state);
    activity.label = clampText(label, 60);
    activity.description = clampText(description, 220);
    activity.status = state;
    activity.error = state === "error" ? activity.description : "";
    activity.updatedAt = now();
    activity.seq = ++seq;
    activity.terminal = TERMINAL.has(state);
    activity.scopes = new Set(scopes);
    if (activity.terminal) scheduleSettle();
    publish(true);
  }

  function runResult(type, options = {}) {
    let latest = null;
    for (const [id, run] of localRuns) {
      if (!type || run.type === type) latest = id; // Map preserves start order
    }
    if (!latest) {
      // A fast run may finish without a matching local id (e.g. a re-entrant
      // action); resolving the newest local activity is still honest.
      for (const [id, activity] of activities) {
        if (activity.local && !activity.terminal && (!type || activity.scopes.has("main"))) latest = id;
      }
    }
    if (!latest) return;
    const waiting = Boolean(options.waiting);
    const family = String(type || "request");
    const state = waiting ? "waiting" : "completed";
    const label = waiting
      ? "Waiting for approval"
      : family === "explore"
        ? "Research complete"
        : family === "command"
          ? "Action complete"
          : family === "chat"
            ? "Completed"
            : "Completed";
    const description = waiting
      ? "The plan is ready; review and approve it to continue."
      : family === "explore"
        ? "The research report is ready."
        : family === "command"
          ? "The approved action finished."
          : family === "chat"
            ? "The answer is ready."
            : "The request finished.";
    const activity = activities.get(latest);
    const scopes = activity ? [...activity.scopes] : ["main"];
    finishLocal(latest, state, label, description, scopes);
  }

  function runError(message) {
    let latest = null;
    for (const [id] of localRuns) {
      latest = id;
    }
    if (!latest) {
      for (const [id, activity] of activities) {
        if (activity.local && !activity.terminal) latest = id;
      }
    }
    if (!latest) return;
    const activity = activities.get(latest);
    const scopes = activity ? [...activity.scopes] : ["main"];
    finishLocal(latest, "error", "Failed", String(message || "The request failed."), scopes);
    if (scopes.includes("explore")) {
      upsertLocal("local:explore:error", "error", "Research failed", String(message || "The research request failed."), ["explore"], { local: true });
    }
  }

  function voiceListening() {
    const id = "voice:local";
    voiceActivityId = id;
    upsertLocal(id, "listening", "Listening", "The microphone is live — speak now.", ["voice", "main"], { local: true, source: "voice", priority: 5 });
    publish(true);
  }

  function voiceProcessing() {
    const id = voiceActivityId || "voice:local";
    voiceActivityId = id;
    upsertLocal(id, "thinking", "Processing speech", "Turning the captured speech into a request.", ["voice", "main"], { local: true, source: "voice", priority: 5 });
  }

  function voiceSpeaking() {
    const id = voiceActivityId || "voice:local";
    voiceActivityId = id;
    upsertLocal(id, "composing", "Speaking", "Reading the response aloud.", ["voice", "main"], { local: true, source: "voice", priority: 5 });
  }

  function voiceError(message) {
    const id = voiceActivityId || "voice:local";
    voiceActivityId = id;
    upsertLocal(id, "error", "Voice unavailable", String(message || "The microphone or speech service reported a failure."), ["voice", "main"], { local: true, source: "voice", priority: 5 });
  }

  function voiceEnd() {
    const id = voiceActivityId || "voice:local";
    const activity = activities.get(id);
    if (activity) {
      activity.state = "idle";
      activity.orb = orbState("idle");
      activity.label = "Voice idle";
      activity.description = "The microphone is off.";
      activity.status = "idle";
      activity.error = "";
      activity.scopes = new Set(["voice"]);
      activity.updatedAt = now();
      activity.terminal = true;
      activity.seq = ++seq;
    }
    voiceActivityId = null;
    lastPublished.delete("main");
    publish(true);
  }

  function setTasks(tasks) {
    taskSnapshot = Array.isArray(tasks)
      ? tasks.filter((task) => task && typeof task === "object").slice(0, 30)
      : [];
    publish(true);
  }

  function setTransport(status, detail = "") {
    const next = status === "connected" ? "connected" : status === "closed" ? "closed" : "reconnecting";
    if (transport === next && transportDetail === detail) return;
    transport = next;
    transportDetail = detail || (next === "closed" ? "The live activity channel closed." : next === "reconnecting" ? "Reconnecting to the live activity channel…" : "");
    publish(true);
  }

  function markStale() {
    const stamp = now();
    let changed = false;
    for (const activity of activities.values()) {
      if (activity.terminal || activity.stale) continue;
      const limit = activity.scopes.has("task") ? TASK_STALE_MS : STALE_MS;
      if (stamp - activity.updatedAt < limit) continue;
      activity.stale = true;
      activity.state = "unknown";
      activity.orb = orbState("unknown");
      activity.label = "Activity status unknown";
      activity.description = "No update arrived for this activity, so it is no longer reported as running.";
      activity.updatedAt = stamp;
      activity.seq = ++seq;
      changed = true;
    }
    if (changed) publish(true);
  }

  function reset() {
    activities.clear();
    localRuns.clear();
    lastPublished.clear();
    seen.clear();
    taskSnapshot = [];
    voiceActivityId = null;
    transport = "connected";
    transportDetail = "";
    if (settleTimer != null) {
      clearTimeout(settleTimer);
      settleTimer = null;
    }
    publish(true);
  }

  const staleTimer = setInterval(markStale, 15000);
  if (staleTimer.unref) staleTimer.unref();

  window.NovaActivity = {
    states: ["idle", "listening", "thinking", "searching", "planning", "connecting", "working", "solving", "recovering", "composing", "waiting", "completed", "cancelled", "error", "unknown"],
    scopes: SCOPES.slice(),
    ingest,
    runStart,
    runResult,
    runError,
    voiceListening,
    voiceProcessing,
    voiceSpeaking,
    voiceError,
    voiceEnd,
    setTasks,
    setTransport,
    getState(scope = "main") {
      const view = viewFor(scope);
      return { ...view, scopes: view.scopes instanceof Set ? [...view.scopes] : view.scopes };
    },
    getScopes() {
      return SCOPES.reduce((acc, scope) => {
        acc[scope] = this.getState(scope);
        return acc;
      }, {});
    },
    subscribe(fn) {
      if (typeof fn !== "function") return () => {};
      subscribers.add(fn);
      SCOPES.forEach((scope) => fn(scope, this.getState(scope)));
      return () => subscribers.delete(fn);
    },
    reset,
    diagnostics() {
      return {
        transport,
        activities: activities.size,
        localRuns: localRuns.size,
        subscribers: subscribers.size,
        tasks: taskSnapshot.length,
      };
    },
  };
})();
