/* NovaForge NF-01 — Forge Mission Control
 *
 * This module owns the NovaForge panel's behavior. It is a CLIENT of the
 * session manager, never a second source of truth:
 *
 *   - every session state, frame, observation and stage comes from a backend
 *     response, and the panel is re-rendered from that response;
 *   - a frame is fetched through an authenticated request (an <img src> cannot
 *     carry the API token), and a stale frame is labelled stale rather than
 *     shown as live;
 *   - the four pipeline stages render the backend's own `available` flags and
 *     reasons, so "Proposed/Executed/Verified" say "not available" instead of
 *     inventing an action;
 *   - Start is disabled until the CURRENT form values have been validated
 *     against an open window; the server validates again on start.
 *
 * Polling is a bounded recursive timeout (one in flight at a time), suspended
 * while the tab is hidden, and stopped as soon as the session is terminal.
 */
(function () {
  "use strict";

  var POLL_MS = 2000;
  var STALE_MS = 5000;
  var TERMINAL = ["STOPPED", "COMPLETED", "FAILED"];
  var ACTIVE = ["STARTING", "RUNNING", "PAUSING", "PAUSED", "STOPPING"];

  function byId(id) { return document.getElementById(id); }

  var forge = {
    sessionId: "",
    activeId: "",
    state: "",
    targetTitle: "",
    timer: null,
    polling: false,
    busy: false,
    validatedSignature: "",
    validatedTarget: null,
    captureSupported: false,
    objectUrl: "",
    lastFrameKey: "",
    frameToken: 0,
    current: null,
    // Every authoritative session response bumps this token. A poll that was
    // already in flight when a command or another tab changed the session is
    // discarded instead of painting an older state over the newer one.
    stateToken: 0,
    // NF-03 replay state. `token` makes a stale timeline response harmless when
    // the user switches sessions or filters while a fetch is in flight, and
    // `frameToken` does the same for frame image fetches.
    timeline: {
      sessionId: "",
      events: [],
      payload: null,
      nextAfterSeq: 0,
      hasMore: false,
      selectedSeq: 0,
      // A deep link from a Coach finding: the event to open after the session's
      // page has loaded. Zero means "no pending selection".
      pendingSeq: 0,
      token: 0,
      frameToken: 0,
      frameUrl: "",
      busy: false,
      filters: { category: "", source: "", severity: "" },
      vocabulary: { category: {}, source: {}, severity: {} },
    },
    // Remediation R1/R2: the plan currently held by the panel. A proposal is
    // the ONLY thing Run may send, and it is replaced (not merged) whenever the
    // session, action inputs or verification change, so a stale digest cannot
    // be replayed against a different action.
    gameplay: {
      proposal: null,
      busy: false,
    },
    // NF-04 state. `runToken` makes a stale poll harmless when a new run starts;
    // `signature` is the EXACT configuration the current preflight validated.
    benchmark: {
      scenarios: [],
      metricCatalog: [],
      selected: null,
      scenarioId: "",
      preflight: null,
      signature: "",
      runId: "",
      run: null,
      runToken: 0,
      timer: null,
      detail: null,
      history: [],
      pendingEvidence: "",
    },
    // NF-05 state. An evaluation is a compatibility record around ONE benchmark
    // run; `signature` is the exact configuration readiness validated, and
    // `confirmed` is the operator's explicit acknowledgement.
    evaluation: {
      games: [],
      selected: null,
      detail: null,
      scenarios: [],
      preflight: null,
      signature: "",
      evaluationId: "",
      run: null,
      runToken: 0,
      timer: null,
      history: [],
      report: null,
    },
    // NF-06 state. `analysis` is the LAST response verbatim — the panel never
    // re-derives a finding, a confidence or a verdict; `experimentId` is the
    // proposal under review, and the poll is one bounded recursive timeout.
    coach: {
      status: null,
      analysis: null,
      evaluationId: "",
      sessionId: "",
      experimentId: "",
      experiment: null,
      history: [],
      timer: null,
      busy: false,
    },
  };

  /* ── Small presentation helpers ────────────────────────────── */

  function notice(message, kind) {
    var box = byId("forgeNotice");
    if (!box) return;
    if (!message) {
      box.hidden = true;
      box.textContent = "";
      box.className = "forge-notice";
      return;
    }
    box.hidden = false;
    box.className = "forge-notice" + (kind === "error" ? " error" : "");
    box.textContent = message;
  }

  function liveError(message) {
    var box = byId("forgeLiveError");
    if (!box) return;
    if (!message) {
      box.hidden = true;
      box.textContent = "";
      return;
    }
    box.hidden = false;
    box.textContent = message;
  }

  function formatTime(iso) {
    if (!iso) return "—";
    var date = new Date(iso);
    if (isNaN(date.getTime())) return String(iso);
    return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }

  function formatDate(iso) {
    if (!iso) return "—";
    var date = new Date(iso);
    if (isNaN(date.getTime())) return String(iso);
    return date.toLocaleString([], {
      month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit",
    });
  }

  function formatDuration(seconds) {
    // `Number(null)` is 0, which would print a missing duration as a measured
    // "0.0 s". A figure that was not recorded is not a zero.
    if (seconds === null || seconds === undefined || seconds === "") return "—";
    var value = Number(seconds);
    if (!isFinite(value) || value < 0) return "—";
    if (value < 60) return value.toFixed(1) + " s";
    var minutes = Math.floor(value / 60);
    var rest = Math.round(value % 60);
    return minutes + " m " + (rest < 10 ? "0" : "") + rest + " s";
  }

  function formatAge(ms) {
    var value = Number(ms);
    if (!isFinite(value)) return "unknown";
    if (value < 1000) return Math.round(value) + " ms ago";
    return (value / 1000).toFixed(1) + " s ago";
  }

  function sourceLabel(source) {
    if (source === "window_crop") return "window crop";
    if (source === "screen") return "full screen";
    return source || "—";
  }

  function setText(id, text) {
    var node = byId(id);
    if (node) node.textContent = text;
  }

  // A measured value and a missing value are different facts. `0` is a real
  // measurement; null/undefined/NaN are not, and the panel says so rather than
  // printing a zero the backend never measured.
  function metricText(value, unit) {
    if (value === null || value === undefined) return "unavailable";
    if (typeof value === "number" && !isFinite(value)) return "unavailable";
    if (typeof value === "string" && value.trim() === "") return "unavailable";
    return unit ? String(value) + " " + unit : String(value);
  }

  function metricRow(label, value, reason, available) {
    var row = el("div", "forge-metric");
    row.dataset.available = available === false ? "false" : "true";
    row.appendChild(el("span", "forge-metric-label", label));
    row.appendChild(el("strong", "forge-metric-value", value));
    if (reason) row.appendChild(el("span", "forge-metric-reason", reason));
    return row;
  }

  function emptyMetricGrid(container, text) {
    container.innerHTML = "";
    container.appendChild(el("p", "muted", text));
  }

  /* ── Mission form ──────────────────────────────────────────── */

  function missionPayload() {
    var timeLimit = byId("forgeTimeLimit").value.trim();
    var maxActions = byId("forgeMaxActions").value.trim();
    return {
      name: byId("forgeMissionName").value.trim(),
      objective: byId("forgeMissionObjective").value.trim(),
      target_title: byId("forgeMissionTarget").value,
      success_criteria: byId("forgeMissionCriteria").value.trim(),
      time_limit_seconds: timeLimit === "" ? null : Number(timeLimit),
      max_actions: maxActions === "" ? null : Number(maxActions),
    };
  }

  function signature() { return JSON.stringify(missionPayload()); }

  function invalidateValidation() {
    forge.validatedSignature = "";
    forge.validatedTarget = null;
    // Keep the session controls following the last authoritative record; only
    // Start depends on the form that was just edited.
    updateControls(forge.current);
  }

  function startPayload() {
    var payload = missionPayload();
    // Pin the window identity that was measured. The server refuses a start when
    // a different window now carries the validated title.
    if (forge.validatedTarget) {
      payload.expected_handle = forge.validatedTarget.handle;
      payload.expected_pid = forge.validatedTarget.pid;
    }
    return payload;
  }

  function fillMission(mission) {
    if (!mission) return;
    byId("forgeMissionName").value = mission.name || "";
    byId("forgeMissionObjective").value = mission.objective || "";
    byId("forgeMissionCriteria").value = mission.success_criteria || "";
    byId("forgeTimeLimit").value = mission.time_limit_seconds == null ? "" : String(mission.time_limit_seconds);
    byId("forgeMaxActions").value = mission.max_actions == null ? "" : String(mission.max_actions);
    if (mission.target_title) rememberTarget(mission.target_title);
  }

  function rememberTarget(title) {
    var select = byId("forgeMissionTarget");
    if (!title) return;
    var found = Array.prototype.some.call(select.options, function (option) { return option.value === title; });
    if (!found) {
      var option = document.createElement("option");
      option.value = title;
      option.textContent = title + " (not open)";
      select.appendChild(option);
    }
    select.value = title;
  }

  function renderFieldErrors(errors) {
    var box = byId("forgeMissionErrors");
    box.innerHTML = "";
    if (!errors || !errors.length) return;
    var list = el("ul", "");
    errors.forEach(function (problem) { list.appendChild(el("li", "", problem)); });
    box.appendChild(list);
  }

  function renderValidation(result) {
    var box = byId("forgeValidation");
    box.innerHTML = "";
    box.className = "forge-validation " + (result.valid ? "ok" : "bad");
    box.appendChild(el("p", "", result.valid
      ? "Mission validated against the live window. Start is now available."
      : "Mission is not runnable yet:"));
    if (!result.valid) {
      var problems = el("ul", "");
      (result.errors || []).forEach(function (problem) { problems.appendChild(el("li", "", problem)); });
      box.appendChild(problems);
      renderFieldErrors(result.errors || []);
      return;
    }
    renderFieldErrors([]);
    if (result.target) {
      var dl = el("dl", "");
      [
        ["Window", result.target.title],
        ["Measured", result.target.width + " × " + result.target.height + " px"],
        ["Handle", String(result.target.handle)],
        ["Process", String(result.target.pid)],
      ].forEach(function (pair) {
        var group = el("div", "");
        group.appendChild(el("dt", "", pair[0]));
        group.appendChild(el("dd", "", pair[1]));
        dl.appendChild(group);
      });
      box.appendChild(dl);
    }
  }

  /* ── Readiness / status ────────────────────────────────────── */

  function renderReadiness(status) {
    var body = byId("forgeReadiness");
    body.innerHTML = "";
    forge.captureSupported = Boolean(status && status.capture_supported);

    var row = el("div", "forge-ready-row");
    row.appendChild(el("span", "forge-badge", status && status.capture_supported ? "capture supported" : "capture unavailable"));
    row.appendChild(el("span", "muted", status && status.active_session
      ? "Active session: " + status.active_session
      : "No active session"));
    body.appendChild(row);

    var list = el("ul", "");
    ((status && status.limitations) || []).forEach(function (line) {
      list.appendChild(el("li", "", line));
    });
    body.appendChild(list);

    var ready = byId("forgeReady");
    ready.textContent = status && status.capture_supported ? "ready" : "unavailable";
    ready.dataset.ok = status && status.capture_supported ? "true" : "false";

    if (status) {
      forge.activeId = status.active_session || "";
      setText("forgeControlNote", status.active_session
        ? "A session is active. Stop it before starting another."
        : forge.validatedSignature
          ? "Mission validated — Start is available."
          : "Validate the mission against an open window to enable Start.");
    }
    updateControls(null);
  }

  /* ── Session rendering ─────────────────────────────────────── */

  function setState(state) {
    forge.state = state || "";
    var badge = byId("forgeState");
    badge.textContent = state || "IDLE";
    badge.dataset.state = state || "IDLE";
    setText("forgeTarget", forge.targetTitle || "no session target");
  }

  /* ── Pure decisions (exported for behavioral tests) ─────────
   *
   * These answer the three questions the panel must never get wrong, and they
   * are pure so a test can pin them without a browser: is this frame stale, is
   * this stage really available, and may this control be pressed. The render
   * functions below are thin wrappers over them.
   */

  function frameFreshness(frame, now) {
    if (!frame) return { stale: true, reason: "no frame" };
    var captured = Date.parse(frame.captured_at || "");
    var at = now == null ? Date.now() : now;
    var age = isNaN(captured) ? null : at - captured;
    // A backend that already said stale wins; otherwise a frame older than the
    // freshness window is stale, and an unreadable timestamp is never "live".
    var stale = Boolean(frame.stale) || age === null || age > STALE_MS;
    return { stale: stale, age_ms: age };
  }

  // A frame fetch is asynchronous and polling can overlap it, so the newest
  // REQUEST wins: an older response must never paint over a newer frame and be
  // labelled "live". The rule is a pure comparison so it can be tested.
  function frameStillCurrent(started, latest) { return started === latest; }

  // The same newest-wins rule for whole session records: a poll that started
  // before a command (or another tab) rendered a newer record must not paint
  // the older one over it.
  function responseStillCurrent(started, latest) { return started === latest; }

  // The save notice is decided from the server's measurement, not assumed:
  // a store that refused the write reports persisted=false, and claiming
  // "saved" would lose a draft the user believes is stored.
  function saveOutcome(result) {
    if (result && result.persisted === false) {
      return {
        message: "The mission draft was NOT saved: the local store refused the write. It will be lost on restart.",
        kind: "error",
      };
    }
    if (result && result.valid) {
      return { message: "Mission draft saved.", kind: "" };
    }
    return { message: "Draft saved with validation problems — fix them before starting.", kind: "error" };
  }

  function visionEmptyText(state) {
    if (state === "RUNNING") {
      return {
        title: "No fresh frame yet.",
        detail: "The session is running; the first frame has not been recorded.",
      };
    }
    if (state === "PAUSED") {
      return {
        title: "Session paused — capture is suspended.",
        detail: "Resume the session to capture new frames.",
      };
    }
    return {
      title: "No session is active.",
      detail: "Configure a mission and start a session to see genuine captured frames from the target window.",
    };
  }

  function stageText(stage) {
    if (!stage) return { available: false, text: "No record." };
    if (!stage.available) {
      return { available: false, text: stage.reason || "Not available." };
    }
    var latest = (stage.records && stage.records[stage.records.length - 1]) || null;
    if (stage.stage === "observed" && latest) {
      return {
        available: true,
        text: latest.summary + " · " + formatTime(latest.at) +
          " · source " + latest.source +
          (latest.evidence ? " · evidence recorded" : " · NO evidence"),
      };
    }
    return {
      available: true,
      text: (stage.summary || "Recorded.") + (stage.at ? " · " + formatTime(stage.at) : ""),
    };
  }

  function controlAvailability(data, context) {
    var ctx = context || {};
    var can = (data && data.can) || {};
    var busy = Boolean(ctx.busy);
    var active = Boolean(ctx.activeId);
    var validated = Boolean(ctx.validated) && ctx.validated === ctx.signature;
    return {
      start: !busy && !active && Boolean(ctx.captureSupported) && validated,
      pause: !busy && active && Boolean(can.pause),
      resume: !busy && active && Boolean(can.resume),
      stop: !busy && active && Boolean(can.stop),
      cancel: !busy && active && Boolean(can.cancel),
      emergencyStop: !busy && Boolean(can.emergency_stop),
    };
  }

  function controlContext() {
    return {
      busy: forge.busy,
      activeId: forge.activeId,
      captureSupported: forge.captureSupported,
      validated: forge.validatedSignature,
      signature: signature(),
    };
  }

  function updateControls(data) {
    var available = controlAvailability(data, controlContext());
    byId("forgeStartButton").disabled = !available.start;
    byId("forgePauseButton").disabled = !available.pause;
    byId("forgeResumeButton").disabled = !available.resume;
    byId("forgeStopButton").disabled = !available.stop;
    byId("forgeCancelButton").disabled = !available.cancel;
    byId("forgeEmergencyStopButton").disabled = !available.emergencyStop;
  }

  /* ── Telemetry and Guardian rendering ──────────────────────── */

  function renderTelemetry(telemetry) {
    var captureBox = byId("forgeTelemetryCapture");
    var sessionBox = byId("forgeTelemetrySession");
    var targetBox = byId("forgeTelemetryTarget");
    var resourceBox = byId("forgeTelemetryResources");
    var notesBox = byId("forgeTelemetryNotes");
    if (!captureBox || !sessionBox || !targetBox || !resourceBox || !notesBox) return;

    if (!telemetry) {
      emptyMetricGrid(captureBox, "No telemetry yet — start a session to collect measurements.");
      emptyMetricGrid(sessionBox, "No session record yet.");
      emptyMetricGrid(targetBox, "No target checks recorded.");
      emptyMetricGrid(resourceBox, "No resource readings recorded.");
      notesBox.innerHTML = "";
      notesBox.appendChild(el("li", "muted", "Start a session to collect telemetry."));
      setText("forgeTelemetryUpdated", "no session");
      setText("forgeTelemetryState", "—");
      setText("forgeTelemetryLatencyNote", "");
      return;
    }

    var session = telemetry.session || {};
    var capture = telemetry.capture || {};
    var latency = capture.latency_ms || {};
    var loop = telemetry.loop || {};
    var target = telemetry.target || {};
    var actions = telemetry.actions || {};
    var resources = telemetry.resources || {};

    setText("forgeTelemetryUpdated", "updated " + formatTime(telemetry.generated_at));
    setText("forgeTelemetryState", session.state || "—");

    captureBox.innerHTML = "";
    captureBox.appendChild(metricRow("Frames captured", metricText(capture.frames)));
    captureBox.appendChild(metricRow("Stale captures", metricText(capture.stale_frames)));
    captureBox.appendChild(metricRow("Capture failures", metricText(capture.capture_failures)));
    captureBox.appendChild(metricRow("Frame gaps", metricText(capture.frame_gaps),
      "Gap threshold: " + metricText(capture.gap_threshold_ms, "ms") + ".", capture.frame_gaps === null ? false : true));
    captureBox.appendChild(metricRow("Largest gap", metricText(capture.max_gap_ms, "ms")));
    captureBox.appendChild(metricRow("Last frame age", metricText(capture.last_frame_age_ms, "ms")));
    captureBox.appendChild(metricRow("Latency samples", metricText(latency.samples),
      String(latency.basis || ""), latency.samples === null ? false : true));
    captureBox.appendChild(metricRow("Latency min", metricText(latency.min_ms, "ms")));
    captureBox.appendChild(metricRow("Latency median", metricText(latency.median_ms, "ms")));
    captureBox.appendChild(metricRow("Latency p95", metricText(latency.p95_ms, "ms"),
      String(latency.p95_reason || ""), latency.p95_available === false ? false : true));
    captureBox.appendChild(metricRow("Latency max", metricText(latency.max_ms, "ms")));
    captureBox.appendChild(metricRow("Latency mean", metricText(latency.mean_ms, "ms")));
    setText("forgeTelemetryLatencyNote",
      (latency.p95_available === false ? latency.p95_reason + " " : "") +
      (latency.invalid_samples ? latency.invalid_samples + " frame(s) reported no usable duration. " : "") +
      "Percentile basis: " + String(latency.basis || "unavailable") + ".");

    sessionBox.innerHTML = "";
    sessionBox.appendChild(metricRow("State", session.state || "—"));
    sessionBox.appendChild(metricRow("Duration", formatDuration(session.duration_seconds)));
    sessionBox.appendChild(metricRow("Active time", formatDuration(session.active_seconds)));
    sessionBox.appendChild(metricRow("Paused time", formatDuration(session.paused_seconds)));
    sessionBox.appendChild(metricRow("Pause count", metricText(session.pause_count)));
    sessionBox.appendChild(metricRow("State changes", metricText(session.state_changes)));
    sessionBox.appendChild(metricRow("Errors recorded", metricText(session.errors)));
    sessionBox.appendChild(metricRow("End reason", session.end_reason || (session.state === "RUNNING" || session.state === "PAUSED" ? "still running" : "—")));
    sessionBox.appendChild(metricRow("Interrupted", session.interrupted ? "yes — durations are last persisted" : "no",
      session.interrupted ? "The process stopped while the session was active; times were not reconstructed." : "", !session.interrupted));

    targetBox.innerHTML = "";
    targetBox.appendChild(metricRow("Target checks", metricText(target.checks)));
    targetBox.appendChild(metricRow("Last check", formatTime(target.last_checked_at)));
    targetBox.appendChild(metricRow("Target losses", metricText(target.losses)));
    targetBox.appendChild(metricRow("Identity changes", metricText(target.changes)));
    targetBox.appendChild(metricRow("Unreadable probes", metricText(target.unreadable)));
    targetBox.appendChild(metricRow("Configured interval", metricText(loop.configured_interval_ms, "ms")));
    targetBox.appendChild(metricRow("Observed frame rate", metricText(loop.observed_frame_rate_hz, "Hz"),
      String(loop.rate_reason || ""), loop.rate_available === false ? false : true));

    resourceBox.innerHTML = "";
    [["cpu", "CPU"], ["memory", "Memory"], ["gpu", "GPU"], ["npu", "NPU"]].forEach(function (pair) {
      var item = resources[pair[0]] || {};
      var available = item.available === true && item.value != null;
      resourceBox.appendChild(metricRow(pair[1], available ? metricText(item.value, item.unit || "") : "unavailable",
        String(item.reason || (available ? "" : "No provider reported a value.")), available));
    });
    var actionsAvailable = actions.available === true;
    resourceBox.appendChild(metricRow("Actions", actionsAvailable ? "measured" : "unsupported",
      String(actions.reason || ""), actionsAvailable));
    var recovery = telemetry.recovery || {};
    var recoveryAvailable = recovery.available === true;
    resourceBox.appendChild(metricRow("Recovery", recoveryAvailable ? "measured" : "unsupported",
      String(recovery.reason || ""), recoveryAvailable));

    notesBox.innerHTML = "";
    var notes = (telemetry.measurement && telemetry.measurement.notes) || [];
    if (!notes.length) notesBox.appendChild(el("li", "muted", "No measurement notes were reported."));
    notes.forEach(function (line) { notesBox.appendChild(el("li", "", String(line))); });
  }

  function guardianLimitState(limit) {
    if (!limit) return "unavailable";
    if (limit.enforced === true) return "enforced";
    if (limit.state === "configured_not_enforceable") return "configured — NOT enforced";
    if (limit.state === "not_configured") return "not configured";
    return "unsupported";
  }

  function renderGuardian(guardian) {
    var badge = byId("forgeGuardianState");
    var reason = byId("forgeGuardianReason");
    var limitsBox = byId("forgeGuardianLimits");
    var targetBox = byId("forgeGuardianTarget");
    var cleanupBox = byId("forgeGuardianCleanup");
    var interventionsBox = byId("forgeGuardianInterventions");
    var guaranteesBox = byId("forgeGuardianGuarantees");
    if (!badge || !limitsBox || !targetBox || !cleanupBox || !interventionsBox || !guaranteesBox) return;

    if (!guardian) {
      badge.textContent = "unarmed";
      badge.dataset.state = "unarmed";
      reason.textContent = "No session is active, so nothing is being guarded.";
      emptyMetricGrid(limitsBox, "No limits to report.");
      emptyMetricGrid(targetBox, "No target validation recorded.");
      emptyMetricGrid(cleanupBox, "No cleanup has run.");
      interventionsBox.innerHTML = "";
      interventionsBox.appendChild(el("li", "muted", "No Guardian intervention has been recorded."));
      guaranteesBox.innerHTML = "";
      guaranteesBox.appendChild(el("li", "muted", "Guarantees are reported for a session."));
      return;
    }

    var target = guardian.target || {};
    var input = guardian.input || {};
    var cleanup = guardian.cleanup || {};
    var limits = guardian.limits || {};
    badge.textContent = String(guardian.safety_state || "unknown").replace(/_/g, " ");
    badge.dataset.state = String(guardian.session_state || "unknown");
    reason.textContent = String(target.reason || input.reason || "The Guardian has no report yet.");

    limitsBox.innerHTML = "";
    limitsBox.appendChild(metricRow("Time limit", guardianLimitState(limits.time_limit),
      limits.time_limit && limits.time_limit.configured
        ? "Configured at " + metricText(limits.time_limit.seconds, "s") + "."
        : "No time limit is configured for this mission.",
      Boolean(limits.time_limit && limits.time_limit.enforced)));
    limitsBox.appendChild(metricRow("Action budget", guardianLimitState(limits.action_budget),
      String((limits.action_budget && limits.action_budget.reason) || ""),
      Boolean(limits.action_budget && limits.action_budget.enforced)));
    var loop = limits.observation_loop || {};
    limitsBox.appendChild(metricRow("Observation loop", loop.bounded ? "bounded" : "unknown",
      "Interval " + metricText(loop.interval_seconds, "s") +
      "; one pending operation; no retries; " + metricText(loop.max_frames_on_disk) + " frames on disk.",
      Boolean(loop.bounded)));

    targetBox.innerHTML = "";
    targetBox.appendChild(metricRow("Target validation", String(target.state || "unknown"),
      String(target.reason || ""), target.valid === true));
    targetBox.appendChild(metricRow("Window", target.title || "—",
      target.handle == null ? "" : "handle " + target.handle + " · pid " + target.pid));
    targetBox.appendChild(metricRow("Input boundary", input.allow ? "allowed" : "blocked",
      String(input.reason || ""), input.allow === true));
    targetBox.appendChild(metricRow("Input path", guardian.input_path_available ? "available" : "unsupported",
      guardian.input_path_available ? "" : "NF-02 sends no gameplay input; the refusal is the boundary.",
      Boolean(guardian.input_path_available)));

    cleanupBox.innerHTML = "";
    cleanupBox.appendChild(metricRow("Cleanup", cleanup.state || "unknown",
      String(cleanup.note || ""), cleanup.state === "ok"));
    cleanupBox.appendChild(metricRow("Held input after cleanup", metricText(cleanup.held_after)));
    cleanupBox.appendChild(metricRow("Releases / presses", metricText(cleanup.releases) + " / " + metricText(cleanup.presses)));

    interventionsBox.innerHTML = "";
    var interventions = guardian.interventions || [];
    if (!interventions.length) {
      interventionsBox.appendChild(el("li", "muted", "No Guardian intervention has been recorded."));
    }
    interventions.slice().reverse().forEach(function (entry) {
      var row = el("li", "forge-intervention");
      row.dataset.outcome = String(entry.outcome || "unknown");
      var main = el("div", "forge-intervention-main");
      main.appendChild(el("span", "forge-intervention-title",
        String(entry.kind || "intervention").replace(/_/g, " ") + " — " + String(entry.outcome || "unknown")));
      if (entry.reason) main.appendChild(el("span", "forge-intervention-detail", String(entry.reason)));
      row.appendChild(main);
      var meta = el("span", "forge-intervention-meta", formatTime(entry.at));
      meta.title = String(entry.detail || "");
      row.appendChild(meta);
      interventionsBox.appendChild(row);
    });

    guaranteesBox.innerHTML = "";
    var guarantees = guardian.guarantees || [];
    if (!guarantees.length) guaranteesBox.appendChild(el("li", "muted", "No guarantees were reported."));
    guarantees.forEach(function (entry) {
      var line = el("li", "",
        String(entry.name || "guarantee").replace(/_/g, " ") + ": " +
        (entry.enforced ? "enforced" : "NOT enforced") + " — " + String(entry.detail || ""));
      line.dataset.enforced = entry.enforced ? "true" : "false";
      guaranteesBox.appendChild(line);
    });
  }

  function renderStages(stages) {
    var byStage = {};
    (stages || []).forEach(function (stage) { byStage[stage.stage] = stage; });
    ["observed", "proposed", "executed", "verified"].forEach(function (name) {
      var target = document.querySelector('[data-stage="' + name + '"]');
      var value = document.querySelector('[data-stage-value="' + name + '"]');
      if (!target || !value) return;
      var view = stageText(byStage[name]);
      target.dataset.available = view.available ? "true" : "false";
      value.textContent = view.text;
    });
  }

  function renderSummary(data) {
    var box = byId("forgeSummary");
    box.innerHTML = "";
    var grid = el("dl", "forge-summary-grid");
    var verification = data.verification || {};
    var cleanup = data.input_cleanup || {};
    var rows = [
      ["Session", data.session_id],
      ["Mission", (data.mission && data.mission.name) || "—"],
      ["State", data.state],
      ["Objective", (data.mission && data.mission.objective) || "—"],
      ["Started", data.started_at ? formatDate(data.started_at) : "—"],
      ["Ended", data.ended_at ? formatDate(data.ended_at) : "—"],
      ["Duration", formatDuration(data.duration_seconds)],
      ["End reason", data.end_reason || (data.active ? "still running" : "—")],
      ["Actions", (data.counts ? data.counts.actions_attempted : 0) + " attempted / " +
        (data.counts ? data.counts.actions_executed : 0) + " executed"],
      ["Verification", verification.outcome
        ? verification.outcome.replace(/_/g, " ") + (verification.achieved === true
          ? " (objective met)" : verification.achieved === false ? " (objective not met)" : "")
        : (verification.supported ? "unverified" : "no verifier")],
      ["Input cleanup", cleanup.ok === undefined ? "not run yet" : (cleanup.ok ? "no held input" : "FAILED")],
    ];
    rows.forEach(function (pair) {
      var group = el("div", "");
      group.appendChild(el("dt", "", pair[0]));
      group.appendChild(el("dd", "", String(pair[1] == null ? "—" : pair[1])));
      grid.appendChild(group);
    });
    box.appendChild(grid);

    if (verification.reason) {
      var verify = el("div", "forge-summary-section");
      verify.appendChild(el("h4", "", "Verification limit"));
      verify.appendChild(el("p", "muted", verification.reason));
      box.appendChild(verify);
    }

    if (data.errors && data.errors.length) {
      var errors = el("div", "forge-summary-section");
      errors.appendChild(el("h4", "", "Errors (" + data.errors.length + ")"));
      var errorList = el("ul", "");
      data.errors.slice(-5).forEach(function (entry) {
        errorList.appendChild(el("li", "", entry.where + ": " + entry.message));
      });
      errors.appendChild(errorList);
      box.appendChild(errors);
    }

    if (cleanup && cleanup.note) {
      var clean = el("div", "forge-summary-section");
      clean.appendChild(el("h4", "", "Input cleanup"));
      clean.appendChild(el("p", "muted", cleanup.note));
      box.appendChild(clean);
    }

    if (data.limitations && data.limitations.length) {
      var limits = el("div", "forge-summary-section");
      limits.appendChild(el("h4", "", "Known limitations"));
      var limitList = el("ul", "");
      data.limitations.forEach(function (line) { limitList.appendChild(el("li", "", line)); });
      limits.appendChild(limitList);
      box.appendChild(limits);
    }
  }

  function setVisionEmpty(message, detail) {
    var empty = byId("forgeVisionEmpty");
    var paragraphs = empty.querySelectorAll("p");
    if (paragraphs[0]) paragraphs[0].textContent = message;
    if (paragraphs[1]) paragraphs[1].textContent = detail || "";
    empty.hidden = false;
  }

  async function loadFrame(frame, state) {
    var image = byId("forgeFrame");
    var ended = TERMINAL.indexOf(state) >= 0;
    // Every call supersedes any fetch still in flight, including the branches
    // that paint no frame at all.
    var token = ++forge.frameToken;
    // A terminal session has no live feed. The last frame is a RECORD, and
    // leaving it in the viewport labelled "live" is exactly the stale-frame
    // confusion this panel exists to prevent.
    if (ended) {
      image.hidden = true;
      image.removeAttribute("src");
      if (forge.objectUrl) {
        URL.revokeObjectURL(forge.objectUrl);
        forge.objectUrl = "";
      }
      forge.lastFrameKey = "";
      byId("forgeFrameStale").hidden = true;
      byId("forgeFrameMode").textContent = "session ended";
      setVisionEmpty(
        "Session " + state + " — no live feed.",
        frame
          ? "Last frame: seq " + frame.seq + " captured " + formatTime(frame.captured_at) + "."
          : "This session recorded no frames."
      );
      return;
    }
    if (!frame) {
      image.hidden = true;
      image.removeAttribute("src");
      byId("forgeFrameStale").hidden = true;
      byId("forgeFrameMode").textContent = state === "RUNNING" ? "waiting for frame" : "no frame";
      var empty = visionEmptyText(state);
      setVisionEmpty(empty.title, empty.detail);
      return;
    }
    var key = forge.sessionId + ":" + frame.seq;
    var stale = frameFreshness(frame).stale;
    byId("forgeFrameStale").hidden = !stale;
    byId("forgeFrameMode").textContent = stale ? "stale" : "live";

    if (key !== forge.lastFrameKey) {
      try {
        var response = await fetch(frame.url, { headers: authHeaders() });
        if (!response.ok) throw new Error("HTTP " + response.status);
        var blob = await response.blob();
        if (!frameStillCurrent(token, forge.frameToken)) return;
        var nextUrl = URL.createObjectURL(blob);
        if (forge.objectUrl) URL.revokeObjectURL(forge.objectUrl);
        forge.objectUrl = nextUrl;
        forge.lastFrameKey = key;
        image.src = nextUrl;
        image.hidden = false;
        byId("forgeVisionEmpty").hidden = true;
        liveError("");
      } catch (error) {
        if (!frameStillCurrent(token, forge.frameToken)) return;
        // Do not leave the previous frame on screen looking current: hide it.
        image.hidden = true;
        setVisionEmpty("The captured frame could not be loaded.",
          "The session may still be running; the next frame will be tried.");
        liveError("Frame " + frame.seq + " could not be loaded: " + String(error.message || error));
      }
    }
  }

  /* ── Gameplay actions and objective verification (R1/R2) ──────── */

  function actionControlsAvailable(data) {
    return !!(data && ACTIVE.indexOf(data.state) >= 0);
  }

  function objectivePayload() {
    var kind = byId("forgeObjectiveKind").value;
    var payload = { kind: kind };
    if (kind === "window_title") payload.parameter = byId("forgeObjectiveParameter").value.trim();
    else payload.description = "Recorded by the panel as an objective this build cannot check.";
    return payload;
  }

  function actionPayload() {
    var kind = byId("forgeActionKind").value;
    var keyField = (byId("forgeActionKey").value || "").trim();
    var point = (byId("forgeActionPoint").value || "").split(",");
    var payload = { kind: kind };
    if (kind === "key" || kind === "hold") {
      var parts = keyField.split(/\s+/);
      payload.key = parts[0] || "";
      if (kind === "hold") {
        var ms = parseInt(parts[1] || "120", 10);
        payload.duration_ms = isNaN(ms) ? 120 : ms;
      }
    }
    if (kind === "click" || kind === "move") {
      var x = parseInt((point[0] || "").trim(), 10);
      var y = parseInt((point[1] || "").trim(), 10);
      if (!isNaN(x)) payload.x = x;
      if (!isNaN(y)) payload.y = y;
    }
    return payload;
  }

  function verificationBadge(verification) {
    var outcome = String((verification && verification.outcome) || "");
    if (!outcome) return "unverified";
    if (outcome === "verified_success") return "verified success";
    if (outcome === "verified_failure") return "verified failure";
    return outcome.replace(/_/g, " ");
  }

  function renderGameplay(data) {
    var badge = byId("forgeVerificationBadge");
    var reason = byId("forgeVerificationReason");
    var log = byId("forgeActionLog");
    if (!badge || !reason || !log) return;
    var verification = (data && data.verification) || {};
    var actions = (data && data.actions) || {};
    badge.textContent = verificationBadge(verification);
    badge.dataset.outcome = String(verification.outcome || "");
    var text = verification.reason || "No verdict yet.";
    if (verification.achieved === true) text = "Objective MET: " + text;
    else if (verification.achieved === false) text = "Objective NOT met: " + text;
    else if (verification.decided === false) text = "Not decided: " + text;
    reason.textContent = text;

    var available = actionControlsAvailable(data);
    var saveObjective = byId("forgeObjectiveSaveButton");
    var verifyButton = byId("forgeVerifyButton");
    var planButton = byId("forgeActionPlanButton");
    var runButton = byId("forgeActionRunButton");
    if (saveObjective) saveObjective.disabled = !available;
    if (verifyButton) verifyButton.disabled = !data;
    if (planButton) planButton.disabled = !available || !actions.available;
    if (runButton) {
      runButton.disabled = !(available && forge.gameplay.proposal && actions.available);
    }
    var objective = data && data.objective;
    var parameter = byId("forgeObjectiveParameter");
    if (objective && parameter && !parameter.value) {
      byId("forgeObjectiveKind").value = objective.kind || "window_title";
      parameter.value = objective.parameter || "";
    }

    log.innerHTML = "";
    var records = (actions.records || []).slice(-5).reverse();
    if (forge.gameplay.proposal) {
      var planned = el("li", "forge-action-planned");
      planned.appendChild(el("strong", "", "Planned: " + (forge.gameplay.proposal.action.kind || "")));
      planned.appendChild(el("span", "muted",
        " " + (forge.gameplay.proposal.action.key || (forge.gameplay.proposal.action.x +
          "," + forge.gameplay.proposal.action.y)) + " · digest " +
        String(forge.gameplay.proposal.digest || "").slice(0, 12)));
      log.appendChild(planned);
    }
    if (!records.length && !forge.gameplay.proposal) {
      log.appendChild(el("li", "muted",
        actions.available
          ? "No action has been planned for this session."
          : "No gameplay input path is wired: " + (actions.reason || "actions are unavailable") ));
    }
    records.forEach(function (row) {
      var item = el("li", "forge-action-row");
      item.dataset.status = String(row.status || "");
      item.appendChild(el("strong", "", String(row.status || "")));
      item.appendChild(el("span", "muted", " " + String(row.reason || "")));
      log.appendChild(item);
    });
  }

  async function postGameplay(path, payload, what) {
    if (forge.gameplay.busy) return null;
    forge.gameplay.busy = true;
    notice("", "");
    try {
      // ``requestJson(path, body)`` posts when the body is truthy and GETs when
      // it is not; an empty object is the POST-with-no-fields form "/verify" wants.
      return await requestJson(path, payload === null ? {} : payload);
    } catch (error) {
      notice(what + " failed: " + String(error.message || error), "error");
      return null;
    } finally {
      forge.gameplay.busy = false;
    }
  }

  async function setObjectiveFromPanel() {
    if (!forge.sessionId) return;
    var answer = await postGameplay(
      "/novaforge/sessions/" + encodeURIComponent(forge.sessionId) + "/objective",
      objectivePayload(), "Set objective");
    if (!answer) return;
    notice(answer.supported
      ? "Objective recorded; a verifier will read the target window's title."
      : "Objective recorded as UNSUPPORTED: " + (answer.reason || ""),
      answer.supported ? "" : "error");
    await refreshSession();
  }

  async function verifyFromPanel() {
    if (!forge.sessionId) return;
    var answer = await postGameplay(
      "/novaforge/sessions/" + encodeURIComponent(forge.sessionId) + "/verify",
      null, "Verification");
    if (!answer) return;
    notice("Verification: " + verificationBadge(answer) + " — " + (answer.reason || ""),
      answer.outcome === "verified_success" ? "" : "error");
    await refreshSession();
  }

  async function planActionFromPanel() {
    if (!forge.sessionId) return;
    var answer = await postGameplay(
      "/novaforge/sessions/" + encodeURIComponent(forge.sessionId) + "/actions/plan",
      actionPayload(), "Plan action");
    if (!answer) return;
    if (answer.status === "proposed") {
      forge.gameplay.proposal = answer.proposal;
      notice("Planned (nothing was sent). Run it to execute this exact proposal.", "");
    } else {
      forge.gameplay.proposal = null;
      var outcome = answer.outcome || {};
      notice("Action refused: " + (outcome.reason || "the Guardian refused it"), "error");
    }
    renderGameplay(forge.current);
  }

  async function runActionFromPanel() {
    var proposal = forge.gameplay.proposal;
    if (!forge.sessionId || !proposal) return;
    var answer = await postGameplay(
      "/novaforge/sessions/" + encodeURIComponent(forge.sessionId) + "/actions/execute",
      { proposal_id: proposal.proposal_id, digest: proposal.digest }, "Run action");
    if (!answer) return;
    forge.gameplay.proposal = null;
    var outcome = answer.outcome || {};
    notice(outcome.status === "executed"
      ? "Executed: " + (answer.verification
        ? verificationBadge(answer.verification) + " — " + (answer.verification.reason || "")
        : "the objective is not configured, so no verdict was produced.")
      : "Run refused (" + String(outcome.status || "denied") + "): " + (outcome.reason || ""),
      outcome.status === "executed" ? "" : "error");
    await refreshSession();
  }

  function renderSession(data) {
    if (!data) return;
    var previousSession = forge.sessionId;
    forge.current = data;
    forge.stateToken += 1;
    forge.sessionId = data.session_id;
    if (previousSession !== data.session_id) forge.lastFrameKey = "";
    forge.targetTitle = (data.target && data.target.title) || "";
    setState(data.state);
    setText("forgeSessionId", data.session_id);
    setText("forgeFrameCount", String((data.counts && data.counts.frames) || 0));
    setText("forgeCaptureMs", data.frame && data.frame.capture_ms != null
      ? Math.round(data.frame.capture_ms) + " ms" : "—");
    setText("forgeDuration", formatDuration(data.duration_seconds));
    setText("forgeActionCount", ((data.counts && data.counts.actions_attempted) || 0) +
      " attempted / " + ((data.counts && data.counts.actions_executed) || 0) + " executed");

    loadFrame(data.frame, data.state);

    if (data.frame) {
      setText("forgeFrameTime", formatTime(data.frame.captured_at));
      setText("forgeFrameAge", formatAge(data.frame.age_ms));
      setText("forgeFrameRes", data.frame.width + " × " + data.frame.height + " px");
      setText("forgeFrameSource", sourceLabel(data.frame.source));
      setText("forgeFrameNote", data.frame.note || (data.frame.capture_mode
        ? "capture mode: " + data.frame.capture_mode : ""));
    } else {
      setText("forgeFrameTime", "—");
      setText("forgeFrameAge", "—");
      setText("forgeFrameRes", "—");
      setText("forgeFrameSource", "—");
      setText("forgeFrameNote", data.state === "RUNNING"
        ? "No fresh frame has been recorded yet."
        : "This session has no captured frame.");
    }

    renderTelemetry(data.telemetry);
    renderGuardian(data.guardian);
    if (previousSession !== data.session_id) forge.gameplay.proposal = null;
    renderGameplay(data);
    renderStages(data.stages);
    var observed = (data.stages || []).filter(function (stage) { return stage.stage === "observed"; })[0];
    byId("forgeIntelStatus").textContent = observed && observed.available
      ? ((data.counts && data.counts.observations) || 0) + " observation(s)"
      : "no evidence";
    renderSummary(data);

    if (ACTIVE.indexOf(data.state) >= 0) {
      forge.activeId = data.session_id;
    } else if (forge.activeId === data.session_id) {
      forge.activeId = "";
    }
    updateControls(data);
    if (forge.activeId && forge.activeId === forge.sessionId) schedulePoll();
    else stopPoll();
  }

  /* ── Polling ───────────────────────────────────────────────── */

  function stopPoll() {
    if (forge.timer) clearTimeout(forge.timer);
    forge.timer = null;
  }

  function schedulePoll() {
    stopPoll();
    if (!forge.sessionId) return;
    forge.timer = setTimeout(refreshSession, POLL_MS);
  }

  async function refreshSession() {
    if (forge.polling || !forge.sessionId) return;
    if (document.hidden) { schedulePoll(); return; }
    forge.polling = true;
    var token = forge.stateToken;
    try {
      var data = await requestJson("/novaforge/sessions/" + encodeURIComponent(forge.sessionId));
      // A command or another tab may have rendered a newer record while this
      // request was in flight; an older response must not overwrite it.
      if (!responseStillCurrent(token, forge.stateToken)) return;
      renderSession(data);
    } catch (error) {
      liveError("Session update failed: " + String(error.message || error) +
        ". The panel is showing the last record it received, not a live claim.");
      schedulePoll();
    } finally {
      forge.polling = false;
    }
  }

  /* ── Actions ───────────────────────────────────────────────── */

  async function withBusy(buttonId, task) {
    if (forge.busy) return null;
    forge.busy = true;
    if (buttonId) byId(buttonId).classList.add("loading");
    updateControls(null);
    try {
      return await task();
    } finally {
      forge.busy = false;
      if (buttonId) byId(buttonId).classList.remove("loading");
      updateControls(forge.current);
    }
  }

  async function loadStatus() {
    var token = forge.stateToken;
    try {
      var body = await requestJson("/novaforge/status");
      // A command or another tab may have rendered a newer session record while
      // this status read was in flight; painting the older one (or its "no
      // active session") over it would undo a confirmed stop.
      if (!responseStillCurrent(token, forge.stateToken)) return;
      renderReadiness(body.status);
      // The status response is authoritative about WHICH session is active;
      // render it when there is one so another tab's start is picked up.
      if (body.active) renderSession(body.active);
      updateControls(body.active);
    } catch (error) {
      if (!responseStillCurrent(token, forge.stateToken)) return;
      var ready = byId("forgeReady");
      ready.textContent = "unavailable";
      ready.dataset.ok = "false";
      var bodyNode = byId("forgeReadiness");
      bodyNode.innerHTML = "";
      bodyNode.appendChild(el("p", "muted", "NovaForge status could not be read: " + String(error.message || error)));
      updateControls(null);
    }
  }

  async function loadWindows() {
    var select = byId("forgeMissionTarget");
    var previous = select.value;
    try {
      var body = await requestJson("/novaforge/windows");
      select.innerHTML = "";
      if (!body.available) {
        var unavailable = document.createElement("option");
        unavailable.value = "";
        unavailable.textContent = "Window list unavailable — " + (body.reason || "unknown reason");
        select.appendChild(unavailable);
        return;
      }
      if (!body.windows.length) {
        var empty = document.createElement("option");
        empty.value = "";
        empty.textContent = "No open windows — open the target first";
        select.appendChild(empty);
        return;
      }
      body.windows.forEach(function (row) {
        var option = document.createElement("option");
        option.value = row.title;
        option.textContent = row.title + " · " + row.process;
        select.appendChild(option);
      });
      if (previous) rememberTarget(previous);
      invalidateValidation();
    } catch (error) {
      select.innerHTML = "";
      var failed = document.createElement("option");
      failed.value = "";
      failed.textContent = "Could not read windows: " + String(error.message || error);
      select.appendChild(failed);
    }
  }

  async function loadMission() {
    try {
      var mission = await requestJson("/novaforge/mission");
      fillMission(mission);
    } catch (_) { /* an unreadable draft is not an error worth blocking on */ }
  }

  async function saveMission() {
    return withBusy("forgeSaveMissionButton", async function () {
      try {
        var result = await requestJson("/novaforge/mission", missionPayload());
        renderFieldErrors(result.valid ? [] : result.errors || []);
        var outcome = saveOutcome(result);
        notice(outcome.message, outcome.kind);
        if (result.valid) invalidateValidation();
      } catch (error) {
        renderFieldErrors([String(error.message || error)]);
        notice("The mission could not be saved: " + String(error.message || error), "error");
      }
    });
  }

  async function validateMission() {
    return withBusy("forgeValidateButton", async function () {
      try {
        var result = await requestJson("/novaforge/validate", missionPayload());
        renderValidation(result);
        forge.validatedSignature = result.valid ? signature() : "";
        forge.validatedTarget = result.valid && result.target
          ? { handle: result.target.handle, pid: result.target.pid, title: result.target.title }
          : null;
        notice(result.valid
          ? "Target validated: " + (result.target ? result.target.title : "window") +
            (result.target ? " (handle " + result.target.handle + ")." : ".")
          : "The mission is not runnable yet.", result.valid ? "" : "error");
        updateControls(null);
      } catch (error) {
        notice("Validation failed: " + String(error.message || error), "error");
      }
    });
  }

  async function startSession() {
    if (forge.validatedSignature !== signature()) {
      notice("Validate the mission first — Start is only enabled for a validated target.", "error");
      return;
    }
    return withBusy("forgeStartButton", async function () {
      try {
        var data = await requestJson("/novaforge/sessions", startPayload());
        forge.validatedSignature = "";
        forge.validatedTarget = null;
        forge.sessionId = data.session_id;
        forge.activeId = data.session_id;
        forge.lastFrameKey = "";
        renderSession(data);
        switchTab("forgeLiveView");
        notice("Session " + data.session_id + " started. Input reaches the target only through a planned, digest-approved action.");
        await loadStatus();
      } catch (error) {
        notice("The session was not started: " + String(error.message || error), "error");
        await loadStatus();
      }
    });
  }

  async function control(action) {
    if (!forge.sessionId || !forge.activeId) return;
    var button = action === "pause" ? "forgePauseButton"
      : action === "resume" ? "forgeResumeButton"
        : action === "cancel" ? "forgeCancelButton" : "forgeStopButton";
    return withBusy(button, async function () {
      try {
        var data = await requestJson(
          "/novaforge/sessions/" + encodeURIComponent(forge.sessionId) + "/" + action, {}
        );
        renderSession(data);
        notice(action === "pause" ? "Paused — the capture loop acknowledged the pause."
          : action === "resume" ? "Resumed against the re-validated window."
            : action === "cancel" ? "Cancelled — the loop was aborted and input cleanup ran: " +
              ((data.input_cleanup && data.input_cleanup.ok === true) ? "no held input." : "see summary.")
              : "Stopped. Input cleanup: " +
              ((data.input_cleanup && data.input_cleanup.ok === true) ? "no held input." : "see summary.") + ".");
        await loadStatus();
        await loadHistory();
      } catch (error) {
        notice("The " + action + " request was refused: " + String(error.message || error), "error");
        await loadStatus();
      }
    });
  }

  async function emergencyStop() {
    if (!forge.sessionId) {
      notice("There is no session to stop.", "error");
      return;
    }
    var target = forge.sessionId;
    return withBusy("forgeEmergencyStopButton", async function () {
      try {
        var data = await requestJson(
          "/novaforge/sessions/" + encodeURIComponent(target) + "/emergency-stop", {}
        );
        // The backend already cancelled, released input and persisted the
        // result before answering; only now may the panel claim it happened.
        renderSession(data);
        var cleanup = data.input_cleanup || {};
        var bounded = (data.guardian && data.guardian.controls &&
          data.guardian.controls.emergency_stop && data.guardian.controls.emergency_stop.bounded_seconds);
        notice("Emergency stop confirmed by the backend: session " + data.state +
          ", input cleanup " + (cleanup.ok === true ? "ok (no held input)" : "needs review") +
          (bounded ? ", bounded wait " + bounded + "s" : "") + ".",
          cleanup.ok === true ? "" : "error");
        await loadStatus();
        await loadHistory();
      } catch (error) {
        // A refused or failed stop must never look successful.
        notice("Emergency stop was NOT confirmed: " + String(error.message || error), "error");
        await loadStatus();
      }
    });
  }

  /* ── History ───────────────────────────────────────────────── */

  function historyRow(row) {
    var details = document.createElement("details");
    details.className = "forge-history-row";

    var summary = document.createElement("summary");
    var main = el("div", "forge-history-main");
    main.appendChild(el("span", "forge-history-title",
      (row.mission && row.mission.name) || "(untitled mission)"));
    var counts = row.counts || {};
    main.appendChild(el("span", "forge-history-meta",
      formatDate(row.created_at) + " · " + formatDuration(row.duration_seconds) +
      " · " + (counts.frames || 0) + " frame(s) · " + (row.end_reason || row.state)));
    summary.appendChild(main);
    var state = el("span", "forge-history-state", row.state);
    state.dataset.state = row.state;
    summary.appendChild(state);
    details.appendChild(summary);

    var body = document.createElement("div");
    body.className = "forge-summary-body";
    var grid = el("dl", "forge-summary-grid");
    [
      ["Session", row.session_id],
      ["Objective", (row.mission && row.mission.objective) || "—"],
      ["Started", row.started_at ? formatDate(row.started_at) : "—"],
      ["Ended", row.ended_at ? formatDate(row.ended_at) : "—"],
      ["Actions", (counts.actions_attempted || 0) + " attempted / " + (counts.actions_executed || 0) + " executed"],
      ["Verification", (row.verification && row.verification.status) || "unverified"],
    ].forEach(function (pair) {
      var group = el("div", "");
      group.appendChild(el("dt", "", pair[0]));
      group.appendChild(el("dd", "", String(pair[1] == null ? "—" : pair[1])));
      grid.appendChild(group);
    });
    body.appendChild(grid);

    if (row.errors && row.errors.length) {
      var errors = el("div", "forge-summary-section");
      errors.appendChild(el("h4", "", "Errors"));
      var list = el("ul", "");
      row.errors.forEach(function (entry) { list.appendChild(el("li", "", entry.message)); });
      errors.appendChild(list);
      body.appendChild(errors);
    }
    details.appendChild(body);
    return details;
  }

  async function loadHistory() {
    var list = byId("forgeHistoryList");
    try {
      var body = await requestJson("/novaforge/sessions?limit=20");
      list.innerHTML = "";
      if (!body.sessions || !body.sessions.length) {
        list.appendChild(el("p", "muted", "No sessions have been recorded yet."));
        return;
      }
      body.sessions.forEach(function (row) { list.appendChild(historyRow(row)); });
    } catch (error) {
      list.innerHTML = "";
      list.appendChild(el("p", "muted", "Session history could not be read: " + String(error.message || error)));
    }
  }

  /* ── NF-03: evidence timeline and read-only replay ─────────── */

  var TL_PAGE = 100;
  var TL_KEYS = ["forgeTimelineSession", "forgeTimelineCompareSelect", "forgeTimelineRefreshButton",
    "forgeTimelineCompareButton", "forgeTimelineCategory", "forgeTimelineSource",
    "forgeTimelineSeverity", "forgeTimelineMoreButton"];

  // The newest request wins for the timeline too: a fetch that started before
  // the user switched sessions or filters must not paint its older response.
  function timelineResponseStillCurrent(started, latest) { return started === latest; }

  function formatElapsed(ms) {
    if (ms === null || ms === undefined) return "elapsed unavailable";
    var value = Number(ms);
    if (!isFinite(value) || value < 0) return "elapsed unavailable";
    if (value < 1000) return Math.round(value) + " ms";
    return (value / 1000).toFixed(2) + " s";
  }

  // Replay safety is read from the server's own flags. If a flag ever comes
  // back true the banner says so; it is never assumed safe because a button is
  // missing from the panel.
  function replayGuard(payload) {
    var replay = (payload && payload.replay) || {};
    var blocked = ["can_start", "can_resume", "can_stop", "can_execute_action", "can_send_input"]
      .filter(function (key) { return replay[key] === true; });
    return {
      readOnly: replay.read_only === true && blocked.length === 0,
      blocked: blocked,
      reason: replay.reason || (replay.read_only === true
        ? "Replay inspects persisted evidence only."
        : "The backend did not confirm that replay is read-only."),
    };
  }

  function evidenceStatus(event) {
    var refs = (event && event.evidence) || [];
    var missing = refs.filter(function (ref) { return ref && ref.available === false; });
    var unresolved = refs.filter(function (ref) {
      return ref && ref.available !== true && ref.available !== false;
    });
    if (!refs.length) return { count: 0, missing: missing, ok: null, text: "no evidence reference" };
    if (missing.length) {
      return { count: refs.length, missing: missing, ok: false, text: missing.length + " evidence unavailable" };
    }
    if (unresolved.length) {
      return { count: refs.length, missing: missing, ok: null, text: "evidence not resolved" };
    }
    if (event && event.evidence_available === false) {
      return { count: refs.length, missing: missing, ok: false, text: "evidence unavailable" };
    }
    return { count: refs.length, missing: missing, ok: true, text: refs.length + " evidence reference(s)" };
  }

  function timelineSourceText(payload) {
    if (!payload) return "no session";
    if (payload.source === "recorded") return "recorded event log";
    if (payload.source === "derived") return "derived from the session record";
    return "source not stated";
  }

  function persistenceText(policy) {
    var state = (policy && policy.state) || "";
    if (state === "verified") {
      return { ok: true, text: "verified — the snapshot read back with its newest event" };
    }
    if (state === "failed") {
      return { ok: false, text: "FAILED — " + (policy.reason || "the snapshot did not read back") };
    }
    if (state === "not_persisted") {
      return { ok: false, text: "not persisted — " + (policy.reason || "no state store is available") };
    }
    return { ok: null, text: "not verified yet — " + ((policy && policy.reason) || "the session has not closed") };
  }

  // `loaded` is how many events the panel actually holds after Load more.
  // Without it the label printed the newest page's count as if it were the
  // number shown, so a loaded timeline read "40 of 240" while displaying 140.
  function timelinePageLabel(page, loaded) {
    if (!page) return "no page";
    if (!page.total) return "no events";
    var shown = typeof loaded === "number" && loaded > 0 ? loaded : page.count;
    return shown + " of " + page.total + " event(s)";
  }

  function categoryLabel(value) {
    return String(value || "unknown")
      .replace(/[_-]/g, " ")
      .replace(/\b\w/g, function (letter) { return letter.toUpperCase(); });
  }

  function classificationRow(entry) {
    var refs = (entry && entry.evidence) || [];
    var direct = Boolean(entry && entry.direct === true);
    return {
      category: categoryLabel(entry && entry.category),
      direct: direct,
      kind: direct ? "directly recorded" : "inferred",
      detail: (entry && entry.detail) || "",
      evidence: refs.length,
      refs: refs,
    };
  }

  // A missing figure is labelled unavailable and carries its reason; it is
  // never printed as zero.
  function compareValue(row, side) {
    if (!row) return { text: "unavailable", available: false, note: "" };
    var value = row[side];
    if (value === null || value === undefined) {
      return {
        text: "unavailable",
        available: false,
        note: row.reason || "This figure was not measured for this session.",
      };
    }
    if (typeof value === "boolean") {
      return { text: value ? "yes" : "no", available: true, note: row.reason || "" };
    }
    return { text: String(value), available: true, note: row.reason || "" };
  }

  function timelineNotice(message, kind) {
    var box = byId("forgeTimelineNotice");
    if (!box) return;
    if (!message) {
      box.hidden = true;
      box.textContent = "";
      box.className = "forge-notice";
      return;
    }
    box.hidden = false;
    box.className = "forge-notice" + (kind === "error" ? " error" : "");
    box.textContent = message;
  }

  function setTimelineBusy(busy) {
    forge.timeline.busy = Boolean(busy);
    TL_KEYS.forEach(function (id) {
      var node = byId(id);
      if (node) node.disabled = Boolean(busy);
    });
    updateTimelineNav();
  }

  function timelineIndexFor(seq) {
    var events = forge.timeline.events;
    for (var index = 0; index < events.length; index += 1) {
      if (events[index].seq === seq) return index;
    }
    return -1;
  }

  function updateTimelineNav() {
    var prev = byId("forgeTimelinePrevButton");
    var next = byId("forgeTimelineNextButton");
    var position = byId("forgeTimelinePosition");
    if (!prev || !next || !position) return;
    var events = forge.timeline.events;
    var index = timelineIndexFor(forge.timeline.selectedSeq);
    prev.disabled = forge.timeline.busy || index <= 0;
    next.disabled = forge.timeline.busy || index < 0 || index >= events.length - 1;
    position.textContent = index < 0 ? "no event" : (index + 1) + " / " + events.length;
  }

  function highlightTimelineSelection() {
    var list = byId("forgeTimelineList");
    if (!list) return;
    var rows = list.children || [];
    for (var index = 0; index < rows.length; index += 1) {
      var row = rows[index];
      if (!row || !row.dataset) continue;
      row.dataset.selected = row.dataset.seq === String(forge.timeline.selectedSeq) ? "true" : "false";
    }
  }

  function rememberVocabulary(events) {
    (events || []).forEach(function (event) {
      if (!event) return;
      if (event.category) forge.timeline.vocabulary.category[event.category] = true;
      if (event.source) forge.timeline.vocabulary.source[event.source] = true;
      if (event.severity) forge.timeline.vocabulary.severity[event.severity] = true;
    });
  }

  function fillFilterSelect(id, allLabel, vocabulary, current) {
    var select = byId(id);
    if (!select) return;
    select.innerHTML = "";
    var all = document.createElement("option");
    all.value = "";
    all.textContent = allLabel;
    select.appendChild(all);
    Object.keys(vocabulary).sort().forEach(function (value) {
      var option = document.createElement("option");
      option.value = value;
      option.textContent = categoryLabel(value);
      select.appendChild(option);
    });
    select.value = current && vocabulary[current] ? current : "";
  }

  function renderFilterOptions() {
    var vocab = forge.timeline.vocabulary;
    fillFilterSelect("forgeTimelineCategory", "all categories", vocab.category, forge.timeline.filters.category);
    fillFilterSelect("forgeTimelineSource", "all sources", vocab.source, forge.timeline.filters.source);
    fillFilterSelect("forgeTimelineSeverity", "all severities", vocab.severity, forge.timeline.filters.severity);
  }

  function renderTimelineMeta(payload) {
    var box = byId("forgeTimelineMeta");
    if (!box) return;
    box.innerHTML = "";
    if (!payload) {
      box.appendChild(el("p", "muted", "Select a recorded session to inspect its evidence timeline."));
      return;
    }
    var persistence = persistenceText(payload.persistence);
    var retention = payload.retention || {};
    var page = payload.page || {};
    var grid = el("dl", "forge-summary-grid");
    [
      ["Session", payload.session_id],
      ["State", payload.state],
      ["Source", timelineSourceText(payload)],
      ["Persistence", persistence.text],
      ["Events", timelinePageLabel(page, forge.timeline.events.length) + " shown"],
      ["Retention", (retention.stored || 0) + " retained · cap " + (retention.max_events || 0) +
        (retention.dropped ? " · " + retention.dropped + " dropped" : "")],
      ["Ordering", (payload.ordering && payload.ordering.primary) || "seq"],
    ].forEach(function (pair) {
      var group = el("div", "");
      group.appendChild(el("dt", "", pair[0]));
      group.appendChild(el("dd", "", String(pair[1] == null ? "—" : pair[1])));
      grid.appendChild(group);
    });
    box.appendChild(grid);

    if (payload.source === "derived") {
      box.appendChild(el("p", "forge-frame-note",
        "Reconstructed timeline: " + (payload.source_note || "the events were derived from the persisted session record.")));
    }
    if (payload.source === "recorded") {
      box.appendChild(el("p", "forge-frame-note",
        "Recorded as the session happened. " + (payload.source_note || "")));
    }
    if (retention.dropped) {
      box.appendChild(el("p", "forge-frame-note", retention.note || "Older events of this session were dropped by retention."));
    }
    // The backend states which pipeline stages this phase does not have (no
    // action and no verification path). Not rendering them left the panel
    // silent about what the original session could never have recorded.
    (payload.limitations || []).forEach(function (note) {
      if (note) box.appendChild(el("p", "forge-frame-note", String(note)));
    });
    if (payload.ordering && payload.ordering.note) {
      box.appendChild(el("p", "forge-frame-note", payload.ordering.note));
    }
    var guard = replayGuard(payload);
    if (!guard.readOnly) {
      box.appendChild(el("p", "forge-frame-note",
        "Replay boundary NOT confirmed: " + (guard.blocked.length ? guard.blocked.join(", ") : guard.reason)));
    }
  }

  function renderTimelineList() {
    var list = byId("forgeTimelineList");
    var count = byId("forgeTimelineCount");
    if (!list) return;
    list.innerHTML = "";
    var events = forge.timeline.events;
    if (count) count.textContent = events.length
      ? timelinePageLabel(forge.timeline.payload && forge.timeline.payload.page, events.length)
      : "no events";
    if (!events.length) {
      list.appendChild(el("li", "muted", "No events match this session and filter."));
      var more = byId("forgeTimelineMoreButton");
      if (more) more.hidden = true;
      updateTimelineNav();
      return;
    }
    events.forEach(function (event) {
      var row = el("li", "forge-timeline-row");
      row.dataset.seq = String(event.seq);
      row.dataset.severity = event.severity || "info";
      row.dataset.selected = event.seq === forge.timeline.selectedSeq ? "true" : "false";
      var button = document.createElement("button");
      button.type = "button";
      button.className = "forge-timeline-button";
      var head = el("span", "forge-timeline-rowhead");
      head.appendChild(el("span", "forge-timeline-seq", "#" + event.seq));
      head.appendChild(el("span", "forge-timeline-time", formatTime(event.at)));
      head.appendChild(el("span", "forge-timeline-elapsed", "+" + formatElapsed(event.elapsed_ms)));
      head.appendChild(el("span", "forge-timeline-source", event.source || "—"));
      head.appendChild(el("span", "forge-timeline-type", event.type));
      button.appendChild(head);
      button.appendChild(el("span", "forge-timeline-summary", event.summary || event.type));
      var flags = el("span", "forge-timeline-flags");
      if (event.derived) flags.appendChild(el("span", "forge-badge", "derived"));
      var status = evidenceStatus(event);
      var badge = el("span", "forge-badge", status.text);
      badge.dataset.ok = status.ok === false ? "false" : (status.ok === true ? "true" : "unknown");
      flags.appendChild(badge);
      if (event.severity && event.severity !== "info") {
        flags.appendChild(el("span", "forge-badge", event.severity));
      }
      button.appendChild(flags);
      button.addEventListener("click", function () { selectTimelineEvent(event.seq); });
      row.appendChild(button);
      list.appendChild(row);
    });
    var moreButton = byId("forgeTimelineMoreButton");
    if (moreButton) moreButton.hidden = !forge.timeline.hasMore;
    updateTimelineNav();
  }

  function setTimelineFrameEmpty(title, detail) {
    var empty = byId("forgeTimelineFrameEmpty");
    if (!empty) return;
    empty.hidden = false;
    var paragraphs = empty.children || [];
    var titleNode = null;
    var detailNode = null;
    for (var index = 0; index < paragraphs.length; index += 1) {
      var node = paragraphs[index];
      if (node.tagName === "p" && titleNode === null) titleNode = node;
      else if (node.tagName === "p") detailNode = node;
    }
    if (titleNode) titleNode.textContent = title;
    if (detailNode) detailNode.textContent = detail || "";
  }

  function releaseTimelineFrame() {
    if (forge.timeline.frameUrl && typeof URL !== "undefined" && URL.revokeObjectURL) {
      URL.revokeObjectURL(forge.timeline.frameUrl);
    }
    forge.timeline.frameUrl = "";
  }

  function clearTimelineFrame(note) {
    releaseTimelineFrame();
    var image = byId("forgeTimelineFrame");
    if (image) {
      image.hidden = true;
      image.removeAttribute("src");
    }
    setTimelineFrameEmpty("No frame evidence for this event.", "The event remains inspectable without an image.");
    var noteNode = byId("forgeTimelineFrameNote");
    if (noteNode) noteNode.textContent = note || "";
  }

  async function fetchTimelineFrame(frame) {
    var token = ++forge.timeline.frameToken;
    var image = byId("forgeTimelineFrame");
    var note = byId("forgeTimelineFrameNote");
    try {
      var response = await fetch(frame.url, { headers: authHeaders() });
      if (!response.ok) throw new Error("HTTP " + response.status);
      var blob = await response.blob();
      if (!timelineResponseStillCurrent(token, forge.timeline.frameToken)) return;
      var url = URL.createObjectURL(blob);
      releaseTimelineFrame();
      forge.timeline.frameUrl = url;
      image.src = url;
      image.hidden = false;
      byId("forgeTimelineFrameEmpty").hidden = true;
      note.textContent = "Frame " + frame.seq +
        (frame.captured_at ? " · captured " + formatDate(frame.captured_at) : "") +
        (frame.width ? " · " + frame.width + " × " + frame.height + " px" : "") +
        " · referenced by event #" + forge.timeline.selectedSeq;
    } catch (error) {
      if (!timelineResponseStillCurrent(token, forge.timeline.frameToken)) return;
      releaseTimelineFrame();
      image.hidden = true;
      image.removeAttribute("src");
      setTimelineFrameEmpty("The recorded frame could not be loaded.",
        "The event stays inspectable; the image is unavailable right now.");
      note.textContent = "Frame " + frame.seq + " could not be loaded: " + String(error.message || error);
    }
  }

  function renderTimelineFrame(event) {
    var wrap = byId("forgeTimelineFrameWrap");
    var image = byId("forgeTimelineFrame");
    var note = byId("forgeTimelineFrameNote");
    if (!wrap || !image || !note) return;
    wrap.hidden = false;
    var refs = (event && event.evidence) || [];
    var frame = null;
    for (var index = 0; index < refs.length; index += 1) {
      if (refs[index] && refs[index].kind === "frame") { frame = refs[index]; break; }
    }
    if (!frame) {
      releaseTimelineFrame();
      image.hidden = true;
      image.removeAttribute("src");
      setTimelineFrameEmpty("No frame evidence for this event.", "The event remains inspectable without an image.");
      note.textContent = "";
      return;
    }
    if (frame.available === false) {
      releaseTimelineFrame();
      image.hidden = true;
      image.removeAttribute("src");
      setTimelineFrameEmpty("Frame " + frame.seq + " is no longer retained.",
        frame.reason || "Frame retention removed the image; the event remains inspectable.");
      note.textContent = frame.reason || "The referenced frame is unavailable.";
      return;
    }
    if (!frame.url) {
      releaseTimelineFrame();
      image.hidden = true;
      image.removeAttribute("src");
      setTimelineFrameEmpty("The frame reference could not be resolved.",
        "No image URL was returned for this reference.");
      note.textContent = "";
      return;
    }
    releaseTimelineFrame();
    image.hidden = true;
    setTimelineFrameEmpty("Loading the recorded frame…", "Frame " + frame.seq + " evidence.");
    note.textContent = "";
    fetchTimelineFrame(frame);
  }

  function renderTimelineDetail(event) {
    forge.timeline.selectedSeq = event ? event.seq : 0;
    highlightTimelineSelection();
    var title = byId("forgeTimelineDetailTitle");
    var body = byId("forgeTimelineDetail");
    if (!title || !body) return;
    body.innerHTML = "";
    if (!event) {
      title.textContent = "Select an event";
      body.appendChild(el("p", "muted",
        "Choose an event from the timeline to inspect its recorded details and evidence."));
      clearTimelineFrame("");
      updateTimelineNav();
      return;
    }
    title.textContent = "#" + event.seq + " · " + event.type;
    var grid = el("dl", "forge-summary-grid");
    [
      ["Recorded at", formatDate(event.at) + " (wall clock label)"],
      ["Elapsed", formatElapsed(event.elapsed_ms)],
      ["Type", event.type],
      ["Category", categoryLabel(event.category)],
      ["Severity", event.severity || "info"],
      ["Source", event.source || "—"],
      ["Event ID", event.event_id || "—"],
    ].forEach(function (pair) {
      var group = el("div", "");
      group.appendChild(el("dt", "", pair[0]));
      group.appendChild(el("dd", "", String(pair[1] == null ? "—" : pair[1])));
      grid.appendChild(group);
    });
    body.appendChild(grid);
    if (event.derived) {
      body.appendChild(el("p", "forge-frame-note",
        "Derived: reconstructed from the persisted session record, not recorded as it happened."));
    }

    var payload = event.payload || {};
    var keys = Object.keys(payload);
    if (keys.length) {
      body.appendChild(el("h5", "", "Payload"));
      var payloadList = el("dl", "forge-timeline-payload");
      keys.forEach(function (key) {
        var row = el("div", "");
        row.appendChild(el("dt", "", key));
        var value = payload[key];
        row.appendChild(el("dd", "", typeof value === "string" ? value : JSON.stringify(value)));
        payloadList.appendChild(row);
      });
      body.appendChild(payloadList);
    }

    body.appendChild(el("h5", "", "Evidence"));
    var refs = event.evidence || [];
    var status = evidenceStatus(event);
    body.appendChild(el("p", "forge-frame-note", status.text));
    if (!refs.length) {
      body.appendChild(el("p", "muted", "This event references no evidence."));
    } else {
      var list = el("ul", "forge-timeline-evidence");
      refs.forEach(function (ref) {
        var item = el("li", "");
        item.dataset.available = ref && ref.available === false ? "false"
          : (ref && ref.available === true ? "true" : "unknown");
        var head = el("div", "forge-timeline-rowhead");
        var kind = (ref && ref.kind) || "reference";
        head.appendChild(el("span", "forge-timeline-type", kind));
        if (ref && ref.seq !== undefined && ref.seq !== null) head.appendChild(el("span", "", "seq " + ref.seq));
        if (ref && ref.index !== undefined && ref.index !== null) head.appendChild(el("span", "", "index " + ref.index));
        item.appendChild(head);
        if (!ref) {
          item.appendChild(el("p", "forge-frame-note", "Empty reference."));
        } else if (ref.available === false) {
          item.appendChild(el("p", "forge-frame-note", "Unavailable: " + (ref.reason || "no reason recorded")));
        } else if (ref.available !== true) {
          item.appendChild(el("p", "forge-frame-note", "Not resolved by this response."));
        } else if (kind === "frame") {
          item.appendChild(el("p", "forge-frame-note",
            (ref.captured_at ? "Captured " + formatDate(ref.captured_at) : "Frame retained") +
            (ref.width ? " · " + ref.width + " × " + ref.height + " px" : "")));
        } else if (kind === "observation") {
          item.appendChild(el("p", "forge-frame-note",
            "Observation " + ref.index + ": " + (ref.summary || "recorded") +
            (ref.evidence === false ? " · recorded WITHOUT visual evidence" : " · evidence recorded")));
        } else if (kind === "intervention") {
          var entry = ref.entry || {};
          item.appendChild(el("p", "forge-frame-note",
            "Guardian " + (entry.kind || "intervention") + " · " + (entry.reason || "reason not recorded") +
            " → " + (entry.outcome || "outcome not recorded")));
        } else if (kind === "event") {
          item.appendChild(el("p", "forge-frame-note", "Evidence event — inspect the timeline entry at that seq."));
        } else {
          item.appendChild(el("p", "forge-frame-note", "Available from " + (ref.url || "the session's API record")));
        }
        list.appendChild(item);
      });
      body.appendChild(list);
    }
    renderTimelineFrame(event);
    updateTimelineNav();
  }

  function selectTimelineEvent(seq) {
    var index = timelineIndexFor(seq);
    if (index < 0) return;
    renderTimelineDetail(forge.timeline.events[index]);
  }

  function stepTimeline(delta) {
    var events = forge.timeline.events;
    if (!events.length) return;
    var index = timelineIndexFor(forge.timeline.selectedSeq);
    if (index < 0) index = 0;
    else index += delta;
    if (index < 0 || index >= events.length) return;
    renderTimelineDetail(events[index]);
  }

  function renderTimelineClassification(classification) {
    var box = byId("forgeTimelineClassification");
    if (!box) return;
    box.innerHTML = "";
    if (!classification) {
      box.appendChild(el("p", "muted",
        "Select a session to see which failure categories its evidence supports."));
      return;
    }
    var head = el("div", "forge-timeline-rowhead");
    var badge = el("span", "forge-badge",
      classification.outcome === "classified" ? "failure classified" : "no failure recorded");
    badge.dataset.ok = classification.outcome === "classified" ? "false" : "true";
    head.appendChild(badge);
    head.appendChild(el("span", "",
      classification.end_reason ? "end reason: " + classification.end_reason : "no end reason recorded"));
    box.appendChild(head);
    box.appendChild(el("p", "forge-frame-note", classification.detail || ""));
    var entries = classification.classifications || [];
    if (entries.length) {
      var list = el("ul", "");
      entries.forEach(function (entry) {
        var row = classificationRow(entry);
        var item = el("li", "");
        item.dataset.direct = row.direct ? "true" : "false";
        var itemHead = el("div", "forge-timeline-rowhead");
        itemHead.appendChild(el("span", "forge-timeline-type", row.category));
        itemHead.appendChild(el("span", "", row.kind));
        itemHead.appendChild(el("span", "", row.evidence + " reference(s)"));
        item.appendChild(itemHead);
        if (row.detail) item.appendChild(el("p", "forge-frame-note", row.detail));
        row.refs.forEach(function (ref) {
          if (!ref || ref.seq === undefined || ref.seq === null) return;
          if (timelineIndexFor(ref.seq) < 0) {
            item.appendChild(el("p", "forge-frame-note", "Evidence seq " + ref.seq + " is outside the loaded page."));
            return;
          }
          var jump = document.createElement("button");
          jump.type = "button";
          jump.className = "cyber-btn small";
          jump.textContent = "Inspect event #" + ref.seq;
          jump.addEventListener("click", function () { selectTimelineEvent(ref.seq); });
          item.appendChild(jump);
        });
        list.appendChild(item);
      });
      box.appendChild(list);
    }
    var completion = classification.completion || {};
    box.appendChild(el("p", "forge-frame-note",
      "Objective verification: " + (completion.verified === true ? "verified" : "NOT verified") +
      " · " + (completion.reason || "no verifier is attached to NF-03.")));
  }

  function renderTimelineCompare(result) {
    var box = byId("forgeTimelineCompareOutput");
    if (!box) return;
    box.innerHTML = "";
    if (!result || !result.rows) {
      box.appendChild(el("p", "muted", "No comparison result was returned."));
      return;
    }
    var left = result.left || {};
    var right = result.right || {};
    box.appendChild(el("p", "forge-frame-note",
      "Left: " + (left.mission || "untitled") + " · " + (left.session_id || "—")));
    box.appendChild(el("p", "forge-frame-note",
      "Right: " + (right.mission || "untitled") + " · " + (right.session_id || "—")));
    var table = document.createElement("table");
    var thead = document.createElement("thead");
    var headRow = document.createElement("tr");
    ["Metric", "Left", "Right", "Delta"].forEach(function (text) {
      var cell = document.createElement("th");
      cell.textContent = text;
      headRow.appendChild(cell);
    });
    thead.appendChild(headRow);
    table.appendChild(thead);
    var tbody = document.createElement("tbody");
    result.rows.forEach(function (row) {
      var tr = document.createElement("tr");
      var labelCell = document.createElement("td");
      labelCell.textContent = row.label || row.key;
      tr.appendChild(labelCell);
      ["left", "right"].forEach(function (side) {
        var cell = document.createElement("td");
        var info = compareValue(row, side);
        cell.textContent = info.text;
        cell.dataset.available = info.available ? "true" : "false";
        if (info.note) cell.title = info.note;
        tr.appendChild(cell);
      });
      var delta = document.createElement("td");
      delta.textContent = row.delta === null || row.delta === undefined
        ? "—" : (row.delta > 0 ? "+" : "") + row.delta;
      tr.appendChild(delta);
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    box.appendChild(table);
    var missing = result.missing || {};
    if ((missing.left || []).length || (missing.right || []).length) {
      box.appendChild(el("p", "forge-frame-note",
        "Missing (not measured): left " + ((missing.left || []).join(", ") || "none") +
        "; right " + ((missing.right || []).join(", ") || "none") + ". " + (missing.note || "")));
    }
  }

  function requestErrorMessage(error) {
    var message = String((error && error.message) || error || "unknown error");
    if (/unauthor|forbidden|token|credential|401|403/i.test(message)) {
      return message + " — set the API token in the header and try again.";
    }
    return message;
  }

  function timelineFilters() {
    return {
      category: byId("forgeTimelineCategory").value,
      source: byId("forgeTimelineSource").value,
      severity: byId("forgeTimelineSeverity").value,
    };
  }

  function timelineQuery(filters) {
    var parts = [];
    if (filters.category) parts.push("categories=" + encodeURIComponent(filters.category));
    if (filters.source) parts.push("sources=" + encodeURIComponent(filters.source));
    if (filters.severity) parts.push("severities=" + encodeURIComponent(filters.severity));
    return parts.length ? "&" + parts.join("&") : "";
  }

  async function loadTimelineSessions() {
    var select = byId("forgeTimelineSession");
    if (!select) return;
    try {
      var body = await requestJson("/novaforge/sessions?limit=40");
      var rows = body.sessions || [];
      var ids = rows.map(function (row) { return row.session_id; });
      var previous = forge.timeline.sessionId || select.value;
      var chosen = ids.indexOf(previous) >= 0 ? previous
        : (ids.indexOf(forge.sessionId) >= 0 ? forge.sessionId : (ids[0] || ""));

      select.innerHTML = "";
      if (!rows.length) {
        var empty = document.createElement("option");
        empty.value = "";
        empty.textContent = "No recorded sessions yet";
        select.appendChild(empty);
        forge.timeline.sessionId = "";
        var emptyCompare = byId("forgeTimelineCompareSelect");
        emptyCompare.innerHTML = "";
        var none = document.createElement("option");
        none.value = "";
        none.textContent = "no comparison";
        emptyCompare.appendChild(none);
        byId("forgeTimelineList").innerHTML = "";
        byId("forgeTimelineList").appendChild(el("li", "muted",
          "No session has been recorded, so there is no evidence to replay."));
        renderTimelineMeta(null);
        renderTimelineClassification(null);
        renderTimelineDetail(null);
        timelineNotice("");
        return;
      }

      var compare = byId("forgeTimelineCompareSelect");
      var comparePrevious = compare.value;
      compare.innerHTML = "";
      var noCompare = document.createElement("option");
      noCompare.value = "";
      noCompare.textContent = "no comparison";
      compare.appendChild(noCompare);
      rows.forEach(function (row) {
        var option = document.createElement("option");
        option.value = row.session_id;
        option.textContent = formatDate(row.created_at) + " · " +
          ((row.mission && row.mission.name) || "untitled mission") + " · " + row.state;
        select.appendChild(option);
        var mirror = document.createElement("option");
        mirror.value = row.session_id;
        mirror.textContent = option.textContent;
        compare.appendChild(mirror);
      });
      select.value = chosen;
      compare.value = ids.indexOf(comparePrevious) >= 0 ? comparePrevious : "";
      forge.timeline.sessionId = chosen;
      if (chosen) await loadTimeline();
    } catch (error) {
      var failed = requestErrorMessage(error);
      var sessionSelect = byId("forgeTimelineSession");
      sessionSelect.innerHTML = "";
      var unavailable = document.createElement("option");
      unavailable.value = "";
      unavailable.textContent = "Session list unavailable";
      sessionSelect.appendChild(unavailable);
      timelineNotice("Recorded sessions could not be read: " + failed, "error");
      var list = byId("forgeTimelineList");
      list.innerHTML = "";
      list.appendChild(el("li", "muted", "The timeline is unavailable: " + failed));
      renderTimelineMeta(null);
      renderTimelineClassification(null);
      renderTimelineDetail(null);
    }
  }

  async function loadTimeline(options) {
    var opts = options || {};
    var select = byId("forgeTimelineSession");
    var sessionId = opts.sessionId || (select && select.value) || forge.timeline.sessionId;
    var token = ++forge.timeline.token;
    var append = Boolean(opts.append);
    if (!sessionId) return;
    forge.timeline.sessionId = sessionId;
    forge.timeline.filters = timelineFilters();
    if (!append) {
      forge.timeline.events = [];
      forge.timeline.nextAfterSeq = 0;
      forge.timeline.hasMore = false;
      forge.timeline.payload = null;
      forge.timeline.selectedSeq = 0;
      byId("forgeTimelineList").innerHTML = "";
      renderTimelineDetail(null);
    }
    setTimelineBusy(true);
    timelineNotice(append ? "Loading more events…" : "Loading the timeline…");
    try {
      var query = "/novaforge/sessions/" + encodeURIComponent(sessionId) + "/timeline?limit=" + TL_PAGE;
      if (append) query += "&after_seq=" + encodeURIComponent(String(forge.timeline.nextAfterSeq));
      query += timelineQuery(forge.timeline.filters);
      var body = await requestJson(query);
      // A newer selection (or filter) supersedes this response entirely.
      if (!timelineResponseStillCurrent(token, forge.timeline.token)) return;
      forge.timeline.sessionId = body.session_id || sessionId;
      var events = body.events || [];
      rememberVocabulary(events);
      forge.timeline.events = append ? forge.timeline.events.concat(events) : events;
      forge.timeline.nextAfterSeq = (body.page && body.page.next_after_seq) || 0;
      forge.timeline.hasMore = Boolean(body.page && body.page.has_more);
      forge.timeline.payload = body;
      renderFilterOptions();
      renderTimelineMeta(body);
      renderTimelineList();
      renderTimelineClassification(body.classification);
      setTimelineBusy(false);
      var guard = replayGuard(body);
      timelineNotice(!guard.readOnly
        ? "Replay boundary not confirmed by the backend: " + (guard.blocked.join(", ") || guard.reason)
        : (body.source === "derived"
          ? "Derived timeline: this session predates the NF-03 event log, so its events are reconstructed from the persisted record (marked derived)."
          : ""), !guard.readOnly ? "error" : "");
      if (!forge.timeline.events.length) {
        renderTimelineDetail(null);
      } else if (!append || timelineIndexFor(forge.timeline.selectedSeq) < 0) {
        renderTimelineDetail(forge.timeline.events[0]);
      }
    } catch (error) {
      if (token !== forge.timeline.token) return;
      setTimelineBusy(false);
      var message = requestErrorMessage(error);
      if (!forge.timeline.events.length) {
        byId("forgeTimelineList").innerHTML = "";
        byId("forgeTimelineList").appendChild(el("li", "muted", "The timeline could not be read: " + message));
        byId("forgeTimelineCount").textContent = "unavailable";
        renderTimelineMeta(null);
        renderTimelineClassification(null);
        renderTimelineDetail(null);
      }
      timelineNotice("Timeline unavailable: " + message, "error");
    }
  }

  async function compareTimelineSessions() {
    var left = forge.timeline.sessionId;
    var right = byId("forgeTimelineCompareSelect").value;
    var box = byId("forgeTimelineCompareOutput");
    if (!left || !right) {
      box.innerHTML = "";
      box.appendChild(el("p", "muted", "Choose a session and a second session to compare."));
      return;
    }
    if (left === right) {
      box.innerHTML = "";
      box.appendChild(el("p", "muted", "Choose two DIFFERENT sessions — a session cannot be compared with itself."));
      return;
    }
    setTimelineBusy(true);
    timelineNotice("Comparing recorded evidence…");
    try {
      var body = await requestJson(
        "/novaforge/timeline/compare?left=" + encodeURIComponent(left) + "&right=" + encodeURIComponent(right)
      );
      renderTimelineCompare(body);
      timelineNotice("");
    } catch (error) {
      box.innerHTML = "";
      box.appendChild(el("p", "muted", "The comparison could not be read: " + requestErrorMessage(error)));
      timelineNotice("Comparison unavailable: " + requestErrorMessage(error), "error");
    } finally {
      setTimelineBusy(false);
    }
  }

  function openTimeline() {
    var pendingSeq = forge.timeline.pendingSeq;
    forge.timeline.pendingSeq = 0;
    return loadTimelineSessions().then(function () {
      var pending = forge.benchmark.pendingEvidence;
      if (!pending) return;
      forge.benchmark.pendingEvidence = "";
      var select = byId("forgeTimelineSession");
      var present = select && Array.prototype.some.call(select.options, function (option) {
        return option.value === pending;
      });
      if (!present) {
        timelineNotice("The referenced session is not in the recorded session list.", "error");
        return;
      }
      select.value = pending;
      forge.timeline.sessionId = pending;
      return loadTimeline().then(function () {
        // A Coach finding deep-links to the exact recorded event. The page is
        // the newest events only, so an older seq is REPORTED as not loaded
        // instead of silently selecting something else.
        if (!pendingSeq) return;
        if (timelineIndexFor(pendingSeq) < 0) {
          timelineNotice("Event #" + pendingSeq + " is older than the loaded page; " +
            "narrow the filters or load more events to reach it.", "error");
          return;
        }
        selectTimelineEvent(pendingSeq);
      });
    });
  }

  /* ── NF-04: Forge Benchmark Lab ───────────────────────────────
   *
   * The lab is a CLIENT of the benchmark API, and the API is a client of the
   * session manager. Nothing here computes a benchmark figure: every number is
   * read from a run record, and every unavailable value keeps the backend's
   * reason instead of becoming zero. Start is gated on the CURRENT preflight
   * signature, so a scenario or budget edit cannot ride on an older validation.
   */

  var BENCHMARK_POLL_MS = 1000;

  function benchmarkNotice(message, kind) {
    var box = byId("forgeBenchmarkNotice");
    if (!box) return;
    if (!message) {
      box.hidden = true;
      box.textContent = "";
      box.className = "forge-notice";
      return;
    }
    box.hidden = false;
    box.className = "forge-notice" + (kind === "error" ? " error" : "");
    box.textContent = message;
  }

  // A measured figure and an unavailable one are different facts. `0` is a
  // measurement; null/undefined is not, and the backend's reason travels with it.
  function benchmarkMetric(row) {
    var data = row || {};
    var available = data.available === true && data.value !== null && data.value !== undefined;
    return {
      available: available,
      text: available ? metricText(data.value, data.unit) : "unavailable",
      reason: String(data.reason || (available ? "" : "The run did not measure this value.")),
      kind: data.kind || "measured",
      unit: data.unit || "",
    };
  }

  function benchmarkCanStart(preflight, run) {
    return Boolean(
      preflight && preflight.valid === true && preflight.runnable === true &&
      !(run && run.status === "running")
    );
  }

  function benchmarkStatusText(run) {
    if (!run) return "no run";
    var status = String(run.status || "unknown");
    if (status === "running") return "running";
    if (status === "completed") return run.valid ? "completed · data complete" : "completed · invalid";
    if (status === "invalid") return "invalid · excluded";
    if (status === "aborted") return "aborted · excluded";
    if (status === "failed") return "failed · excluded";
    return status;
  }

  function benchmarkVerdictText(verdict) {
    if (!verdict) return "Not established — no comparison has run.";
    if (verdict.established) return "Established: " + String(verdict.reason || "");
    return "Not established — " + String(verdict.reason || "no basis for a claim.");
  }

  function benchmarkDeltaText(row) {
    if (!row) return "—";
    if (row.delta === null || row.delta === undefined) return "—";
    return (row.delta > 0 ? "+" : "") + row.delta + (row.unit ? " " + row.unit : "");
  }

  function benchmarkSignature() {
    return JSON.stringify(benchmarkPayload());
  }

  function benchmarkScenario() {
    var selected = forge.benchmark.selected;
    if (selected) return selected;
    var select = byId("forgeBenchmarkScenario");
    var value = select ? select.value : "";
    return forge.benchmark.scenarios.find(function (item) {
      return item.scenario_id + "@" + item.version === value;
    }) || null;
  }

  function benchmarkPayload() {
    var scenario = benchmarkScenario();
    var duration = byId("forgeBenchmarkDuration");
    var interval = byId("forgeBenchmarkInterval");
    var target = byId("forgeBenchmarkTarget");
    var notes = byId("forgeBenchmarkNotes");
    return {
      scenario_id: scenario ? scenario.scenario_id : "",
      version: scenario ? scenario.version : null,
      target_title: target ? String(target.value || "").trim() : "",
      duration_seconds: duration && duration.value !== "" ? Number(duration.value) : null,
      frame_interval_seconds: interval && interval.value !== "" ? Number(interval.value) : null,
      notes: notes ? String(notes.value || "").trim() : "",
    };
  }

  function invalidateBenchmarkPreflight() {
    forge.benchmark.preflight = null;
    forge.benchmark.signature = "";
    var box = byId("forgeBenchmarkPreflight");
    if (box) {
      box.innerHTML = "";
      box.className = "forge-validation";
      box.appendChild(el("p", "muted", "Configuration changed — validate again before starting."));
    }
    updateBenchmarkControls();
  }

  function updateBenchmarkControls() {
    var start = byId("forgeBenchmarkStartButton");
    var cancel = byId("forgeBenchmarkCancelButton");
    var exportButton = byId("forgeBenchmarkExportButton");
    var run = forge.benchmark.run;
    var preflight = forge.benchmark.preflight;
    if (start) {
      start.disabled = !(benchmarkCanStart(preflight, run) && forge.benchmark.signature === benchmarkSignature());
      start.title = preflight && preflight.valid
        ? "Start this validated scenario"
        : "Validate the scenario against a live window first";
    }
    if (cancel) cancel.disabled = !(run && run.status === "running");
    if (exportButton) exportButton.disabled = !forge.benchmark.detail;
  }

  function renderBenchmarkCatalog() {
    var select = byId("forgeBenchmarkScenario");
    if (!select) return;
    var previous = forge.benchmark.scenarioId || select.value;
    select.innerHTML = "";
    forge.benchmark.scenarios.forEach(function (scenario) {
      var option = document.createElement("option");
      option.value = scenario.scenario_id + "@" + scenario.version;
      option.textContent = scenario.name + " (v" + scenario.version + ")" +
        (scenario.supported ? "" : " — unsupported");
      select.appendChild(option);
    });
    if (!forge.benchmark.scenarios.length) {
      var empty = document.createElement("option");
      empty.value = "";
      empty.textContent = "No scenario catalog available";
      select.appendChild(empty);
      byId("forgeBenchmarkScenarioDetail").innerHTML = "";
      byId("forgeBenchmarkScenarioDetail").appendChild(
        el("p", "muted", "The backend returned no benchmark scenarios.")
      );
      return;
    }
    var keys = forge.benchmark.scenarios.map(function (item) {
      return item.scenario_id + "@" + item.version;
    });
    select.value = keys.indexOf(previous) >= 0 ? previous : keys[0];
    forge.benchmark.scenarioId = select.value;
    selectBenchmarkScenario();
  }

  async function loadBenchmarkCatalog() {
    var badge = byId("forgeBenchmarkCatalogState");
    if (!badge) return;
    badge.textContent = "loading";
    badge.dataset.ok = "pending";
    try {
      var body = await requestJson("/novaforge/benchmarks/scenarios");
      forge.benchmark.scenarios = body.scenarios || [];
      forge.benchmark.metricCatalog = body.metric_catalog || [];
      renderBenchmarkCatalog();
      badge.textContent = body.supported_count + " runnable · " + body.unsupported_count + " unsupported";
      badge.dataset.ok = "true";
    } catch (error) {
      badge.textContent = "unavailable";
      badge.dataset.ok = "false";
      var detail = byId("forgeBenchmarkScenarioDetail");
      detail.innerHTML = "";
      detail.appendChild(el("p", "muted", "The scenario catalog could not be read: " + requestErrorMessage(error)));
      benchmarkNotice("Benchmark catalog unavailable: " + requestErrorMessage(error), "error");
    }
  }

  function detailRow(labelText, valueText) {
    var group = el("div", "forge-benchmark-pair");
    group.appendChild(el("span", "forge-benchmark-label", labelText));
    group.appendChild(el("strong", "forge-benchmark-value", valueText));
    return group;
  }

  function renderBenchmarkScenarioDetail(scenario) {
    var box = byId("forgeBenchmarkScenarioDetail");
    if (!box) return;
    box.innerHTML = "";
    if (!scenario) {
      box.appendChild(el("p", "muted", "No scenario is selected."));
      return;
    }
    var head = el("div", "forge-benchmark-head");
    var badge = el("span", "forge-badge", scenario.supported ? "supported" : "unsupported");
    badge.dataset.ok = scenario.supported ? "true" : "false";
    head.appendChild(badge);
    head.appendChild(el("span", "forge-benchmark-version", "v" + scenario.version + " · " + scenario.evaluation));
    box.appendChild(head);
    box.appendChild(el("p", "", scenario.objective));
    if (!scenario.supported) {
      var reasonBox = el("p", "forge-benchmark-unsupported", scenario.unsupported_reason);
      box.appendChild(reasonBox);
    }
    var pairs = el("div", "forge-benchmark-pairs");
    pairs.appendChild(detailRow("Observation source", scenario.observation_source));
    pairs.appendChild(detailRow("Target", scenario.target_requirement));
    pairs.appendChild(detailRow("Budget / interval", scenario.default_duration_seconds + " s / " + scenario.frame_interval_seconds + " s"));
    pairs.appendChild(detailRow("Minimum frames", String(scenario.min_frames)));
    pairs.appendChild(detailRow("Verification", scenario.verifier && scenario.verifier.available
      ? "available" : "unavailable — " + String((scenario.verifier && scenario.verifier.reason) || "no verifier")));
    box.appendChild(pairs);

    box.appendChild(el("h4", "", "Preconditions"));
    var preconditions = el("ul", "forge-note-list");
    (scenario.preconditions || []).forEach(function (line) { preconditions.appendChild(el("li", "", line)); });
    box.appendChild(preconditions);

    box.appendChild(el("h4", "", "Metrics (definitions, not results)"));
    var metrics = el("ul", "forge-note-list");
    (scenario.metric_definitions || []).forEach(function (spec) {
      metrics.appendChild(el("li", "", spec.label + " [" + spec.unit + "] — " +
        spec.kind + ": " + spec.numerator + " / " + spec.denominator +
        (spec.kind === "unsupported" ? ". Unavailable: " + spec.method : "")));
    });
    box.appendChild(metrics);

    if ((scenario.limitations || []).length) {
      box.appendChild(el("h4", "", "Limitations"));
      var limits = el("ul", "forge-note-list");
      scenario.limitations.forEach(function (line) { limits.appendChild(el("li", "", line)); });
      box.appendChild(limits);
    }
  }

  function selectBenchmarkScenario() {
    var select = byId("forgeBenchmarkScenario");
    if (!select) return;
    var scenario = forge.benchmark.scenarios.find(function (item) {
      return item.scenario_id + "@" + item.version === select.value;
    }) || null;
    forge.benchmark.selected = scenario;
    forge.benchmark.scenarioId = select.value;
    if (scenario) {
      byId("forgeBenchmarkDuration").value = String(scenario.default_duration_seconds);
      byId("forgeBenchmarkInterval").value = String(scenario.frame_interval_seconds);
    }
    renderBenchmarkScenarioDetail(scenario);
    invalidateBenchmarkPreflight();
  }

  function renderBenchmarkPreflight(body) {
    var box = byId("forgeBenchmarkPreflight");
    if (!box) return;
    box.innerHTML = "";
    box.className = "forge-validation " + (body.valid ? "ok" : "bad");
    box.appendChild(el("p", "", body.valid
      ? "Preflight passed. The window was measured and no session was started."
      : "This run cannot start as configured:"));
    if ((body.errors || []).length) {
      var errors = el("ul", "");
      body.errors.forEach(function (problem) { errors.appendChild(el("li", "", problem)); });
      box.appendChild(errors);
    }
    if (body.target) {
      var pairs = el("div", "forge-benchmark-pairs");
      pairs.appendChild(detailRow("Window", String(body.target.title)));
      pairs.appendChild(detailRow("Measured", body.target.width + " × " + body.target.height + " px"));
      pairs.appendChild(detailRow("Budget / interval",
        (body.config ? body.config.duration_seconds : "—") + " s / " +
        (body.config ? body.config.frame_interval_seconds : "—") + " s"));
      pairs.appendChild(detailRow("Expected opportunities", body.expected_frames == null
        ? "not stated" : String(body.expected_frames)));
      box.appendChild(pairs);
    }
    if ((body.conflicts || []).length) {
      var conflicts = el("ul", "");
      body.conflicts.forEach(function (reason) { conflicts.appendChild(el("li", "", reason)); });
      box.appendChild(conflicts);
    }
    if ((body.warnings || []).length) {
      var warnings = el("ul", "forge-note-list");
      body.warnings.forEach(function (reason) { warnings.appendChild(el("li", "", reason)); });
      box.appendChild(warnings);
    }
  }

  async function preflightBenchmark() {
    var scenario = benchmarkScenario();
    if (!scenario) {
      benchmarkNotice("Choose a scenario before validating.", "error");
      return;
    }
    benchmarkNotice("Validating the scenario against the live window…");
    try {
      var body = await requestJson("/novaforge/benchmarks/preflight", benchmarkPayload());
      forge.benchmark.preflight = body;
      forge.benchmark.signature = body.valid ? benchmarkSignature() : "";
      renderBenchmarkPreflight(body);
      benchmarkNotice(body.valid ? "" : "Preflight refused the run; the reasons are listed above.",
        body.valid ? "" : "error");
      updateBenchmarkControls();
    } catch (error) {
      forge.benchmark.preflight = null;
      forge.benchmark.signature = "";
      benchmarkNotice("Preflight failed: " + requestErrorMessage(error), "error");
      updateBenchmarkControls();
    }
  }

  function benchmarkLoadWindows() {
    var select = byId("forgeBenchmarkTarget");
    if (!select) return Promise.resolve();
    return requestJson("/novaforge/windows").then(function (body) {
      var previous = select.value;
      select.innerHTML = "";
      if (!body.available) {
        var unavailable = document.createElement("option");
        unavailable.value = "";
        unavailable.textContent = body.reason || "Window enumeration is unavailable";
        select.appendChild(unavailable);
        invalidateBenchmarkPreflight();
        return;
      }
      (body.windows || []).forEach(function (row) {
        var option = document.createElement("option");
        option.value = row.title;
        option.textContent = row.title + " · " + row.process;
        select.appendChild(option);
      });
      if (!select.options.length) {
        var empty = document.createElement("option");
        empty.value = "";
        empty.textContent = "No open windows";
        select.appendChild(empty);
      } else {
        select.value = Array.prototype.some.call(select.options, function (option) {
          return option.value === previous;
        }) ? previous : select.options[0].value;
      }
      // Re-entering the tab refreshes the list; only a changed target (or a
      // list that became unavailable) invalidates the validation the user ran.
      if (select.value !== previous) invalidateBenchmarkPreflight();
    }).catch(function (error) {
      select.innerHTML = "";
      var failed = document.createElement("option");
      failed.value = "";
      failed.textContent = "Window list unavailable";
      select.appendChild(failed);
      // The selected target is no longer known to be open, so a preflight that
      // measured it cannot authorize a start.
      invalidateBenchmarkPreflight();
      benchmarkNotice("The target list could not be read: " + requestErrorMessage(error), "error");
    });
  }

  async function startBenchmark() {
    if (!forge.benchmark.preflight || forge.benchmark.signature !== benchmarkSignature()) {
      benchmarkNotice("Validate this exact configuration before starting.", "error");
      return;
    }
    benchmarkNotice("Starting the session…");
    try {
      var run = await requestJson("/novaforge/benchmarks/runs", benchmarkPayload());
      forge.benchmark.runId = run.id;
      forge.benchmark.run = run;
      forge.benchmark.runToken += 1;
      renderBenchmarkRun(run, true);
      updateBenchmarkControls();
      loadBenchmarkHistory();
      pollBenchmarkRun(run.id, forge.benchmark.runToken);
      benchmarkNotice("Run started. Its session is the only active session; the panel polls its record.");
    } catch (error) {
      benchmarkNotice("The run could not start: " + requestErrorMessage(error), "error");
      loadBenchmarkHistory();
    }
  }

  async function cancelBenchmark() {
    var run = forge.benchmark.run;
    if (!run || run.status !== "running") return;
    benchmarkNotice("Cancelling through the session's stop path…");
    try {
      var body = await requestJson(
        "/novaforge/benchmarks/runs/" + encodeURIComponent(run.id) + "/cancel", {}
      );
      forge.benchmark.run = body.run;
      renderBenchmarkRun(body.run, false);
      updateBenchmarkControls();
      loadBenchmarkHistory();
      loadBenchmarkRunDetail(body.run.id);
      // The notice follows the run's ACTUAL ending: a cancel that raced the
      // time limit leaves the run completed, and claiming "recorded as aborted"
      // would contradict the record rendered beside it.
      var finishedAs = String((body.run || {}).status || "unknown");
      benchmarkNotice(finishedAs === "aborted"
        ? "Run cancelled. It is recorded as aborted and excluded from aggregates."
        : "The run had already ended as " + finishedAs + "; no cancellation was recorded.");
    } catch (error) {
      benchmarkNotice("Cancel failed: " + requestErrorMessage(error), "error");
    }
  }

  function pollBenchmarkRun(runId, token) {
    if (token !== forge.benchmark.runToken) return;
    requestJson("/novaforge/benchmarks/runs/" + encodeURIComponent(runId)).then(function (run) {
      if (token !== forge.benchmark.runToken) return;
      forge.benchmark.run = run;
      renderBenchmarkRun(run, run.status === "running");
      updateBenchmarkControls();
      if (run.status === "running") {
        forge.benchmark.timer = setTimeout(function () {
          pollBenchmarkRun(runId, token);
        }, BENCHMARK_POLL_MS);
      } else {
        forge.benchmark.timer = null;
        loadBenchmarkHistory();
        loadBenchmarkRunDetail(runId);
      }
    }).catch(function (error) {
      if (token !== forge.benchmark.runToken) return;
      benchmarkNotice("The run's status could not be read: " + requestErrorMessage(error), "error");
      forge.benchmark.timer = setTimeout(function () {
        pollBenchmarkRun(runId, token);
      }, BENCHMARK_POLL_MS);
    });
  }

  function benchmarkTable(rows) {
    var table = el("table", "forge-benchmark-table");
    var head = el("thead", "");
    var headRow = el("tr", "");
    ["Metric", "Value", "Kind", "Source / reason"].forEach(function (labelText) {
      headRow.appendChild(el("th", "", labelText));
    });
    head.appendChild(headRow);
    table.appendChild(head);
    var body = el("tbody", "");
    rows.forEach(function (entry) {
      var tr = el("tr", "");
      tr.dataset.available = entry.metric.available ? "true" : "false";
      tr.appendChild(el("td", "", entry.label));
      tr.appendChild(el("td", "", entry.metric.text));
      tr.appendChild(el("td", "", entry.metric.kind));
      tr.appendChild(el("td", "", entry.metric.reason || entry.source || ""));
      body.appendChild(tr);
    });
    table.appendChild(body);
    return table;
  }

  function renderBenchmarkRun(run, live) {
    var box = byId("forgeBenchmarkRun");
    var badge = byId("forgeBenchmarkRunState");
    if (!box || !badge) return;
    badge.textContent = benchmarkStatusText(run);
    badge.dataset.state = String((run && run.status) || "IDLE");
    box.innerHTML = "";
    if (!run) {
      box.appendChild(el("p", "muted", "No benchmark run is active."));
      return;
    }
    var head = el("div", "forge-benchmark-head");
    head.appendChild(el("span", "forge-benchmark-version", String((run.scenario || {}).name || "scenario")));
    head.appendChild(el("span", "forge-benchmark-version", "run " + String(run.id || "")));
    if (live) head.appendChild(el("span", "forge-badge", "polling"));
    box.appendChild(head);
    box.appendChild(el("p", "", String(run.reason || (run.status === "running"
      ? "The session is active; figures update from its record."
      : "No reason was recorded."))));

    var config = run.configuration || {};
    var pairs = el("div", "forge-benchmark-pairs");
    pairs.appendChild(detailRow("Scenario", String((run.scenario || {}).scenario_id || "") +
      " v" + String((run.scenario || {}).version || "")));
    pairs.appendChild(detailRow("Budget / interval",
      String(config.duration_seconds) + " s / " + String(config.frame_interval_seconds) + " s"));
    pairs.appendChild(detailRow("Target", String((run.target || {}).title || config.target_title || "—")));
    pairs.appendChild(detailRow("Session", String(run.session_id || "starting…")));
    pairs.appendChild(detailRow("Model / policy", "unavailable — " +
      String((run.model && run.model.reason) || "no model is invoked")));
    pairs.appendChild(detailRow("Objective", run.objective && run.objective.achieved === null
      ? "not evaluated — " + String(run.objective.reason || "no verifier")
      : (run.objective && run.objective.achieved ? "achieved" : "not evaluated")));
    box.appendChild(pairs);

    var rows = Object.keys(run.metrics || {}).map(function (key) {
      var row = run.metrics[key] || {};
      return { label: row.label || key, metric: benchmarkMetric(row), source: row.source || "" };
    });
    if (rows.length) {
      box.appendChild(el("h4", "", "Recorded metrics"));
      box.appendChild(benchmarkTable(rows));
    } else {
      box.appendChild(el("p", "muted", "No metric has been recorded yet. A running session's values appear only after the backend measures them."));
    }

    var evidence = run.evidence || {};
    box.appendChild(el("h4", "", "Evidence"));
    var evidencePairs = el("div", "forge-benchmark-pairs");
    evidencePairs.appendChild(detailRow("Timeline", evidence.timeline_endpoint || "not recorded"));
    // The record keeps the newest three frame REFERENCES, not the retained frame
    // count; calling them "retained frames" claimed a number the run never held.
    evidencePairs.appendChild(detailRow("Frame references", String((evidence.frames || []).length)));
    evidencePairs.appendChild(detailRow("Timeline source", String(evidence.timeline_source || "not stated")));
    evidencePairs.appendChild(detailRow("Run persistence",
      String((run.persistence || {}).state || "not stated")));
    box.appendChild(evidencePairs);
    (evidence.gaps || []).forEach(function (gap) {
      box.appendChild(el("p", "forge-frame-note", "Evidence gap: " + gap));
    });
    var actions = el("div", "button-row");
    if (run.session_id) {
      var evidenceButton = el("button", "cyber-btn small", "Open Evidence Timeline");
      evidenceButton.type = "button";
      evidenceButton.addEventListener("click", function () { openBenchmarkEvidence(run.session_id); });
      actions.appendChild(evidenceButton);
    }
    if (run.status === "running") {
      var cancelButton = el("button", "cyber-btn small danger", "Cancel This Run");
      cancelButton.type = "button";
      cancelButton.addEventListener("click", cancelBenchmark);
      actions.appendChild(cancelButton);
    }
    if (actions.children.length) box.appendChild(actions);

    var runner = run.runner || {};
    if (runner.collection_ms != null) {
      box.appendChild(el("p", "forge-frame-note",
        "Runner: poll " + String(runner.poll_interval_ms) + " ms · " + String(runner.polls) +
        " polls · post-session collection " + String(runner.collection_ms) + " ms. " +
        "Polling is asleep; these are the runner's own overhead figures."));
    }
  }

  async function loadBenchmarkRunDetail(runId) {
    var box = byId("forgeBenchmarkRunDetail");
    if (!box) return;
    try {
      var run = await requestJson("/novaforge/benchmarks/runs/" + encodeURIComponent(runId));
      forge.benchmark.detail = run;
      box.innerHTML = "";
      var head = el("div", "forge-benchmark-head");
      head.appendChild(el("span", "forge-badge", benchmarkStatusText(run)));
      head.appendChild(el("span", "forge-benchmark-version", String(run.id)));
      box.appendChild(head);
      box.appendChild(el("p", "", String(run.reason || "No reason was recorded.")));

      // Reproducibility and honesty belong on the detail surface: what was run,
      // where it ran, what was deliberately not invoked, and what the result
      // does not claim.
      var scenario = run.scenario || {};
      var config = run.configuration || {};
      box.appendChild(el("h4", "", "Configuration"));
      var configPairs = el("div", "forge-benchmark-pairs");
      configPairs.appendChild(detailRow("Scenario",
        String(scenario.scenario_id || "—") + " v" + String(scenario.version || "—")));
      configPairs.appendChild(detailRow("Budget / interval",
        String(config.duration_seconds) + " s / " + String(config.frame_interval_seconds) + " s"));
      configPairs.appendChild(detailRow("Target",
        String((run.target || {}).title || config.target_title || "—")));
      configPairs.appendChild(detailRow("Session", String(run.session_id || "not started")));
      if (config.notes) configPairs.appendChild(detailRow("Notes", String(config.notes)));
      box.appendChild(configPairs);

      var environment = run.environment || {};
      var platformInfo = environment.platform || {};
      var targetInfo = run.target || {};
      box.appendChild(el("h4", "", "Environment"));
      var environmentPairs = el("div", "forge-benchmark-pairs");
      environmentPairs.appendChild(detailRow("Platform",
        [platformInfo.system, platformInfo.release, platformInfo.machine]
          .filter(function (part) { return part; }).join(" ") || "not recorded"));
      environmentPairs.appendChild(detailRow("Python / CPUs",
        String(platformInfo.python || "—") + " / " +
        String(platformInfo.cpu_count === null || platformInfo.cpu_count === undefined
          ? "—" : platformInfo.cpu_count)));
      environmentPairs.appendChild(detailRow("Window geometry",
        String(targetInfo.width === null || targetInfo.width === undefined ? "—" : targetInfo.width) +
        " × " +
        String(targetInfo.height === null || targetInfo.height === undefined ? "—" : targetInfo.height) +
        " px · scale " + String(targetInfo.scale === null || targetInfo.scale === undefined
          ? "—" : targetInfo.scale)));
      var usage = environment.usage || {};
      if (usage.available === false) {
        environmentPairs.appendChild(detailRow("Resource use",
          "unavailable — " + String(usage.reason || "no per-run probe is attached")));
      }
      box.appendChild(environmentPairs);

      var model = run.model || {};
      var policy = run.policy || {};
      box.appendChild(el("h4", "", "Model / policy"));
      var modelPairs = el("div", "forge-benchmark-pairs");
      modelPairs.appendChild(detailRow("Model", model.available
        ? String(model.id || "available")
        : "unavailable — " + String(model.reason || "no model is invoked")));
      modelPairs.appendChild(detailRow("Policy", policy.available
        ? String(policy.id || "available")
        : "unavailable — " + String(policy.reason || "no policy is invoked")));
      box.appendChild(modelPairs);

      box.appendChild(el("h4", "", "Metrics"));
      var rows = Object.keys(run.metrics || {}).map(function (key) {
        var row = run.metrics[key] || {};
        return { label: row.label || key, metric: benchmarkMetric(row), source: row.source || "" };
      });
      if (rows.length) box.appendChild(benchmarkTable(rows));
      else box.appendChild(el("p", "muted", "This run recorded no metrics (it may not have started)."));
      box.appendChild(el("p", "forge-frame-note",
        "CPU, memory, GPU/NPU use and objective success are unavailable in NF-04: no per-run probe or verifier exists. They are reported as unavailable, never as zero."));

      var evidence = run.evidence || {};
      box.appendChild(el("h4", "", "Evidence"));
      var evidencePairs = el("div", "forge-benchmark-pairs");
      evidencePairs.appendChild(detailRow("Timeline", evidence.timeline_endpoint || "not recorded"));
      evidencePairs.appendChild(detailRow("Timeline events", String(evidence.timeline_events == null ? "not stated" : evidence.timeline_events)));
      evidencePairs.appendChild(detailRow("Persistence", String((evidence.persistence || {}).state || "not stated")));
      evidencePairs.appendChild(detailRow("Run persistence",
        String((run.persistence || {}).state || "not stated")));
      box.appendChild(evidencePairs);
      var frames = el("ul", "forge-note-list");
      (evidence.frames || []).forEach(function (frame) {
        frames.appendChild(el("li", "", "Frame #" + frame.seq + " · " + formatTime(frame.captured_at) +
          " · " + String(frame.endpoint || "")));
      });
      if (!(evidence.frames || []).length) frames.appendChild(el("li", "muted", "No frame reference was retained."));
      box.appendChild(frames);
      (evidence.gaps || []).forEach(function (gap) {
        box.appendChild(el("p", "forge-frame-note", "Evidence gap: " + gap));
      });
      if ((run.errors || []).length) {
        box.appendChild(el("h4", "", "Session errors"));
        var errors = el("ul", "");
        run.errors.forEach(function (entry) {
          errors.appendChild(el("li", "", String(entry.message || entry.error || "error")));
        });
        box.appendChild(errors);
      }
      if ((run.limitations || []).length) {
        box.appendChild(el("h4", "", "Limitations"));
        var limitations = el("ul", "forge-note-list");
        run.limitations.forEach(function (line) {
          limitations.appendChild(el("li", "", String(line)));
        });
        box.appendChild(limitations);
      }
      var exportButton = byId("forgeBenchmarkExportButton");
      if (exportButton) exportButton.disabled = false;
      updateBenchmarkControls();
    } catch (error) {
      box.innerHTML = "";
      box.appendChild(el("p", "muted", "The run could not be read: " + requestErrorMessage(error)));
    }
  }

  function benchmarkRunRow(run) {
    var row = el("div", "forge-benchmark-run-row");
    row.dataset.status = String(run.status || "");
    var main = el("div", "forge-benchmark-run-main");
    var head = el("div", "forge-benchmark-head");
    var badge = el("span", "forge-badge", benchmarkStatusText(run));
    badge.dataset.ok = run.status === "completed" && run.valid ? "true" : "false";
    head.appendChild(badge);
    head.appendChild(el("span", "muted", formatDate(run.created_at)));
    main.appendChild(head);
    main.appendChild(el("p", "", String((run.scenario || {}).name || (run.scenario || {}).scenario_id || "scenario") +
      " · " + String(run.target_title || "no target")));
    var metrics = run.metrics || {};
    main.appendChild(el("p", "forge-frame-note",
      "frames " + metricText(metrics.frames, "") +
      " · latency " + metricText(metrics.latency_median_ms, "ms") +
      " · yield " + metricText(metrics.frame_yield, "") +
      " · " + formatDuration(run.duration_seconds)));
    if (run.reason) main.appendChild(el("p", "muted", String(run.reason)));
    row.appendChild(main);
    var open = el("button", "cyber-btn small", "Open");
    open.type = "button";
    open.addEventListener("click", function () { loadBenchmarkRunDetail(run.id); });
    row.appendChild(open);
    return row;
  }

  function fillBenchmarkCompareOptions(runs) {
    ["forgeBenchmarkCompareLeft", "forgeBenchmarkCompareRight"].forEach(function (id) {
      var select = byId(id);
      if (!select) return;
      var previous = select.value;
      select.innerHTML = "";
      runs.forEach(function (run) {
        var option = document.createElement("option");
        option.value = run.id;
        option.textContent = formatDate(run.created_at) + " · " +
          String((run.scenario || {}).name || (run.scenario || {}).scenario_id || "run") +
          " · " + benchmarkStatusText(run);
        select.appendChild(option);
      });
      if (Array.prototype.some.call(select.options, function (option) { return option.value === previous; })) {
        select.value = previous;
      }
    });
    // Two distinct sides are the only useful default; leaving both selects on
    // the newest run made Compare refuse its own default selection.
    var leftSelect = byId("forgeBenchmarkCompareLeft");
    var rightSelect = byId("forgeBenchmarkCompareRight");
    if (leftSelect && rightSelect && runs.length >= 2) {
      if (!leftSelect.value) leftSelect.value = runs[0].id;
      if (!rightSelect.value || rightSelect.value === leftSelect.value) {
        var other = runs.find(function (run) { return run.id !== leftSelect.value; });
        if (other) rightSelect.value = other.id;
      }
    }
    var button = byId("forgeBenchmarkCompareButton");
    if (button) button.disabled = runs.length < 2;
  }

  async function loadBenchmarkHistory() {
    var list = byId("forgeBenchmarkHistoryList");
    if (!list) return;
    try {
      var body = await requestJson("/novaforge/benchmarks/runs?limit=20");
      forge.benchmark.history = body.runs || [];
      list.innerHTML = "";
      if (!forge.benchmark.history.length) {
        list.appendChild(el("p", "muted", "No benchmark run has been recorded yet."));
      } else {
        forge.benchmark.history.forEach(function (run) { list.appendChild(benchmarkRunRow(run)); });
      }
      fillBenchmarkCompareOptions(forge.benchmark.history);
      if (body.active_run_id && (!forge.benchmark.run || forge.benchmark.run.id !== body.active_run_id)) {
        forge.benchmark.runToken += 1;
        pollBenchmarkRun(body.active_run_id, forge.benchmark.runToken);
      }
    } catch (error) {
      list.innerHTML = "";
      list.appendChild(el("p", "muted", "Benchmark history could not be read: " + requestErrorMessage(error)));
    }
  }

  async function aggregateBenchmarks() {
    var scenario = benchmarkScenario();
    var box = byId("forgeBenchmarkAggregate");
    var badge = byId("forgeBenchmarkAggregateState");
    if (!box || !scenario) return;
    if (badge) badge.textContent = "computing";
    try {
      var result = await requestJson(
        "/novaforge/benchmarks/aggregate?scenario=" + encodeURIComponent(scenario.scenario_id) +
        "&version=" + encodeURIComponent(scenario.version)
      );
      box.innerHTML = "";
      if (badge) {
        // sample_size is the cohort, not every eligible run: a configuration
        // split makes those two figures differ on purpose.
        badge.textContent = result.sample_size + " sample / " + result.attempts + " attempts";
        badge.dataset.ok = result.sample_size ? "true" : "false";
      }
      var pairs = el("div", "forge-benchmark-pairs");
      pairs.appendChild(detailRow("Attempts", String(result.attempts)));
      pairs.appendChild(detailRow("Eligible", String(result.eligible)));
      pairs.appendChild(detailRow("Sample size", String(result.sample_size)));
      var cohort = result.cohort || {};
      var cohortConfig = cohort.configuration || {};
      if (cohortConfig.scenario_id) {
        pairs.appendChild(detailRow("Cohort", String(cohortConfig.scenario_id) +
          " v" + String(cohortConfig.version) + " · " + String(cohortConfig.duration_seconds) +
          " s / " + String(cohortConfig.frame_interval_seconds) + " s · " +
          String((cohort.window || {}).width || "—") + " × " +
          String((cohort.window || {}).height || "—") + " px · " +
          String(cohort.platform_system || "platform not stated")));
      }
      Object.keys(result.status_counts || {}).forEach(function (status) {
        pairs.appendChild(detailRow(status, String(result.status_counts[status])));
      });
      box.appendChild(pairs);
      if (result.mixed_configurations) {
        box.appendChild(el("p", "forge-frame-note",
          "Only the cohort above is pooled: " + String(result.configuration_excluded_count || 0) +
          " completed run(s) recorded on another configuration are excluded rather than " +
          "averaged into an incomparable figure."));
      }
      box.appendChild(el("p", "muted", String(result.variability || "")));
      box.appendChild(el("p", "forge-frame-note", String(result.denominator_policy || "")));
      box.appendChild(el("p", "forge-frame-note", String(result.objective_note || "")));

      var table = el("table", "forge-benchmark-table");
      var head = el("thead", "");
      var headRow = el("tr", "");
      ["Metric", "Median", "n", "min", "max", "stdev"].forEach(function (labelText) {
        headRow.appendChild(el("th", "", labelText));
      });
      head.appendChild(headRow);
      table.appendChild(head);
      var body = el("tbody", "");
      Object.keys(result.metrics || {}).forEach(function (key) {
        var stats = result.metrics[key] || {};
        var tr = el("tr", "");
        tr.dataset.available = stats.available ? "true" : "false";
        tr.appendChild(el("td", "", String(stats.label || key) + (stats.unit ? " [" + stats.unit + "]" : "")));
        tr.appendChild(el("td", "", stats.available
          ? metricText(stats.value, stats.unit)
          : "unavailable — " + String(stats.reason || "no reason recorded")));
        tr.appendChild(el("td", "", String(stats.n || 0)));
        tr.appendChild(el("td", "", stats.available ? metricText(stats.min, stats.unit) : (stats.reason || "")));
        tr.appendChild(el("td", "", stats.available ? metricText(stats.max, stats.unit) : "—"));
        tr.appendChild(el("td", "", stats.stdev === null || stats.stdev === undefined
          ? (stats.n < 2 ? "needs 2+ runs" : "unavailable") : String(stats.stdev)));
        body.appendChild(tr);
      });
      table.appendChild(body);
      box.appendChild(table);

      if ((result.excluded || []).length) {
        box.appendChild(el("h4", "", "Excluded attempts"));
        var excluded = el("ul", "forge-note-list");
        result.excluded.forEach(function (run) {
          excluded.appendChild(el("li", "", String(run.run_id) + " · " + String(run.status) +
            " · " + String(run.reason || "no reason")));
        });
        box.appendChild(excluded);
      }
    } catch (error) {
      box.innerHTML = "";
      if (badge) { badge.textContent = "unavailable"; badge.dataset.ok = "false"; }
      box.appendChild(el("p", "muted", "The aggregate could not be read: " + requestErrorMessage(error)));
    }
  }

  async function compareBenchmarks() {
    var left = byId("forgeBenchmarkCompareLeft");
    var right = byId("forgeBenchmarkCompareRight");
    var box = byId("forgeBenchmarkCompareOutput");
    if (!left || !right || !box) return;
    if (!left.value || !right.value) {
      box.innerHTML = "";
      box.appendChild(el("p", "muted", "Choose a run on both sides."));
      return;
    }
    if (left.value === right.value) {
      box.innerHTML = "";
      box.appendChild(el("p", "muted", "A run cannot be compared with itself."));
      return;
    }
    try {
      var result = await requestJson(
        "/novaforge/benchmarks/compare?left=" + encodeURIComponent(left.value) +
        "&right=" + encodeURIComponent(right.value)
      );
      box.innerHTML = "";
      var head = el("div", "forge-benchmark-head");
      var badge = el("span", "forge-badge", result.compatible ? "compatible" : "not comparable");
      badge.dataset.ok = result.compatible ? "true" : "false";
      head.appendChild(badge);
      head.appendChild(el("span", "muted", "samples " + result.sample.left + " / " + result.sample.right));
      box.appendChild(head);
      (result.reasons || []).forEach(function (reason) {
        box.appendChild(el("p", "forge-benchmark-unsupported", reason));
      });
      (result.warnings || []).forEach(function (reason) {
        box.appendChild(el("p", "forge-frame-note", "Warning: " + reason));
      });
      box.appendChild(el("p", "", benchmarkVerdictText(result.verdict)));
      box.appendChild(el("p", "forge-frame-note", String(result.note || "")));

      var table = el("table", "forge-benchmark-table");
      var thead = el("thead", "");
      var headRow = el("tr", "");
      ["Metric", "Left", "Right", "Delta"].forEach(function (labelText) {
        headRow.appendChild(el("th", "", labelText));
      });
      thead.appendChild(headRow);
      table.appendChild(thead);
      var tbody = el("tbody", "");
      (result.metrics || []).forEach(function (row) {
        var leftMetric = benchmarkMetric(Object.assign({}, row.left || {},
          { label: row.label, unit: row.unit }));
        var rightMetric = benchmarkMetric(Object.assign({}, row.right || {},
          { label: row.label, unit: row.unit }));
        var tr = el("tr", "");
        tr.dataset.available = leftMetric.available && rightMetric.available ? "true" : "false";
        tr.appendChild(el("td", "", String(row.label || row.key) + (row.unit ? " [" + row.unit + "]" : "")));
        tr.appendChild(el("td", "", leftMetric.available ? leftMetric.text : "unavailable — " + leftMetric.reason));
        tr.appendChild(el("td", "", rightMetric.available ? rightMetric.text : "unavailable — " + rightMetric.reason));
        tr.appendChild(el("td", "", row.delta === null || row.delta === undefined
          ? String(row.delta_reason || "—") : benchmarkDeltaText(row)));
        tbody.appendChild(tr);
      });
      table.appendChild(tbody);
      box.appendChild(table);

      var evidence = el("div", "forge-benchmark-pairs");
      evidence.appendChild(detailRow("Left evidence", String(((result.left || {}).evidence || {}).timeline_endpoints || "none")));
      evidence.appendChild(detailRow("Right evidence", String(((result.right || {}).evidence || {}).timeline_endpoints || "none")));
      box.appendChild(evidence);
    } catch (error) {
      box.innerHTML = "";
      box.appendChild(el("p", "muted", "The comparison was refused: " + requestErrorMessage(error)));
    }
  }

  function exportBenchmarkRun() {
    var run = forge.benchmark.detail;
    if (!run) return;
    var name = "novaforge-benchmark-" +
      String((run.scenario || {}).scenario_id || "run") + "-" + String(run.id || "result") + ".json";
    var blob = new Blob([JSON.stringify(run, null, 2)], { type: "application/json" });
    var url = URL.createObjectURL(blob);
    var link = document.createElement("a");
    link.href = url;
    link.download = name;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  }

  function openBenchmarkEvidence(sessionId) {
    forge.benchmark.pendingEvidence = String(sessionId || (forge.benchmark.run && forge.benchmark.run.session_id) || "");
    switchTab("forgeTimelineView");
  }

  function openBenchmarks() {
    // The target list is refreshed on entry: a benchmark never launches a target,
    // so the operator must pick one that is open right now. The catalog is only
    // re-rendered when it was never loaded: rendering it again reset the budget,
    // interval and validated preflight merely because the tab was re-opened.
    benchmarkLoadWindows();
    if (!forge.benchmark.scenarios.length) loadBenchmarkCatalog();
    loadBenchmarkHistory();
  }

  /* ── NF-05: Controlled Game Evaluation ────────────────────────
   *
   * An evaluation is a compatibility record around ONE NF-04 benchmark run.
   * This panel starts nothing until readiness passes AND the operator ticks an
   * explicit confirmation; it never launches a game, sends input or claims an
   * objective. Every figure and every reason comes from the backend record.
   */

  var EVALUATION_POLL_MS = 1000;

  function evaluationNotice(message, kind) {
    var box = byId("forgeEvaluationNotice");
    if (!box) return;
    if (!message) {
      box.hidden = true;
      box.textContent = "";
      box.className = "forge-notice";
      return;
    }
    box.hidden = false;
    box.className = "forge-notice" + (kind === "error" ? " error" : "");
    box.textContent = message;
  }

  function evaluationGame() {
    if (forge.evaluation.selected) return forge.evaluation.selected;
    var select = byId("forgeEvaluationGame");
    if (!select) return null;
    return forge.evaluation.games.find(function (item) {
      return item.game_id + "@" + item.version === select.value;
    }) || null;
  }

  function evaluationScenario() {
    var select = byId("forgeEvaluationScenario");
    if (!select) return null;
    return forge.evaluation.scenarios.find(function (item) {
      return item.scenario_id + "@" + item.version === select.value;
    }) || null;
  }

  function evaluationPayload() {
    var game = evaluationGame();
    var scenario = evaluationScenario();
    var duration = byId("forgeEvaluationDuration");
    var interval = byId("forgeEvaluationInterval");
    var target = byId("forgeEvaluationTarget");
    var notes = byId("forgeEvaluationNotes");
    var offline = byId("forgeEvaluationOffline");
    var baseline = byId("forgeEvaluationBaseline");
    return {
      game_id: game ? game.game_id : "",
      game_version: game ? game.version : null,
      scenario_id: scenario ? scenario.scenario_id : "",
      scenario_version: scenario ? scenario.version : null,
      target_title: target ? String(target.value || "").trim() : "",
      duration_seconds: duration && duration.value !== "" ? Number(duration.value) : null,
      frame_interval_seconds: interval && interval.value !== "" ? Number(interval.value) : null,
      notes: notes ? String(notes.value || "").trim() : "",
      confirm_offline: offline ? offline.checked === true : false,
      baseline_id: baseline ? String(baseline.value || "") : "",
    };
  }

  function evaluationSignature() {
    var data = evaluationPayload();
    // The baseline choice does not change readiness, so selecting one must not
    // force a new validation; the configuration and offline declaration do.
    delete data.baseline_id;
    return JSON.stringify(data);
  }

  // Start needs a PASSING readiness verdict for THIS configuration, no active
  // run, and the operator's explicit confirmation. Absence of a token is never
  // treated as consent.
  function evaluationCanStart(preflight, run, confirmed, signatureMatches) {
    return Boolean(
      preflight && preflight.ready === true &&
      !(run && run.status === "running") &&
      confirmed === true &&
      signatureMatches !== false
    );
  }

  function evaluationStatusText(run) {
    if (!run) return "no evaluation";
    var status = String(run.status || "unknown");
    if (status === "running") return "running";
    if (status === "completed") {
      return run.valid ? "completed · data complete" : "completed · invalid";
    }
    if (status === "unsupported") return "unsupported · not run";
    if (status === "invalid") return "invalid · excluded";
    if (status === "aborted") return "aborted · excluded";
    if (status === "failed") return "failed · excluded";
    return status;
  }

  function readinessStateText(check) {
    if (!check) return "unavailable";
    var state = String(check.status || "unavailable");
    return state + (check.required === false ? " · not required" : "");
  }

  // A readiness verdict that was never recorded is NOT a zero count: a legacy or
  // partial record must say so instead of reading like a measured "0 pass".
  function readinessCountsText(readiness) {
    var counts = (readiness || {}).counts || {};
    var keys = Object.keys(counts);
    if (!keys.length) return "not recorded for this evaluation";
    return keys.map(function (key) {
      var value = counts[key];
      return String(value == null ? "?" : value) + " " + key;
    }).join(" · ");
  }

  function evaluationBaselineText(baseline) {
    if (!baseline) return "No baseline information was returned.";
    if (baseline.available) {
      var config = baseline.configuration || {};
      return "Baseline " + String(baseline.evaluation_id || "unnamed") +
        " · " + String(config.duration_seconds == null ? "—" : config.duration_seconds) +
        " s / " + String(config.frame_interval_seconds == null ? "—" : config.frame_interval_seconds) +
        " s · " + String(baseline.samples == null ? "?" : baseline.samples) + " completed run(s)";
    }
    return String(baseline.reason || "No baseline has been recorded yet.");
  }

  function evaluationComparisonText(comparison) {
    if (!comparison) return "This evaluation is the baseline; there is nothing to compare it with yet.";
    if (comparison.available === false) {
      return "Not compared — " + String((comparison.reasons || ["no reason recorded"]).join(" "));
    }
    if (!comparison.compatible) {
      return "Baseline incompatible — " + String((comparison.reasons || []).join(" "));
    }
    return "Compatible with the baseline · " + benchmarkVerdictText(comparison.verdict);
  }

  function updateEvaluationControls() {
    var start = byId("forgeEvaluationStartButton");
    var cancel = byId("forgeEvaluationCancelButton");
    var report = byId("forgeEvaluationReportButton");
    var exportButton = byId("forgeEvaluationExportButton");
    var confirm = byId("forgeEvaluationConfirm");
    var run = forge.evaluation.run;
    if (start) {
      start.disabled = !evaluationCanStart(
        forge.evaluation.preflight,
        run,
        confirm ? confirm.checked === true : false,
        forge.evaluation.signature === evaluationSignature()
      );
      start.title = forge.evaluation.preflight && forge.evaluation.preflight.ready
        ? (confirm && confirm.checked ? "Start this validated evaluation" : "Confirm the evaluation first")
        : "Validate readiness against a live window first";
    }
    if (cancel) cancel.disabled = !(run && run.status === "running");
    if (report) report.disabled = !(run && run.status !== "running");
    if (exportButton) exportButton.disabled = !forge.evaluation.report;
  }

  function invalidateEvaluationPreflight() {
    forge.evaluation.preflight = null;
    forge.evaluation.signature = "";
    var box = byId("forgeEvaluationPreflight");
    if (box) {
      box.innerHTML = "";
      box.className = "forge-validation";
      box.appendChild(el("p", "muted", "Configuration changed — validate readiness again before starting."));
    }
    var start = byId("forgeEvaluationStartButton");
    if (start) start.disabled = true;
    updateEvaluationControls();
  }

  function renderEvaluationRegistry() {
    var select = byId("forgeEvaluationGame");
    if (!select) return;
    var previous = forge.evaluation.selected ?
      forge.evaluation.selected.game_id + "@" + forge.evaluation.selected.version : select.value;
    select.innerHTML = "";
    forge.evaluation.games.forEach(function (game) {
      var option = document.createElement("option");
      option.value = game.game_id + "@" + game.version;
      option.textContent = game.display_name + " (v" + game.version + ") — " + game.status;
      select.appendChild(option);
    });
    if (!forge.evaluation.games.length) {
      var empty = document.createElement("option");
      empty.value = "";
      empty.textContent = "No environment registry available";
      select.appendChild(empty);
      return;
    }
    var keys = forge.evaluation.games.map(function (game) {
      return game.game_id + "@" + game.version;
    });
    select.value = keys.indexOf(previous) >= 0 ? previous : keys[0];
    selectEvaluationGame();
  }

  async function loadEvaluationRegistry() {
    var badge = byId("forgeEvaluationRegistryState");
    if (!badge) return;
    badge.textContent = "loading";
    badge.dataset.ok = "pending";
    try {
      var body = await requestJson("/novaforge/games");
      forge.evaluation.games = body.games || [];
      renderEvaluationRegistry();
      badge.textContent = body.runnable_count + " runnable · " +
        body.observation_only_count + " observation-only · " +
        body.controllable_count + " controllable";
      badge.dataset.ok = "true";
      if (!body.controllable_count) {
        evaluationNotice("No environment is controllable in this build: every runnable entry is observation-only and no objective verifier exists.");
      }
    } catch (error) {
      badge.textContent = "unavailable";
      badge.dataset.ok = "false";
      var detail = byId("forgeEvaluationGameDetail");
      if (detail) {
        detail.innerHTML = "";
        detail.appendChild(el("p", "muted", "The registry could not be read: " + requestErrorMessage(error)));
      }
      evaluationNotice("Environment registry unavailable: " + requestErrorMessage(error), "error");
    }
  }

  function renderEvaluationScenarios(scenarios, preferred) {
    var select = byId("forgeEvaluationScenario");
    if (!select) return;
    forge.evaluation.scenarios = scenarios || [];
    select.innerHTML = "";
    forge.evaluation.scenarios.forEach(function (scenario) {
      var option = document.createElement("option");
      option.value = scenario.scenario_id + "@" + scenario.version;
      option.textContent = scenario.name + " (v" + scenario.version + ")";
      select.appendChild(option);
    });
    if (!forge.evaluation.scenarios.length) {
      var empty = document.createElement("option");
      empty.value = "";
      empty.textContent = "No scenario is available for this environment";
      select.appendChild(empty);
      return;
    }
    var keys = forge.evaluation.scenarios.map(function (scenario) {
      return scenario.scenario_id + "@" + scenario.version;
    });
    select.value = preferred && keys.indexOf(preferred) >= 0 ? preferred : keys[0];
    applyEvaluationScenarioDefaults();
  }

  function applyEvaluationScenarioDefaults() {
    var scenario = evaluationScenario();
    if (!scenario) return;
    var duration = byId("forgeEvaluationDuration");
    var interval = byId("forgeEvaluationInterval");
    if (duration) duration.value = String(scenario.default_duration_seconds);
    if (interval) interval.value = String(scenario.frame_interval_seconds);
  }

  function renderEvaluationGameDetail(detail) {
    var box = byId("forgeEvaluationGameDetail");
    if (!box) return;
    box.innerHTML = "";
    if (!detail) {
      box.appendChild(el("p", "muted", "No environment is selected."));
      return;
    }
    var head = el("div", "forge-benchmark-head");
    var badge = el("span", "forge-badge", String(detail.status));
    badge.dataset.ok = detail.runnable ? "true" : "false";
    head.appendChild(badge);
    head.appendChild(el("span", "forge-benchmark-version", "v" + String(detail.version) +
      " · " + (detail.control_capable ? "controllable" : "observation only")));
    box.appendChild(head);
    box.appendChild(el("p", detail.runnable ? "" : "forge-benchmark-unsupported",
      String(detail.status_reason || "No compatibility reason was recorded.")));

    var pairs = el("div", "forge-benchmark-pairs");
    pairs.appendChild(detailRow("Systems", (detail.supported_systems || []).join(", ") || "—"));
    pairs.appendChild(detailRow("Modes", (detail.evaluation_modes || []).join(", ") || "none"));
    pairs.appendChild(detailRow("Capture", detail.capture && detail.capture.available ? "available" : "unavailable"));
    pairs.appendChild(detailRow("Input", detail.input && detail.input.available
      ? "available" : "unavailable — " + String((detail.input || {}).reason || "no input path")));
    pairs.appendChild(detailRow("Launch", detail.launch && detail.launch.available
      ? "available" : "unavailable — " + String((detail.launch || {}).reason || "never launched")));
    pairs.appendChild(detailRow("Objective verifier", detail.objective_verification && detail.objective_verification.available
      ? "available" : "unavailable — " + String((detail.objective_verification || {}).reason || "no verifier")));
    pairs.appendChild(detailRow("Offline classification", String((detail.offline || {}).classification || "not stated")));
    box.appendChild(pairs);
    box.appendChild(el("p", "forge-frame-note", String((detail.offline || {}).evidence || "")));

    if ((detail.known_limitations || []).length) {
      box.appendChild(el("h4", "", "Known limitations"));
      var limits = el("ul", "forge-note-list");
      detail.known_limitations.forEach(function (line) { limits.appendChild(el("li", "", String(line))); });
      box.appendChild(limits);
    }
    if ((detail.validation || []).length) {
      box.appendChild(el("p", "forge-benchmark-unsupported",
        "Registry validation: " + detail.validation.join(" ")));
    }
  }

  async function selectEvaluationGame() {
    var select = byId("forgeEvaluationGame");
    if (!select) return;
    var game = forge.evaluation.games.find(function (item) {
      return item.game_id + "@" + item.version === select.value;
    }) || null;
    forge.evaluation.selected = game;
    if (!game) {
      forge.evaluation.detail = null;
      renderEvaluationGameDetail(null);
      renderEvaluationScenarios([], "");
      invalidateEvaluationPreflight();
      return;
    }
    try {
      var detail = await requestJson(
        "/novaforge/games/" + encodeURIComponent(game.game_id) +
        "?version=" + encodeURIComponent(game.version)
      );
      forge.evaluation.detail = detail;
      renderEvaluationGameDetail(detail);
      renderEvaluationScenarios(detail.scenarios || [], "");
      var offlineRow = byId("forgeEvaluationOfflineRow");
      if (offlineRow) offlineRow.hidden = !(detail.offline && detail.offline.confirmation_required);
      var offline = byId("forgeEvaluationOffline");
      if (offline && offlineRow && offlineRow.hidden) offline.checked = false;
    } catch (error) {
      forge.evaluation.detail = null;
      renderEvaluationGameDetail(null);
      renderEvaluationScenarios([], "");
      evaluationNotice("The environment detail could not be read: " + requestErrorMessage(error), "error");
    }
    invalidateEvaluationPreflight();
  }

  function evaluationLoadWindows() {
    var select = byId("forgeEvaluationTarget");
    if (!select) return Promise.resolve();
    return requestJson("/novaforge/windows").then(function (body) {
      var previous = select.value;
      select.innerHTML = "";
      if (!body.available) {
        var unavailable = document.createElement("option");
        unavailable.value = "";
        unavailable.textContent = body.reason || "Window enumeration is unavailable";
        select.appendChild(unavailable);
        invalidateEvaluationPreflight();
        return;
      }
      (body.windows || []).forEach(function (row) {
        var option = document.createElement("option");
        option.value = row.title;
        option.textContent = row.title + " · " + row.process;
        select.appendChild(option);
      });
      if (!select.options.length) {
        var empty = document.createElement("option");
        empty.value = "";
        empty.textContent = "No open windows";
        select.appendChild(empty);
      } else {
        select.value = Array.prototype.some.call(select.options, function (option) {
          return option.value === previous;
        }) ? previous : select.options[0].value;
      }
      if (select.value !== previous) invalidateEvaluationPreflight();
    }).catch(function (error) {
      select.innerHTML = "";
      var failed = document.createElement("option");
      failed.value = "";
      failed.textContent = "Window list unavailable";
      select.appendChild(failed);
      invalidateEvaluationPreflight();
      evaluationNotice("The target list could not be read: " + requestErrorMessage(error), "error");
    });
  }

  function renderEvaluationBaseline(baseline) {
    var box = byId("forgeEvaluationBaselineCard");
    if (!box) return;
    box.innerHTML = "";
    box.appendChild(el("p", baseline && baseline.available ? "" : "muted",
      evaluationBaselineText(baseline)));
    if (baseline && baseline.policy) {
      box.appendChild(el("p", "forge-frame-note", String(baseline.policy)));
    }
  }

  function renderEvaluationPreflight(body) {
    var box = byId("forgeEvaluationPreflight");
    if (!box) return;
    box.innerHTML = "";
    box.className = "forge-validation " + (body.ready ? "ok" : "bad");
    box.appendChild(el("p", "", body.ready
      ? "Readiness passed. Every required check was judged against the live window; nothing was started."
      : "This evaluation cannot start as configured:"));
    if ((body.errors || []).length) {
      var errors = el("ul", "");
      body.errors.forEach(function (problem) { errors.appendChild(el("li", "", String(problem))); });
      box.appendChild(errors);
    }
    var list = el("div", "forge-readiness-list");
    (body.checks || []).forEach(function (check) {
      var row = el("div", "forge-readiness-row");
      row.dataset.status = String(check.status || "unavailable");
      row.appendChild(el("span", "forge-readiness-state", readinessStateText(check)));
      var main = el("div", "forge-benchmark-run-main");
      main.appendChild(el("p", "", String(check.label || check.id || "check")));
      if (check.evidence) main.appendChild(el("p", "forge-frame-note", String(check.evidence)));
      if (check.reason) main.appendChild(el("p", "muted", String(check.reason)));
      row.appendChild(main);
      list.appendChild(row);
    });
    box.appendChild(list);
    if ((body.blocking || []).length) {
      box.appendChild(el("p", "forge-benchmark-unsupported",
        "Blocking checks: " + body.blocking.join(", ")));
    }
    if (body.target) {
      var pairs = el("div", "forge-benchmark-pairs");
      pairs.appendChild(detailRow("Window", String(body.target.title)));
      pairs.appendChild(detailRow("Identity", "handle " + String(body.target.handle) +
        " · pid " + String(body.target.pid)));
      pairs.appendChild(detailRow("Measured", body.target.width + " × " + body.target.height + " px"));
      box.appendChild(pairs);
    }
    renderEvaluationBaseline(body.baseline);
    if (body.note) box.appendChild(el("p", "forge-frame-note", String(body.note)));
  }

  async function preflightEvaluation() {
    var game = evaluationGame();
    if (!game) {
      evaluationNotice("Choose an environment before validating readiness.", "error");
      return;
    }
    evaluationNotice("Judging readiness against the live window…");
    try {
      var body = await requestJson("/novaforge/evaluations/preflight", evaluationPayload());
      forge.evaluation.preflight = body;
      forge.evaluation.signature = body.ready ? evaluationSignature() : "";
      renderEvaluationPreflight(body);
      evaluationNotice(body.ready ? "" : "Readiness refused the evaluation; the blocking checks are listed above.",
        body.ready ? "" : "error");
      updateEvaluationControls();
    } catch (error) {
      forge.evaluation.preflight = null;
      forge.evaluation.signature = "";
      evaluationNotice("Readiness could not be judged: " + requestErrorMessage(error), "error");
      updateEvaluationControls();
    }
  }

  function evaluationStartPayload() {
    var payload = evaluationPayload();
    payload.confirm = true;
    var identity = (forge.evaluation.preflight || {}).identity || {};
    if (identity.handle != null) payload.expected_handle = identity.handle;
    if (identity.pid != null) payload.expected_pid = identity.pid;
    return payload;
  }

  async function startEvaluation() {
    var confirm = byId("forgeEvaluationConfirm");
    if (!evaluationCanStart(
      forge.evaluation.preflight,
      forge.evaluation.run,
      confirm ? confirm.checked === true : false,
      forge.evaluation.signature === evaluationSignature()
    )) {
      evaluationNotice("Validate readiness and confirm this exact configuration before starting.", "error");
      return;
    }
    evaluationNotice("Starting the bounded observation session…");
    try {
      var run = await requestJson("/novaforge/evaluations", evaluationStartPayload());
      forge.evaluation.evaluationId = run.id;
      forge.evaluation.run = run;
      forge.evaluation.report = null;
      forge.evaluation.runToken += 1;
      renderEvaluationRun(run, true);
      updateEvaluationControls();
      loadEvaluationHistory();
      pollEvaluation(run.id, forge.evaluation.runToken);
      evaluationNotice("Evaluation started. No gameplay input is sent and no objective is verified.");
    } catch (error) {
      evaluationNotice("The evaluation could not start: " + requestErrorMessage(error), "error");
      loadEvaluationHistory();
    }
  }

  async function cancelEvaluation() {
    var run = forge.evaluation.run;
    if (!run || run.status !== "running") return;
    evaluationNotice("Cancelling through the session's stop path…");
    try {
      var body = await requestJson(
        "/novaforge/evaluations/" + encodeURIComponent(run.id) + "/cancel", {}
      );
      forge.evaluation.run = body.evaluation;
      renderEvaluationRun(body.evaluation, false);
      updateEvaluationControls();
      loadEvaluationHistory();
      var finishedAs = String((body.evaluation || {}).status || "unknown");
      evaluationNotice(finishedAs === "aborted"
        ? "Evaluation cancelled. It is recorded as aborted and excluded from baselines."
        : "The evaluation had already ended as " + finishedAs + "; no cancellation was recorded.");
    } catch (error) {
      evaluationNotice("Cancel failed: " + requestErrorMessage(error), "error");
    }
  }

  function pollEvaluation(evaluationId, token) {
    if (token !== forge.evaluation.runToken) return;
    requestJson("/novaforge/evaluations/" + encodeURIComponent(evaluationId)).then(function (run) {
      if (token !== forge.evaluation.runToken) return;
      forge.evaluation.run = run;
      renderEvaluationRun(run, run.status === "running");
      updateEvaluationControls();
      if (run.status === "running") {
        forge.evaluation.timer = setTimeout(function () {
          pollEvaluation(evaluationId, token);
        }, EVALUATION_POLL_MS);
      } else {
        forge.evaluation.timer = null;
        loadEvaluationHistory();
      }
    }).catch(function (error) {
      if (token !== forge.evaluation.runToken) return;
      evaluationNotice("The evaluation's status could not be read: " + requestErrorMessage(error), "error");
      forge.evaluation.timer = setTimeout(function () {
        pollEvaluation(evaluationId, token);
      }, EVALUATION_POLL_MS);
    });
  }

  function renderEvaluationRun(run, live) {
    var box = byId("forgeEvaluationRun");
    var badge = byId("forgeEvaluationRunState");
    if (!box || !badge) return;
    badge.textContent = evaluationStatusText(run);
    badge.dataset.state = String((run && run.status) || "IDLE");
    box.innerHTML = "";
    if (!run) {
      box.appendChild(el("p", "muted", "No evaluation is active. Starting one uses the same one-session slot as Mission Control."));
      return;
    }
    var head = el("div", "forge-benchmark-head");
    head.appendChild(el("span", "forge-benchmark-version",
      String((run.game || {}).display_name || "environment") + " · " +
      String((run.scenario || {}).name || "scenario")));
    head.appendChild(el("span", "forge-benchmark-version", "evaluation " + String(run.id || "")));
    if (live) head.appendChild(el("span", "forge-badge", "polling"));
    box.appendChild(head);
    box.appendChild(el("p", "", String(run.reason || (run.status === "running"
      ? "The observation is active; evidence is finalized when it ends."
      : "No reason was recorded."))));

    var pairs = el("div", "forge-benchmark-pairs");
    pairs.appendChild(detailRow("Mode", String(run.mode || "observation_only")));
    pairs.appendChild(detailRow("Benchmark run", String(run.benchmark_run_id || "not started")));
    pairs.appendChild(detailRow("Session", String(run.session_id || "not started")));
    pairs.appendChild(detailRow("Baseline role", String((run.baseline || {}).role || "—")));
    pairs.appendChild(detailRow("Objective", "not evaluated — " +
      String((run.objective || {}).reason || "no verifier")));
    pairs.appendChild(detailRow("Duration", formatDuration(run.duration_seconds)));
    box.appendChild(pairs);
    box.appendChild(el("p", "forge-frame-note", evaluationComparisonText(run.comparison)));

    box.appendChild(el("p", "forge-frame-note",
      "Readiness: " + readinessCountsText(run.readiness) + "."));

    var actions = el("div", "button-row");
    if (run.session_id) {
      // Live observation and the Guardian belong to the SESSION's own tabs; the
      // evaluation card points at that session rather than building a second
      // live view (the same rule the evidence link follows).
      var liveButton = el("button", "cyber-btn small", "Watch Live Session");
      liveButton.type = "button";
      liveButton.addEventListener("click", function () { watchEvaluationSession(run.session_id); });
      actions.appendChild(liveButton);
      var evidenceButton = el("button", "cyber-btn small", "Open Evidence Timeline");
      evidenceButton.type = "button";
      evidenceButton.addEventListener("click", function () { openEvaluationEvidence(run.session_id); });
      actions.appendChild(evidenceButton);
    }
    if (run.status === "running") {
      var cancelButton = el("button", "cyber-btn small danger", "Cancel This Evaluation");
      cancelButton.type = "button";
      cancelButton.addEventListener("click", cancelEvaluation);
      actions.appendChild(cancelButton);
    }
    if (actions.children.length) box.appendChild(actions);
  }

  function evaluationRunRow(run) {
    var row = el("div", "forge-benchmark-run-row");
    row.dataset.status = String(run.status || "");
    var main = el("div", "forge-benchmark-run-main");
    var head = el("div", "forge-benchmark-head");
    var badge = el("span", "forge-badge", evaluationStatusText(run));
    badge.dataset.ok = run.status === "completed" && run.valid ? "true" : "false";
    head.appendChild(badge);
    head.appendChild(el("span", "muted", formatDate(run.created_at)));
    main.appendChild(head);
    main.appendChild(el("p", "",
      String((run.game || {}).display_name || (run.game || {}).game_id || "environment") +
      " · " + String((run.scenario || {}).name || "scenario") +
      " · " + String(run.mode || "observation_only")));
    if (run.baseline_role) {
      main.appendChild(el("p", "forge-frame-note", "Baseline role: " + String(run.baseline_role)));
    }
    if (run.reason) main.appendChild(el("p", "muted", String(run.reason)));
    row.appendChild(main);
    var open = el("button", "cyber-btn small", "Open Report");
    open.type = "button";
    open.addEventListener("click", function () {
      forge.evaluation.evaluationId = run.id;
      forge.evaluation.run = run;
      updateEvaluationControls();
      openEvaluationReport(run.id);
    });
    row.appendChild(open);
    return row;
  }

  function fillEvaluationBaselineOptions(rows) {
    var select = byId("forgeEvaluationBaseline");
    if (!select) return;
    var previous = select.value;
    select.innerHTML = "";
    var automatic = document.createElement("option");
    automatic.value = "";
    automatic.textContent = "Automatic (oldest completed baseline)";
    select.appendChild(automatic);
    rows.forEach(function (run) {
      if (run.status !== "completed" || !run.valid) return;
      var option = document.createElement("option");
      option.value = run.id;
      option.textContent = "Baseline " + String(run.id).slice(0, 10) + " · " +
        String((run.scenario || {}).scenario_id || "scenario") + " · " + formatDate(run.created_at);
      select.appendChild(option);
    });
    if (Array.prototype.some.call(select.options, function (option) { return option.value === previous; })) {
      select.value = previous;
    }
  }

  async function loadEvaluationHistory() {
    var list = byId("forgeEvaluationHistoryList");
    if (!list) return;
    try {
      var body = await requestJson("/novaforge/evaluations?limit=25");
      forge.evaluation.history = body.evaluations || [];
      list.innerHTML = "";
      if (!forge.evaluation.history.length) {
        list.appendChild(el("p", "muted", "No evaluation has been recorded yet."));
      } else {
        forge.evaluation.history.forEach(function (run) { list.appendChild(evaluationRunRow(run)); });
      }
      fillEvaluationBaselineOptions(forge.evaluation.history);
    } catch (error) {
      list.innerHTML = "";
      list.appendChild(el("p", "muted", "Evaluation history could not be read: " + requestErrorMessage(error)));
    }
  }

  async function openEvaluationReport(evaluationId) {
    var box = byId("forgeEvaluationReport");
    var id = evaluationId || (forge.evaluation.run && forge.evaluation.run.id) ||
      forge.evaluation.evaluationId;
    if (!box || !id) return;
    try {
      var report = await requestJson("/novaforge/evaluations/" + encodeURIComponent(id) + "/report");
      forge.evaluation.report = report;
      renderEvaluationReport(report);
      updateEvaluationControls();
    } catch (error) {
      box.innerHTML = "";
      box.appendChild(el("p", "muted", "The report could not be read: " + requestErrorMessage(error)));
      updateEvaluationControls();
    }
  }

  function renderEvaluationReport(report) {
    var box = byId("forgeEvaluationReport");
    if (!box) return;
    box.innerHTML = "";
    var head = el("div", "forge-benchmark-head");
    var badge = el("span", "forge-badge", evaluationStatusText(report));
    badge.dataset.ok = report.status === "completed" && report.valid ? "true" : "false";
    head.appendChild(badge);
    head.appendChild(el("span", "forge-benchmark-version",
      String((report.game || {}).display_name || "environment") + " · " + String(report.evaluation_id)));
    box.appendChild(head);
    box.appendChild(el("p", report.invalid ? "forge-benchmark-unsupported" : "",
      String(report.reason || "No reason was recorded.")));
    box.appendChild(el("p", "", String(report.note || "")));

    var pairs = el("div", "forge-benchmark-pairs");
    var compatibility = report.compatibility || {};
    pairs.appendChild(detailRow("Compatibility", String(compatibility.status || "not stated") +
      " — " + String(compatibility.reason || "no reason")));
    pairs.appendChild(detailRow("Mode", String(report.mode || "observation_only")));
    pairs.appendChild(detailRow("Objective verified", "no — " +
      String((report.objective || {}).reason || "no verifier")));
    pairs.appendChild(detailRow("Gameplay control exercised", report.real_gameplay_control_exercised ? "yes" : "no"));
    pairs.appendChild(detailRow("Benchmark run", String(report.benchmark_run_id || "not started")));
    pairs.appendChild(detailRow("Session", String(report.session_id || "not started")));
    pairs.appendChild(detailRow("Readiness", readinessCountsText(report.readiness)));
    box.appendChild(pairs);

    box.appendChild(el("h4", "", "Baseline"));
    box.appendChild(el("p", "", evaluationComparisonText(report.comparison)));
    var comparison = report.comparison;
    if (comparison && comparison.available && (comparison.metrics || []).length) {
      var table = el("table", "forge-benchmark-table");
      var thead = el("thead", "");
      var headRow = el("tr", "");
      ["Metric", "Baseline", "This run", "Delta"].forEach(function (labelText) {
        headRow.appendChild(el("th", "", labelText));
      });
      thead.appendChild(headRow);
      table.appendChild(thead);
      var tbody = el("tbody", "");
      comparison.metrics.forEach(function (row) {
        var leftMetric = benchmarkMetric(Object.assign({}, row.left || {}, { label: row.label, unit: row.unit }));
        var rightMetric = benchmarkMetric(Object.assign({}, row.right || {}, { label: row.label, unit: row.unit }));
        var tr = el("tr", "");
        tr.dataset.available = leftMetric.available && rightMetric.available ? "true" : "false";
        tr.appendChild(el("td", "", String(row.label || row.key) + (row.unit ? " [" + row.unit + "]" : "")));
        tr.appendChild(el("td", "", leftMetric.available ? leftMetric.text : "unavailable — " + leftMetric.reason));
        tr.appendChild(el("td", "", rightMetric.available ? rightMetric.text : "unavailable — " + rightMetric.reason));
        tr.appendChild(el("td", "", row.delta === null || row.delta === undefined
          ? String(row.delta_reason || "—") : benchmarkDeltaText(row)));
        tbody.appendChild(tr);
      });
      table.appendChild(tbody);
      box.appendChild(table);
    }

    box.appendChild(el("h4", "", "Evidence"));
    var evidence = report.evidence || {};
    var evidencePairs = el("div", "forge-benchmark-pairs");
    evidencePairs.appendChild(detailRow("Benchmark record", String(evidence.benchmark_run || "not recorded")));
    evidencePairs.appendChild(detailRow("Timeline", String(evidence.timeline || "not recorded")));
    evidencePairs.appendChild(detailRow("Telemetry", String(evidence.telemetry || "not recorded")));
    evidencePairs.appendChild(detailRow("Timeline source", String(evidence.timeline_source || "not stated")));
    evidencePairs.appendChild(detailRow("Persistence", String((evidence.persistence || {}).state || "not stated")));
    box.appendChild(evidencePairs);
    (report.missing_evidence || []).forEach(function (gap) {
      box.appendChild(el("p", "forge-frame-note", "Missing evidence: " + String(gap)));
    });

    if ((report.limitations || []).length) {
      box.appendChild(el("h4", "", "Limitations"));
      var limits = el("ul", "forge-note-list");
      report.limitations.forEach(function (line) { limits.appendChild(el("li", "", String(line))); });
      box.appendChild(limits);
    }
  }

  function exportEvaluationReport() {
    var report = forge.evaluation.report;
    if (!report) return;
    var name = "novaforge-evaluation-" + String(report.evaluation_id || "report") + ".json";
    var blob = new Blob([JSON.stringify(report, null, 2)], { type: "application/json" });
    var url = URL.createObjectURL(blob);
    var link = document.createElement("a");
    link.href = url;
    link.download = name;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  }

  function watchEvaluationSession(sessionId) {
    if (!sessionId) return;
    forge.sessionId = String(sessionId);
    forge.activeId = forge.sessionId;
    forge.lastFrameKey = "";
    forge.stateToken += 1;
    switchTab("forgeLiveView");
    refreshSession();
    loadStatus();
  }

  function openEvaluationEvidence(sessionId) {
    forge.benchmark.pendingEvidence = String(sessionId || (forge.evaluation.run && forge.evaluation.run.session_id) || "");
    switchTab("forgeTimelineView");
  }

  function openEvaluations() {
    evaluationLoadWindows();
    if (!forge.evaluation.games.length) loadEvaluationRegistry();
    loadEvaluationHistory();
  }

  /* ── NF-06: Forge Coach ───────────────────────────────────────
   *
   * The Coach is a client of the analysis and experiment API, and nothing here
   * computes a diagnosis: every finding, confidence value and verdict is read
   * from the response. The panel's job is to render uncertainty honestly —
   * "insufficient evidence", "blocked", "not comparable" and "inconclusive"
   * are shown as their own states rather than collapsed into a success look.
   */

  var COACH_POLL_MS = 1500;
  var EXPERIMENT_TERMINAL = ["completed", "failed", "cancelled", "rejected"];

  var EXPERIMENT_STATUS_TEXT = {
    proposed: "proposed · awaiting approval",
    approved: "approved · not started",
    blocked: "blocked · the refusal reasons are below",
    running: "running",
    completed: "completed",
    failed: "failed",
    cancelled: "cancelled",
    rejected: "rejected",
  };

  function coachNotice(message, kind) {
    var box = byId("forgeCoachNotice");
    if (!box) return;
    if (!message) {
      box.hidden = true;
      box.textContent = "";
      box.className = "forge-notice";
      return;
    }
    box.hidden = false;
    box.className = "forge-notice" + (kind === "error" ? " error" : "");
    box.textContent = message;
  }

  function experimentNotice(message, kind) {
    var box = byId("forgeExperimentNotice");
    if (!box) return;
    if (!message) {
      box.hidden = true;
      box.textContent = "";
      box.className = "forge-notice";
      return;
    }
    box.hidden = false;
    box.className = "forge-notice" + (kind === "error" ? " error" : "");
    box.textContent = message;
  }

  function coachChip(text, tone) {
    var chip = el("span", "coach-chip", String(text));
    if (tone) chip.dataset.tone = tone;
    return chip;
  }

  // The four kinds are the whole classification vocabulary. A reader must be
  // able to tell "the record says this" from "this is arithmetic" from "this is
  // a guess", so the label is never shortened to just a category name.
  function coachKindLabel(kind) {
    if (kind === "observed") return "observed fact";
    if (kind === "derived") return "derived metric";
    if (kind === "hypothesis") return "hypothesis";
    return "unknown";
  }

  function coachSeverityTone(severity) {
    if (severity === "critical" || severity === "high") return "bad";
    if (severity === "medium") return "warn";
    if (severity === "low") return "muted";
    return "info";
  }

  function coachVerdictTone(label) {
    if (label === "improved") return "good";
    if (label === "regressed") return "bad";
    if (label === "not_comparable") return "warn";
    return "muted";
  }

  function coachEvidenceText(ref) {
    var item = ref || {};
    if (item.kind === "event") {
      return "event #" + item.seq + (item.summary ? " — " + item.summary : "");
    }
    if (item.kind === "metric") {
      return "metric " + item.metric + " = " +
        (item.value === null || item.value === undefined ? "unavailable" : String(item.value));
    }
    if (item.kind === "session") return "session " + String(item.session_id || "");
    if (item.kind === "evaluation") return "evaluation " + String(item.evaluation_id || "") +
      (item.status ? " (" + item.status + ")" : "");
    if (item.kind === "comparison") return "comparison refused: " +
      ((item.reasons || []).join(" ") || "no reason recorded");
    if (item.kind === "sample") return "sample size: baseline " + String(item.left) +
      ", candidate " + String(item.right);
    if (item.kind === "capability") return "capability " + String(item.name || "") +
      " available: " + (item.available === true ? "yes" : "no");
    if (item.kind === "timeline_persistence") return "timeline persistence: " + String(item.state || "unknown");
    if (item.kind === "evaluation_evidence") return "evidence gaps: " +
      ((item.gaps || []).join(" ") || "none listed");
    return JSON.stringify(item);
  }

  function coachEvidenceList(refs, sessionId) {
    var list = el("ul", "coach-evidence-list");
    (refs || []).forEach(function (ref) {
      var item = el("li", "coach-evidence");
      if (ref && ref.kind === "event" && sessionId) {
        var link = el("button", "coach-evidence-link", coachEvidenceText(ref));
        link.type = "button";
        link.dataset.coachEvidence = String(ref.seq);
        link.dataset.coachSession = String(sessionId);
        link.title = "Open this event in the Evidence Timeline";
        item.appendChild(link);
      } else {
        item.appendChild(el("span", "", coachEvidenceText(ref)));
      }
      list.appendChild(item);
    });
    if (!list.childElementCount) list.appendChild(el("li", "muted", "No evidence reference recorded."));
    return list;
  }

  function renderCoachSubject(payload) {
    var box = byId("forgeCoachSubject");
    if (!box) return;
    box.innerHTML = "";
    var insufficient = (payload && payload.insufficient_evidence) || {};
    if (insufficient.state === true) {
      var banner = el("div", "coach-banner", "INSUFFICIENT EVIDENCE — " +
        String(insufficient.reason || "the record has nothing to analyse."));
      banner.dataset.tone = "warn";
      box.appendChild(banner);
      (insufficient.what_would_help || []).forEach(function (line) {
        box.appendChild(el("p", "forge-frame-note", "What would help: " + String(line)));
      });
    }
    var subject = (payload && payload.subject) || {};
    var evidence = (payload && payload.evidence) || {};
    var classification = (payload && payload.classification) || {};
    var pairs = el("div", "forge-benchmark-pairs");
    pairs.appendChild(detailRow("Session", String(subject.session_id || "none")));
    pairs.appendChild(detailRow("Evaluation", String(subject.evaluation_id || "none")));
    pairs.appendChild(detailRow("Environment", String((subject.game || {}).display_name ||
      (subject.game || {}).game_id || "not recorded")));
    pairs.appendChild(detailRow("Scenario", String((subject.scenario || {}).scenario_id || "not recorded") +
      ((subject.scenario || {}).version ? "@" + subject.scenario.version : "")));
    pairs.appendChild(detailRow("Target", String((subject.target || {}).title || "not recorded")));
    pairs.appendChild(detailRow("State · end reason", String(subject.state || "unknown") +
      " · " + String(subject.end_reason || "none recorded")));
    pairs.appendChild(detailRow("Timeline", String(evidence.timeline_events || 0) + " event(s), source " +
      String(evidence.timeline_source || "unknown")));
    pairs.appendChild(detailRow("Frames", String(evidence.frames || 0) +
      (evidence.frames_available === 0 ? " (none recorded)" : "")));
    pairs.appendChild(detailRow("Failure classification", String(classification.outcome || "not classified") +
      (classification.end_reason ? " · " + classification.end_reason : "")));
    box.appendChild(pairs);
    box.appendChild(el("p", "forge-frame-note", String(payload.methodology || "")));
    box.appendChild(el("p", "muted", String(payload.note || "")));
    var limits = el("ul", "forge-note-list");
    (payload.limitations || []).forEach(function (line) {
      limits.appendChild(el("li", "", String(line)));
    });
    if (limits.childElementCount) {
      box.appendChild(el("h4", "", "Limitations"));
      box.appendChild(limits);
    }
  }

  function renderCoachEarliest(earliest) {
    var box = byId("forgeCoachEarliest");
    var badge = byId("forgeCoachEarliestBadge");
    if (!box) return;
    box.innerHTML = "";
    var data = earliest || {};
    if (badge) {
      badge.textContent = data.available ? "candidate found" : "none recorded";
      badge.dataset.ok = data.available ? "false" : "true";
    }
    if (!data.available) {
      box.appendChild(el("p", "forge-frame-note", String(data.reason ||
        "No failure signature was recorded.")));
      return;
    }
    var candidate = data.candidate || {};
    var head = el("div", "coach-verdict");
    head.dataset.tone = "warn";
    head.appendChild(el("strong", "", "Earliest supported candidate: " +
      String(candidate.summary || ("event #" + candidate.seq))));
    box.appendChild(head);
    var pairs = el("div", "forge-benchmark-pairs");
    pairs.appendChild(detailRow("Event", "#" + String(candidate.seq) + " · " + String(candidate.type || "")));
    pairs.appendChild(detailRow("Elapsed", candidate.elapsed_ms === null || candidate.elapsed_ms === undefined
      ? "not recorded" : formatElapsed(candidate.elapsed_ms)));
    pairs.appendChild(detailRow("Severity", String(candidate.severity || "not stated")));
    pairs.appendChild(detailRow("Basis", String(data.basis || "")));
    pairs.appendChild(detailRow("Causation claimed", data.causation_claimed ? "yes" : "no"));
    box.appendChild(pairs);
    box.appendChild(el("p", "forge-frame-note", String(data.uncertainty || "")));
    box.appendChild(el("h4", "", "Later symptoms (order, not causation)"));
    box.appendChild(coachEvidenceList(data.later_symptoms || [],
      (forge.coach.analysis && forge.coach.analysis.subject && forge.coach.analysis.subject.session_id) || ""));
    if ((data.counter_evidence || []).length) {
      box.appendChild(el("h4", "", "Counter-evidence"));
      var counter = el("ul", "forge-note-list");
      data.counter_evidence.forEach(function (line) { counter.appendChild(el("li", "", String(line))); });
      box.appendChild(counter);
    }
    if ((data.alternatives || []).length) {
      box.appendChild(el("h4", "", "Alternative explanations"));
      var alternatives = el("ul", "forge-note-list");
      data.alternatives.forEach(function (line) { alternatives.appendChild(el("li", "", String(line))); });
      box.appendChild(alternatives);
    }
  }

  function renderCoachFindings(findings, sessionId) {
    var box = byId("forgeCoachFindings");
    var count = byId("forgeCoachFindingsCount");
    if (!box) return;
    box.innerHTML = "";
    var rows = findings || [];
    if (count) count.textContent = String(rows.length);
    if (!rows.length) {
      box.appendChild(el("p", "muted", "No finding is supported by this record's evidence."));
      return;
    }
    rows.forEach(function (finding) {
      var details = document.createElement("details");
      details.className = "forge-history-row coach-finding";
      details.dataset.severity = String(finding.severity || "info");
      details.dataset.kind = String(finding.kind || "unknown");
      var summary = document.createElement("summary");
      summary.className = "forge-history-main";
      summary.appendChild(coachChip(String(finding.severity || "info").toUpperCase(),
        coachSeverityTone(finding.severity)));
      summary.appendChild(el("span", "forge-history-text", String(finding.title || "Finding")));
      summary.appendChild(coachChip(coachKindLabel(finding.kind), finding.kind === "observed" ? "good" : "muted"));
      summary.appendChild(coachChip("evidence: " + String(finding.confidence || "unknown"),
        finding.confidence === "strong" ? "good" : finding.confidence === "insufficient" ? "bad" : "warn"));
      details.appendChild(summary);

      var body = el("div", "forge-history-detail");
      body.appendChild(el("p", "", String(finding.explanation || "")));
      var pairs = el("div", "forge-benchmark-pairs");
      pairs.appendChild(detailRow("Why this severity", String(finding.severity_basis || "not stated")));
      pairs.appendChild(detailRow("Why this confidence", String(finding.confidence_basis || "not stated")));
      pairs.appendChild(detailRow("Classification", coachKindLabel(finding.kind) +
        (finding.direct ? " — the record states it" : " — not a directly recorded fact")));
      body.appendChild(pairs);
      body.appendChild(el("h4", "", "Evidence"));
      body.appendChild(coachEvidenceList(finding.evidence || [], sessionId));
      if ((finding.alternatives || []).length) {
        body.appendChild(el("h4", "", "Alternative explanations"));
        var alternatives = el("ul", "forge-note-list");
        finding.alternatives.forEach(function (line) { alternatives.appendChild(el("li", "", String(line))); });
        body.appendChild(alternatives);
      }
      if ((finding.limitations || []).length) {
        body.appendChild(el("h4", "", "Known limitations"));
        var limits = el("ul", "forge-note-list");
        finding.limitations.forEach(function (line) { limits.appendChild(el("li", "", String(line))); });
        body.appendChild(limits);
      }
      details.appendChild(body);
      box.appendChild(details);
    });
  }

  function recommendationStatusTone(status) {
    if (status === "approved" || status === "completed") return "good";
    if (status === "rejected" || status === "failed") return "bad";
    if (status === "running") return "warn";
    if (status === "inconclusive") return "warn";
    return "muted";
  }

  function renderCoachRecommendations(rows, sessionId) {
    var box = byId("forgeCoachRecommendations");
    var count = byId("forgeCoachRecommendationCount");
    if (!box) return;
    box.innerHTML = "";
    var items = rows || [];
    if (count) count.textContent = String(items.length);
    if (!items.length) {
      box.appendChild(el("p", "muted", "No recommendation is justified by these findings."));
      return;
    }
    items.forEach(function (rec) {
      var card = el("div", "forge-history-row coach-recommendation");
      card.dataset.recommendation = String(rec.recommendation_id || "");
      var head = el("div", "forge-history-main");
      head.appendChild(coachChip("priority " + String(rec.priority || "?"), "info"));
      head.appendChild(el("span", "forge-history-text", String(rec.target_issue || "")));
      head.appendChild(coachChip(String(rec.status || "proposed"), recommendationStatusTone(rec.status)));
      card.appendChild(head);

      var body = el("div", "forge-history-detail");
      body.appendChild(el("p", "", String(rec.action || "")));
      var pairs = el("div", "forge-benchmark-pairs");
      pairs.appendChild(detailRow("Expected measurable effect", String(rec.expected_effect || "")));
      pairs.appendChild(detailRow("Metric to observe", String(rec.metric_label || rec.metric || "") +
        " (" + String(rec.metric || "") + ")"));
      pairs.appendChild(detailRow("Success criterion", String(rec.success_criterion || "")));
      pairs.appendChild(detailRow("Risk", String(rec.risk || "")));
      pairs.appendChild(detailRow("Effort", String(rec.effort || "")));
      pairs.appendChild(detailRow("Approval", rec.requires_approval ?
        "required before anything runs" : "not required"));
      body.appendChild(pairs);
      if ((rec.preconditions || []).length) {
        body.appendChild(el("h4", "", "Preconditions"));
        var preconditions = el("ul", "forge-note-list");
        rec.preconditions.forEach(function (line) { preconditions.appendChild(el("li", "", String(line))); });
        body.appendChild(preconditions);
      }
      body.appendChild(el("h4", "", "Evidence"));
      body.appendChild(coachEvidenceList(rec.evidence || [], sessionId));

      var experiment = rec.experiment || {};
      var actions = el("div", "button-row coach-recommendation-actions");
      var propose = el("button", "cyber-btn small", "Propose Experiment");
      propose.type = "button";
      propose.dataset.coachPropose = String(rec.recommendation_id || "");
      if (!experiment.supported || !experiment.kind) {
        propose.disabled = true;
        propose.title = String(experiment.reason ||
          "This finding needs an operator change rather than a benchmark run.");
      }
      actions.appendChild(propose);
      [
        ["approved", "Approve recommendation"],
        ["rejected", "Reject recommendation"],
        ["inconclusive", "Mark inconclusive"],
      ].forEach(function (pair) {
        var button = el("button", "cyber-btn small", pair[1]);
        button.type = "button";
        button.dataset.coachStatus = pair[0];
        button.dataset.coachRecommendation = String(rec.recommendation_id || "");
        actions.appendChild(button);
      });
      body.appendChild(actions);
      if (!experiment.supported || !experiment.kind) {
        body.appendChild(el("p", "forge-frame-note", "Experiment: not available — " +
          String(experiment.reason || "no declared runnable experiment for this category.")));
      } else {
        body.appendChild(el("p", "forge-frame-note", "Proposed experiment: " +
          String(experiment.kind === "repeat" ? "repeat the same configuration" :
            "change " + String(experiment.variable || "one variable")) +
          " · primary metric " + String(experiment.primary_metric || "") +
          " · scenario " + String(experiment.scenario_id || "the subject's own")));
      }
      card.appendChild(body);
      box.appendChild(card);
    });
  }

  function renderCoachAnalysis(payload) {
    forge.coach.analysis = payload || null;
    var sessionId = payload && payload.subject ? String(payload.subject.session_id || "") : "";
    renderCoachSubject(payload);
    renderCoachEarliest(payload && payload.earliest_recoverable_error);
    renderCoachFindings(payload && payload.findings, sessionId);
    renderCoachRecommendations(payload && payload.recommendations, sessionId);
    var badge = byId("forgeCoachStatus");
    if (badge && payload) {
      badge.textContent = "analysed · " + String((payload.findings || []).length) + " finding(s)";
      badge.dataset.ok = (payload.insufficient_evidence || {}).state === true ? "false" : "true";
    }
  }

  function coachSelection() {
    var evaluation = byId("forgeCoachEvaluation");
    var session = byId("forgeCoachSession");
    var evaluationId = (evaluation && evaluation.value) || forge.coach.evaluationId || "";
    var sessionId = (session && session.value) || forge.coach.sessionId || "";
    return { evaluationId: evaluationId, sessionId: evaluationId ? "" : sessionId };
  }

  async function loadCoachStatus() {
    var badge = byId("forgeCoachStatus");
    try {
      var status = await requestJson("/novaforge/coach");
      forge.coach.status = status;
      if (badge) {
        // Analysing is READ ONLY: approval belongs to running an experiment,
        // not to reading a recorded one, and the badge must not imply otherwise.
        badge.textContent = "ready · read-only";
        badge.dataset.ok = "true";
      }
    } catch (error) {
      if (badge) {
        badge.textContent = "unavailable";
        badge.dataset.ok = "false";
      }
      coachNotice("The Coach status could not be read: " + requestErrorMessage(error), "error");
    }
  }

  function fillCoachSelect(select, rows, valueOf, labelOf, previous, emptyLabel) {
    if (!select) return "";
    select.innerHTML = "";
    if (!rows.length) {
      var empty = document.createElement("option");
      empty.value = "";
      empty.textContent = emptyLabel;
      select.appendChild(empty);
      return "";
    }
    var values = rows.map(valueOf);
    rows.forEach(function (row) {
      var option = document.createElement("option");
      option.value = valueOf(row);
      option.textContent = labelOf(row);
      select.appendChild(option);
    });
    var chosen = values.indexOf(previous) >= 0 ? previous : values[0];
    select.value = chosen;
    return chosen;
  }

  async function loadCoachSubjects() {
    var results = await Promise.all([
      requestJson("/novaforge/evaluations?limit=30").catch(function () { return { evaluations: [] }; }),
      requestJson("/novaforge/sessions?limit=30").catch(function () { return { sessions: [] }; }),
    ]);
    var evaluations = results[0].evaluations || [];
    var sessions = results[1].sessions || [];
    forge.coach.evaluationId = fillCoachSelect(
      byId("forgeCoachEvaluation"), evaluations,
      function (row) { return String(row.id || ""); },
      function (row) {
        var game = row.game || {};
        var scenario = row.scenario || {};
        return String(game.display_name || game.game_id || "environment") + " · " +
          String(scenario.scenario_id || "scenario") + " · " + String(row.status || "unknown") +
          " · " + formatDate(row.created_at || "");
      },
      forge.coach.evaluationId, "No evaluation has been recorded yet"
    );
    forge.coach.sessionId = fillCoachSelect(
      byId("forgeCoachSession"), sessions,
      function (row) { return String(row.session_id || ""); },
      function (row) {
        return String((row.mission || {}).name || "session") + " · " +
          String(row.state || "unknown") + " · " + formatDate(row.created_at || "");
      },
      forge.coach.sessionId, "No session has been recorded yet"
    );
  }

  async function analyseCoach() {
    var selection = coachSelection();
    if (!selection.evaluationId && !selection.sessionId) {
      coachNotice("Select a recorded evaluation or session to analyse.", "error");
      return null;
    }
    var payload = {};
    if (selection.evaluationId) payload.evaluation_id = selection.evaluationId;
    else payload.session_id = selection.sessionId;
    coachNotice("Analysing the recorded evidence…");
    try {
      var analysis = await requestJson("/novaforge/coach/analyse", payload);
      renderCoachAnalysis(analysis);
      var insufficient = (analysis.insufficient_evidence || {}).state === true;
      coachNotice(insufficient
        ? "Insufficient evidence: " + String((analysis.insufficient_evidence || {}).reason || "")
        : "Analysis complete from recorded evidence — nothing was re-run and no record changed.",
        insufficient ? "error" : "");
      return analysis;
    } catch (error) {
      coachNotice("The analysis was refused: " + requestErrorMessage(error), "error");
      return null;
    }
  }

  function openCoachEvidence(sessionId, seq) {
    if (!sessionId || !seq) return;
    forge.timeline.pendingEvidence = String(sessionId);
    forge.timeline.pendingSeq = Number(seq) || 0;
    switchTab("forgeTimelineView");
  }

  async function withCoachBusy(buttonId, task) {
    if (forge.coach.busy) return null;
    forge.coach.busy = true;
    var button = buttonId ? byId(buttonId) : null;
    if (button) button.classList.add("loading");
    try {
      return await task();
    } finally {
      forge.coach.busy = false;
      if (button) button.classList.remove("loading");
    }
  }

  async function setRecommendationStatus(recommendationId, status) {
    if (!recommendationId) return;
    try {
      await requestJson("/novaforge/coach/recommendations/" + encodeURIComponent(recommendationId),
        { status: status, note: "Set from the Forge Coach panel." });
      var analysis = forge.coach.analysis;
      if (analysis && (analysis.recommendations || []).length) {
        analysis.recommendations.forEach(function (rec) {
          if (rec.recommendation_id === recommendationId) rec.status = status;
        });
        renderCoachRecommendations(analysis.recommendations,
          String(((analysis.subject || {}).session_id) || ""));
      }
      coachNotice("Recommendation marked " + status + ". This records an operator decision; it changes no recorded evidence.");
    } catch (error) {
      coachNotice("The recommendation status was not recorded: " + requestErrorMessage(error), "error");
    }
  }

  async function proposeCoachExperiment(recommendationId) {
    if (!recommendationId) return;
    var selection = coachSelection();
    var payload = { recommendation_id: recommendationId };
    if (selection.evaluationId) payload.evaluation_id = selection.evaluationId;
    if (selection.sessionId) payload.session_id = selection.sessionId;
    try {
      var record = await requestJson("/novaforge/experiments", payload);
      forge.coach.experimentId = String(record.id || "");
      renderExperiment(record);
      experimentNotice(record.status === "blocked"
        ? "The proposal is blocked and cannot run: " + String(record.reason || "")
        : "Proposed. Review the configuration, then approve it — approval is bound to this exact configuration.",
        record.status === "blocked" ? "error" : "");
      loadExperimentHistory();
    } catch (error) {
      experimentNotice("The proposal was refused: " + requestErrorMessage(error), "error");
    }
  }

  function experimentStatusText(record) {
    if (!record) return "none selected";
    var status = String(record.status || "unknown");
    var verdict = record.results && record.results.verdict ? record.results.verdict.label : "";
    return (EXPERIMENT_STATUS_TEXT[status] || status) + (verdict ? " · " + verdict : "");
  }

  function experimentCanApprove(record) {
    if (!record) return false;
    var status = String(record.status || "");
    return status === "proposed" || status === "approved";
  }

  function experimentCanRun(record) {
    if (!record) return false;
    var status = String(record.status || "");
    // A readiness refusal at RUN time does not revoke the approval: the
    // configuration did not change, so once the environment is ready the same
    // approval may start the run. A proposal blocked by its own errors cannot.
    if (status !== "approved" && status !== "blocked") return false;
    if (status === "blocked" && (record.errors || []).length) return false;
    return Boolean(record.approval && record.approval.spec_digest === (record.spec || {}).digest);
  }

  function experimentCanCancel(record) {
    if (!record) return false;
    return EXPERIMENT_TERMINAL.indexOf(String(record.status || "")) < 0;
  }

  function experimentGuardrailText(guardrail) {
    var item = guardrail || {};
    var bound = item.direction === "min" ? "at least " + String(item.bound) : "at most " + String(item.bound);
    var value = item.value === null || item.value === undefined ? "not measured" : String(item.value);
    var state = item.passed === true ? "within bound" : item.passed === false ? "BREACHED" : "unverified";
    return String(item.metric || "") + ": " + value + " (" + bound + ") — " + state;
  }

  function renderExperimentSpec(record, box) {
    var spec = (record && record.spec) || {};
    var pairs = el("div", "forge-benchmark-pairs coach-spec");
    pairs.appendChild(detailRow("Experiment", String(spec.experiment_id || record.id || "")));
    pairs.appendChild(detailRow("Kind", String(spec.kind || "") +
      (spec.kind === "repeat" ? " — the same configuration twice" : " — one variable changes")));
    pairs.appendChild(detailRow("Variable", spec.kind === "repeat" ? "none" : String(spec.variable || "")));
    pairs.appendChild(detailRow("Environment · scenario", String(spec.game_id || "") + " · " +
      String(spec.scenario_id || "") + "@" + String(spec.scenario_version || "")));
    pairs.appendChild(detailRow("Target window", String(spec.target_title || "")));
    var baseline = spec.baseline_configuration || {};
    var candidate = spec.candidate_configuration || {};
    pairs.appendChild(detailRow("Baseline arm", "budget " + String(baseline.duration_seconds) +
      "s · interval " + String(baseline.frame_interval_seconds) + "s"));
    pairs.appendChild(detailRow("Candidate arm", "budget " + String(candidate.duration_seconds) +
      "s · interval " + String(candidate.frame_interval_seconds) + "s"));
    pairs.appendChild(detailRow("Repeats per arm", String(spec.repeat_count || 1)));
    pairs.appendChild(detailRow("Primary metric", String(spec.primary_metric || "") +
      " · better when it " + (spec.primary_direction === "increase" ? "increases" : "decreases") +
      " · threshold " + String(spec.threshold) + " (absolute)"));
    pairs.appendChild(detailRow("Baseline runs", (spec.baseline_run_ids || []).length
      ? spec.baseline_run_ids.join(", ")
      : "none — the baseline arm is recorded by this experiment"));
    pairs.appendChild(detailRow("Parent finding", String((spec.subject || {}).finding_id || "none")));
    pairs.appendChild(detailRow("Recommendation", String((spec.subject || {}).recommendation_id || "none")));
    box.appendChild(pairs);
    if ((spec.secondary_metrics || []).length) {
      box.appendChild(el("p", "forge-frame-note", "Secondary metrics: " + spec.secondary_metrics.join(", ")));
    }
    if ((spec.guardrails || []).length) {
      box.appendChild(el("h4", "", "Regression guardrails"));
      var guardrails = el("ul", "forge-note-list");
      spec.guardrails.forEach(function (guardrail) {
        guardrails.appendChild(el("li", "", experimentGuardrailText({
          metric: guardrail.metric, direction: guardrail.direction, bound: guardrail.bound,
        })));
      });
      box.appendChild(guardrails);
    }
    var abort = spec.abort || {};
    box.appendChild(el("p", "forge-frame-note", "Abort conditions: " +
      (String(abort.max_seconds_per_arm || "?") + "s per arm; " +
        ((abort.cancel_on || []).join(" ") || "operator cancellation"))));
    if (record.approval) {
      box.appendChild(el("p", "forge-frame-note", "Approved " +
        formatDate(record.approval.approved_at || "") + " by " + String(record.approval.approved_by || "operator") +
        " — bound to configuration " + String(record.approval.spec_digest || "")));
    } else {
      box.appendChild(el("p", "forge-frame-note", "Not approved. Approval is bound to the configuration above; nothing runs without it."));
    }
    (record.errors || []).forEach(function (line) {
      box.appendChild(el("p", "forge-frame-note", "Unsupported: " + String(line)));
    });
    // A run-time readiness refusal keeps its reasons on the record, and they
    // must survive a reload: the status badge alone never says why.
    var preflight = record.preflight || {};
    if (record.status === "blocked" && (preflight.reasons || []).length) {
      box.appendChild(el("h4", "", "Why the run was refused"));
      var refused = el("ul", "forge-note-list");
      preflight.reasons.forEach(function (line) {
        refused.appendChild(el("li", "", String(line)));
      });
      box.appendChild(refused);
    }
    var arms = record.arms || {};
    var rows = (arms.baseline || []).concat(arms.candidate || []);
    if (rows.length) {
      box.appendChild(el("h4", "", "Arms"));
      var table = el("table", "forge-benchmark-table");
      var thead = el("thead", "");
      var headRow = el("tr", "");
      ["Arm", "#", "Status", "Evaluation", "Reason"].forEach(function (labelText) {
        headRow.appendChild(el("th", "", labelText));
      });
      thead.appendChild(headRow);
      table.appendChild(thead);
      var tbody = el("tbody", "");
      rows.forEach(function (row) {
        var tr = el("tr", "");
        tr.dataset.available = row.status === "completed" ? "true" : "false";
        tr.appendChild(el("td", "", String(row.arm || "")));
        tr.appendChild(el("td", "", String((row.index || 0) + 1)));
        tr.appendChild(el("td", "", String(row.status || "")));
        tr.appendChild(el("td", "", String(row.evaluation_id || "")));
        tr.appendChild(el("td", "", String(row.reason || "")));
        tbody.appendChild(tr);
      });
      table.appendChild(tbody);
      box.appendChild(table);
    }
  }

  function renderExperimentResults(results) {
    var box = byId("forgeExperimentResults");
    if (!box) return;
    box.innerHTML = "";
    if (!results) {
      box.appendChild(el("p", "muted",
        "The before/after comparison appears when both arms have finished. A refused comparison says why instead of showing a delta."));
      return;
    }
    var verdict = results.verdict || {};
    var banner = el("div", "coach-verdict");
    banner.dataset.tone = coachVerdictTone(verdict.label);
    banner.appendChild(el("strong", "", String(verdict.label || "inconclusive").toUpperCase() +
      " — " + String(verdict.reason || "no reason recorded")));
    box.appendChild(banner);
    if (verdict.limited_to_single_run) {
      box.appendChild(el("p", "forge-frame-note",
        "Limited to fewer than two eligible runs per side: a single measurement shows no variability."));
    }
    box.appendChild(el("p", "forge-frame-note",
      "Gameplay improvement claimed: " + (verdict.gameplay_claimed ? "yes" : "no") +
      " · objective verified: " + (verdict.objective_verified ? "yes" : "no") +
      " · a configurational change is not a gameplay improvement."));
    var sample = results.sample || {};
    box.appendChild(el("p", "forge-frame-note", "Sample: baseline " + String(sample.baseline || 0) +
      " run(s) (" + String(sample.eligible_baseline || 0) + " eligible) · candidate " +
      String(sample.candidate || 0) + " run(s) (" + String(sample.eligible_candidate || 0) + " eligible)"));

    var primary = results.primary || {};
    box.appendChild(el("h4", "", "Primary metric"));
    var primaryTable = el("table", "forge-benchmark-table");
    var primaryHead = el("thead", "");
    var primaryHeadRow = el("tr", "");
    ["Metric", "Baseline", "Candidate", "Delta", "Threshold", "Basis"].forEach(function (labelText) {
      primaryHeadRow.appendChild(el("th", "", labelText));
    });
    primaryHead.appendChild(primaryHeadRow);
    primaryTable.appendChild(primaryHead);
    var primaryBody = el("tbody", "");
    var primaryRow = el("tr", "");
    primaryRow.dataset.available = primary.delta === null || primary.delta === undefined ? "false" : "true";
    primaryRow.appendChild(el("td", "", String(primary.label || primary.key || "") +
      (primary.unit ? " [" + primary.unit + "]" : "")));
    primaryRow.appendChild(el("td", "", String((primary.baseline || {}).value === null ||
      (primary.baseline || {}).value === undefined ? "unavailable" : (primary.baseline || {}).value)));
    primaryRow.appendChild(el("td", "", String((primary.candidate || {}).value === null ||
      (primary.candidate || {}).value === undefined ? "unavailable" : (primary.candidate || {}).value)));
    primaryRow.appendChild(el("td", "", primary.delta === null || primary.delta === undefined
      ? String(primary.reason || "no delta") : String(primary.delta)));
    primaryRow.appendChild(el("td", "", primary.threshold === null || primary.threshold === undefined
      ? "—" : String(primary.threshold)));
    primaryRow.appendChild(el("td", "", primary.source === "nf04_comparison"
      ? "NF-04 certified comparison" : "per-arm description (not an effect)"));
    primaryBody.appendChild(primaryRow);
    primaryTable.appendChild(primaryBody);
    box.appendChild(primaryTable);
    if (primary.reason) box.appendChild(el("p", "forge-frame-note", String(primary.reason)));

    if ((results.secondary || []).length) {
      box.appendChild(el("h4", "", "Secondary metrics"));
      var secondary = el("ul", "forge-note-list");
      results.secondary.forEach(function (row) {
        var left = (row.baseline || {}).value;
        var right = (row.candidate || {}).value;
        secondary.appendChild(el("li", "", String(row.label || row.key) + ": baseline " +
          (left === null || left === undefined ? "unavailable" : String(left)) + " → candidate " +
          (right === null || right === undefined ? "unavailable" : String(right)) +
          (row.delta === null || row.delta === undefined ? " (no delta)" : " (Δ " + String(row.delta) + ")")));
      });
      box.appendChild(secondary);
    }

    if ((results.guardrails || []).length) {
      box.appendChild(el("h4", "", "Regression guardrails"));
      var guardrails = el("ul", "forge-note-list");
      results.guardrails.forEach(function (guardrail) {
        var item = el("li", "", experimentGuardrailText(guardrail));
        item.dataset.passed = guardrail.passed === true ? "true" : guardrail.passed === false ? "false" : "unknown";
        guardrails.appendChild(item);
      });
      box.appendChild(guardrails);
    }

    var comparison = results.comparison || {};
    if ((comparison.reasons || []).length) {
      box.appendChild(el("h4", "", comparison.compatible ? "Comparison warnings" : "Why the comparison was refused"));
      var reasons = el("ul", "forge-note-list");
      comparison.reasons.forEach(function (line) { reasons.appendChild(el("li", "", String(line))); });
      box.appendChild(reasons);
    }
    if ((results.failed_runs || []).length) {
      box.appendChild(el("h4", "", "Failed, aborted or invalid runs"));
      var failed = el("ul", "forge-note-list");
      results.failed_runs.forEach(function (row) {
        failed.appendChild(el("li", "", String(row.arm || "") + " #" + String((row.index || 0) + 1) +
          " — " + String(row.status || "") + ": " + String(row.reason || "")));
      });
      box.appendChild(failed);
    }
    if ((results.limitations || []).length) {
      box.appendChild(el("h4", "", "Limitations"));
      var limits = el("ul", "forge-note-list");
      results.limitations.forEach(function (line) { limits.appendChild(el("li", "", String(line))); });
      box.appendChild(limits);
    }
  }

  function renderExperiment(record) {
    forge.coach.experiment = record || null;
    var badge = byId("forgeExperimentState");
    if (badge) {
      badge.textContent = experimentStatusText(record);
      badge.dataset.ok = EXPERIMENT_TERMINAL.indexOf(String((record || {}).status || "")) >= 0 ? "true" : "false";
    }
    var detail = byId("forgeExperimentDetail");
    if (detail) {
      detail.innerHTML = "";
      if (!record) {
        detail.appendChild(el("p", "muted",
          "Turn a recommendation into a proposal to review its configuration. Nothing runs until the exact configuration is approved."));
      } else {
        renderExperimentSpec(record, detail);
      }
    }
    renderExperimentResults(record && record.results);
    var approve = byId("forgeExperimentApproveButton");
    var reject = byId("forgeExperimentRejectButton");
    var run = byId("forgeExperimentRunButton");
    var cancel = byId("forgeExperimentCancelButton");
    var exportButton = byId("forgeExperimentExportButton");
    if (approve) approve.disabled = !experimentCanApprove(record);
    if (reject) reject.disabled = !(record && (record.status === "proposed" || record.status === "approved"));
    if (run) run.disabled = !experimentCanRun(record);
    if (cancel) cancel.disabled = !experimentCanCancel(record);
    if (exportButton) exportButton.disabled = !(record && record.results);
  }

  async function loadCoachExperiment(experimentId) {
    if (!experimentId) return null;
    try {
      var record = await requestJson("/novaforge/experiments/" + encodeURIComponent(experimentId));
      forge.coach.experimentId = String(record.id || experimentId);
      renderExperiment(record);
      if (record.status === "running") scheduleExperimentPoll();
      return record;
    } catch (error) {
      experimentNotice("The experiment could not be read: " + requestErrorMessage(error), "error");
      return null;
    }
  }

  function stopExperimentPoll() {
    if (forge.coach.timer) {
      clearTimeout(forge.coach.timer);
      forge.coach.timer = null;
    }
  }

  function scheduleExperimentPoll() {
    stopExperimentPoll();
    if (document.hidden) return;
    forge.coach.timer = setTimeout(async function () {
      forge.coach.timer = null;
      var record = await loadCoachExperiment(forge.coach.experimentId);
      if (record && EXPERIMENT_TERMINAL.indexOf(String(record.status || "")) >= 0) {
        loadExperimentHistory();
        experimentNotice(record.status === "completed"
          ? "Experiment finished: " + String((((record.results || {}).verdict) || {}).label || "completed") +
            " — read the verdict and its limitations above."
          : "Experiment " + String(record.status) + ": " + String(record.reason || ""),
          record.status === "completed" ? "" : "error");
      }
    }, COACH_POLL_MS);
  }

  async function approveExperiment() {
    var id = forge.coach.experimentId;
    if (!id) return;
    return withCoachBusy("forgeExperimentApproveButton", async function () {
      try {
        var record = await requestJson("/novaforge/experiments/" + encodeURIComponent(id) + "/approve",
          { note: "Approved from the Forge Coach panel." });
        renderExperiment(record);
        experimentNotice("Approved. The approval is bound to configuration " +
          String((record.approval || {}).spec_digest || "") + "; editing it requires approving again.");
        loadExperimentHistory();
      } catch (error) {
        experimentNotice("The approval was refused: " + requestErrorMessage(error), "error");
      }
    });
  }

  async function rejectExperiment() {
    var id = forge.coach.experimentId;
    if (!id) return;
    return withCoachBusy("forgeExperimentRejectButton", async function () {
      try {
        var record = await requestJson("/novaforge/experiments/" + encodeURIComponent(id) + "/reject",
          { note: "Rejected from the Forge Coach panel." });
        renderExperiment(record);
        experimentNotice("Rejected. The proposal stays in the history with this decision.");
        loadExperimentHistory();
      } catch (error) {
        experimentNotice("The rejection was refused: " + requestErrorMessage(error), "error");
      }
    });
  }

  async function runExperiment() {
    var id = forge.coach.experimentId;
    if (!id) return;
    return withCoachBusy("forgeExperimentRunButton", async function () {
      try {
        var record = await requestJson("/novaforge/experiments/" + encodeURIComponent(id) + "/run", {});
        renderExperiment(record);
        if (record.status === "blocked") {
          experimentNotice("Not started: " + String(record.reason || "the readiness revalidation refused it."), "error");
          return;
        }
        experimentNotice("Running both arms through the evaluation lab. The one-session slot is shared, so nothing else can overlap it.");
        scheduleExperimentPoll();
      } catch (error) {
        experimentNotice("The experiment did not start: " + requestErrorMessage(error), "error");
        loadCoachExperiment(id);
      }
    });
  }

  async function cancelExperiment() {
    var id = forge.coach.experimentId;
    if (!id) return;
    return withCoachBusy("forgeExperimentCancelButton", async function () {
      try {
        var record = await requestJson("/novaforge/experiments/" + encodeURIComponent(id) + "/cancel",
          { reason: "Cancelled from the Forge Coach panel." });
        renderExperiment(record);
        experimentNotice(record.already_stopped
          ? "The experiment had already finished; nothing was cancelled."
          : "Cancelled. The active arm was cancelled through its own stop path.");
        loadExperimentHistory();
      } catch (error) {
        experimentNotice("The cancellation was refused: " + requestErrorMessage(error), "error");
      }
    });
  }

  function exportExperimentResult() {
    var record = forge.coach.experiment;
    if (!record || !record.results) return;
    var name = "novaforge-experiment-" + String(record.id || "result") + ".json";
    var blob = new Blob([JSON.stringify(record, null, 2)], { type: "application/json" });
    var url = URL.createObjectURL(blob);
    var link = document.createElement("a");
    link.href = url;
    link.download = name;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  }

  function experimentHistoryRow(row) {
    var button = el("button", "forge-history-row");
    button.type = "button";
    button.dataset.experimentId = String(row.id || "");
    button.dataset.status = String(row.status || "");
    var main = el("div", "forge-history-main");
    main.appendChild(coachChip(String(row.status || "unknown"),
      row.status === "completed" ? "good" : row.status === "failed" ? "bad" : "muted"));
    main.appendChild(el("span", "forge-history-text", String(row.scenario ?
      ((row.scenario || {}).scenario_id || "scenario") : "scenario") + " · " +
      String(row.kind || "") + (row.variable && row.variable !== "none" ? " (" + row.variable + ")" : "") +
      " · " + String(row.primary_metric || "")));
    main.appendChild(el("span", "forge-history-meta", formatDate(row.created_at || "")));
    button.appendChild(main);
    var verdict = (row.verdict || {}).label;
    var meta = el("div", "forge-history-meta");
    meta.textContent = "target " + String(row.target_title || "not recorded") +
      (verdict ? " · verdict " + String(verdict) : "") +
      (row.approved ? " · approved" : " · not approved");
    button.appendChild(meta);
    return button;
  }

  async function loadExperimentHistory() {
    var list = byId("forgeExperimentHistory");
    if (!list) return;
    try {
      var payload = await requestJson("/novaforge/experiments?limit=20");
      var rows = payload.experiments || [];
      forge.coach.history = rows;
      list.innerHTML = "";
      if (!rows.length) {
        list.appendChild(el("p", "muted", "No experiment has been proposed yet."));
        return;
      }
      rows.forEach(function (row) { list.appendChild(experimentHistoryRow(row)); });
    } catch (error) {
      list.innerHTML = "";
      list.appendChild(el("p", "muted", "The experiment history could not be read: " +
        requestErrorMessage(error)));
    }
  }

  function openCoach() {
    loadCoachStatus();
    loadCoachSubjects();
    loadExperimentHistory();
    if (forge.coach.experimentId) loadCoachExperiment(forge.coach.experimentId);
  }

  /* ── Tabs and wiring ───────────────────────────────────────── */

  function switchTab(viewId) {
    document.querySelectorAll(".forge-tab").forEach(function (button) {
      var target = button.dataset.forgeTab;
      var selected = target === viewId;
      button.classList.toggle("active", selected);
      button.setAttribute("aria-selected", selected ? "true" : "false");
    });
    document.querySelectorAll(".forge-view").forEach(function (view) {
      var selected = view.id === viewId;
      view.classList.toggle("active", selected);
      view.hidden = !selected;
    });
    if (viewId === "forgeHistoryView") loadHistory();
    if (viewId === "forgeBenchmarkView") openBenchmarks();
    if (viewId === "forgeEvaluationView") openEvaluations();
    if (viewId === "forgeTimelineView") openTimeline();
    if (viewId === "forgeCoachView") openCoach();
  }

  function setupNovaForge() {
    if (!byId("forgeStartButton")) return;

    document.querySelectorAll(".forge-tab").forEach(function (button) {
      button.addEventListener("click", function () { switchTab(button.dataset.forgeTab); });
    });

    ["forgeMissionName", "forgeMissionObjective", "forgeMissionTarget",
      "forgeMissionCriteria", "forgeTimeLimit", "forgeMaxActions"].forEach(function (id) {
      var field = byId(id);
      field.addEventListener("input", invalidateValidation);
      field.addEventListener("change", invalidateValidation);
    });

    byId("forgeRefreshWindowsButton").addEventListener("click", loadWindows);
    byId("forgeSaveMissionButton").addEventListener("click", saveMission);
    byId("forgeValidateButton").addEventListener("click", validateMission);
    byId("forgeStartButton").addEventListener("click", startSession);
    byId("forgePauseButton").addEventListener("click", function () { control("pause"); });
    byId("forgeResumeButton").addEventListener("click", function () { control("resume"); });
    byId("forgeStopButton").addEventListener("click", function () { control("stop"); });
    byId("forgeCancelButton").addEventListener("click", function () { control("cancel"); });
    byId("forgeEmergencyStopButton").addEventListener("click", emergencyStop);
    // Remediation R1/R2: objective, plan, run and verify. Each is its own
    // request because each has a different failure to report; Run sends only
    // the proposal the panel planned, never a rebuilt action.
    byId("forgeObjectiveSaveButton").addEventListener("click", function () { setObjectiveFromPanel(); });
    byId("forgeVerifyButton").addEventListener("click", function () { verifyFromPanel(); });
    byId("forgeActionPlanButton").addEventListener("click", function () { planActionFromPanel(); });
    byId("forgeActionRunButton").addEventListener("click", function () { runActionFromPanel(); });
    ["forgeActionKind", "forgeActionKey", "forgeActionPoint"].forEach(function (id) {
      var node = byId(id);
      if (!node) return;
      // Any edit invalidates the plan: the digest binds the ACTION, so a
      // changed input must not be executable under the old approval.
      node.addEventListener(node.tagName === "SELECT" ? "change" : "input", function () {
        if (!forge.gameplay.proposal) return;
        forge.gameplay.proposal = null;
        renderGameplay(forge.current);
      });
    });
    byId("forgeHistoryRefreshButton").addEventListener("click", loadHistory);

    // NF-04 Benchmark Lab controls. Every edit invalidates the current
    // preflight, so Start can never act on a configuration nobody validated.
    if (byId("forgeBenchmarkScenario")) {
      byId("forgeBenchmarkRefreshButton").addEventListener("click", loadBenchmarkCatalog);
      byId("forgeBenchmarkScenario").addEventListener("change", selectBenchmarkScenario);
      ["forgeBenchmarkTarget", "forgeBenchmarkDuration", "forgeBenchmarkInterval",
        "forgeBenchmarkNotes"].forEach(function (id) {
        var field = byId(id);
        field.addEventListener("input", invalidateBenchmarkPreflight);
        field.addEventListener("change", invalidateBenchmarkPreflight);
      });
      byId("forgeBenchmarkPreflightButton").addEventListener("click", preflightBenchmark);
      byId("forgeBenchmarkStartButton").addEventListener("click", startBenchmark);
      byId("forgeBenchmarkCancelButton").addEventListener("click", cancelBenchmark);
      byId("forgeBenchmarkHistoryRefreshButton").addEventListener("click", loadBenchmarkHistory);
      byId("forgeBenchmarkAggregateButton").addEventListener("click", aggregateBenchmarks);
      byId("forgeBenchmarkCompareButton").addEventListener("click", compareBenchmarks);
      byId("forgeBenchmarkExportButton").addEventListener("click", exportBenchmarkRun);
    }

    // NF-05 Controlled Game Evaluation. Every configuration edit invalidates
    // readiness; Start additionally needs the explicit confirmation checkbox.
    if (byId("forgeEvaluationGame")) {
      byId("forgeEvaluationRefreshButton").addEventListener("click", loadEvaluationRegistry);
      byId("forgeEvaluationGame").addEventListener("change", selectEvaluationGame);
      byId("forgeEvaluationScenario").addEventListener("change", function () {
        applyEvaluationScenarioDefaults();
        invalidateEvaluationPreflight();
      });
      ["forgeEvaluationTarget", "forgeEvaluationDuration", "forgeEvaluationInterval",
        "forgeEvaluationNotes", "forgeEvaluationOffline"].forEach(function (id) {
        var field = byId(id);
        field.addEventListener("input", invalidateEvaluationPreflight);
        field.addEventListener("change", invalidateEvaluationPreflight);
      });
      byId("forgeEvaluationBaseline").addEventListener("change", function () {
        updateEvaluationControls();
      });
      byId("forgeEvaluationConfirm").addEventListener("change", updateEvaluationControls);
      byId("forgeEvaluationPreflightButton").addEventListener("click", preflightEvaluation);
      byId("forgeEvaluationStartButton").addEventListener("click", startEvaluation);
      byId("forgeEvaluationCancelButton").addEventListener("click", cancelEvaluation);
      byId("forgeEvaluationHistoryRefreshButton").addEventListener("click", loadEvaluationHistory);
      byId("forgeEvaluationReportButton").addEventListener("click", function () { openEvaluationReport(); });
      byId("forgeEvaluationExportButton").addEventListener("click", exportEvaluationReport);
      updateEvaluationControls();
    }

    // NF-06 Forge Coach. Analyse is a POST because it takes a subject, but it
    // mutates nothing; the recommendation and experiment controls are the only
    // state-changing actions here, and each one posts what it did.
    if (byId("forgeCoachAnalyseButton")) {
      byId("forgeCoachAnalyseButton").addEventListener("click", function () { analyseCoach(); });
      byId("forgeCoachRefreshButton").addEventListener("click", function () {
        loadCoachStatus();
        loadCoachSubjects();
      });
      byId("forgeCoachEvaluation").addEventListener("change", function () {
        forge.coach.evaluationId = byId("forgeCoachEvaluation").value;
      });
      byId("forgeCoachSession").addEventListener("change", function () {
        forge.coach.sessionId = byId("forgeCoachSession").value;
      });
      byId("forgeCoachFindings").addEventListener("click", function (event) {
        var link = event.target.closest ? event.target.closest("[data-coach-evidence]") : null;
        if (!link) return;
        openCoachEvidence(link.dataset.coachSession, Number(link.dataset.coachEvidence));
      });
      byId("forgeCoachRecommendations").addEventListener("click", function (event) {
        var target = event.target.closest ? event.target.closest("button") : null;
        if (!target) return;
        if (target.dataset.coachPropose) {
          proposeCoachExperiment(target.dataset.coachPropose);
          return;
        }
        if (target.dataset.coachStatus && target.dataset.coachRecommendation) {
          setRecommendationStatus(target.dataset.coachRecommendation, target.dataset.coachStatus);
        }
      });
      byId("forgeExperimentApproveButton").addEventListener("click", approveExperiment);
      byId("forgeExperimentRejectButton").addEventListener("click", rejectExperiment);
      byId("forgeExperimentRunButton").addEventListener("click", runExperiment);
      byId("forgeExperimentCancelButton").addEventListener("click", cancelExperiment);
      byId("forgeExperimentExportButton").addEventListener("click", exportExperimentResult);
      byId("forgeExperimentHistoryRefreshButton").addEventListener("click", loadExperimentHistory);
      byId("forgeExperimentHistory").addEventListener("click", function (event) {
        var row = event.target.closest ? event.target.closest("[data-experiment-id]") : null;
        if (!row) return;
        forge.coach.experimentId = String(row.dataset.experimentId || "");
        experimentNotice("");
        loadCoachExperiment(forge.coach.experimentId);
      });
      renderExperiment(null);
      loadCoachStatus();
    }

    // NF-03 replay controls. This is the whole interaction surface: it selects
    // and inspects recorded evidence and never calls a session/control route.
    byId("forgeTimelineRefreshButton").addEventListener("click", function () { loadTimelineSessions(); });
    byId("forgeTimelineSession").addEventListener("change", function () {
      forge.timeline.sessionId = byId("forgeTimelineSession").value;
      loadTimeline();
    });
    byId("forgeTimelineCompareButton").addEventListener("click", compareTimelineSessions);
    ["forgeTimelineCategory", "forgeTimelineSource", "forgeTimelineSeverity"].forEach(function (id) {
      byId(id).addEventListener("change", function () {
        forge.timeline.filters = timelineFilters();
        loadTimeline();
      });
    });
    byId("forgeTimelineMoreButton").addEventListener("click", function () {
      loadTimeline({ append: true });
    });
    byId("forgeTimelinePrevButton").addEventListener("click", function () { stepTimeline(-1); });
    byId("forgeTimelineNextButton").addEventListener("click", function () { stepTimeline(1); });

    // A terminal session is not polled; resuming the tab refetches the record.
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden && forge.sessionId && forge.activeId) refreshSession();
    });
    byId("forgeFrame").addEventListener("error", function () {
      liveError("The captured frame could not be displayed.");
    });

    loadStatus();
    loadMission().then(loadWindows);
    loadHistory();
  }

  window.setupNovaForge = setupNovaForge;
  window.NovaForgeLogic = {
    frameFreshness: frameFreshness,
    frameStillCurrent: frameStillCurrent,
    responseStillCurrent: responseStillCurrent,
    saveOutcome: saveOutcome,
    visionEmptyText: visionEmptyText,
    stageText: stageText,
    controlAvailability: controlAvailability,
    guardianLimitState: guardianLimitState,
    metricText: metricText,
    renderGuardian: renderGuardian,
    renderTelemetry: renderTelemetry,
    formatDuration: formatDuration,
    formatElapsed: formatElapsed,
    timelineResponseStillCurrent: timelineResponseStillCurrent,
    replayGuard: replayGuard,
    evidenceStatus: evidenceStatus,
    timelineSourceText: timelineSourceText,
    persistenceText: persistenceText,
    classificationRow: classificationRow,
    compareValue: compareValue,
    timelinePageLabel: timelinePageLabel,
    renderTimelineList: renderTimelineList,
    renderTimelineDetail: renderTimelineDetail,
    benchmarkMetric: benchmarkMetric,
    benchmarkCanStart: benchmarkCanStart,
    benchmarkStatusText: benchmarkStatusText,
    benchmarkVerdictText: benchmarkVerdictText,
    benchmarkDeltaText: benchmarkDeltaText,
    evaluationCanStart: evaluationCanStart,
    evaluationStatusText: evaluationStatusText,
    readinessStateText: readinessStateText,
    readinessCountsText: readinessCountsText,
    evaluationBaselineText: evaluationBaselineText,
    evaluationComparisonText: evaluationComparisonText,
    // The evaluation renderers are exported so what they BUILD for an
    // unsupported environment, a refused readiness verdict and an aborted
    // report is asserted on the rendered nodes, not on the source text.
    renderEvaluationGameDetail: renderEvaluationGameDetail,
    renderEvaluationPreflight: renderEvaluationPreflight,
    renderEvaluationReport: renderEvaluationReport,
    renderTimelineClassification: renderTimelineClassification,
    // NF-06 renderers are exported for the same reason as NF-05's: the pins
    // assert on the NODES built for insufficient evidence, a blocked
    // experiment and an inconclusive verdict, never on source text.
    coachKindLabel: coachKindLabel,
    coachSeverityTone: coachSeverityTone,
    coachVerdictTone: coachVerdictTone,
    coachEvidenceText: coachEvidenceText,
    experimentStatusText: experimentStatusText,
    experimentCanApprove: experimentCanApprove,
    experimentCanRun: experimentCanRun,
    experimentCanCancel: experimentCanCancel,
    experimentGuardrailText: experimentGuardrailText,
    renderCoachSubject: renderCoachSubject,
    renderCoachEarliest: renderCoachEarliest,
    renderCoachFindings: renderCoachFindings,
    renderCoachRecommendations: renderCoachRecommendations,
    renderCoachAnalysis: renderCoachAnalysis,
    renderExperimentSpec: renderExperimentSpec,
    renderExperimentResults: renderExperimentResults,
    renderExperiment: renderExperiment,
    coachState: function () { return forge.coach; },
    renderTimelineCompare: renderTimelineCompare,
    renderTimelineMeta: renderTimelineMeta,
    timelineState: function () { return forge.timeline; },
  };
})();
