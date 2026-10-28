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
    targetLabel: "",
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
    // NF-19: the control card's own last read, kept out of `current` so a
    // session poll never invents a control state it did not receive.
    control: { body: null, polling: false, busy: false },
    // NF-20: the plan card's own last read. Same rule: a session poll never
    // invents a plan, and a build/execute answer is what repaints this card.
    plan: { body: null, polling: false, busy: false },
    // NF-22: the autonomous card's own last read. Same rule again: a poll
    // never invents a run, and only the supervisor's answer repaints it.
    autonomy: { body: null, polling: false, busy: false },
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
    // NF-08: the closed loop. ``capability`` is read from the application (the
    // loop is composed AROUND the manager), ``result`` is the last run's own
    // record, and ``running`` disables both buttons rather than pretending a
    // request that can take a deadline is instant.
    loop: {
      capability: null,
      running: false,
      result: null,
    },
    // NF-10 trial rows and aggregates, read from the server's own evaluation
    // module. `null` means "not read yet", never "no trials".
    trials: null,
    // NF-15: unified observation. Nothing here predicts the pipeline's state:
    // `status` is the observer's own response, `lastKey` prevents re-fetching a
    // frame that was already validated, and `frameToken` makes a slow image
    // fetch harmless when the target changes or stops.
    observation: {
      status: null,
      timer: null,
      polling: false,
      busy: false,
      objectUrl: "",
      lastKey: "",
      frameToken: 0,
      // The ladder is read on demand (never on the 1.5 s poll): its first two
      // stages resolve a title against the catalog, which probes processes.
      ladderToken: 0,
      // A status poll that lands after a command response would repaint the
      // older state; the token makes the older answer harmless.
      statusToken: 0,
    },
    // NF-16: the last state reading, and one guard so a double-click cannot ask
    // for two readings (the service refuses the second anyway).
    understanding: {
      status: null,
      busy: false,
    },
    // NF-17: the last objective interpretation. `busy` guards the read button;
    // interpretation itself happens inside the state reading it consumes.
    objectives: {
      status: null,
      busy: false,
    },
    // NF-18: durable game knowledge for ONE game. `game` is the scope the last
    // read used; `body` is the server's own answer (never a synthesised one).
    knowledge: {
      body: null,
      game: "",
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
    // `mode` is the operator's choice, never inferred from a run. The plan is a
    // DRAFT held here (not read back from the DOM) so the payload, the signature
    // and the rendered summary can never disagree about what is proposed.
    evaluation: {
      games: [],
      selected: null,
      detail: null,
      scenarios: [],
      mode: "observation_only",
      preflight: null,
      signature: "",
      approval: null,
      control: null,
      plan: [],
      busy: false,
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
    // NF-12 state. `payload` is the LAST catalog read verbatim (never rebuilt
    // from the rows); `plan` is the exact preview awaiting approval and
    // `result` the last execution/close response. `token` makes a slow search
    // response harmless when the query changed while it was in flight.
    library: {
      payload: null,
      plan: null,
      result: null,
      query: "",
      token: 0,
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
    if (source === "window_surface") return "window surface";
    if (source === "screen") return "full screen";
    return source || "—";
  }

  function backendLabel(backend) {
    if (backend === "printwindow") return "PrintWindow (window surface)";
    if (backend === "gdi_display") return "GDI display crop";
    return backend || "—";
  }

  // The validation verdict in the operator's words. Each fact is printed
  // separately because they fail independently: a fresh timestamp proves
  // nothing about whether the pixels are the target.
  function validationLabel(validation) {
    if (!validation || typeof validation !== "object") return "not validated";
    var parts = [];
    parts.push(validation.capture_ok ? "captured" : "no capture");
    parts.push(validation.target_visible ? "target visible" : "target not visible");
    parts.push(validation.fresh ? "fresh" : "stale");
    parts.push(validation.content_ok ? "content is the target" : "content unverified");
    return parts.join(" · ");
  }

  // The target's visibility is a separate fact from its identity. A minimized
  // fullscreen game still has the same handle/pid, so the panel says
  // "minimized" instead of leaving the title looking like a live target.
  function targetVisibilityLabel(target) {
    if (!target) return "";
    if (target.minimized === true) return "minimized";
    if (target.visible === false) return "hidden";
    if (target.foreground === true) return "foreground";
    if (target.foreground === false) return "background";
    return "";
  }

  function targetLine(target) {
    var title = (target && target.title) || "";
    var state = targetVisibilityLabel(target);
    if (!title) return state;
    return state ? title + " · " + state : title;
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
    setText("forgeTarget", forge.targetLabel || forge.targetTitle || "no session target");
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

  // A frame is only live while the session is RUNNING. A paused session —
  // including one paused because its target was minimized — must not leave
  // the last image on screen looking current.
  function frameLiveState(state, frame, now) {
    if (TERMINAL.indexOf(state) >= 0) {
      return { show: false, mode: "session ended", stale: false };
    }
    if (state !== "RUNNING") {
      return { show: false, mode: state === "PAUSED" ? "paused" : "not live", stale: false };
    }
    if (!frame) return { show: false, mode: "waiting for frame", stale: false };
    // A frame is only shown as LIVE when it passed validation. A session frame
    // is normally already validated, but a record written before validation
    // existed (or a backend that reported itself unusable) must not borrow the
    // label: that is how H02 presented the desktop as gameplay. ``screen`` is
    // the full-desktop fallback and is never a frame OF the target.
    var verdict = frame.validation && typeof frame.validation === "object"
      ? frame.validation : {};
    if (frame.source === "screen" || verdict.livable === false) {
      return {
        show: false,
        mode: "target not visible — feed not live",
        stale: false,
        reason: verdict.reason || "The capture was not a validated frame of the target.",
      };
    }
    var freshness = frameFreshness(frame, now);
    return { show: true, mode: freshness.stale ? "stale" : "live", stale: freshness.stale };
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
    targetBox.appendChild(metricRow("Window state", targetVisibilityLabel(target) || "unknown",
      target.minimized === true
        ? "The target is minimized; capture and input are paused until it is restored."
        : target.visible === false
          ? "The target is not visible; capture and input are paused."
          : "",
      target.minimized !== true && target.visible !== false));
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
    var live = frameLiveState(state, frame, Date.now());
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
    if (!live.show) {
      image.hidden = true;
      image.removeAttribute("src");
      if (forge.objectUrl) {
        URL.revokeObjectURL(forge.objectUrl);
        forge.objectUrl = "";
      }
      forge.lastFrameKey = "";
      byId("forgeFrameStale").hidden = true;
      byId("forgeFrameMode").textContent = live.mode;
      var empty = visionEmptyText(state);
      if (live.reason) {
        empty = {
          title: "TARGET NOT VISIBLE — FEED NOT LIVE",
          detail: live.reason + (frame
            ? " Last accepted frame: seq " + frame.seq + " captured " + formatTime(frame.captured_at) + "."
            : ""),
        };
      }
      if (state !== "RUNNING" && state !== "PAUSED") {
        empty = {
          title: "Session " + (state || "inactive") + " — no live feed.",
          detail: "The frame area is deliberately blank unless the session is RUNNING.",
        };
      }
      setVisionEmpty(empty.title, empty.detail);
      return;
    }
    var key = forge.sessionId + ":" + frame.seq;
    var stale = live.stale;
    byId("forgeFrameStale").hidden = !stale;
    byId("forgeFrameMode").textContent = live.mode;

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
    var parameter = byId("forgeObjectiveParameter").value.trim();
    var mode = byId("forgeObjectiveMode").value || "contains";
    var region = (byId("forgeObjectiveRegion").value || "").trim();
    if (kind === "window_title") return { kind: kind, parameter: parameter };
    if (kind === "ocr_text") {
      var ocr = { kind: kind, parameter: parameter, mode: mode };
      // A blank region means the whole window frame; sending "" would be the
      // same fact stated twice, so it is omitted.
      if (region) ocr.region = region;
      return ocr;
    }
    return {
      kind: kind,
      description: "Recorded by the panel as an objective this build cannot check.",
    };
  }

  function syncObjectiveFields() {
    var kind = byId("forgeObjectiveKind").value;
    var label = byId("forgeObjectiveParameterLabel");
    var region = byId("forgeObjectiveRegion");
    if (label) {
      label.textContent = kind === "ocr_text"
        ? "Expected on-screen text"
        : kind === "window_title" ? "Expected title text" : "Free-text objective";
    }
    if (region) region.disabled = kind !== "ocr_text";
    if (byId("forgeObjectiveParameter")) {
      byId("forgeObjectiveParameter").placeholder = kind === "ocr_text"
        ? "e.g. LEVEL COMPLETE" : "e.g. READY";
    }
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

  /* ── NF-19: the bounded control interface ────────────────────────
   *
   * This card sends ONE typed input operation at a time through the session's
   * own guard, and it reports what the INPUT INTERFACE did. `completed` never
   * means the game reacted: every result carries `effect: null`, and the panel
   * says so in the same breath as the status.
   */

  function syncControlFields() {
    var kind = byId("forgeControlKind");
    if (!kind) return;
    var key = byId("forgeControlKey");
    var point = byId("forgeControlPoint");
    var value = kind.value;
    var keyed = value.indexOf("key_") === 0;
    var mouse = value.indexOf("mouse_") === 0;
    key.disabled = value === "mouse_move" || value === "mouse_press" ||
      value === "mouse_release";
    point.disabled = !mouse;
    key.placeholder = value === "key_hold" ? "w 120"
      : value === "wait" ? "250"
      : value === "mouse_hold" ? "120"
      : keyed ? "w" : "120";
    point.placeholder = value === "mouse_hold" ? "500,300 (client point)" : "500,300";
  }

  function controlPayloadFor(kind, keyFieldValue, pointFieldValue) {
    var keyField = String(keyFieldValue || "").trim();
    var pointField = String(pointFieldValue || "").trim();
    var payload = { kind: kind };
    var parts = keyField.split(/\s+/);
    function point() {
      var pair = pointField.split(",");
      var x = parseInt((pair[0] || "").trim(), 10);
      var y = parseInt((pair[1] || "").trim(), 10);
      // Both or neither: a half point is a request the server can only refuse,
      // and omitting it says "no point" instead of guessing zero.
      if (!isNaN(x) && !isNaN(y)) { payload.x = x; payload.y = y; }
    }
    function ms() {
      var value = parseInt(parts[0] || "", 10);
      payload.duration_ms = isNaN(value) ? 120 : value;
    }
    if (kind === "key_press" || kind === "key_release") payload.key = parts[0] || "";
    if (kind === "key_hold") { payload.key = parts[0] || ""; ms(); }
    if (kind === "mouse_move" || kind === "mouse_press" || kind === "mouse_release") point();
    if (kind === "mouse_hold") { point(); ms(); }
    if (kind === "wait") ms();
    return payload;
  }

  function controlPayload() {
    return controlPayloadFor(
      byId("forgeControlKind").value,
      byId("forgeControlKey").value,
      byId("forgeControlPoint").value
    );
  }

  // The buttons are gated on the SERVER's own view, never on the session state:
  // a RUNNING session with a closed gate cannot send, and an in-flight action
  // disables both sending and releasing (cancel is the way out). Pure, so a
  // Node test drives exactly the decision the panel ships.
  function inputControlState(status) {
    var session = (status && status.session) || null;
    var gate = (session && session.gate) || {};
    var available = Boolean(status && status.available);
    var active = Boolean(status && status.active);
    var held = (status && status.held) || [];
    var badge = !status ? "untested"
      : !session ? "no session"
      : !available ? "no input path"
      : gate.allow ? "ready" : String(gate.state || "blocked");
    return {
      badge: badge,
      available: available,
      active: active,
      held: held,
      run: available && Boolean(gate.allow) && !active,
      cancel: active,
      release: Boolean(held.length) && !active,
      heldText: controlHeldText(held),
      gateReason: available
        ? String(gate.reason || "")
        : "No gameplay input path is wired; nothing can be sent.",
    };
  }

  function controlHeldText(held) {
    if (!held || !held.length) return "No input is held by this interface.";
    return "Held by this interface: " + held.map(function (row) {
      return String(row.kind) + ":" + String(row.name);
    }).join(", ") + ". Release it, or let the session's cleanup free it.";
  }

  function renderControl(status) {
    forge.control.body = status || null;
    var badge = byId("forgeControlBadge");
    if (!badge) return;
    var view = inputControlState(status);
    var session = (status && status.session) || null;
    var gate = (session && session.gate) || {};
    var available = view.available;
    var active = view.active;
    var held = view.held;
    badge.textContent = view.badge;
    badge.dataset.state = view.badge;

    var lines = [];
    if (!status) {
      lines.push("The control interface has not been read for this session.");
    } else if (!available) {
      lines.push("No gameplay input path is wired; nothing can be sent.");
    } else {
      var policy = status.policy || {};
      lines.push("One bounded action at a time · deadline " +
        String(policy.action_deadline_ms || "?") + " ms · key hold ≤ " +
        String(policy.max_key_hold_ms || "?") + " ms · wait ≤ " +
        String(policy.max_wait_ms || "?") + " ms.");
      if (session && !gate.allow) lines.push("Gate: " + String(gate.reason || gate.state || "refused"));
      lines.push(controlHeldText(held));
      if (active && status.active) {
        lines.push("In flight: " + String(status.active.kind || "") + " (" +
          String(status.active.status || "") + ").");
      }
      var cleanup = status.last_cleanup || {};
      if (cleanup && cleanup.ok === false) {
        lines.push("Cleanup reported a problem: " + String(cleanup.note || "release failed") + ".");
      }
    }
    setText("forgeControlHint", lines.join(" "));

    var runButton = byId("forgeControlRunButton");
    var cancelButton = byId("forgeControlCancelButton");
    var releaseButton = byId("forgeControlReleaseButton");
    if (runButton) runButton.disabled = !view.run || forge.control.busy;
    if (cancelButton) cancelButton.disabled = !view.cancel;
    if (releaseButton) releaseButton.disabled = !view.release || forge.control.busy;

    var log = byId("forgeControlLog");
    if (!log) return;
    log.innerHTML = "";
    var rows = (status && status.history) || [];
    if (!rows.length) {
      log.appendChild(el("li", "muted", "No control action has been sent for this session."));
      return;
    }
    rows.slice(0, 6).forEach(function (row) {
      var item = el("li", "forge-action-row");
      item.dataset.status = String(row.status || "");
      item.appendChild(el("strong", "", String(row.status || "") + " · " + String(row.kind || "")));
      item.appendChild(el("span", "muted", " " + String(row.reason || "")));
      log.appendChild(item);
    });
  }

  async function loadControlStatus() {
    if (!forge.sessionId || forge.control.polling) return;
    var token = forge.stateToken;
    forge.control.polling = true;
    try {
      var body = await requestJson(
        "/novaforge/sessions/" + encodeURIComponent(forge.sessionId) + "/control");
      if (!responseStillCurrent(token, forge.stateToken)) return;
      renderControl(body);
    } catch (error) {
      // A missing/finished session is not an error to shout about; the card
      // says what it could not read rather than keeping a stale "ready".
      renderControl(null);
    } finally {
      forge.control.polling = false;
    }
  }

  async function postControl(path, payload, what) {
    if (forge.control.busy) return null;
    forge.control.busy = true;
    notice("", "");
    try {
      return await requestJson(path, payload === null ? {} : payload);
    } catch (error) {
      notice(what + " failed: " + String(error.message || error), "error");
      return null;
    } finally {
      forge.control.busy = false;
    }
  }

  async function sendControlAction() {
    if (!forge.sessionId) return;
    var answer = await postControl(
      "/novaforge/sessions/" + encodeURIComponent(forge.sessionId) + "/control",
      controlPayload(), "Control action");
    if (!answer) return;
    var result = answer.result || {};
    notice(result.ok
      ? "Input operation completed. No in-game effect was observed: completion is not gameplay success."
      : "Control action " + String(answer.status || "refused") + " (" +
        String(answer.code || "") + "): " + String(answer.reason || ""),
      result.ok ? "" : "error");
    await loadControlStatus();
    await refreshSession();
  }

  async function cancelControlAction() {
    if (!forge.sessionId) return;
    var answer = await postControl(
      "/novaforge/sessions/" + encodeURIComponent(forge.sessionId) + "/control/cancel",
      null, "Cancel action");
    if (!answer) return;
    notice(answer.cancelled
      ? String(answer.reason || "Cancel requested.")
      : String(answer.reason || "No control action is in flight."),
      answer.cancelled ? "" : "error");
    await loadControlStatus();
  }

  async function releaseControlInputs() {
    if (!forge.sessionId) return;
    var answer = await postControl(
      "/novaforge/sessions/" + encodeURIComponent(forge.sessionId) + "/control/release",
      null, "Release inputs");
    if (!answer) return;
    var cleanup = answer.cleanup || {};
    notice(cleanup.ok
      ? "Released " + String(cleanup.released || 0) + " input(s); nothing is held by this interface."
      : "Release incomplete: " + String(cleanup.note || "input may still be held."),
      cleanup.ok ? "" : "error");
    await loadControlStatus();
  }

  /* ── NF-20: bounded planning ────────────────────────────────────
   *
   * A plan is built from the session's verified state, objective and knowledge,
   * scored by declared criteria, and executed ONLY after approval. Every status
   * here distinguishes a proposal from an input operation, and no receipt ever
   * claims an in-game effect: that belongs to NF-21.
   */

  var PLAN_ENDED = [
    "executed", "stopped", "failed", "cancelled", "rejected",
    "insufficient_information", "no_action", "replan_required",
    "replan_exhausted", "invalidated"
  ];

  // Pure: the button rules, shared by the card and its tests. `build` follows
  // the SERVER's gate, `execute` follows the plan's own approval state, and a
  // running plan disables everything but Cancel.
  function planView(status) {
    var view = {
      badge: "untested",
      build: false,
      execute: false,
      cancel: false,
      replan: false,
      current: null,
      gateReason: "No plan has been read for this session."
    };
    if (!status) return view;
    var session = status.session || {};
    var gate = session.gate || {};
    var current = status.current || null;
    var running = Boolean(status.running);
    var busy = Boolean(forge.plan.busy);
    var name = current ? String(current.status || "") : "";
    var policy = status.policy || {};
    view.current = current;
    view.badge = running ? "running" : (name || (status.available ? "ready" : "untested"));
    view.gateReason = gate.allow ? "" : String(gate.reason || gate.state || "blocked");
    view.build = Boolean(gate.allow) && !running && !busy;
    view.execute = Boolean(gate.allow) && !running && !busy &&
      Boolean(current && (current.steps || []).length) &&
      (name === "awaiting_approval" || name === "planned");
    view.cancel = !busy && (name === "awaiting_approval" || name === "planned" || running);
    view.replan = Boolean(gate.allow) && !running && !busy && Boolean(current) &&
      PLAN_ENDED.indexOf(name) >= 0 &&
      Number(current.replan_count || 0) < Number(policy.max_replans || 0);
    return view;
  }

  function planAgeText(seconds) {
    if (seconds === null || seconds === undefined) return "unreadable";
    return Number(seconds).toFixed(1) + " s";
  }

  function renderPlan(status) {
    forge.plan.body = status || null;
    var badge = byId("forgePlanBadge");
    if (!badge) return;
    var view = planView(status);
    var current = view.current;
    badge.textContent = view.badge;
    badge.dataset.state = view.badge;

    var buildButton = byId("forgePlanBuildButton");
    var executeButton = byId("forgePlanExecuteButton");
    var cancelButton = byId("forgePlanCancelButton");
    var replanButton = byId("forgePlanReplanButton");
    if (buildButton) buildButton.disabled = !view.build;
    if (executeButton) executeButton.disabled = !view.execute;
    if (cancelButton) cancelButton.disabled = !view.cancel;
    if (replanButton) replanButton.disabled = !view.replan;

    var lines = [];
    if (!status) {
      lines.push("The planner has not been read for this session.");
    } else if (view.gateReason) {
      lines.push("Planning gate: " + view.gateReason);
    }
    if (current) {
      lines.push("Plan " + String(current.plan_id || "") + " · " +
        String(current.status || "") + (current.code ? " (" + String(current.code) + ")" : "") + ".");
      if (current.reason) lines.push(String(current.reason));
    } else if (status) {
      lines.push("No plan is current. Build one from the latest verified state; nothing is sent until you approve it.");
    }
    setText("forgePlanHint", lines.join(" "));

    var facts = byId("forgePlanFacts");
    if (facts) {
      facts.innerHTML = "";
      if (current) {
        var objective = current.objective || {};
        var refs = current.evidence_refs || {};
        var budgets = current.budgets || {};
        var approval = current.approval || {};
        [
          ["Objective", String(objective.description || "unavailable")],
          ["State", String(refs.state_id || "unavailable") + " · " + planAgeText(refs.state_age_seconds)],
          ["Steps", String((current.steps || []).length) + " of " + String(budgets.max_actions || "?")],
          ["Approval", String(approval.status || "unknown")],
          ["Replans", String(current.replan_count || 0)],
          ["Effect", "not observed (NF-21)"]
        ].forEach(function (pair) {
          var row = el("div", "");
          row.appendChild(el("dt", "", pair[0]));
          row.appendChild(el("dd", "", pair[1]));
          facts.appendChild(row);
        });
      }
    }

    var candidates = byId("forgePlanCandidates");
    if (candidates) {
      candidates.innerHTML = "";
      var rows = (current && current.candidates) || [];
      if (rows.length) {
        var list = el("ul", "");
        rows.slice(0, 6).forEach(function (row) {
          var item = el("li", "");
          item.dataset.feasible = row.feasible ? "true" : "false";
          item.appendChild(el("strong", "", (row.feasible ? "selected: " : "rejected: ") +
            String(row.description || row.candidate_id || "")));
          var note = row.feasible
            ? "score " + String(row.score === null || row.score === undefined ? "n/a" : row.score) +
              " · risk " + String(row.risk || "") + " · cost " + String(row.expected_cost_ms || 0) + " ms"
            : String(row.rejection || "a precondition was not true");
          item.appendChild(el("span", "muted", " " + note));
          list.appendChild(item);
        });
        candidates.appendChild(list);
      }
    }

    var history = byId("forgePlanHistory");
    if (history) {
      history.innerHTML = "";
      var historyRows = (status && status.history) || [];
      if (!historyRows.length) {
        history.appendChild(el("li", "muted", "No plan has been built for this session."));
      } else {
        historyRows.slice(0, 6).forEach(function (row) {
          var item = el("li", "forge-action-row");
          item.dataset.status = String(row.status || "");
          item.appendChild(el("strong", "", String(row.status || "") +
            (row.code ? " (" + String(row.code) + ")" : "")));
          item.appendChild(el("span", "muted", " " + String(row.reason || "")));
          history.appendChild(item);
        });
      }
    }
  }

  async function loadPlanStatus() {
    if (!forge.sessionId || forge.plan.polling) return;
    var token = forge.stateToken;
    forge.plan.polling = true;
    try {
      var body = await requestJson(
        "/novaforge/sessions/" + encodeURIComponent(forge.sessionId) + "/plan");
      if (!responseStillCurrent(token, forge.stateToken)) return;
      renderPlan(body);
    } catch (error) {
      renderPlan(null);
    } finally {
      forge.plan.polling = false;
    }
  }

  async function postPlan(path, payload, what) {
    if (forge.plan.busy) return null;
    forge.plan.busy = true;
    notice("", "");
    try {
      return await requestJson(path, payload === null ? {} : payload);
    } catch (error) {
      notice(what + " failed: " + String(error.message || error), "error");
      return null;
    } finally {
      forge.plan.busy = false;
    }
  }

  function planPath(suffix) {
    return "/novaforge/sessions/" + encodeURIComponent(forge.sessionId) + "/plan" + (suffix || "");
  }

  async function buildPlan() {
    if (!forge.sessionId) return;
    var answer = await postPlan(planPath(""), null, "Build plan");
    if (!answer) return;
    var plan = answer.plan || {};
    notice(plan.status === "awaiting_approval"
      ? "Plan built and awaiting approval. Nothing has been sent."
      : "No plan was selected: " + String(plan.status || "refused") +
        (plan.code ? " (" + String(plan.code) + ")" : "") + ". " + String(plan.reason || ""),
      plan.status === "awaiting_approval" ? "" : "error");
    renderPlan(await loadPlanBody());
  }

  async function loadPlanBody() {
    // One read after a mutation keeps the card on the server's own record.
    try {
      return await requestJson(planPath(""));
    } catch (error) {
      return null;
    }
  }

  async function executePlan() {
    var status = forge.plan.body || {};
    var current = status.current || {};
    if (!forge.sessionId || !current.plan_id) return;
    // The approval is the SERVER-minted token carried by this plan preview, not
    // the `approved` flag: it is single-use and bound to this plan id, so an
    // approval can never be replayed onto another plan or a later run.
    var approval = current.approval || {};
    var answer = await postPlan(planPath("/execute"),
      { plan_id: current.plan_id, approved: true, approval_token: approval.token || "" },
      "Execute plan");
    if (!answer) {
      // A missing or expired token is a refusal, not a dead end: repaint from a
      // fresh preview so the next press approves the plan on screen.
      renderPlan(await loadPlanBody());
      return;
    }
    var plan = answer.plan || {};
    var executed = plan.status === "executed";
    notice(executed
      ? "Plan executed: every planned input operation completed. The in-game effect was NOT observed or verified."
      : "Plan ended " + String(plan.status || "refused") +
        (plan.code ? " (" + String(plan.code) + ")" : "") + ": " + String(plan.reason || ""),
      executed ? "" : "error");
    renderPlan(await loadPlanBody());
    await refreshSession();
  }

  async function cancelPlan() {
    var status = forge.plan.body || {};
    var current = status.current || {};
    if (!forge.sessionId) return;
    var answer = await postPlan(planPath("/cancel"),
      current.plan_id ? { plan_id: current.plan_id } : {}, "Cancel plan");
    if (!answer) return;
    notice(answer.cancelled
      ? String((answer.plan && answer.plan.reason) || "Plan cancelled before the next step.")
      : "No cancellable plan was running.", answer.cancelled ? "" : "error");
    renderPlan(await loadPlanBody());
  }

  async function replanPlan() {
    var status = forge.plan.body || {};
    var current = status.current || {};
    if (!forge.sessionId || !current.plan_id) return;
    var answer = await postPlan(planPath("/replan"),
      { plan_id: current.plan_id, reason: "operator" }, "Replan");
    if (!answer) return;
    var plan = answer.plan || {};
    notice(plan.status === "awaiting_approval"
      ? "Replanned from fresh evidence; the new plan awaits approval."
      : "Replanning did not produce a runnable plan: " + String(plan.status || "refused") +
        (plan.code ? " (" + String(plan.code) + ")" : "") + ". " + String(plan.reason || ""),
      plan.status === "awaiting_approval" ? "" : "error");
    renderPlan(await loadPlanBody());
  }

  /* ── NF-22: one bounded autonomous run ───────────────────────────
   *
   * The card is a CLIENT of the supervisor the backend owns: it renders the
   * phase, the pinned objective, the plan, the budgets and the last verdict, and
   * its controls post the operator's own decisions (start with approval, pause,
   * resume, cancel, and one answer per parked plan). Nothing here starts a run,
   * widens a budget or restarts one that ended, and a run whose frames come from
   * the simulated world is labelled simulated rather than live.
   */

  function autonomyPath(suffix) {
    return "/novaforge/sessions/" + encodeURIComponent(forge.sessionId) + "/autonomy" + (suffix || "");
  }

  function autonomyRun(body) {
    return (body && body.runs && body.runs[0]) || null;
  }

  // The button rules, pure and shared with the tests: Start follows the run's own
  // permission (approval is the operator's), Pause needs a live run, Resume needs
  // a paused one, and a run that ended enables nothing at all.
  function autonomyView(body) {
    var run = autonomyRun(body);
    var view = {
      badge: body && body.available ? "idle" : "untested",
      phase: run ? String(run.phase || "") : "",
      start: false,
      pause: false,
      resume: false,
      cancel: false,
      approve: false,
      reject: false,
      reason: "No autonomous run has been read for this session."
    };
    if (!body) return view;
    var busy = Boolean(forge.autonomy.busy);
    var terminal = Boolean(run && run.terminal);
    view.phase = run ? String(run.phase || "") : "";
    // Only a run whose evidence the supervisor reports as LIVE may wear the
    // autonomous label. A run driving the simulated world says so instead: a
    // badge that reads "autonomous" over simulated evidence would tell the
    // operator the machine is controlling a game when it is not.
    view.evidence_source = String((run && run.evidence_source) || body.evidence_source || "");
    var live = view.evidence_source === "live";
    view.badge = run
      ? (terminal ? "ended" : (live ? "autonomous" : "simulated"))
      : (body.available ? "idle" : "untested");
    view.start = Boolean(body.can_start) && !busy;
    // Pause is offered only while something is actually running to pause: a
    // parked run is waiting on the operator's answer, and a control that does
    // nothing when pressed teaches the operator to distrust the card.
    view.pause = Boolean(run && run.active && view.phase !== "PAUSED"
      && view.phase !== "WAITING_FOR_APPROVAL") && !busy;
    view.resume = Boolean(run && view.phase === "PAUSED") && !busy;
    view.cancel = Boolean(run && run.active) && !busy;
    view.approve = Boolean(run && view.phase === "WAITING_FOR_APPROVAL") && !busy;
    view.reject = view.approve;
    view.reason = run
      ? (String(run.reason || "") || "The run is " + view.phase + ".")
      : "No autonomous run yet. Approval is required: a run sends bounded input to the game.";
    return view;
  }

  function autonomyBudgets(limits) {
    return "cycles " + String(limits.max_cycles || "?") +
      ", actions " + String(limits.max_actions || "?") +
      " (max " + String(limits.max_actions_per_plan || "?") + " per plan)" +
      ", " + String(limits.session_seconds || "?") + " s" +
      ", recoveries " + String(limits.recovery_attempts || 0) +
      ", replans " + String(limits.replans || 0);
  }

  // What the card shows. Everything is read from the supervisor's own record: the
  // pinned objective, the criteria it was approved against, the state and plan of
  // the last cycle, the evidence source, the budgets and the last verdict.
  function autonomyFactList(body) {
    var run = autonomyRun(body);
    var limits = (body && body.limits) || {};
    var policy = (body && body.policy) || {};
    var resources = (body && body.resources) || {};
    if (!run) {
      return [
        ["Evidence source", String((body && body.evidence_source) || "unknown")],
        ["Approval", (body && body.approval_required)
          ? "required before anything is sent" : "not required by the policy"],
        ["Plan approval", String(policy.plan_approval || "run")],
        ["Resource gate", resources.available
          ? (String(resources.level || "") + ": " + String(resources.reason || ""))
          : "not measured on this host"],
        ["Budgets", autonomyBudgets(limits)],
        ["Objective", "none pinned: a run pins its objective and completion criteria before its first cycle."]
      ];
    }
    var cycles = run.cycles || [];
    var last = cycles.length ? cycles[cycles.length - 1] : {};
    var planCycle = autonomyPlanCycle(cycles);
    var objective = last.objective || {};
    var state = last.state || {};
    var plan = planCycle.plan || {};
    var approval = planCycle.approval || {};
    var planCycleNote = (planCycle.index && planCycle !== last)
      ? " (cycle " + String(planCycle.index) + ")" : "";
    var verification = last.verification || {};
    var observation = last.observation || {};
    var steps = run.steps || [];
    var action = steps.length ? steps[steps.length - 1] : {};
    return [
      ["Game", String(run.game || "unknown")],
      ["Session", String(run.session_id || "")],
      ["Evidence source", String(run.evidence_source || "unknown")],
      ["Phase", String(run.phase || "") +
        (run.active ? " (controlling)" : (run.terminal ? " (ended)" : ""))],
      ["Objective", String(run.objective || run.objective_id || "unread")],
      ["Completion criteria", (run.completion_criteria || []).join("; ") || "none declared"],
      ["State", String(state.status || "unread")],
      ["Objective status", String(objective.status || "unread")],
      ["Plan", plan.plan_id
        ? (String(plan.plan_id) + " (" + String(plan.status || "") + ", " +
          String((plan.steps || []).length) + " step(s))" + planCycleNote)
        : "none in this cycle"],
      ["Dispatched actions", String(run.actions_dispatched || 0) +
        " of " + String(limits.max_actions || "?")],
      ["Approval", approval.mode === "run"
        ? "covered by the run's own approval"
        : (approval.approved ? "approved by the operator" : String(approval.reason || "not answered"))],
      ["Last observation", String(observation.captured_at || "none")],
      ["Last verified outcome", verification.status
        ? (String(verification.status) + " — " + String(verification.reason || ""))
        : (run.outcome ? String(run.outcome) : "none yet")],
      ["Recoveries / replans", String(run.recoveries || 0) + " / " + String(run.replans || 0)],
      ["Budgets", autonomyBudgets(limits)],
      ["Elapsed", (run.elapsed_seconds === null || run.elapsed_seconds === undefined)
        ? "running"
        : (String(run.elapsed_seconds) + " s of " + String(limits.session_seconds || "?") + " s")],
      ["Last cycle step", String(action.phase || "") +
        (action.outcome ? " (" + String(action.outcome) + ")" : "")]
    ];
  }

  // The CURRENT plan is the newest plan the run built. A cycle that ends the run
  // can hold no plan at all (the one that observed completion), and a cycle that
  // failed before planning holds none either — reading the last cycle alone would
  // show "no plan" after three dispatched actions, which is exactly the opposite
  // of what the operator needs to audit.
  function autonomyPlanCycle(cycles) {
    var rows = cycles || [];
    for (var index = rows.length - 1; index >= 0; index -= 1) {
      if ((((rows[index] || {}).plan || {}).steps || []).length) return rows[index];
    }
    return rows.length ? rows[rows.length - 1] : {};
  }

  // Scope H asks for the planned and executed ACTIONS, not only their count: the
  // current plan's own steps, and NF-19's record for each one is what happened to
  // it. A step with no record was never dispatched — a parked plan's steps are
  // listed with exactly that.
  function autonomyActionList(body) {
    var run = autonomyRun(body);
    var cycles = (run && run.cycles) || [];
    var last = autonomyPlanCycle(cycles);
    var prefix = cycles.length > 1 && last.index ? "cycle " + String(last.index) + " · " : "";
    var steps = ((last.plan || {}).steps) || [];
    var records = ((last.execution || {}).steps) || [];
    var byAction = {};
    records.forEach(function (record) {
      byAction[String(record.action_id || record.index)] = record;
    });
    return steps.map(function (step) {
      var record = byAction[String(step.action_id || step.index)] || {};
      var status = record.status
        ? String(record.status)
        : (last.approval && last.approval.approved === false ? "refused" : "not dispatched");
      return prefix + String(step.index || "?") + ". " + String(step.kind || "action") + " — " +
        String(step.description || step.expected_effect || "no description") +
        " (" + status + (record.code ? ": " + String(record.code) : "") + ")";
    });
  }

  function renderAutonomy(body) {
    var view = autonomyView(body);
    var run = autonomyRun(body);
    var badge = byId("forgeAutonomyBadge");
    if (badge) {
      badge.textContent = view.badge;
      badge.dataset.state = view.badge === "autonomous" ? "active"
        : (view.badge === "ended" ? "done"
          : (view.badge === "simulated" ? "simulated" : "untested"));
    }
    setText("forgeAutonomyHint", view.phase ? (view.phase + ": " + view.reason) : view.reason);
    var facts = byId("forgeAutonomyFacts");
    if (facts) {
      facts.textContent = "";
      autonomyFactList(body).forEach(function (pair) {
        var wrap = el("div", "forge-observation-fact");
        wrap.appendChild(el("dt", "", pair[0]));
        wrap.appendChild(el("dd", "", pair[1] === "" ? "—" : String(pair[1])));
        facts.appendChild(wrap);
      });
    }
    var actionList = byId("forgeAutonomyActions");
    if (actionList) {
      actionList.textContent = "";
      var actionRows = run ? autonomyActionList(body) : [];
      if (!actionRows.length) {
        actionList.appendChild(el("li", "muted", run
          ? "The last cycle built no plan."
          : "No plan has been built for this session yet."));
      }
      actionRows.forEach(function (row) {
        actionList.appendChild(el("li", "", row));
      });
    }
    var cycles = byId("forgeAutonomyCycles");
    if (cycles) {
      cycles.textContent = "";
      var rows = (run && run.cycles) || [];
      if (!rows.length) {
        cycles.appendChild(el("li", "muted", "No decision cycle has run yet."));
      }
      rows.forEach(function (row) {
        var rowPlan = row.plan || {};
        var rowVerification = row.verification || {};
        var outcome = row.outcome || rowVerification.status || rowPlan.status || "";
        cycles.appendChild(el("li", "", "cycle " + String(row.index) + ": " +
          String(row.phase || "") + " — " + String(row.reason || "") +
          (outcome ? " (" + String(outcome) + ")" : "") +
          (row.actions_dispatched
            ? " [" + String(row.actions_dispatched) + " dispatched]" : "")));
      });
    }
    var approval = byId("forgeAutonomyApproval");
    if (approval) approval.hidden = view.phase !== "WAITING_FOR_APPROVAL";
    var buttons = {
      forgeAutonomyStartButton: view.start,
      forgeAutonomyPauseButton: view.pause,
      forgeAutonomyResumeButton: view.resume,
      forgeAutonomyCancelButton: view.cancel,
      forgeAutonomyApproveButton: view.approve,
      forgeAutonomyRejectButton: view.reject
    };
    Object.keys(buttons).forEach(function (id) {
      var button = byId(id);
      if (button) button.disabled = !buttons[id];
    });
    if (!run) {
      setText("forgeAutonomyOutcome", "No autonomous run for this session.");
      return;
    }
    setText("forgeAutonomyOutcome", run.terminal
      ? ("Ended: " + String(run.outcome || run.phase) + " — " + String(run.reason || ""))
      : ("Running: " + String(run.phase || "") + " (" + String(run.evidence_source || "") + " evidence)"));
  }

  async function loadAutonomy() {
    if (!forge.sessionId || forge.autonomy.polling) return;
    var token = forge.stateToken;
    forge.autonomy.polling = true;
    try {
      var body = await requestJson(autonomyPath(""));
      if (!responseStillCurrent(token, forge.stateToken)) return;
      forge.autonomy.body = body;
      renderAutonomy(body);
    } catch (error) {
      // A session that has ended is not an error to shout about: the card says
      // what it could not read instead of keeping a stale "autonomous" label.
      forge.autonomy.body = null;
      renderAutonomy(null);
    } finally {
      forge.autonomy.polling = false;
    }
  }

  async function postAutonomy(path, payload, what) {
    if (!forge.sessionId || forge.autonomy.busy) return null;
    forge.autonomy.busy = true;
    notice("", "");
    try {
      return await requestJson(autonomyPath(path), payload === null ? {} : payload);
    } catch (error) {
      notice(what + " failed: " + String(error.message || error), "error");
      return null;
    } finally {
      forge.autonomy.busy = false;
    }
  }

  async function startAutonomy() {
    var answer = await postAutonomy("/start",
      { approved: true, task: "", note: "operator" }, "Start autonomous run");
    if (!answer) return;
    notice(answer.started
      ? ("Autonomous run started on " + String(answer.evidence_source) +
        " evidence. It drives the game through the session's own control " +
        "interface until its objective, one of its budgets or the operator ends it.")
      : ("Nothing was started: " + String(answer.code || "") + " — " + String(answer.reason || "")),
      answer.started ? "" : "error");
    await loadAutonomy();
    await refreshSession();
  }

  async function pauseAutonomy() {
    var answer = await postAutonomy("/pause", { reason: "operator" }, "Pause run");
    if (!answer) return;
    notice(String(answer.reason || "Pause requested."), answer.paused ? "" : "error");
    await loadAutonomy();
  }

  async function resumeAutonomy() {
    var answer = await postAutonomy("/resume", null, "Resume run");
    if (!answer) return;
    notice(String(answer.reason || "Resume requested."), answer.resumed ? "" : "error");
    await loadAutonomy();
    await refreshSession();
  }

  async function cancelAutonomy() {
    var answer = await postAutonomy("/cancel", { reason: "operator" }, "Cancel run");
    if (!answer) return;
    notice(String(answer.reason || "Cancel requested."), answer.cancelled ? "" : "error");
    await loadAutonomy();
  }

  async function answerAutonomy(approved) {
    var answer = await postAutonomy("/approve",
      {
        approved: approved,
        note: approved ? "operator approved this plan" : "operator refused this plan"
      },
      approved ? "Approve plan" : "Refuse plan");
    if (!answer) return;
    notice(String(answer.reason || (approved ? "Plan approved." : "Plan refused.")),
      answer.approved === approved ? "" : "error");
    await loadAutonomy();
  }

  /* ── NF-08: the closed loop ────────────────────────────────────── */

  function loopStatusText(status) {
    var labels = {
      completed: "completed — objective verified",
      failed: "failed — the verifier decided against it",
      stopped: "stopped before/inside the loop",
      refused: "refused by a gate",
      canceled: "cancelled by the operator",
      unavailable: "unavailable on this host",
      error: "error",
    };
    return labels[String(status || "")] || String(status || "no run");
  }

  function trialOutcomeText(outcome) {
    var labels = {
      verified_success: "verified success",
      verified_failure: "verified failure",
      inconclusive: "inconclusive — the verifier did not decide",
      refused: "refused by a gate",
      aborted: "aborted",
      verification_error: "verification error",
      unavailable: "unavailable on this host",
      error: "error",
    };
    return labels[String(outcome || "")] || String(outcome || "no outcome");
  }

  function renderTrialHistory(record) {
    var out = byId("forgeTrialHistory");
    if (!out) return;
    out.innerHTML = "";
    // ``record`` is the fetched payload; omitting it renders the last one read.
    var data = record || forge.trials;
    if (!data) {
      out.appendChild(el("p", "muted", "Reading the trial record…"));
      return;
    }
    if (data.error) {
      out.appendChild(el("p", "muted", "Trial history unavailable: " + String(data.error)));
      return;
    }
    var rows = data.trials || [];
    var summaries = data.summaries || [];
    var head = el("div", "forge-card-head");
    head.appendChild(el("strong", "", "Trial history"));
    head.appendChild(el("span", "forge-badge",
      rows.length + (rows.length === 1 ? " trial" : " trials")));
    out.appendChild(head);
    if (!rows.length) {
      out.appendChild(el("p", "muted",
        "No policy trial has finished in this process yet. A run appears here once " +
        "the loop returns, whatever its outcome."));
      return;
    }
    // Aggregates come from the server's policy_eval, so the panel and the report
    // cannot disagree about what happened. A figure with no samples is omitted,
    // never shown as zero.
    summaries.forEach(function (summary) {
      var outcomes = summary.outcomes || {};
      var trials = Number(summary.trials || 0);
      var line = [
        String(summary.policy || "policy") +
          (summary.policy_version ? " v" + Number(summary.policy_version) : ""),
        trials + (trials === 1 ? " trial" : " trials"),
        "verified: " + Number(outcomes.verified_success || 0) + " success / " +
          Number(outcomes.verified_failure || 0) + " failure",
        summary.success_rate == null ? "rate unavailable" :
          "rate " + Number(summary.success_rate),
        summary.actions_executed_mean == null ? "" :
          "actions " + Number(summary.actions_executed_mean),
        summary.elapsed_seconds_mean == null ? "" :
          "mean " + Number(summary.elapsed_seconds_mean) + " s",
        "decided differently after a transition: " +
          Number(summary.responded_to_transition_trials || 0) + "/" + trials,
        summary.model_backed_trials == null || Number(summary.model_backed_trials) === 0
          ? "no model-backed decision"
          : "model-backed: " + Number(summary.model_backed_trials),
      ].filter(Boolean).join(" · ");
      out.appendChild(el("p", "muted", line));
    });
    var list = el("ul", "forge-history-list");
    rows.slice().reverse().slice(0, 5).forEach(function (trial) {
      var row = el("li", "forge-action-row");
      row.dataset.status = String(trial.outcome || "");
      row.appendChild(el("strong", "",
        "#" + Number(trial.trial || 0) + " " + trialOutcomeText(trial.outcome)));
      var meta = [
        String(trial.policy || ""),
        trial.actions == null ? "" : "actions: " + Number(trial.actions),
        trial.refused ? "refused: " + Number(trial.refused) : "",
        trial.elapsed_s == null ? "" : Number(trial.elapsed_s) + " s",
        trial.responded_to_transition
          ? "decided differently after a transition"
          : "no changed decision",
      ].filter(Boolean).join(" · ");
      row.appendChild(el("div", "muted", meta));
      if (trial.reason) row.appendChild(el("div", "muted", String(trial.reason)));
      list.appendChild(row);
    });
    out.appendChild(list);
    var note = String(data.note || "");
    if (note) out.appendChild(el("p", "forge-limit-hint", note));
  }

  async function refreshTrialHistory() {
    try {
      forge.trials = await requestJson("/novaforge/trials");
    } catch (error) {
      forge.trials = { error: String(error.message || error) };
    }
    renderTrialHistory();
  }

  function renderLoop(data) {
    var badge = byId("forgeLoopBadge");
    var policy = byId("forgeLoopPolicy");
    var runButton = byId("forgeLoopRunButton");
    var cancelButton = byId("forgeLoopCancelButton");
    var body = byId("forgeLoopResult");
    if (!badge || !policy || !runButton || !cancelButton || !body) return;
    var capability = forge.loop.capability;
    var available = !!(capability && capability.available);
    var active = !!(data && ACTIVE.indexOf(data.state) >= 0);
    badge.textContent = capability
      ? (available
        ? String((capability.policy && capability.policy.policy) || "installed")
        : "unavailable")
      : "checking";
    badge.dataset.available = available ? "true" : "false";
    if (capability) {
      var modelBacked = Boolean(capability.policy && capability.policy.model_backed);
      var version = capability.policy && capability.policy.version;
      policy.textContent = (available ? "Decision pipeline: " : "Decision pipeline unavailable: ") +
        String(capability.reason || "") +
        (available
          ? (modelBacked ? " Model-backed." : " The installed policy is a deterministic rule, not a gameplay brain.")
          : "") +
        (version ? " Policy version " + String(version) + "." : "");
    }
    // Fill the selector from the capability when the build offers policies the
    // static markup does not know, and keep the operator's current choice.
    var policySelect = byId("forgeLoopPolicySelect");
    if (policySelect && capability && capability.policies && capability.policies.length) {
      var chosen = policySelect.value;
      capability.policies.forEach(function (row) {
        if (!row || !row.id) return;
        var exists = false;
        for (var i = 0; i < policySelect.options.length; i += 1) {
          if (policySelect.options[i].value === row.id) { exists = true; break; }
        }
        if (!exists) {
          var option = document.createElement("option");
          option.value = row.id;
          option.textContent = String(row.policy || row.id) +
            (row.model_backed ? " (model-backed)" : "");
          policySelect.appendChild(option);
        }
      });
      if (chosen) policySelect.value = chosen;
    }
    runButton.disabled = !(active && available && !forge.loop.running);
    cancelButton.disabled = !forge.loop.running;
    // NF-09: visual interpretation is reported on its OWN line. "A model can
    // see" is a different capability from "a model can decide", and neither
    // implies the other, so the panel never lets one sentence stand for both.
    var visionLine = byId("forgeLoopVision");
    var describeButton = byId("forgeDescribeFrame");
    var visionCapability = capability && capability.vision;
    if (visionLine) {
      if (!visionCapability) {
        visionLine.textContent = "Visual interpretation: this build did not report one.";
      } else if (visionCapability.available) {
        visionLine.textContent = "Visual interpretation: " +
          (visionCapability.model || visionCapability.provider || "a vision model") +
          (visionCapability.provider ? " via " + visionCapability.provider : "") +
          " · a model description is a claim, not evidence.";
      } else {
        visionLine.textContent = "Visual interpretation unavailable: " +
          String(visionCapability.reason || "no image-capable model is wired") +
          " OCR and window-title evidence remain available.";
      }
    }
    if (describeButton) {
      describeButton.disabled = !(active && forge.sessionId);
    }

    body.innerHTML = "";
    if (forge.loop.running) {
      body.appendChild(el("p", "muted",
        "Running… the loop stops at the first untrusted observation, refused action, " +
        "spent budget or deadline. This request returns when it stops."));
      return;
    }
    var result = forge.loop.result;
    if (!result) {
      body.appendChild(el("p", "muted", active
        ? "No closed loop has run for this session."
        : "Start (or resume) a session to run the loop against it."));
      return;
    }
    var head = el("div", "forge-card-head");
    head.appendChild(el("strong", "", "Closed loop: " + loopStatusText(result.status)));
    head.appendChild(el("span", "forge-badge", verificationBadge(result.verification)));
    body.appendChild(head);
    body.appendChild(el("p", "muted", String(result.reason || "No reason was reported.")));
    var reportedPolicy = result.policy ||
      (capability && capability.policy && capability.policy.policy) || "not reported";
    var verification = result.verification || null;
    if (verification) {
      // The verdict is the VERIFIER's, quoted with its own reason: the loop's
      // stop reason is a different fact and must not stand in for it.
      body.appendChild(el("p", "muted",
        "Verifier: " + String(verification.outcome || "no verdict") +
        (verification.reason ? " — " + String(verification.reason) : "")));
    }
    body.appendChild(el("p", "muted",
      "Policy: " + String(reportedPolicy) +
      (result.policy_version ? " v" + Number(result.policy_version) : "") +
      (result.model_backed
        ? " · at least one decision was model-backed"
        : " · deterministic decisions only") +
      " · actions executed: " + Number(result.actions_executed || 0) +
      " · refused: " + Number(result.actions_refused || 0) +
      " · decisions: " + Number(result.decisions || 0) +
      (result.elapsed_seconds == null ? "" : " · " + Number(result.elapsed_seconds) + " s")));
    var steps = (result.steps || []).slice(-8);
    if (steps.length) {
      var list = el("ul", "forge-history-list");
      steps.forEach(function (step) {
        var row = el("li", "forge-action-row");
        row.dataset.status = String(step.execute_status || step.plan_status || (step.proposal ? "proposed" : "observed"));
        var action = (step.proposal && step.proposal.action) || {};
        var label = action.kind
          ? action.kind + (action.key ? " " + action.key : "") +
            (action.x == null ? "" : " " + action.x + "," + action.y)
          : "observation";
        row.appendChild(el("strong", "", "#" + (Number(step.index || 0) + 1) + " " + label));
        var meta = [
          step.plan_status ? "plan: " + step.plan_status : "",
          step.execute_status ? "execute: " + step.execute_status : "",
          step.outcome_met === true ? "expectation met"
            : step.outcome_met === false ? "expectation missed" : "",
          step.proposal && step.proposal.model_backed ? "model-backed decision" : "",
          step.observation_age_seconds == null ? "" :
            "frame age: " + Number(step.observation_age_seconds) + " s",
          step.decision_latency_ms == null ? "" :
            "decided in " + Number(step.decision_latency_ms) + " ms",
        ].filter(Boolean).join(" · ");
        row.appendChild(el("span", "muted", " " + (meta || String(step.reason || ""))));
        // NF-10: the decision, not just the keystroke. The observed state, the
        // policy's own reason, what it expected, what it was unsure about and
        // the evidence it cited are all shown — a proposal without them is a
        // script, and this panel must not make one look like a decision.
        var observed = step.observation || {};
        var state = [
          observed.status && observed.status !== "observed"
            ? "observation: " + String(observed.status) : "",
          observed.title ? "state: " + String(observed.title) : "",
          observed.text ? "text: " + String(observed.text).slice(0, 80) : "",
        ].filter(Boolean).join(" · ");
        if (state) row.appendChild(el("div", "muted", state));
        if (step.proposal && step.proposal.reason) {
          row.appendChild(el("div", "muted", "Reason: " + String(step.proposal.reason)));
        }
        if (step.expected_outcome) {
          row.appendChild(el("div", "muted", "Expected: " + String(step.expected_outcome)));
        }
        if (step.proposal && step.proposal.uncertainty && step.proposal.uncertainty.length) {
          row.appendChild(el("div", "muted",
            "Uncertainty: " + step.proposal.uncertainty.join(" ")));
        }
        var refs = (step.proposal && step.proposal.evidence) || [];
        if (refs.length) {
          row.appendChild(el("div", "muted", "Evidence: " + refs.join(" · ")));
        }
        list.appendChild(row);
      });
      body.appendChild(list);
    }
    body.appendChild(el("p", "forge-limit-hint",
      "A completed run means the objective's verifier decided success from the target's " +
      "own evidence. A stopped run is not a failure unless the verifier said so."));
  }

  function visionStatusText(status) {
    var labels = {
      interpreted: "described within the schema",
      degraded: "answered without a usable structure",
      invalid: "answer could not be parsed",
      stale: "frame was already stale — not sent to the model",
      clipped: "frame was clipped — not sent to the model",
      unavailable: "no interpretation was produced",
    };
    return labels[String(status || "")] || String(status || "no interpretation");
  }

  function renderVisionInterpretation(body) {
    var out = byId("forgeVisionInterpretation");
    if (!out) return;
    out.innerHTML = "";
    var vision = (body && body.vision) || null;
    var capability = (body && body.capability) || {};
    var observation = (body && body.observation) || {};
    if (!vision) {
      // A missing interpretation has two different causes: the model is not
      // wired, or the OBSERVATION never produced a frame to ask about (stale,
      // unreadable, target gone). Reporting the capability reason for the
      // second case would blame the model for the frame's problem.
      var observationReason = observation.status && observation.status !== "observed"
        ? "the observation was " + String(observation.status) + " — " +
          String(observation.reason || "no reason was reported")
        : String(capability.reason || "no image-capable model is wired on this host");
      out.appendChild(el("p", "muted",
        "No visual interpretation exists: " + observationReason +
        " The measured evidence (window title, OCR text and frame) is unaffected."));
      return;
    }
    var head = el("div", "forge-card-head");
    head.appendChild(el("strong", "", "Vision: " + visionStatusText(vision.status)));
    // Only a value that actually carries a description is a model
    // interpretation; a timeout or refusal has no interpretation to label.
    head.appendChild(el("span", "forge-badge",
      vision.interpreted ? "model interpretation" : "no interpretation"));
    out.appendChild(head);
    if (vision.description) {
      out.appendChild(el("p", "", "Model interpretation (not evidence): " + vision.description));
    }
    if (vision.reason && !vision.description) {
      out.appendChild(el("p", "muted", String(vision.reason)));
    }
    var meta = [
      vision.model || vision.provider ? "model: " + (vision.model || vision.provider) : "",
      vision.duration_ms == null ? "" : "inference: " + Number(vision.duration_ms) + " ms",
      !vision.frame || vision.frame.age_seconds == null ? "" :
        "frame age: " + Number(vision.frame.age_seconds) + " s",
      vision.cached ? "cached answer" : "",
      "schema: " + (vision.schema_valid ? "valid" : "not valid"),
    ].filter(Boolean).join(" · ");
    out.appendChild(el("p", "muted", meta));
    if (vision.labels && vision.labels.length) {
      out.appendChild(el("p", "muted", "Visible text the model claimed: " + vision.labels.join(" · ")));
    }
    if (body && body.measured && body.measured.length) {
      out.appendChild(el("p", "muted", "Measured evidence read from the target: " + body.measured.join(", ")));
    }
    var conflicts = (body && body.conflicts && body.conflicts.length)
      ? body.conflicts
      : (vision.conflicts || []);
    if (conflicts.length) {
      var list = el("ul", "forge-history-list");
      conflicts.forEach(function (item) {
        list.appendChild(el("li", "forge-action-row", String(item)));
      });
      out.appendChild(el("p", "forge-limit-hint",
        "Model and measurement disagree; the observation is reported as uncertain rather than choosing one account."));
      out.appendChild(list);
    }
    if (vision.uncertainty && vision.uncertainty.length) {
      out.appendChild(el("p", "forge-limit-hint", vision.uncertainty.join(" ")));
    }
    out.appendChild(el("p", "forge-limit-hint",
      "A model description is a claim about pixels. It cannot approve, plan or execute an action, and it is never an objective verdict."));
  }

  async function describeFrameFromPanel() {
    if (!forge.sessionId) return;
    var out = byId("forgeVisionInterpretation");
    var button = byId("forgeDescribeFrame");
    if (out) {
      out.innerHTML = "";
      out.appendChild(el("p", "muted",
        "Capturing a fresh frame and asking the vision model… this can take longer than a text answer."));
    }
    if (button) button.disabled = true;
    try {
      var body = await requestJson(
        "/novaforge/sessions/" + encodeURIComponent(forge.sessionId) + "/vision/interpret", {});
      renderVisionInterpretation(body);
    } catch (error) {
      if (out) {
        out.innerHTML = "";
        out.appendChild(el("p", "muted",
          "Visual interpretation failed: " + String(error.message || error)));
      }
      notice("Visual interpretation failed: " + String(error.message || error), "error");
    } finally {
      renderLoop(forge.current);
    }
  }

  async function loadLoopCapability() {
    try {
      var body = await requestJson("/novaforge/capabilities");
      forge.loop.capability = ((body && body.capabilities) || {}).decision_pipeline || null;
    } catch (error) {
      forge.loop.capability = {
        available: false,
        reason: "The capability could not be read: " + String(error.message || error),
      };
    }
    renderLoop(forge.current);
    await refreshTrialHistory();
  }

  async function runLoopFromPanel() {
    if (!forge.sessionId || forge.loop.running) return;
    var actions = parseInt(byId("forgeLoopMaxActions").value, 10);
    var deadline = parseInt(byId("forgeLoopDeadline").value, 10);
    var policySelect = byId("forgeLoopPolicySelect");
    var policy = policySelect && policySelect.value ? policySelect.value : "ready";
    forge.loop.running = true;
    forge.loop.result = null;
    renderLoop(forge.current);
    try {
      var answer = await requestJson(
        "/novaforge/sessions/" + encodeURIComponent(forge.sessionId) + "/loop",
        {
          policy: policy,
          max_actions: isNaN(actions) ? 6 : actions,
          deadline_seconds: isNaN(deadline) ? 30 : deadline,
        });
      forge.loop.result = answer;
      notice("Closed loop: " + loopStatusText(answer.status) + " — " + String(answer.reason || ""),
        answer.status === "completed" ? "" : "error");
    } catch (error) {
      forge.loop.result = { status: "error", reason: String(error.message || error) };
      notice("Closed loop failed: " + String(error.message || error), "error");
    } finally {
      forge.loop.running = false;
      renderLoop(forge.current);
    }
    // A finished run is a trial now; read the record back rather than assuming
    // the panel's own numbers match what the server stored.
    await refreshTrialHistory();
    await refreshSession();
  }

  async function cancelLoopFromPanel() {
    if (!forge.sessionId) return;
    try {
      var answer = await requestJson(
        "/novaforge/sessions/" + encodeURIComponent(forge.sessionId) + "/loop/cancel", {});
      notice(answer.cancelled
        ? "Stop requested: " + String(answer.reason || "the loop will stop before its next action.")
        : "No loop is running: " + String(answer.reason || ""),
        answer.cancelled ? "" : "error");
    } catch (error) {
      notice("Loop stop failed: " + String(error.message || error), "error");
    }
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
    var payload = objectivePayload();
    var answer = await postGameplay(
      "/novaforge/sessions/" + encodeURIComponent(forge.sessionId) + "/objective",
      payload, "Set objective");
    if (!answer) return;
    var reads = payload.kind === "ocr_text"
      ? "the text recognised on a fresh frame of the window"
      : payload.kind === "window_title" ? "the target window's title" : "the objective";
    notice(answer.supported
      ? "Objective recorded; a verifier will read " + reads + "."
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
    forge.targetLabel = targetLine(data.target);
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
      setText("forgeFrameBackend", backendLabel(data.frame.backend));
      setText("forgeFrameValidation", validationLabel(data.frame.validation));
      setText("forgeFrameNote", data.frame.note || (data.frame.capture_mode
        ? "capture mode: " + data.frame.capture_mode : ""));
    } else {
      setText("forgeFrameTime", "—");
      setText("forgeFrameAge", "—");
      setText("forgeFrameRes", "—");
      setText("forgeFrameSource", "—");
      setText("forgeFrameBackend", "—");
      setText("forgeFrameValidation", "—");
      setText("forgeFrameNote", data.state === "RUNNING"
        ? "No fresh frame has been recorded yet."
        : "This session has no captured frame.");
    }

    renderTelemetry(data.telemetry);
    renderGuardian(data.guardian);
    if (previousSession !== data.session_id) {
      forge.gameplay.proposal = null;
      // A new session is not the last session's loop run OR plan.
      forge.loop.result = null;
      forge.plan.body = null;
      forge.autonomy.body = null;
    }
    renderGameplay(data);
    renderControl(forge.control.body && forge.control.body.session &&
      forge.control.body.session.session_id === data.session_id
      ? forge.control.body : null);
    renderPlan(forge.plan.body && forge.plan.body.session &&
      forge.plan.body.session.session_id === data.session_id
      ? forge.plan.body : null);
    renderAutonomy(forge.autonomy.body && forge.autonomy.body.runs
      ? forge.autonomy.body : null);
    if (ACTIVE.indexOf(data.state) >= 0) {
      loadControlStatus();
      loadPlanStatus();
      loadAutonomy();
    }
    renderLoop(data);
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

  /* ── NF-15: unified game observation ────────────────────────
   *
   * The card shows the OBSERVER's state, not the session's. A frame is fetched
   * only after the observer says it validated one, and it is labelled
   * "validated now" only while the observation is running and the frame is
   * inside the freshness window. Everything else is a record, not a feed.
   */

  var OBSERVATION_ACTIVE = ["observing", "paused"];
  var OBSERVATION_POLL_MS = 1500;

  function observationControls(status) {
    var state = (status && status.state) || "idle";
    var userPaused = Boolean(status && status.pause && status.pause.user_requested);
    return {
      start: OBSERVATION_ACTIVE.indexOf(state) < 0,
      pause: state === "observing" || (state === "paused" && !userPaused),
      resume: state === "paused" && userPaused,
      stop: OBSERVATION_ACTIVE.indexOf(state) >= 0,
      once: state === "observing" || (state === "paused" && !userPaused),
    };
  }

  function syncObservationControls(status) {
    var controls = observationControls(status);
    [
      ["forgeObservationStart", "start"],
      ["forgeObservationPause", "pause"],
      ["forgeObservationResume", "resume"],
      ["forgeObservationStop", "stop"],
      ["forgeObservationOnce", "once"],
    ].forEach(function (pair) {
      var node = byId(pair[0]);
      if (node) node.disabled = !controls[pair[1]];
    });
  }

  // A missing measurement is NOT zero. Rate, latency and scheduling delay are
  // printed by `metricText`-style rules so an unmeasured figure says so.
  function observationRateText(measured) {
    var rate = measured && measured.rate_hz;
    if (rate === null || rate === undefined) return "not measured yet";
    return Number(rate).toFixed(2) + " Hz";
  }

  function observationLatencyText(measured) {
    var latency = (measured && measured.latency_ms) || {};
    if (latency.last === null || latency.last === undefined) return "not measured yet";
    return latency.last + " ms (avg " + (latency.average === null || latency.average === undefined ? "—" : latency.average) +
      ", max " + (latency.max === null || latency.max === undefined ? "—" : latency.max) + ")";
  }

  function observationDelayText(measured) {
    var delay = (measured && measured.schedule_delay_ms) || {};
    if (delay.last === null || delay.last === undefined) return "not measured yet";
    return delay.last + " ms (max " + (delay.max === null || delay.max === undefined ? "—" : delay.max) + ")";
  }

  function observationFreshness(status, now) {
    var success = status && status.last_success;
    var state = (status && status.state) || "idle";
    if (!success) return { show: false, mode: "not tested", stale: true };
    var at = Date.parse(success.at || "");
    var age = isNaN(at) ? null : (now == null ? Date.now() : now) - at;
    var stale = age === null || age > STALE_MS;
    if (state !== "observing") {
      return { show: false, mode: "observation is " + state + " — the last validated frame is a record", stale: true };
    }
    if (stale) return { show: false, mode: "the last validated frame is stale", stale: true };
    return { show: true, mode: "validated now", stale: false };
  }

  function observationFact(dl, label, value) {
    var group = document.createElement("div");
    group.appendChild(el("dt", "", label));
    group.appendChild(el("dd", "", value));
    dl.appendChild(group);
  }

  // The facts are built as data so a Node test can assert every row without a
  // DOM: a missing measurement must say so and a validated frame must name its
  // own time and sequence.
  function observationFactRows(status) {
    var counts = (status && status.counts) || null;
    var success = (status && status.last_success) || null;
    var last = (status && status.last_observation) || null;
    return [
      ["Game", (status && status.game) || "not bound"],
      ["Target", observationTargetText(status)],
      ["Window", observationWindowText(status)],
      ["Last read", last ? last.status + " · " + formatTime(last.captured_at) : "not tested"],
      ["Last validated", success ? formatTime(success.at) + " · seq " + success.seq : "never"],
      ["Backend", (status && status.backend) || "none accepted"],
      ["Frame", success ? success.width + "×" + success.height + " (seq " + success.seq + ")" : "no validated frame"],
      ["Measured rate", observationRateText(status && status.measured)],
      ["Capture latency", observationLatencyText(status && status.measured)],
      ["Scheduling delay", observationDelayText(status && status.measured)],
      ["Consecutive failures", status ? String(status.consecutive_failures) : "not read"],
      ["Counts", counts
        ? counts.observed + " observed · " + counts.rejected + " rejected · " + counts.unavailable + " unavailable"
        : "not read"],
      ["Session", (status && status.session_id) || "none"],
    ];
  }

  function observationTargetText(status) {
    var target = status && status.target;
    if (!target || !target.title) return "not bound";
    var parts = [target.title];
    if (target.pid) parts.push("pid " + target.pid);
    if (target.start_time) parts.push("start " + target.start_time);
    if (target.handle) parts.push("hwnd " + target.handle);
    return parts.join(" · ");
  }

  // Window status is its own fact, separate from process identity: a window
  // can keep its handle and pid while it is minimized, hidden or behind another
  // window, and those are exactly the states in which a frame is not live.
  function observationWindowText(status) {
    if (!status) return "not read";
    var pause = status.pause || {};
    if (pause.code) {
      return pause.code + (pause.user_requested ? " · paused by the user" : " · paused");
    }
    var last = status.last_observation;
    if (!last) return "not tested";
    var visibility = last.visibility || {};
    var bits = [];
    if (visibility.minimized) bits.push("minimized");
    else if (visibility.visible === false) bits.push("hidden");
    else bits.push("visible");
    if (visibility.foreground === true) bits.push("foreground");
    else if (visibility.foreground === false) bits.push("background");
    bits.push(last.target_valid === false ? "target invalid" : "target valid");
    return bits.join(" · ");
  }

  function observationReasonText(status) {
    if (!status) return "Observation has not been read.";
    var pause = status.pause || {};
    if (pause.detail) return pause.detail;
    if (status.reason) return String(status.reason);
    if (status.state === "observing") return "Observation is running; frames are validated before they are shown.";
    if (status.state === "idle") return "Observation has not been started. Nothing is captured until a target is bound.";
    return "Observation is " + String(status.state) + ".";
  }

  /* ── NF-23: the observation ladder ─────────────────────────
   *
   * Ten separate capabilities stand between "installed on this machine" and
   * "an observation was accepted", and the card never lets one imply the next.
   * The badge shows exactly one of these labels, decided from the observer's
   * own signals: a frame that is merely old is not a capture failure, a
   * minimized window is not a backend error, and a stationary scene that
   * validates is live.
   */

  var OBSERVATION_LIVE_LABELS = {
    live_valid: "LIVE AND VALID",
    capture_unavailable: "CAPTURE UNAVAILABLE",
    target_not_visible: "TARGET HIDDEN OR MINIMIZED",
    stale_frame: "STALE FRAME",
    invalid_frame: "INVALID OR UNVERIFIED FRAME",
    identity_mismatch: "TARGET IDENTITY MISMATCH",
    backend_error: "BACKEND ERROR",
    not_observed: "NOT OBSERVED YET",
  };

  // Codes that describe the TARGET's state — its own window — rather than a
  // capture failure: the observer pauses on these instead of counting them as
  // failures, and the card says so in the same words.
  // Includes the observer's PAUSE codes for the same conditions (a paused
  // observation records no frame, so its silence must read as a target state,
  // never as "the frame is invalid" — there is no frame).
  var OBSERVATION_TARGET_STATE_CODES = {
    target_minimized: true,
    target_hidden: true,
    target_offscreen: true,
    target_not_foreground: true,
    window_missing: true,
    target_missing: true,
    target_identity_unavailable: true,
  };

  // The target is still capturable, but it is not the window/process that was
  // bound: an identity failure, not a backend one.
  var OBSERVATION_REPLACED_STATE_CODES = {
    target_replaced: true,
  };

  // Codes that mean the capture itself produced no usable frame.
  var OBSERVATION_BACKEND_ERROR_CODES = {
    capture_failed: true,
    capture_timeout: true,
    observation_error: true,
    frame_directory_unusable: true,
    target_probe_failed: true,
  };

  function observationLiveState(status) {
    function out(state, detail) {
      return {
        state: state,
        label: OBSERVATION_LIVE_LABELS[state] || state,
        detail: detail || "",
      };
    }
    if (!status || typeof status !== "object") {
      return out("not_observed", "Observation has not been read.");
    }
    if (status.available === false) {
      return out("capture_unavailable",
        "No capture is wired on this host, so no frame can be acquired for any target.");
    }
    var pause = status.pause || {};
    var pauseCode = String(pause.code || "");
    if (pauseCode && OBSERVATION_REPLACED_STATE_CODES[pauseCode]) {
      return out("identity_mismatch", String(pause.detail ||
        "The bound target is no longer the same window or process."));
    }
    if (pauseCode && OBSERVATION_TARGET_STATE_CODES[pauseCode]) {
      return out("target_not_visible", String(pause.detail || (
        "The target window is " + pauseCode.replace("target_", "") + ", so it cannot be captured.")));
    }
    var last = status.last_observation || null;
    if (!last) {
      return out("not_observed", "No read has been recorded yet.");
    }
    var verdict = (last.validation && typeof last.validation === "object") ? last.validation : {};
    var code = String(last.reason_code || "");
    var detail = String(last.reason || verdict.reason || "");
    if (last.status === "observed") {
      if (verdict.fresh === false) {
        return out("stale_frame",
          detail || "The validated frame is older than the freshness window.");
      }
      if (status.state !== "observing") {
        return out("stale_frame", "Observation is " + String(status.state || "idle") +
          " — the last validated frame is a record, not a feed.");
      }
      return out("live_valid",
        "The newest frame passed target, freshness and content validation while the " +
        "observation is running.");
    }
    // A read that produced no frame, in the order that names the real cause.
    if (code && OBSERVATION_BACKEND_ERROR_CODES[code]) {
      return out("backend_error", detail || code);
    }
    if (verdict.capture_ok === false) {
      return out("backend_error", detail || "The capture backend produced no frame.");
    }
    if (code && OBSERVATION_REPLACED_STATE_CODES[code]) {
      return out("identity_mismatch",
        detail || "The bound target is no longer the same window or process.");
    }
    if (verdict.target_visible === false || (code && OBSERVATION_TARGET_STATE_CODES[code])) {
      return out("target_not_visible",
        detail || "The target window is minimized, hidden or otherwise not capturable.");
    }
    if (verdict.content === "not_target") {
      return out("identity_mismatch", detail || "The frame is not a frame of the target.");
    }
    if (verdict.content === "blank") {
      return out("invalid_frame", detail || "Nothing was painted in the frame.");
    }
    if (!verdict.content && verdict.capture_ok !== true) {
      // No frame was read for this record at all (a pause or an end record):
      // calling that an INVALID FRAME would describe pixels that never existed.
      return out("not_observed", detail || "No frame has been read for this observation.");
    }
    if (verdict.fresh === false) {
      return out("stale_frame", detail || "The frame is older than the freshness window.");
    }
    return out("invalid_frame", detail || "The last read produced no validated frame.");
  }

  var OBSERVATION_STAGE_LABELS = {
    discovered: "Game discovered",
    launchable: "Game launchable",
    process_started: "Process started",
    window_identified: "Window identified",
    embedding_compatible: "Window compatible with embedding",
    embedded: "Embedded natively",
    backend_selected: "Capture backend selected",
    frame_acquired: "Frame acquired",
    frame_validated: "Frame validated",
    observation_accepted: "Observation accepted",
  };

  var OBSERVATION_STAGE_STATES = {
    true: "yes",
    false: "no",
    unknown: "unknown",
    not_wired: "not wired",
  };

  function observationStageStateText(stage, state) {
    if (stage === "embedding_compatible" && state === "false") return "EMBEDDING UNSUPPORTED";
    if (stage === "embedded" && state === "false") return "not embedded";
    return OBSERVATION_STAGE_STATES[state] || String(state || "unknown");
  }

  function observationLadderHeadline(ladder) {
    if (!ladder || typeof ladder !== "object") {
      return "The observation ladder has not been read.";
    }
    if (ladder.accepted) return "An observation has been accepted through this path.";
    var held = String(ladder.held_at || "");
    if (!held) return "Every stage on the observation path is satisfied.";
    return "Held at: " + (OBSERVATION_STAGE_LABELS[held] || held) + ".";
  }

  // Rows are data so a Node test can assert every stage without a DOM, and so
  // the embedding rows can be marked as embedding — they are measured, never
  // required, and never the reason an observation is held up.
  function observationLadderRows(ladder) {
    var stages = (ladder && ladder.stages) || [];
    var embeddingStages = (ladder && ladder.embedding_stages) || [];
    return stages.map(function (row) {
      var stage = String(row.stage || "");
      var state = String(row.state || "unknown");
      return {
        stage: stage,
        name: OBSERVATION_STAGE_LABELS[stage] || stage,
        state: state,
        text: observationStageStateText(stage, state),
        embedding: embeddingStages.indexOf(stage) >= 0,
        detail: String(row.detail || ""),
      };
    });
  }

  function renderObservationLadder(ladder) {
    var box = byId("forgeObservationLadder");
    if (!box) return;
    box.innerHTML = "";
    if (!ladder || typeof ladder !== "object") {
      box.appendChild(el("p", "muted", "The observation ladder has not been read."));
      return;
    }
    box.appendChild(el("p", "", observationLadderHeadline(ladder)));
    var list = el("ul", "");
    observationLadderRows(ladder).forEach(function (row) {
      var suffix = row.embedding ? " (embedding · not required for capture)" : "";
      var item = el("li", "", row.name + ": " + row.text + suffix);
      if (row.detail) item.appendChild(el("div", "muted", row.detail));
      list.appendChild(item);
    });
    box.appendChild(list);
    if (ladder.note) box.appendChild(el("p", "muted", String(ladder.note)));
  }

  // On demand only: the ladder's first two stages resolve a title against the
  // catalog, which runs a process probe. Polling it would launch PowerShell
  // every 1.5 s to answer a question that rarely changes.
  async function loadObservationLadder() {
    var box = byId("forgeObservationLadder");
    if (!box) return;
    var status = forge.observation.status;
    var title = (status && status.target && status.target.title) || "";
    if (!title) {
      var typed = byId("forgeObservationTarget");
      title = typed ? String(typed.value || "").trim() : "";
    }
    var token = ++forge.observation.ladderToken;
    try {
      var body = await requestJson("/novaforge/capabilities" +
        (title ? "?title=" + encodeURIComponent(title) : ""));
      if (token !== forge.observation.ladderToken) return;
      renderObservationLadder(
        (body && body.capabilities && body.capabilities.observation_ladder) || null);
    } catch (error) {
      if (token !== forge.observation.ladderToken) return;
      box.innerHTML = "";
      box.appendChild(el("p", "muted",
        "The observation ladder could not be read: " + String(error.message || error) + "."));
    }
  }

  function renderObservationHistory(status) {
    var box = byId("forgeObservationHistory");
    if (!box) return;
    box.innerHTML = "";
    var rows = (status && status.history) || [];
    if (!rows.length) {
      box.appendChild(el("p", "muted", "No observation records yet. A read that produced no frame is recorded too, with its reason."));
      return;
    }
    box.appendChild(el("p", "muted", "Recent reads (newest last). A rejected or unavailable read is kept so a failure can be diagnosed."));
    var list = el("ul", "");
    rows.slice(-8).forEach(function (row) {
      var bits = [
        "#" + row.seq,
        String(row.status),
        row.backend || "no backend",
        row.width && row.height ? row.width + "×" + row.height : "no frame",
        row.capture_ms == null ? "latency not measured" : row.capture_ms + " ms",
        row.reason_code || "",
        formatTime(row.captured_at),
      ].filter(Boolean);
      var item = el("li", "", bits.join(" · "));
      list.appendChild(item);
      if (row.reason) item.appendChild(el("div", "muted", String(row.reason)));
    });
    box.appendChild(list);
  }

  function revokeObservationFrame() {
    if (forge.observation.objectUrl) {
      URL.revokeObjectURL(forge.observation.objectUrl);
      forge.observation.objectUrl = "";
    }
  }

  async function loadObservationFrame(status) {
    var wrap = byId("forgeObservationFrameWrap");
    var image = byId("forgeObservationFrame");
    var empty = byId("forgeObservationFrameEmpty");
    var label = byId("forgeObservationFrameLabel");
    if (!wrap || !image || !empty) return;
    var token = ++forge.observation.frameToken;
    var freshness = observationFreshness(status, Date.now());
    if (!freshness.show) {
      wrap.hidden = true;
      image.removeAttribute("src");
      revokeObservationFrame();
      forge.observation.lastKey = "";
      empty.hidden = false;
      var success = status && status.last_success;
      empty.textContent = success
        ? "Last validated frame: seq " + success.seq + " captured " + formatTime(success.at) + " — " + freshness.mode + "."
        : "No validated frame yet. Observation has not been tested.";
      return;
    }
    var latest = status.last_success;
    var key = String(status.session_id || "") + ":" + latest.seq;
    empty.hidden = true;
    wrap.hidden = false;
    if (label) label.textContent = freshness.mode;
    if (key === forge.observation.lastKey) return;
    try {
      var response = await fetch("/novaforge/observation/frame", { headers: authHeaders() });
      if (!response.ok) throw new Error("HTTP " + response.status);
      var blob = await response.blob();
      if (!frameStillCurrent(token, forge.observation.frameToken)) return;
      var nextUrl = URL.createObjectURL(blob);
      revokeObservationFrame();
      forge.observation.objectUrl = nextUrl;
      forge.observation.lastKey = key;
      image.src = nextUrl;
    } catch (error) {
      if (!frameStillCurrent(token, forge.observation.frameToken)) return;
      wrap.hidden = true;
      empty.hidden = false;
      empty.textContent = "The validated frame could not be loaded: " + String(error.message || error) + ".";
    }
  }

  function renderObservationStatus(status) {
    var badge = byId("forgeObservationState");
    if (!badge) return;
    forge.observation.status = status || null;
    var state = (status && status.state) || "idle";
    // The badge names the DISPLAY state (one of the eight), and keeps the
    // observer's own lifecycle state beside it: "paused" never reads as live.
    var live = observationLiveState(status);
    badge.textContent = live.label;
    badge.dataset.state = live.state;
    badge.dataset.observerState = state;
    syncObservationControls(status);
    var hint = observationReasonText(status);
    if (live.detail && hint.indexOf(live.detail) < 0) {
      hint = hint + " " + live.detail;
    }
    setText("forgeObservationHint", hint);
    var facts = byId("forgeObservationFacts");
    if (facts) {
      facts.innerHTML = "";
      observationFactRows(status).forEach(function (row) {
        observationFact(facts, row[0], row[1]);
      });
    }
    renderObservationHistory(status);
    loadObservationFrame(status);
  }

  function scheduleObservationPoll() {
    if (forge.observation.timer) clearTimeout(forge.observation.timer);
    forge.observation.timer = null;
    var status = forge.observation.status;
    if (!status || OBSERVATION_ACTIVE.indexOf(status.state) < 0) return;
    forge.observation.timer = setTimeout(loadObservationStatus, OBSERVATION_POLL_MS);
  }

  async function loadObservationStatus() {
    if (forge.observation.polling) return;
    forge.observation.polling = true;
    var token = ++forge.observation.statusToken;
    try {
      var body = await requestJson("/novaforge/observation");
      if (token !== forge.observation.statusToken) return;
      renderObservationStatus(body);
    } catch (error) {
      if (token !== forge.observation.statusToken) return;
      setText("forgeObservationHint", "Observation status could not be read: " + String(error.message || error));
    } finally {
      forge.observation.polling = false;
      scheduleObservationPoll();
    }
  }

  async function observationCommand(path, payload) {
    if (forge.observation.busy) return null;
    forge.observation.busy = true;
    // This command's answer is newer than any status read already in flight.
    forge.observation.statusToken += 1;
    try {
      var body = await requestJson(path, payload || {});
      // This answer is the newest state; a status read that was in flight when
      // the command was issued must not repaint over it.
      forge.observation.statusToken += 1;
      renderObservationStatus(body);
      // A command just changed which stages are reachable, so the ladder is
      // re-read here rather than on the poll.
      loadObservationLadder();
      // A refusal is guidance, not a crash: it names what to do next and the
      // card keeps the observer's real state (which the response carries).
      if (body && body.reason_code) {
        setText("forgeObservationHint", String(body.reason || body.reason_code));
      }
      return body;
    } catch (error) {
      setText("forgeObservationHint", "Observation command failed: " + String(error.message || error));
      return null;
    } finally {
      forge.observation.busy = false;
      scheduleObservationPoll();
    }
  }

  function observationTargetChoice() {
    var typed = (byId("forgeObservationTarget").value || "").trim();
    if (typed) return typed;
    var select = byId("forgeMissionTarget");
    return select ? String(select.value || "").trim() : "";
  }

  function startObservation() {
    var title = observationTargetChoice();
    if (!title) {
      setText("forgeObservationHint", "Choose a target window (the mission target or a typed title) before starting.");
      return;
    }
    var payload = { title: title };
    var game = (byId("forgeObservationGame").value || "").trim();
    if (game) payload.game = game;
    var interval = Number(byId("forgeObservationInterval").value);
    if (isFinite(interval) && interval > 0) payload.interval = interval;
    // Pin the process the mission validation measured, so a same-titled window
    // from another process cannot be observed instead.
    var validated = forge.validatedTarget;
    if (validated && validated.pid && String(validated.title || "") === title) {
      payload.pid = validated.pid;
    }
    return observationCommand("/novaforge/observation/start", payload);
  }

  function pauseObservation() { return observationCommand("/novaforge/observation/pause"); }
  function resumeObservation() { return observationCommand("/novaforge/observation/resume"); }
  function stopObservation() { return observationCommand("/novaforge/observation/stop"); }
  function observeOnce() { return observationCommand("/novaforge/observation/observe"); }

  /* ── NF-16: game state understanding ────────────────────────
   *
   * A state is a READING of one validated observation. The card shows what was
   * read and what could not be read, with the same five-status vocabulary the
   * service uses: recognized / absent / ambiguous / unreadable / unsupported.
   * Entity kinds are printed as unknown because that is what the detector can
   * say, and "quality" is a heuristic coverage count, not a confidence.
   */

  function stateProfileText(status) {
    var state = status && status.state;
    if (!state) return "not read";
    var profile = state.profile || {};
    if (!profile.matched) return "none matched";
    return (profile.profile_id || "") + " v" + (profile.version || "?") +
      (profile.forced ? " (pinned)" : "");
  }

  function stateCoverageText(quality) {
    if (!quality) return "not measured";
    var declared = quality.declared;
    var recognized = quality.recognized;
    if (declared === null || declared === undefined || !declared) {
      return "no declared fields (generic reading)";
    }
    return recognized + " of " + declared + " declared field(s) · heuristic";
  }

  function stateProcessingText(measured) {
    var processing = (measured && measured.processing_ms) || {};
    if (processing.last === null || processing.last === undefined) return "not measured yet";
    return processing.last + " ms (avg " +
      (processing.average === null || processing.average === undefined ? "—" : processing.average) +
      ", max " + (processing.max === null || processing.max === undefined ? "—" : processing.max) + ")";
  }

  function stateEntitySummary(state) {
    if (!state) return "not read";
    var status = state.entity_status;
    if (status === "detected") {
      return (state.entities || []).length + " region(s) · kind unknown";
    }
    return String(status || "unknown");
  }

  function stateFactRows(status) {
    var state = (status && status.state) || null;
    var counts = (status && status.counts) || {};
    var error = status && status.last_error;
    var refusal = status && status.last_refusal;
    return [
      ["Game", state ? (state.game || "unlabelled") : "not read"],
      ["Observation", state
        ? state.observation.observation_id + " · " + formatTime(state.observation.at)
        : "never"],
      ["Session", state ? (state.observation.session_id || "none") : "none"],
      ["Profile", stateProfileText(status)],
      ["State", state ? String(state.status) : "untested"],
      ["Scene", stateSceneText(state)],
      ["Quality", stateCoverageText(state && state.quality)],
      ["Processing", stateProcessingText(status && status.measured)],
      ["Entities", stateEntitySummary(state)],
      ["Changes", state ? String((state.changes || []).length) : "not read"],
      ["Counts", (counts.analyzed || 0) + " analyzed · " + (counts.failed || 0) +
        " failed · " + (counts.superseded || 0) + " superseded"],
      ["Last error", error ? (error.reason_code + " — " + error.reason) : "none"],
      ["Last refusal", refusal
        ? (refusal.reason_code + " — " + refusal.reason)
        : "none"],
    ];
  }

  function stateHudRows(state) {
    return ((state && state.hud) || []).map(function (row) {
      return {
        label: row.label || row.element_id,
        element: row.element_id,
        status: String(row.status),
        text: row.text || "",
        value: row.value === null || row.value === undefined ? "" : String(row.value),
        reason: row.reason || "",
      };
    });
  }

  function stateSceneText(state) {
    if (!state) return "not read";
    var scene = state.scene || {};
    var transition = state.transition || {};
    var kind = scene.kind || "unknown";
    var how = scene.status || "unknown";
    if (scene.source && scene.source !== "none") how += " via " + scene.source;
    var text = kind + " (" + how + ")";
    // An unknown screen is only useful with its reason: "unknown (unknown)" tells
    // an operator nothing about whether OCR failed or no rule matched.
    if (kind === "unknown" && (scene.evidence || []).length) {
      text += " — " + scene.evidence[0];
    }
    if (typeof scene.confidence === "number") {
      text += " · confidence " + scene.confidence.toFixed(2);
    }
    if ((scene.alternatives || []).length) {
      text += " · alternatives " + scene.alternatives.join(", ");
    }
    // The CONFIRMED scene is the durable answer; this frame's candidate is
    // reported beside it so a pending change never reads as a state change.
    var confirmed = transition.confirmed || "unknown";
    text += " · confirmed " + confirmed;
    if (transition.state && transition.state !== "none") {
      text += " · " + transition.state;
      if (transition.state === "pending" || transition.state === "contradicted") {
        text += " " + (transition.confirmations || 0) + "/" + (transition.required || 0);
      }
      if (transition.reason) text += " — " + transition.reason;
    }
    if (transition.interrupted) text += " · run restarted after a capture interruption";
    if ((transition.contradictions || []).length) {
      text += " · contradiction: " + transition.contradictions[0];
    }
    return text;
  }

  function stateChangeText(change) {
    var arrow = change.before && change.after
      ? change.before + " → " + change.after
      : (change.after || change.before || "");
    return change.field + ": " + change.kind + (arrow ? " · " + arrow : "") +
      (change.detail ? " — " + change.detail : "");
  }

  function stateHintText(status) {
    if (!status) return "Game state has not been read.";
    var state = status.state;
    if (!state) {
      if (status.last_error) return status.last_error.reason_code + " — " + status.last_error.reason;
      return "No state has been read. Analysis uses the newest validated observation frame; it never captures a frame of its own.";
    }
    if (state.reasons && state.reasons.length) return String(state.reasons[0]);
    if (!state.profile || !state.profile.matched) {
      return (state.profile && state.profile.reason) || "No profile matched; the reading is generic.";
    }
    return "Reading " + (state.observation.observation_id || "an observation") +
      " · " + stateCoverageText(state.quality) + ".";
  }

  function renderStateList(containerId, rows, emptyText) {
    var box = byId(containerId);
    if (!box) return;
    box.innerHTML = "";
    if (!rows || !rows.length) {
      box.appendChild(el("p", "muted", emptyText));
      return;
    }
    var list = el("ul", "");
    rows.forEach(function (row) {
      var item = el("li", "", row);
      list.appendChild(item);
    });
    box.appendChild(list);
  }

  function renderGameState(status) {
    forge.understanding.status = status || null;
    var badge = byId("forgeStateBadge");
    if (!badge) return;
    var state = (status && status.state) || null;
    var label = state ? String(state.status) : (status && status.analyzing ? "analyzing" : "untested");
    badge.textContent = label;
    badge.dataset.state = label;
    setText("forgeStateHint", stateHintText(status));
    var facts = byId("forgeStateFacts");
    if (facts) {
      facts.innerHTML = "";
      stateFactRows(status).forEach(function (row) {
        observationFact(facts, row[0], row[1]);
      });
    }
    renderStateList(
      "forgeStateHud",
      stateHudRows(state).map(function (row) {
        var detail = row.status + (row.value ? " = " + row.value : (row.text ? " = \"" + row.text + "\"" : ""));
        return row.label + " (" + row.element + "): " + detail + (row.reason ? " — " + row.reason : "");
      }),
      state ? "The matched profile declares no interface field for this target." : "No HUD field has been read."
    );
    renderStateList(
      "forgeStateEntities",
      (state && state.entities || []).map(function (row) {
        var box = row.box ? " @ " + row.box.x + "," + row.box.y + " " + row.box.width + "×" + row.box.height : "";
        return "region " + row.entity_id + box + " · kind " + row.kind +
          (row.overlaps_hud ? " · overlaps a declared HUD region" : "");
      }),
      state ? stateEntitySummary(state) + (state.entity_reason ? " — " + state.entity_reason : "") : "No entity has been detected."
    );
    renderStateList(
      "forgeStateChanges",
      ((state && state.changes) || []).map(stateChangeText),
      (state && state.tracking && state.tracking.reason) || "Nothing comparable came before this state."
    );
    var unknownRows = ((state && state.unknown) || []).map(function (row) {
      return row.field + ": " + row.status + (row.reason ? " — " + row.reason : "");
    }).concat(((state && state.unsupported) || []).map(function (row) { return "unsupported: " + row; }));
    renderStateList(
      "forgeStateUnknown",
      unknownRows,
      state ? "No field was left unstated." : "Nothing has been attempted yet."
    );
    var history = byId("forgeStateHistory");
    if (history) {
      history.innerHTML = "";
      var rows = (status && status.history) || [];
      if (!rows.length) {
        history.appendChild(el("p", "muted", "No state readings yet."));
      } else {
        history.appendChild(el("p", "muted", "Recent readings (newest last). A refusal is reported beside them, not as a state."));
        var list = el("ul", "");
        rows.slice(-6).forEach(function (row) {
          var quality = row.quality || {};
          list.appendChild(el("li", "",
            "#" + row.sequence + " · " + row.status + " · " +
            ((row.profile && row.profile.profile_id) || "no profile") + " · scene " +
            ((row.scene && row.scene.kind) || "unknown") + " · " +
            (quality.recognized || 0) + "/" + (quality.declared || 0) + " field(s) · " +
            (row.entities || []).length + " region(s) · " + formatTime(row.timestamp)));
        });
        history.appendChild(list);
      }
    }
  }

  async function loadGameState() {
    try {
      var body = await requestJson("/novaforge/state");
      renderGameState(body);
    } catch (error) {
      setText("forgeStateHint", "Game state could not be read: " + String(error.message || error));
    }
  }

  async function analyzeGameState() {
    if (forge.understanding.busy) return null;
    forge.understanding.busy = true;
    var button = byId("forgeStateAnalyzeButton");
    if (button) button.classList.add("loading");
    try {
      var payload = {};
      var profileId = (byId("forgeStateProfile").value || "").trim();
      if (profileId) payload.profile_id = profileId;
      var body = await requestJson("/novaforge/state/analyze", payload);
      // Refresh FIRST: the reload repaints the hint from the status the server
      // just stored, so a refusal explained before it would be overwritten and
      // the user would see the previous state's sentence instead of the reason.
      await loadGameState();
      // The objective card describes the SAME reading, so a new state must
      // repaint it too — otherwise two cards disagree about which frame they
      // are showing. A refusal changes nothing, so the reload is harmless.
      await loadObjectives();
      // Knowledge was ingested by THIS reading (if it succeeded), so the card
      // must repaint too: a stale knowledge card beside a fresh state is the
      // same disagreement two objective cards once had.
      await loadKnowledge();
      if (!body.analyzed) {
        setText(
          "forgeStateHint",
          (body.reason_code ? body.reason_code + " — " : "") +
            (body.reason || "The reading was refused.")
        );
      }
      return body;
    } catch (error) {
      setText("forgeStateHint", "The state reading failed: " + String(error.message || error));
      return null;
    } finally {
      forge.understanding.busy = false;
      if (button) button.classList.remove("loading");
    }
  }

  async function resetGameState() {
    try {
      await requestJson("/novaforge/state/reset", {});
      await loadGameState();
    } catch (error) {
      setText("forgeStateHint", "The baseline could not be reset: " + String(error.message || error));
    }
  }

  /* ── NF-17: objective understanding ────────────────────────── */

  function objectiveProgressText(progress) {
    if (!progress) return "no progress evidence";
    if (progress.kind === "none") return progress.reason || "no progress evidence";
    var parts = [];
    if (progress.value !== null && progress.value !== undefined) {
      parts.push(
        progress.target !== null && progress.target !== undefined
          ? progress.value + " / " + progress.target
          : String(progress.value)
      );
    } else if (progress.text) {
      parts.push(String(progress.text));
    }
    if (progress.fraction !== null && progress.fraction !== undefined) {
      parts.push(Math.round(progress.fraction * 100) + "%");
    }
    if (progress.resets) parts.push(progress.resets + " reset(s) observed");
    if (!parts.length) parts.push(progress.reason || "no numeric progress");
    return parts.join(" · ");
  }

  function objectiveRow(objective) {
    var text = objective.description + " · " + objective.status + " · " +
      objective.category + (objective.explicit ? " · explicit" : " · inferred") +
      (objective.source ? " · source " + objective.source : "") +
      " · " + objectiveProgressText(objective.progress);
    if (objective.source_text && objective.source_text !== objective.description) {
      text += ' · read as "' + objective.source_text + '"';
    }
    // A counter that could not be read this frame keeps its last KNOWN value,
    // so the row must say that value is older than the reading. Otherwise a
    // temporarily obscured counter reads as a current measurement.
    if (objective.progress && objective.progress.at && objective.last_updated &&
        objective.progress.at !== objective.last_updated) {
      text += " · progress last read " + formatTime(objective.progress.at);
    }
    var dependencies = objective.dependencies || [];
    if (dependencies.length) {
      text += " · requires " + dependencies.map(function (item) {
        return item.requires + (item.satisfied ? " (met)" : " (unmet)");
      }).join(", ");
    }
    var evidence = (objective.evidence || [])
      .filter(function (item) { return item.kind !== "objective_text"; })
      .slice(0, 2)
      .map(function (item) { return item.summary; });
    if (evidence.length) text += " [" + evidence.join("; ") + "]";
    if (objective.reason) text += " — " + objective.reason;
    return text;
  }

  function objectiveStatusSummary(objectives) {
    var counts = {};
    (objectives || []).forEach(function (item) {
      counts[item.status] = (counts[item.status] || 0) + 1;
    });
    var order = ["in_progress", "active", "discovered", "completed", "failed",
      "blocked", "not_verifiable", "unknown", "superseded"];
    var parts = [];
    order.forEach(function (key) {
      if (counts[key]) parts.push(counts[key] + " " + key);
    });
    return parts.length ? parts.join(" · ") : "none";
  }

  function objectiveFacts(status) {
    var state = (status && status.state) || null;
    var counts = (status && status.counts) || {};
    var measured = (status && status.measured && status.measured.interpretation_ms) || {};
    var error = status && status.last_error;
    var all = state ? (state.objectives || []) : [];
    // The list shows CURRENT objectives, so the counts beside it must count the
    // same set; superseded records stay visible only as a separate figure.
    var current = all.filter(function (item) { return item.status !== "superseded"; });
    return [
      ["Game", state ? (state.game || "not named") : "not read"],
      ["Session", state ? (state.session_id || "none") : "none"],
      ["Observation", state ? (state.observation_id + " · " + formatTime(state.at)) : "never"],
      ["Profile", state ? (state.profile_id || "none matched") : "not read"],
      ["Objectives", state ? String(current.length) : "not read"],
      ["Statuses", state ? objectiveStatusSummary(current) : "not read"],
      ["Superseded", all.length === current.length
        ? "none"
        : (all.length - current.length) + " kept in history"],
      ["Restored", status && status.restored
        ? "history from an earlier run; the current picture is not restored"
        : "no"],
      ["Events", state ? String((state.events || []).length) : "not read"],
      ["Readings", (counts.received || 0) + " read · " + (counts.ignored || 0) +
        " out of order · " + (counts.duplicates || 0) + " repeated"],
      ["Interpretation", (measured.average === null || measured.average === undefined)
        ? "not measured yet"
        : (measured.last + " ms last · " + measured.average + " ms avg")],
      ["Last error", error ? (error.reason_code + " — " + error.reason) : "none"],
    ];
  }

  function objectiveHint(status) {
    if (!status) return "No objective has been interpreted yet.";
    var state = status.state;
    if (!state) {
      if (status.last_error) {
        return status.last_error.reason_code + " — " + status.last_error.reason;
      }
      return "No objective has been interpreted. Reading walks the newest validated state " +
        "through the objective rules; it never captures, plans or acts.";
    }
    var diagnostics = state.diagnostics || {};
    var conflicts = diagnostics.conflicts || [];
    var unattributed = diagnostics.unattributed || [];
    if (conflicts.length) return "Conflicting evidence: " + conflicts[0];
    if (unattributed.length) return unattributed[0];
    if (!(state.objectives || []).length) {
      return (state.reasons && state.reasons[0]) ||
        "No objective was found in this reading.";
    }
    var current = (state.objectives || []).filter(function (item) {
      return item.status !== "superseded";
    });
    return current.length + " objective(s) — " + objectiveStatusSummary(current) +
      " · completion needs explicit evidence, never a vanished objective.";
  }

  function objectiveEventText(event) {
    var arrow = event.before && event.after ? event.before + " → " + event.after : "";
    return event.kind + (arrow ? " · " + arrow : "") +
      " · " + event.objective_id + (event.reason ? " — " + event.reason : "");
  }

  function renderObjectives(status) {
    forge.objectives.status = status || null;
    var badge = byId("forgeObjectivesBadge");
    if (!badge) return;
    var state = (status && status.state) || null;
    var current = state ? (state.objectives || []).filter(function (item) {
      return item.status !== "superseded";
    }) : [];
    var label = state ? (current.length + " objective(s)") : "untested";
    badge.textContent = label;
    badge.dataset.state = state ? String(state.objectives.length ? "read" : "empty") : "untested";
    setText("forgeObjectivesHint", objectiveHint(status));
    var facts = byId("forgeObjectivesFacts");
    if (facts) {
      facts.innerHTML = "";
      objectiveFacts(status).forEach(function (row) {
        observationFact(facts, row[0], row[1]);
      });
    }
    renderStateList(
      "forgeObjectivesList",
      current.map(objectiveRow),
      state ? "No objective is current in this reading." : "No objective has been interpreted."
    );
    var diagnostics = (state && state.diagnostics) || {};
    renderStateList(
      "forgeObjectivesDiagnostics",
      [].concat(
        (diagnostics.conflicts || []).map(function (row) { return "conflict: " + row; }),
        (diagnostics.dependency_issues || []).map(function (row) { return "dependency: " + row; }),
        (diagnostics.unattributed || []).map(function (row) { return "unattributed: " + row; }),
        ((state && state.reasons) || []).map(function (row) { return "reason: " + row; })
      ),
      state ? "No conflict or unreadable field was reported." : "Nothing has been attempted yet."
    );
    var history = byId("forgeObjectivesHistory");
    if (history) {
      history.innerHTML = "";
      var events = (status && status.events) || [];
      if (!events.length) {
        history.appendChild(el("p", "muted", "No objective transition has been recorded."));
      } else {
        history.appendChild(el("p", "muted", "Objective history (newest last, bounded)."));
        var list = el("ul", "");
        events.slice(-8).forEach(function (row) {
          list.appendChild(el("li", "", formatTime(row.at) + " · " + objectiveEventText(row)));
        });
        history.appendChild(list);
      }
    }
  }

  async function loadObjectives() {
    try {
      var body = await requestJson("/novaforge/objectives");
      renderObjectives(body);
    } catch (error) {
      setText("forgeObjectivesHint",
        "Objectives could not be read: " + String(error.message || error));
    }
  }

  async function readObjectives() {
    if (forge.objectives.busy) return null;
    forge.objectives.busy = true;
    var button = byId("forgeObjectivesReadButton");
    if (button) button.classList.add("loading");
    try {
      // Interpretation happens INSIDE the state reading (one capture, one read,
      // one interpretation), so the shared reading refreshes BOTH cards; a
      // refusal here means no objective was updated.
      return await analyzeGameState();
    } finally {
      forge.objectives.busy = false;
      if (button) button.classList.remove("loading");
    }
  }

  async function resetObjectives() {
    try {
      await requestJson("/novaforge/objectives/reset", {});
      await loadObjectives();
    } catch (error) {
      setText("forgeObjectivesHint",
        "The objectives could not be reset: " + String(error.message || error));
    }
  }

  /* ── NF-18: game knowledge ─────────────────────────────────── */

  function knowledgeGameFromPanel() {
    var input = byId("forgeKnowledgeGame");
    var typed = input ? String(input.value || "").trim() : "";
    if (typed) return typed;
    var state = forge.understanding.status && forge.understanding.status.state;
    if (state && state.game) return String(state.game);
    return forge.knowledge.game || "";
  }

  function knowledgeRecordRow(record) {
    var count = record.independent_evidence || 0;
    var text = record.category + " · " + record.key + " · " + record.status +
      " · " + count + (count === 1 ? " observation" : " observations");
    if (record.game_version) text += " · version " + record.game_version;
    if (record.reason) text += " · " + record.reason;
    if (record.status === "contradicted" || record.status === "stale") {
      text += " · needs revalidation";
    }
    if (record.updated_at) text += " · updated " + formatTime(record.updated_at);
    var history = record.history || [];
    if (history.length) {
      var last = history[history.length - 1];
      if (last && last.from && last.to) text += " · " + last.from + " → " + last.to;
    }
    return text;
  }

  function knowledgeFacts(body) {
    // A card with no reading still states its scope, rather than showing an
    // empty definition list that could be mistaken for "nothing is stored".
    body = body || {};
    var summary = body.summary || {};
    var status = body.knowledge || {};
    var session = body.session_memory || {};
    var error = status.last_error || null;
    var observed = (session.observations || []).length;
    var objectives = (session.objectives || []).length;
    return [
      ["Game", body.game || "not selected"],
      ["Records", summary.records === undefined ? "not read" : String(summary.records)],
      ["Verified", String(summary.verified || 0)],
      ["Provisional", String(summary.provisional || 0)],
      ["Contradicted", String(summary.contradicted || 0)],
      ["Stale", String(summary.stale || 0)],
      ["Open questions", String(summary.unresolved_questions || 0)],
      ["Revalidation", (summary.due_for_revalidation || []).length + " due (after " +
        (summary.revalidate_after_days === undefined ? "a configured period" :
          summary.revalidate_after_days + " days") + ")"],
      ["Session memory", observed + " observation(s), " + objectives +
        " objective reading(s) — not persisted"],
      ["Persistence", status.persistence || "not reported"],
      ["Last error", error ? (error.reason_code + " — " + error.reason) : "none"],
    ];
  }

  function knowledgeHint(body) {
    if (!body) {
      return "No game knowledge has been read. Knowledge is scoped to the game named " +
        "by a validated state.";
    }
    if (body.error) return "Knowledge could not be read: " + body.error;
    var summary = body.summary || {};
    var session = body.session_memory || {};
    if (!body.game) {
      var games = summary.games || [];
      return games.length
        ? "Known games: " + games.join(", ") +
          ". Name one, or read a state, to see its records."
        : "No game knowledge is stored yet. Read a state to record only what NF-16 could recognise.";
    }
    if (!(summary.records || 0)) {
      return "No knowledge is stored for " + body.game +
        " yet. Reading a state records its recognised fields and objectives.";
    }
    var text = summary.records + " record(s) · " + (summary.verified || 0) +
      " verified · " + (summary.provisional || 0) + " provisional · " +
      (summary.contradicted || 0) + " contradicted · " + (summary.stale || 0) + " stale.";
    var due = (summary.due_for_revalidation || []).length;
    if (due) text += " " + due + " due for revalidation.";
    text += " Session memory is not part of these records.";
    return text;
  }

  function knowledgeHistoryRows(records) {
    var rows = [];
    (records || []).forEach(function (record) {
      (record.history || []).forEach(function (event) {
        rows.push(record.key + " · " + event.from + " → " + event.to +
          (event.at ? " · " + formatTime(event.at) : "") +
          (event.reason ? " — " + event.reason : ""));
      });
    });
    return rows.slice(-8);
  }

  function renderKnowledge(body) {
    forge.knowledge.body = body || null;
    var badge = byId("forgeKnowledgeBadge");
    if (!badge) return;
    var summary = (body && body.summary) || null;
    var label = body && body.game
      ? ((summary && summary.records) || 0) + " record(s)"
      : "untested";
    badge.textContent = label;
    badge.dataset.state = body && body.game ? String((summary && summary.records) || 0) : "untested";
    setText("forgeKnowledgeHint", knowledgeHint(body));
    var input = byId("forgeKnowledgeGame");
    if (input && body && body.game && !String(input.value || "").trim()) {
      input.value = body.game;
    }
    var facts = byId("forgeKnowledgeFacts");
    if (facts) {
      facts.innerHTML = "";
      knowledgeFacts(body).forEach(function (row) {
        observationFact(facts, row[0], row[1]);
      });
    }
    var records = (body && body.records) || [];
    var current = records.filter(function (record) { return record.category !== "question"; });
    var questions = records.filter(function (record) { return record.category === "question"; });
    renderStateList(
      "forgeKnowledgeList",
      current.map(knowledgeRecordRow),
      body && body.game ? "No knowledge is stored for this game." :
        "No game is selected; name one or read a state."
    );
    renderStateList(
      "forgeKnowledgeQuestions",
      questions.map(function (record) {
        return "open question · " + record.key + " · " + record.status +
          (record.reason ? " · " + record.reason : "");
      }),
      body && body.game ? "No unresolved question is recorded." :
        "Unresolved questions appear once a game is read."
    );
    var due = ((summary && summary.due_for_revalidation) || []).map(function (row) {
      return "revalidate " + row.key + " · " + row.status + " · " + row.age_days + " day(s) old";
    });
    renderStateList(
      "forgeKnowledgeDue",
      due,
      "No record is due for revalidation."
    );
    var history = byId("forgeKnowledgeHistory");
    if (history) {
      history.innerHTML = "";
      var rows = knowledgeHistoryRows(records);
      if (!rows.length) {
        history.appendChild(el("p", "muted", "No knowledge status has changed yet."));
      } else {
        history.appendChild(el("p", "muted", "Knowledge history (newest last, bounded)."));
        var list = el("ul", "");
        rows.forEach(function (row) { list.appendChild(el("li", "", row)); });
        history.appendChild(list);
      }
    }
  }

  async function loadKnowledge(explicitGame) {
    var game = typeof explicitGame === "string" ? explicitGame.trim() : knowledgeGameFromPanel();
    forge.knowledge.game = game;
    if (!game) {
      renderKnowledge(null);
      return null;
    }
    try {
      var query = "?game=" + encodeURIComponent(game);
      var body = await requestJson("/novaforge/knowledge" + query);
      var listed = await requestJson("/novaforge/knowledge/records" + query + "&limit=60");
      body.records = listed.records || [];
      renderKnowledge(body);
      return body;
    } catch (error) {
      renderKnowledge({ game: game, error: String(error.message || error) });
      return null;
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
    var runObjective = run.objective || {};
    pairs.appendChild(detailRow("Objective", runObjective.achieved === true
      ? "achieved — " + String(runObjective.reason || "the verifier read success")
      : runObjective.decided
        ? "not achieved — " + String(runObjective.reason || "the verifier decided it was not met")
        : "not evaluated — " + String(runObjective.reason || "no verifier")));
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
        "CPU, memory and GPU/NPU use are unavailable in NF-04: no per-run probe exists. Objective success is reported only when a controlled run's verifier decided one; an undecided run is unavailable, never zero."));

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
  var CONTROL_MAX_ACTIONS = 8;
  var CONTROL_ACTION_KINDS = ["key", "hold", "click", "move"];
  var TITLE_MODES = ["contains", "equals", "absent"];
  var GAME_STATE_MODES = ["equals", "contains", "absent", "gte", "lte"];

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

  function evaluationMode() {
    return forge.evaluation.mode === "control" ? "control" : "observation_only";
  }

  function evaluationModeText(mode) {
    return String(mode || "observation_only") === "control"
      ? "controlled · experimental"
      : "observation only";
  }

  function controlPolicy() {
    var policy = (forge.evaluation.detail || {}).action_policy || {};
    return policy && typeof policy === "object" ? policy : {};
  }

  function controlMaxActions(policy) {
    var steps = Math.floor(Number((policy || {}).max_steps));
    if (!isFinite(steps) || steps < 1) return CONTROL_MAX_ACTIONS;
    return Math.min(CONTROL_MAX_ACTIONS, steps);
  }

  function controlActionSummary(action) {
    var item = action || {};
    var kind = String(item.kind || "?");
    if (kind === "key") {
      return "key " + String(item.key || "?") + " · " +
        String(item.duration_ms == null ? 60 : item.duration_ms) + " ms";
    }
    if (kind === "hold") {
      return "hold " + String(item.key || "?") + " for " +
        String(item.duration_ms == null ? 500 : item.duration_ms) + " ms";
    }
    if (kind === "click") {
      return "click " + String(item.button || "left") + " at " +
        String(item.x == null ? "?" : item.x) + "," + String(item.y == null ? "?" : item.y);
    }
    if (kind === "move") {
      return "move to " + String(item.x == null ? "?" : item.x) + "," +
        String(item.y == null ? "?" : item.y);
    }
    return "unknown action";
  }

  function controlObjectiveSummary(objective) {
    if (!objective || !objective.kind) return "not configured";
    var kind = String(objective.kind);
    var mode = String(objective.mode || (kind === "window_title" ? "contains" : "equals"));
    if (kind === "window_title") {
      return "title " + mode + " \"" + String(objective.parameter || "") + "\"";
    }
    if (kind === "game_state_file") {
      return String(objective.path || "(no path)") +
        (objective.pointer ? " · " + String(objective.pointer) : "") + " " + mode +
        (mode === "absent" ? "" : " \"" + String(objective.parameter || "") + "\"");
    }
    return kind;
  }

  // The verdict is the backend's own outcome, translated — never inferred from
  // "completed". A missing verdict is "not evaluated"; an undecided one is
  // "inconclusive"; only a decided outcome names a result.
  function controlVerdictText(objective) {
    var block = objective || {};
    if (block.configured === false || block.available === false) return "not evaluated";
    var outcome = String(block.outcome || "");
    if (outcome === "verified_success") return "verified success";
    if (outcome === "verified_failure") return "verified failure — not achieved";
    if (outcome === "verification_error") return "verification error";
    if (outcome === "in_progress") return "inconclusive — no verdict yet";
    if (block.decided === true && outcome) return outcome;
    return "inconclusive";
  }

  function controlVerdictTone(objective) {
    var outcome = String((objective || {}).outcome || "");
    if (outcome === "verified_success") return "ok";
    if (outcome === "verified_failure" || outcome === "verification_error") return "bad";
    if (outcome === "in_progress" || (objective || {}).decided === false) return "warn";
    return "muted";
  }

  function controlStageText(state) {
    var value = String(state || "pending");
    if (value === "verified_success") return "verified success";
    if (value === "verified_failure") return "verified failure — not achieved";
    if (value === "running") return "in progress";
    if (value === "not_applicable") return "not applicable";
    if (value === "not_started") return "not started";
    if (value === "failed") return "failed";
    if (value === "refused") return "refused";
    if (value === "skipped") return "skipped";
    if (value === "completed") return "completed";
    if (value === "approved") return "approved";
    if (value === "issued") return "issued";
    if (value === "recorded") return "recorded";
    if (value === "pending") return "pending";
    return value || "unknown";
  }

  function controlStageTone(state) {
    var value = String(state || "");
    if (value === "verified_success" || value === "completed" || value === "approved" ||
        value === "issued" || value === "recorded") return "ok";
    if (value === "verified_failure" || value === "failed" || value === "refused") return "bad";
    if (value === "running") return "warn";
    return "muted";
  }

  // A metric row is the backend's measurement. `available: false` is rendered as
  // "Unavailable" WITH its reason — never as 0, which would be a measurement the
  // build did not take.
  function controlMetricText(run, key) {
    var row = ((run || {}).metrics || {})[key] || {};
    if (row.available !== true) {
      return "Unavailable" + (row.reason ? " — " + String(row.reason) : "");
    }
    if (row.value === null || row.value === undefined) {
      return "Unavailable — no value was recorded";
    }
    if (key === "objective_success") {
      return Number(row.value) === 1 ? "achieved" : "not achieved";
    }
    if (key === "completion_time_seconds") return String(row.value) + " s";
    if (key === "actions_executed") return String(row.value);
    return String(row.value) + (row.unit ? " " + String(row.unit) : "");
  }

  function controlObjectiveErrors(objective) {
    var block = objective || {};
    var kind = String(block.kind || "");
    var problems = [];
    if (kind === "window_title") {
      if (!String(block.parameter || "").trim()) {
        problems.push("Enter the title text the verifier must read.");
      }
      if (TITLE_MODES.indexOf(String(block.mode || "contains")) < 0) {
        problems.push("The comparison must be contains, equals or absent.");
      }
    } else if (kind === "game_state_file") {
      var path = String(block.path || "").trim().replace(/\\/g, "/");
      if (!path) {
        problems.push("Enter the artifact's path, relative to a declared root.");
      } else {
        if (path.charAt(0) === "/" || /^[A-Za-z]:/.test(path)) {
          problems.push("The artifact path must be relative to a declared root, not absolute.");
        }
        if (path.split("/").indexOf("..") >= 0) {
          problems.push("The artifact path must not contain '..'.");
        }
        if (/[*?[\]]/.test(path)) {
          problems.push("The artifact path must name one file, not a pattern.");
        }
      }
      var mode = String(block.mode || "equals");
      if (GAME_STATE_MODES.indexOf(mode) < 0) {
        problems.push("The comparison must be one this verifier supports.");
      } else if (mode !== "absent" && !String(block.parameter || "").trim()) {
        problems.push("Enter the value the verifier must compare against.");
      }
      var age = block.max_age_seconds;
      if (age !== null && age !== undefined && age !== "") {
        var number = Number(age);
        if (!isFinite(number) || number < 1 || number > 3600) {
          problems.push("Maximum age must be between 1 and 3600 seconds, or blank.");
        }
      }
    } else {
      problems.push("Choose an objective kind this host can verify.");
    }
    return problems;
  }

  function controlCoordinate(value) {
    if (value === null || value === undefined || String(value).trim() === "") return null;
    var number = Number(value);
    return isFinite(number) ? number : null;
  }

  // UI-side validation only. The backend parses, validates and refuses the plan
  // again, so this list is a courtesy that blocks an obviously bad request — it
  // is never treated as the authoritative check.
  function controlPlanErrors(plan, budget, policy) {
    var actions = Array.isArray(plan) ? plan : [];
    var limits = policy || {};
    var problems = [];
    var maxActions = controlMaxActions(limits);
    if (!actions.length) problems.push("An action plan needs at least one action.");
    if (actions.length > maxActions) {
      problems.push("The plan may contain at most " + maxActions + " actions.");
    }
    var budgetNumber = Number(budget);
    if (budget === null || budget === undefined || budget === "" || !isFinite(budgetNumber)) {
      problems.push("An action budget is required.");
    } else if (budgetNumber < actions.length) {
      problems.push("The action budget (" + budgetNumber + ") is smaller than the plan (" +
        actions.length + " actions).");
    } else if (budgetNumber < 1) {
      problems.push("The action budget must be at least 1.");
    }
    var maxHold = Number(limits.max_hold_ms);
    if (!isFinite(maxHold) || maxHold < 1) maxHold = 2000;
    var allowedKeys = Array.isArray(limits.allowed_keys) ? limits.allowed_keys : [];
    var allowedButtons = Array.isArray(limits.allowed_buttons) ? limits.allowed_buttons : [];
    actions.forEach(function (action, index) {
      var item = action || {};
      var label = "Action " + (index + 1);
      var kind = String(item.kind || "");
      if (CONTROL_ACTION_KINDS.indexOf(kind) < 0) {
        problems.push(label + " has an unknown kind.");
        return;
      }
      if (kind === "key" || kind === "hold") {
        var key = String(item.key || "").trim().toLowerCase();
        if (!key) problems.push(label + " needs a key.");
        else if (allowedKeys.length && allowedKeys.indexOf(key) < 0) {
          problems.push(label + " uses " + key + ", which the action policy does not allow.");
        }
        var duration = Number(item.duration_ms);
        if (!isFinite(duration) || duration < 1 || duration > maxHold) {
          problems.push(label + " must last between 1 and " + maxHold + " ms.");
        }
      }
      if (kind === "click" || kind === "move") {
        var x = controlCoordinate(item.x);
        var y = controlCoordinate(item.y);
        if (x === null || y === null || x < 0 || y < 0) {
          problems.push(label + " needs non-negative x and y client-area coordinates.");
        }
      }
      if (kind === "click" && allowedButtons.length &&
          allowedButtons.indexOf(String(item.button || "left")) < 0) {
        problems.push(label + " uses a mouse button the policy does not allow.");
      }
    });
    return problems;
  }

  function controlStartGuard(busy, preflight, run, approved, signatureMatches) {
    if (busy === true) return "A start is already in flight; wait for it to finish.";
    if (!preflight || preflight.ready !== true) return "Validate readiness for this exact configuration first.";
    if (run && run.status === "running") return "An evaluation is already running.";
    if (signatureMatches === false) return "The configuration changed; validate readiness again.";
    if (approved !== true) {
      return String(preflight.mode || "observation_only") === "control"
        ? "Approve the exact plan before starting it."
        : "Confirm this exact configuration before starting.";
    }
    if (String(preflight.mode || "observation_only") === "control") {
      if (!((preflight.approval || {}).digest)) {
        return "No approval digest was issued; validate readiness again.";
      }
    }
    return "";
  }

  // After an ambiguous start failure, adopt a run that is ALREADY executing this
  // exact digest. Never re-submit: a second POST is a second execution request.
  function controlStartRecovery(rows, digest) {
    var wanted = String(digest || "");
    if (!wanted) return null;
    var found = (rows || []).filter(function (row) {
      return row && row.status === "running" && String(row.plan_digest || "") === wanted;
    });
    return found.length ? found[0] : null;
  }

  // ---- controlled-mode form -------------------------------------------------

  function evaluationObjectiveKind() {
    var select = byId("forgeEvaluationObjectiveKind");
    return select ? String(select.value || "window_title") : "window_title";
  }

  // The comparison list is the VERIFIER's own vocabulary: a window title can be
  // contains/equals/absent, a JSON value can also be ordered. Offering a mode
  // the verifier cannot apply would be a control that always fails preflight.
  function syncEvaluationObjectiveFields() {
    var file = evaluationObjectiveKind() === "game_state_file";
    ["forgeEvaluationObjectivePathRow", "forgeEvaluationObjectivePointerRow",
      "forgeEvaluationObjectiveAgeRow"].forEach(function (id) {
      var row = byId(id);
      if (row) row.hidden = !file;
    });
    var select = byId("forgeEvaluationObjectiveMode");
    if (select) {
      var allowed = file ? GAME_STATE_MODES : TITLE_MODES;
      var previous = String(select.value || "");
      select.innerHTML = "";
      allowed.forEach(function (value) {
        var option = document.createElement("option");
        option.value = value;
        option.textContent = value === "gte" ? "greater than or equal"
          : value === "lte" ? "less than or equal" : value;
        select.appendChild(option);
      });
      select.value = allowed.indexOf(previous) >= 0 ? previous : allowed[0];
    }
    var evidence = byId("forgeEvaluationObjectiveEvidence");
    if (evidence) {
      evidence.textContent = file
        ? "The verifier reads the artifact the target writes; a missing, stale or malformed artifact is reported as such and never counted as success."
        : "The target's own title is read by the verifier; the action record is never evidence.";
    }
  }

  function evaluationPlanAction(kind) {
    if (kind === "click") return { kind: "click", x: null, y: null, button: "left", note: "" };
    if (kind === "move") return { kind: "move", x: null, y: null, note: "" };
    if (kind === "hold") return { kind: "hold", key: "", duration_ms: 500, note: "" };
    return { kind: "key", key: "", duration_ms: 60, note: "" };
  }

  function controlPlanNumberField(field) {
    return field === "duration_ms" || field === "x" || field === "y";
  }

  function controlPlanInput(action, field, placeholder, options, onEdit) {
    var input = document.createElement(options && options.select ? "select" : "input");
    if (!(options && options.select)) {
      input.type = controlPlanNumberField(field) ? "number" : "text";
      input.autocomplete = "off";
      if (placeholder) input.placeholder = placeholder;
      input.value = action[field] == null ? "" : String(action[field]);
    }
    input.className = "cyber-input";
    if (options && options.select) {
      (options.values || []).forEach(function (value) {
        var option = document.createElement("option");
        option.value = value;
        option.textContent = value;
        input.appendChild(option);
      });
      input.value = String(action[field] || options.values[0]);
    }
    input.addEventListener("input", function () {
      var value = String(input.value == null ? "" : input.value).trim();
      action[field] = controlPlanNumberField(field)
        ? (value === "" ? null : Number(value))
        : value;
      // The row's own summary is refreshed in place: a full re-render would
      // rebuild the inputs and steal focus mid-edit.
      if (onEdit) onEdit();
      invalidateEvaluationDigest();
      updateEvaluationPlanSummary();
    });
    if (options && options.select) {
      input.addEventListener("change", function () {
        action[field] = String(input.value || options.values[0]);
        if (onEdit) onEdit();
        invalidateEvaluationDigest();
        updateEvaluationPlanSummary();
      });
    }
    return input;
  }

  function controlPlanField(labelText, node) {
    var wrap = el("label", "forge-plan-field");
    wrap.appendChild(el("span", "", labelText));
    wrap.appendChild(node);
    return wrap;
  }

  function controlPlanRow(action, index, policy) {
    var row = el("div", "forge-plan-row");
    row.dataset.kind = String(action.kind || "");
    var head = el("div", "forge-plan-row-head");
    head.appendChild(el("span", "forge-plan-index", String(index + 1)));
    head.appendChild(el("span", "forge-plan-kind", String(action.kind || "?")));
    var summaryNode = el("span", "forge-plan-summary", controlActionSummary(action));
    head.appendChild(summaryNode);
    var onEdit = function () {
      summaryNode.textContent = controlActionSummary(action);
    };
    var remove = el("button", "cyber-btn small danger", "Remove");
    remove.type = "button";
    remove.addEventListener("click", function () {
      forge.evaluation.plan.splice(index, 1);
      invalidateEvaluationDigest();
      renderEvaluationPlan();
    });
    head.appendChild(remove);
    row.appendChild(head);

    var fields = el("div", "forge-plan-fields");
    var allowedKeys = Array.isArray(policy.allowed_keys) ? policy.allowed_keys : [];
    if (action.kind === "key" || action.kind === "hold") {
      fields.appendChild(controlPlanField("Key", controlPlanInput(
        action, "key", allowedKeys.length ? allowedKeys[0] : "space",
        allowedKeys.length ? { select: true, values: allowedKeys } : null, onEdit
      )));
      fields.appendChild(controlPlanField("Duration (ms)", controlPlanInput(
        action, "duration_ms", action.kind === "hold" ? "500" : "60", null, onEdit
      )));
    }
    if (action.kind === "click" || action.kind === "move") {
      fields.appendChild(controlPlanField("X (client area)",
        controlPlanInput(action, "x", "0", null, onEdit)));
      fields.appendChild(controlPlanField("Y (client area)",
        controlPlanInput(action, "y", "0", null, onEdit)));
    }
    if (action.kind === "click") {
      var buttons = Array.isArray(policy.allowed_buttons) && policy.allowed_buttons.length
        ? policy.allowed_buttons : ["left"];
      fields.appendChild(controlPlanField("Button", controlPlanInput(
        action, "button", "", { select: true, values: buttons }, onEdit
      )));
    }
    row.appendChild(fields);
    return row;
  }

  function controlPolicyText(policy) {
    var limits = policy || {};
    var parts = [];
    if (Array.isArray(limits.allowed_keys) && limits.allowed_keys.length) {
      parts.push("keys " + limits.allowed_keys.join(", "));
    }
    if (Array.isArray(limits.allowed_buttons) && limits.allowed_buttons.length) {
      parts.push("buttons " + limits.allowed_buttons.join(", "));
    }
    var maxHold = Number(limits.max_hold_ms);
    if (isFinite(maxHold) && maxHold > 0) parts.push("hold at most " + maxHold + " ms");
    parts.push("at most " + controlMaxActions(limits) + " actions per run");
    return "Declared action policy: " + parts.join(" · ") + ".";
  }

  function evaluationPlanSummaryText(plan) {
    var actions = plan || [];
    if (!actions.length) return "No action is planned yet.";
    return actions.length + (actions.length === 1 ? " action: " : " actions: ") +
      actions.map(function (action) { return controlActionSummary(action); }).join(" → ");
  }

  // UI-side validation is a courtesy; the backend parses and refuses the plan
  // again, and its refusal is the one the record keeps.
  function evaluationPlanProblems() {
    var budget = byId("forgeEvaluationBudget");
    var plan = forge.evaluation.plan;
    var policy = controlPolicy();
    return controlObjectiveErrors(evaluationObjectivePayload()).concat(
      controlPlanErrors(plan, budget ? budget.value : "", policy)
    );
  }

  function updateEvaluationPlanSummary() {
    var summary = byId("forgeEvaluationPlanSummary");
    if (summary) summary.textContent = evaluationPlanSummaryText(forge.evaluation.plan);
    renderEvaluationPlanProblems();
    updateEvaluationObjectiveState();
  }

  function renderEvaluationPlanProblems() {
    var box = byId("forgeEvaluationPlanProblems");
    if (!box) return;
    var problems = evaluationPlanProblems();
    box.innerHTML = "";
    box.hidden = !problems.length;
    if (!problems.length) return;
    var list = el("ul", "");
    problems.forEach(function (problem) { list.appendChild(el("li", "", problem)); });
    box.appendChild(list);
  }

  function updateEvaluationObjectiveState() {
    var badge = byId("forgeEvaluationObjectiveState");
    if (!badge) return;
    if (evaluationMode() !== "control") {
      badge.textContent = "not evaluated";
      badge.dataset.ok = "false";
      return;
    }
    var problems = controlObjectiveErrors(evaluationObjectivePayload());
    badge.textContent = problems.length ? "incomplete" : "configured";
    badge.dataset.ok = problems.length ? "false" : "true";
  }

  function renderEvaluationPlan() {
    var list = byId("forgeEvaluationPlanList");
    var badge = byId("forgeEvaluationPlanState");
    var limitsBox = byId("forgeEvaluationPlanLimits");
    var policy = controlPolicy();
    var plan = forge.evaluation.plan;
    if (list) {
      list.innerHTML = "";
      plan.forEach(function (action, index) {
        list.appendChild(controlPlanRow(action, index, policy));
      });
      if (!plan.length) {
        list.appendChild(el("p", "muted",
          "No action is planned. A controlled run needs at least one operator-authored action."));
      }
    }
    if (badge) {
      badge.textContent = plan.length + (plan.length === 1 ? " action" : " actions");
      badge.dataset.ok = plan.length ? "true" : "false";
    }
    if (limitsBox) limitsBox.textContent = controlPolicyText(policy);
    updateEvaluationPlanSummary();
  }

  // An edit breaks the digest an operator would approve, so the previous
  // preflight and approval can never be reused for the edited plan.
  function invalidateEvaluationDigest() {
    forge.evaluation.preflight = null;
    forge.evaluation.signature = "";
    forge.evaluation.approval = null;
    var approve = byId("forgeEvaluationApprove");
    if (approve) {
      approve.checked = false;
      approve.disabled = true;
    }
    var row = byId("forgeEvaluationApprovalRow");
    if (row) row.hidden = true;
    updateEvaluationControls();
  }

  function addEvaluationPlanAction(kind) {
    var policy = controlPolicy();
    if (forge.evaluation.plan.length >= controlMaxActions(policy)) {
      evaluationNotice("The declared policy allows at most " + controlMaxActions(policy) +
        " actions in one plan.", "error");
      return;
    }
    forge.evaluation.plan.push(evaluationPlanAction(kind));
    invalidateEvaluationDigest();
    renderEvaluationPlan();
  }

  function applyEvaluationMode() {
    var control = evaluationMode() === "control";
    var banner = byId("forgeEvaluationControlBanner");
    if (banner) banner.hidden = !control;
    var config = byId("forgeEvaluationControlConfig");
    if (config) config.hidden = !control;
    var confirmRow = byId("forgeEvaluationConfirmRow");
    if (confirmRow) confirmRow.hidden = control;
    var confirm = byId("forgeEvaluationConfirm");
    if (confirm && control) confirm.checked = false;
    var approvalRow = byId("forgeEvaluationApprovalRow");
    if (approvalRow) approvalRow.hidden = true;
    if (control) {
      syncEvaluationObjectiveFields();
      renderEvaluationPlan();
    }
    updateEvaluationControls();
  }

  function setEvaluationMode(mode) {
    forge.evaluation.mode = String(mode) === "control" ? "control" : "observation_only";
    var select = byId("forgeEvaluationMode");
    if (select) select.value = forge.evaluation.mode;
    forge.evaluation.approval = null;
    if (evaluationMode() === "control") forge.evaluation.plan = forge.evaluation.plan || [];
    applyEvaluationMode();
    invalidateEvaluationPreflight();
  }

  function controlDigestText(body) {
    var approval = (body || {}).approval || {};
    var digest = String(approval.digest || ((body || {}).control || {}).plan_digest || "");
    if (!digest) return "No digest was issued.";
    return digest;
  }

  function renderEvaluationControlPreflight(body) {
    var control = (body || {}).control;
    if (!control) return null;
    var section = el("div", "forge-control-digest");
    section.appendChild(el("h4", "", "Approved plan"));
    var pairs = el("div", "forge-benchmark-pairs");
    pairs.appendChild(detailRow("Objective", controlObjectiveSummary(control.objective)));
    pairs.appendChild(detailRow("Actions", String((control.actions || []).length) +
      " · budget " + String((control.limits || {}).max_actions == null
        ? "?" : (control.limits || {}).max_actions)));
    pairs.appendChild(detailRow("Target", String((control.target || {}).title || "not resolved") +
      ((control.target || {}).handle == null ? "" : " · handle " + control.target.handle)));
    pairs.appendChild(detailRow("Approval", String(((body || {}).approval || {}).digest)
      ? "issued · single use" : "not issued"));
    section.appendChild(pairs);

    if ((control.actions || []).length) {
      var list = el("ol", "forge-note-list");
      control.actions.forEach(function (action) {
        list.appendChild(el("li", "", controlActionSummary(action)));
      });
      section.appendChild(list);
    }
    var digest = controlDigestText(body);
    var digestRow = el("p", "forge-digest", "Plan digest: " + digest);
    digestRow.dataset.digest = digest;
    section.appendChild(digestRow);
    section.appendChild(el("p", "forge-frame-note",
      "Approval is bound to this digest and is single-use. Editing the objective, plan, "+
      "target or limits requires a new validation and a new approval."));
    if (control.reason) section.appendChild(el("p", "muted", String(control.reason)));
    return section;
  }

  function controlStatesTable(control) {
    var table = el("table", "forge-benchmark-table");
    var head = el("thead", "");
    var headRow = el("tr", "");
    ["Stage", "State", "Reason"].forEach(function (labelText) {
      headRow.appendChild(el("th", "", labelText));
    });
    head.appendChild(headRow);
    table.appendChild(head);
    var body = el("tbody", "");
    ((control || {}).states || []).forEach(function (row) {
      var tr = el("tr", "");
      tr.dataset.status = String((row || {}).state || "");
      tr.appendChild(el("td", "", String((row || {}).stage || "")));
      tr.appendChild(el("td", "", controlStageText((row || {}).state)));
      tr.appendChild(el("td", "", String((row || {}).reason || "")));
      body.appendChild(tr);
    });
    table.appendChild(body);
    return table;
  }

  function controlStepsTable(control) {
    var steps = ((control || {}).steps || []);
    if (!steps.length) return null;
    var table = el("table", "forge-benchmark-table");
    var head = el("thead", "");
    var headRow = el("tr", "");
    ["#", "Action", "Status", "Reason"].forEach(function (labelText) {
      headRow.appendChild(el("th", "", labelText));
    });
    head.appendChild(headRow);
    table.appendChild(head);
    var body = el("tbody", "");
    steps.forEach(function (step) {
      var tr = el("tr", "");
      tr.dataset.status = String((step || {}).status || "");
      tr.appendChild(el("td", "", String((step || {}).index == null ? "" : step.index)));
      tr.appendChild(el("td", "", controlActionSummary((step || {}).action)));
      tr.appendChild(el("td", "", controlStageText((step || {}).status)));
      tr.appendChild(el("td", "", String((step || {}).reason || "")));
      body.appendChild(tr);
    });
    table.appendChild(body);
    return table;
  }

  function controlMetricsTable(run) {
    var metrics = (run || {}).metrics || {};
    var keys = Object.keys(metrics);
    if (!keys.length) return null;
    return benchmarkTable(keys.map(function (key) {
      var row = Object.assign({}, metrics[key] || {}, { label: key });
      return {
        label: key,
        metric: Object.assign(benchmarkMetric(row), { text: controlMetricText(run, key) }),
        source: String((metrics[key] || {}).source || ""),
      };
    }));
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

  function evaluationObjectivePayload() {
    var kind = byId("forgeEvaluationObjectiveKind");
    var mode = byId("forgeEvaluationObjectiveMode");
    var parameter = byId("forgeEvaluationObjectiveParameter");
    var path = byId("forgeEvaluationObjectivePath");
    var pointer = byId("forgeEvaluationObjectivePointer");
    var age = byId("forgeEvaluationObjectiveAge");
    var objective = {
      kind: kind ? String(kind.value || "") : "",
      mode: mode ? String(mode.value || "") : "contains",
      parameter: parameter ? String(parameter.value || "").trim() : "",
    };
    if (objective.kind === "game_state_file") {
      objective.path = path ? String(path.value || "").trim() : "";
      objective.pointer = pointer ? String(pointer.value || "").trim() : "";
      objective.max_age_seconds = age && String(age.value || "").trim() !== ""
        ? Number(age.value)
        : null;
    }
    return objective;
  }

  // The payload carries ONLY the fields the action's own kind defines, so a
  // stale coordinate from a previously-selected kind cannot ride along.
  function evaluationPlanPayload() {
    return forge.evaluation.plan.map(function (action) {
      var kind = String((action || {}).kind || "");
      var item = {
        kind: kind,
        button: String(action.button || "left"),
        duration_ms: Number(action.duration_ms == null ? 60 : action.duration_ms),
        note: String(action.note || ""),
      };
      if (kind === "key" || kind === "hold") item.key = String(action.key || "");
      if (kind === "click" || kind === "move") {
        item.x = controlCoordinate(action.x);
        item.y = controlCoordinate(action.y);
      }
      return item;
    });
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
    var budget = byId("forgeEvaluationBudget");
    var payload = {
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
      mode: evaluationMode(),
    };
    if (payload.mode === "control") {
      payload.objective = evaluationObjectivePayload();
      payload.action_plan = evaluationPlanPayload();
      payload.max_actions = budget && String(budget.value || "").trim() !== ""
        ? Number(budget.value)
        : null;
      // The backend's own defaults, named here so the digest covers what will
      // actually run rather than whatever the server would have chosen.
      payload.action_interval_seconds = 0.5;
      payload.verify_timeout_seconds = 15;
    }
    return payload;
  }

  function evaluationSignature() {
    var data = evaluationPayload();
    // The baseline choice does not change readiness, so selecting one must not
    // force a new validation; the configuration, mode, objective and plan do.
    delete data.baseline_id;
    return JSON.stringify(data);
  }

  // Start needs a PASSING readiness verdict for THIS configuration, no active
  // run, and the operator's explicit consent (for a controlled run: approval of
  // the exact digest). Absence of either is never treated as consent.
  function evaluationCanStart(preflight, run, confirmed, signatureMatches) {
    return controlStartGuard(false, preflight, run, confirmed, signatureMatches) === "";
  }

  function evaluationStatusText(run) {
    if (!run) return "no evaluation";
    var status = String(run.status || "unknown");
    var control = String(run.mode || "observation_only") === "control";
    if (status === "running") return "running";
    if (status === "completed") {
      if (control) {
        // A full record carries `objective.decided`; a list row carries
        // `objective_verified`. Reading both keeps a verified success from
        // rendering as "inconclusive" just because the row is a summary.
        var decided = (run.objective || {}).decided === true || run.objective_verified === true;
        return decided ? "completed · objective verified" : "completed · objective inconclusive";
      }
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

  function evaluationControlPreflight() {
    var preflight = forge.evaluation.preflight;
    return preflight && String(preflight.mode || "") === "control" ? preflight : null;
  }

  function updateEvaluationControls() {
    var start = byId("forgeEvaluationStartButton");
    var cancel = byId("forgeEvaluationCancelButton");
    var report = byId("forgeEvaluationReportButton");
    var exportButton = byId("forgeEvaluationExportButton");
    var confirm = byId("forgeEvaluationConfirm");
    var approve = byId("forgeEvaluationApprove");
    var approvalRow = byId("forgeEvaluationApprovalRow");
    var run = forge.evaluation.run;
    var controlPreflight = evaluationControlPreflight();
    var approved = approve ? approve.checked === true : false;
    var consent = evaluationMode() === "control"
      ? approved
      : (confirm ? confirm.checked === true : false);
    var signatureMatches = forge.evaluation.signature === evaluationSignature();
    var reason = controlStartGuard(
      forge.evaluation.busy, forge.evaluation.preflight, run, consent, signatureMatches
    );
    var canStart = evaluationCanStart(
      forge.evaluation.preflight, run, consent, signatureMatches
    );
    if (approvalRow) approvalRow.hidden = !(evaluationMode() === "control" && controlPreflight);
    if (approve) approve.disabled = !(controlPreflight && controlPreflight.ready === true);
    if (start) {
      start.disabled = !canStart || forge.evaluation.busy === true;
      start.textContent = evaluationMode() === "control" ? "Approve And Run" : "Start Evaluation";
      start.title = reason || "Start this validated evaluation";
    }
    if (cancel) cancel.disabled = !(run && run.status === "running");
    if (report) report.disabled = !(run && run.status !== "running");
    if (exportButton) exportButton.disabled = !forge.evaluation.report;
  }

  function invalidateEvaluationPreflight() {
    forge.evaluation.preflight = null;
    forge.evaluation.signature = "";
    forge.evaluation.approval = null;
    forge.evaluation.control = null;
    var approve = byId("forgeEvaluationApprove");
    if (approve) approve.checked = false;
    var state = byId("forgeEvaluationObjectiveState");
    if (state) {
      state.textContent = evaluationMode() === "control" ? "not validated" : "not evaluated";
      state.dataset.ok = "false";
    }
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
        evaluationNotice("No environment is controllable on this host yet: a controlled run needs a Guardian-enforced injector and a ready objective verifier. Observation-only entries can still be run.");
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
    var declaresControl = (detail.evaluation_modes || []).indexOf("control") >= 0;
    head.appendChild(el("span", "forge-benchmark-version", "v" + String(detail.version) +
      " · " + (detail.control_capable
        ? "controllable on this host"
        : declaresControl
          ? "control declared — unavailable on this host"
          : "observation only")));
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
      // The plan editor and its limits are the ENVIRONMENT's declared policy.
      if (evaluationMode() === "control") renderEvaluationPlan();
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
    var control = renderEvaluationControlPreflight(body);
    if (control) box.appendChild(control);
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
      // A new validation mints a new digest, so a tick from the previous one
      // must never carry over as consent for this one.
      if (String(body.mode || "") === "control") {
        var approve = byId("forgeEvaluationApprove");
        if (approve) approve.checked = false;
      }
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
    if (payload.mode === "control") {
      // The digest is the approval: echoing a digest the operator never saw is
      // impossible because the preflight response is the only source of it.
      payload.approval_digest = String(
        ((forge.evaluation.preflight || {}).approval || {}).digest || ""
      );
    }
    return payload;
  }

  // A failed start may still have reached the server. Adopt a run that is
  // ALREADY executing this exact digest instead of re-posting the plan — a
  // second POST would be a second execution request, not a retry.
  async function recoverEvaluationStart(digest) {
    if (!digest) return false;
    try {
      var body = await requestJson("/novaforge/evaluations?limit=25");
      forge.evaluation.history = body.evaluations || [];
      var running = controlStartRecovery(forge.evaluation.history, digest);
      if (!running) return false;
      forge.evaluation.evaluationId = running.id;
      forge.evaluation.run = running;
      forge.evaluation.runToken += 1;
      renderEvaluationRun(running, true);
      updateEvaluationControls();
      pollEvaluation(running.id, forge.evaluation.runToken);
      evaluationNotice("The start response was lost, but this digest is already running as " +
        String(running.id) + "; the panel adopted that run instead of starting a second one.");
      return true;
    } catch (error) {
      return false;
    }
  }

  async function startEvaluation() {
    var confirm = byId("forgeEvaluationConfirm");
    var approve = byId("forgeEvaluationApprove");
    var control = evaluationMode() === "control";
    var consent = control
      ? (approve ? approve.checked === true : false)
      : (confirm ? confirm.checked === true : false);
    var reason = controlStartGuard(
      forge.evaluation.busy,
      forge.evaluation.preflight,
      forge.evaluation.run,
      consent,
      forge.evaluation.signature === evaluationSignature()
    );
    if (reason) {
      evaluationNotice(reason, "error");
      return;
    }
    var digest = control
      ? String(((forge.evaluation.preflight || {}).approval || {}).digest || "")
      : "";
    forge.evaluation.busy = true;
    updateEvaluationControls();
    evaluationNotice(control
      ? "Starting the approved plan through the Guardian…"
      : "Starting the bounded observation session…");
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
      evaluationNotice(control
        ? "Controlled evaluation started. Only the approved actions run; the verdict is the verifier's own reading of the target, and this panel does not claim one earlier."
        : "Evaluation started. No gameplay input is sent and no objective is verified.");
    } catch (error) {
      var adopted = control ? await recoverEvaluationStart(digest) : false;
      if (!adopted) {
        evaluationNotice("The evaluation could not start: " + requestErrorMessage(error), "error");
      }
      // An approval is single-use, so a start that may have consumed it can
      // never be retried on the same digest; a fresh validation is required.
      if (control) invalidateEvaluationDigest();
      loadEvaluationHistory();
    } finally {
      forge.evaluation.busy = false;
      updateEvaluationControls();
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

    var control = String(run.mode || "") === "control";
    var block = run.control || {};
    var pairs = el("div", "forge-benchmark-pairs");
    pairs.appendChild(detailRow("Mode", evaluationModeText(run.mode)));
    pairs.appendChild(detailRow("Benchmark run", String(run.benchmark_run_id || "not started")));
    pairs.appendChild(detailRow("Session", String(run.session_id || "not started")));
    pairs.appendChild(detailRow("Baseline role", String((run.baseline || {}).role || "—")));
    pairs.appendChild(detailRow("Objective", control
      ? controlVerdictText(run.objective)
      : "not evaluated — " + String((run.objective || {}).reason || "no verifier")));
    if (control) {
      pairs.appendChild(detailRow("Plan digest", String(block.plan_digest || "not recorded")));
      pairs.appendChild(detailRow("Control state", controlStageText(block.state)));
      pairs.appendChild(detailRow("Actions recorded", String((block.steps || []).length) +
        " · budget " + String((block.limits || {}).max_actions == null
          ? "?" : (block.limits || {}).max_actions)));
    }
    pairs.appendChild(detailRow("Duration", formatDuration(run.duration_seconds)));
    box.appendChild(pairs);
    if (control) {
      var verdict = el("p", "forge-verdict", controlVerdictText(run.objective) + " — " +
        String((run.objective || {}).reason || "no reason recorded"));
      verdict.dataset.tone = controlVerdictTone(run.objective);
      box.appendChild(verdict);
      box.appendChild(el("h4", "", "Control stages"));
      box.appendChild(controlStatesTable(block));
      var steps = controlStepsTable(block);
      if (steps) {
        box.appendChild(el("h4", "", "Approved actions"));
        box.appendChild(steps);
      }
      var metrics = controlMetricsTable(run);
      if (metrics) {
        box.appendChild(el("h4", "", "Measured metrics"));
        box.appendChild(metrics);
      }
    }
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

    var control = String(report.mode || "") === "control";
    var pairs = el("div", "forge-benchmark-pairs");
    var compatibility = report.compatibility || {};
    pairs.appendChild(detailRow("Compatibility", String(compatibility.status || "not stated") +
      " — " + String(compatibility.reason || "no reason")));
    pairs.appendChild(detailRow("Mode", evaluationModeText(report.mode)));
    pairs.appendChild(detailRow("Objective verified", control
      ? controlVerdictText(report.objective)
      : "no — " + String((report.objective || {}).reason || "no verifier")));
    pairs.appendChild(detailRow("Gameplay control exercised", report.real_gameplay_control_exercised ? "yes" : "no"));
    if (control) {
      pairs.appendChild(detailRow("Plan digest", String(report.plan_digest || "not recorded")));
      pairs.appendChild(detailRow("Approval", String((report.approval || {}).digest)
        ? "issued for this digest" : "not recorded"));
      pairs.appendChild(detailRow("Verdict", String(report.verdict || controlVerdictText(report.objective))));
      pairs.appendChild(detailRow("Objective", controlObjectiveSummary(
        ((report.control || {}).objective) || (report.objective || {}).objective
      )));
    }
    pairs.appendChild(detailRow("Benchmark run", String(report.benchmark_run_id || "not started")));
    pairs.appendChild(detailRow("Session", String(report.session_id || "not started")));
    pairs.appendChild(detailRow("Readiness", readinessCountsText(report.readiness)));
    box.appendChild(pairs);

    if (control) {
      // The verdict is rendered as the VERIFIER's reading, with its reason, and
      // never as a consequence of the actions having executed.
      var verdictLine = el("p", "forge-verdict",
        String(report.verdict || controlVerdictText(report.objective)) + " — " +
        String((report.objective || {}).reason || "no reason recorded"));
      verdictLine.dataset.tone = controlVerdictTone(report.objective);
      box.appendChild(verdictLine);
      box.appendChild(el("h4", "", "Control stages"));
      box.appendChild(controlStatesTable(report.control));
      var executedSteps = controlStepsTable(report.control);
      if (executedSteps) {
        box.appendChild(el("h4", "", "Approved actions"));
        box.appendChild(executedSteps);
      }
      var controlMetrics = controlMetricsTable(report);
      if (controlMetrics) {
        box.appendChild(el("h4", "", "Measured metrics"));
        box.appendChild(controlMetrics);
      }
      var verifierEvidence = ((report.objective || {}).evidence || []);
      if (verifierEvidence.length) {
        box.appendChild(el("h4", "", "Verifier evidence"));
        var evidenceList = el("ul", "forge-note-list");
        verifierEvidence.forEach(function (row) {
          evidenceList.appendChild(el("li", "", String(row)));
        });
        box.appendChild(evidenceList);
      }
    }

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
    applyEvaluationMode();
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

  /* ── NF-12: Game Library ───────────────────────────────────── */
  //
  // The tab is a CLIENT of the NF-11 service. Every row, count, status and
  // reason comes from /novaforge/library; the panel never re-derives whether a
  // game is installed, running or tested. A launch has two steps on purpose:
  // the first request only previews (and mints the server's token), the second
  // approves that exact plan. The four pipeline facts — requested, process,
  // window, verified — are painted from the response, so a started command can
  // never look like a verified launch, and a verified launch never implies
  // that capture works.

  var GAME_STATUS = {
    INSTALLED_AND_LAUNCHABLE: { label: "installed · launchable", tone: "good" },
    ALREADY_RUNNING: { label: "already running", tone: "muted" },
    INSTALLED_BUT_LAUNCH_FAILED: { label: "launch failed", tone: "bad" },
    NOT_FOUND_ON_INTERNAL_STORAGE: { label: "not found", tone: "muted" },
    REQUIRES_LAUNCHER_OR_RUNTIME: { label: "needs a runtime", tone: "warn" },
    AMBIGUOUS_MATCH: { label: "ambiguous", tone: "warn" },
    LAUNCH_REFUSED: { label: "refused", tone: "warn" },
    NOT_TESTED: { label: "not tested", tone: "muted" },
  };

  function gameStatusLabel(status) {
    var row = GAME_STATUS[String(status || "")];
    if (row) return row.label;
    return String(status || "unknown").replace(/_/g, " ").toLowerCase();
  }

  function gameStatusTone(status) {
    var row = GAME_STATUS[String(status || "")];
    return row ? row.tone : "muted";
  }

  // Pure translation of the backend's four booleans into the words the panel
  // shows. Exported so a Node test can assert that "process started" is never
  // rendered as "verified".
  function gameStageText(stages) {
    var s = stages || {};
    return {
      requested: s.launch_requested ? "Launch requested and approved." : "No launch requested.",
      process: s.process_started ? "A process started." : "No process started.",
      window: s.window_detected ? "A window appeared." : "No window was detected.",
      verified: s.launch_verified
        ? "Launch verified against the process and window."
        : "Launch NOT verified.",
    };
  }

  function memoryText(memory) {
    if (!memory) return "";
    if (!memory.available) {
      return "Resource gate: memory could not be read (" + String(memory.reason || "no measurement") +
        "); the check was skipped rather than pretended.";
    }
    var snapshot = memory.snapshot || {};
    var parts = [];
    if (typeof snapshot.free_gb === "number") parts.push(snapshot.free_gb.toFixed(2) + " GB free");
    if (typeof snapshot.used_percent === "number") parts.push(snapshot.used_percent.toFixed(0) + "% used");
    return (memory.ok ? "Resource gate passed" : "Resource gate REFUSED") +
      (parts.length ? ": " + parts.join(", ") : "") +
      (memory.reason ? " — " + String(memory.reason) : "");
  }

  function libraryNotice(message, kind) {
    var box = byId("forgeLibraryNotice");
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

  function setLibraryState(text, tone) {
    var badge = byId("forgeLibraryState");
    if (!badge) return;
    badge.textContent = text;
    badge.dataset.state = tone || "";
  }

  function setLaunchStage(id, text, tone) {
    var node = byId(id);
    if (!node) return;
    node.textContent = text;
    node.dataset.tone = tone || "";
  }

  function renderLaunchStages(stages) {
    var text = gameStageText(stages);
    var s = stages || {};
    setLaunchStage("forgeLaunchStageRequested", text.requested, s.launch_requested ? "good" : "");
    setLaunchStage("forgeLaunchStageProcess", text.process, s.process_started ? "good" : "");
    setLaunchStage("forgeLaunchStageWindow", text.window, s.window_detected ? "good" : "");
    setLaunchStage("forgeLaunchStageVerified", text.verified, s.launch_verified ? "good" : "warn");
  }

  function renderObservation(observation) {
    var box = byId("forgeLibraryObservation");
    if (!box) return;
    if (!observation) {
      box.textContent = "Observation has not been checked.";
      return;
    }
    box.textContent = (observation.available
      ? "Frame capture is available on this host. "
      : "Frame capture is unavailable" + (observation.reason ? ": " + String(observation.reason) : "") + ". ") +
      String(observation.note || "");
  }

  function libraryCounts(payload) {
    var parts = [];
    var counts = payload.counts || {};
    Object.keys(counts).sort().forEach(function (status) {
      if (!counts[status]) return;
      parts.push(counts[status] + " " + gameStatusLabel(status));
    });
    var total = typeof payload.total === "number" ? payload.total : (payload.entries || []).length;
    var shown = typeof payload.matched === "number" ? payload.matched : total;
    var head = shown + " of " + total + " discovered title" + (total === 1 ? "" : "s");
    return head + (parts.length ? " · " + parts.join(" · ") : "") +
      (payload.query ? " · filtered by \"" + payload.query + "\"" : "");
  }

  function libraryRow(row, owned) {
    var artifact = row.artifact || {};
    var item = el("li", "forge-library-row");
    item.dataset.status = String(row.status || "");
    var head = el("div", "forge-library-head");
    head.appendChild(el("strong", "forge-library-title", String(row.title || "unknown")));
    head.appendChild(coachChip(gameStatusLabel(row.status), gameStatusTone(row.status)));
    if (row.tested) head.appendChild(coachChip("launch-tested", "good"));
    item.appendChild(head);
    var evidence = artifact.launch_target
      ? String(artifact.source || "unknown") + " · " + String(artifact.launch_target)
      : (artifact.source ? String(artifact.source) + " · no verified target" : "no verified artifact");
    item.appendChild(el("p", "forge-library-evidence", evidence));
    if (row.reason) item.appendChild(el("p", "forge-library-reason", String(row.reason)));
    var actions = el("div", "button-row");
    var hasActions = false;
    if (row.status === "INSTALLED_AND_LAUNCHABLE") {
      var launch = el("button", "cyber-btn small primary", row.tested ? "Launch Again" : "Preview Launch");
      launch.type = "button";
      launch.dataset.launchGame = String(row.title || "");
      actions.appendChild(launch);
      hasActions = true;
    }
    if (row.status === "ALREADY_RUNNING") {
      var attach = el("button", "cyber-btn small", "Attach");
      attach.type = "button";
      attach.dataset.attachGame = String(row.title || "");
      actions.appendChild(attach);
      hasActions = true;
    }
    if (owned && owned.name === row.title) {
      var close = el("button", "cyber-btn small danger", "Close (test-launched)");
      close.type = "button";
      close.dataset.closeGame = String(owned.launch_id || "");
      actions.appendChild(close);
      hasActions = true;
    }
    if (hasActions) item.appendChild(actions);
    return item;
  }

  function renderLibrary(payload) {
    var list = byId("forgeLibraryList");
    if (!list) return;
    list.innerHTML = "";
    var badge = byId("forgeLibraryBadge");
    if (badge) badge.textContent = String((payload.entries || []).length) + " shown";
    var counts = byId("forgeLibraryCounts");
    if (counts) counts.textContent = libraryCounts(payload);
    var limits = byId("forgeLibraryLimits");
    if (limits) {
      limits.innerHTML = "";
      (payload.limitations || []).forEach(function (line) { limits.appendChild(el("li", "", String(line))); });
      if (!(payload.limitations || []).length) limits.appendChild(el("li", "muted", "No limitations reported."));
    }
    var closeButton = byId("forgeLibraryCloseButton");
    if (closeButton) {
      closeButton.disabled = !payload.owned;
      closeButton.textContent = payload.owned
        ? "Close " + String((payload.owned || {}).name || "test-launched game")
        : "Close Test-Launched Game";
    }
    var rows = payload.entries || [];
    if (!rows.length) {
      list.appendChild(el("li", "muted", payload.query
        ? "No discovered title matches \"" + payload.query + "\"."
        : "No game was discovered on this machine's internal fixed drives."));
      return;
    }
    rows.forEach(function (row) { list.appendChild(libraryRow(row, payload.owned)); });
  }

  function renderGamePlan(plan) {
    var box = byId("forgeLibraryResult");
    if (!box) return;
    box.innerHTML = "";
    setLibraryState(String(plan.status || "planned"), plan.approval && plan.approval.token ? "waiting" : "blocked");
    box.appendChild(el("p", "forge-library-summary", String(plan.summary || "")));
    var steps = plan.steps || [];
    if (steps.length) {
      var list = el("ol", "action-steps");
      steps.forEach(function (step) { list.appendChild(el("li", "", String(step))); });
      box.appendChild(list);
    }
    if (plan.memory) box.appendChild(el("p", "forge-limit-hint", memoryText(plan.memory)));
    if (plan.candidates && plan.candidates.length) {
      var candidates = el("ul", "forge-roadmap-list");
      plan.candidates.forEach(function (row) {
        candidates.appendChild(el("li", "", String(row.title || "unknown") + " — " + gameStatusLabel(row.status)));
      });
      box.appendChild(candidates);
    }
    if (plan.approval && plan.approval.token) {
      var row = el("div", "button-row");
      var approve = el("button", "cyber-btn primary", "Approve & Launch");
      approve.type = "button";
      approve.id = "forgeLaunchApproveButton";
      approve.addEventListener("click", function () { approvePlannedGame(); });
      var cancel = el("button", "cyber-btn", "Cancel");
      cancel.type = "button";
      cancel.addEventListener("click", function () { forge.library.plan = null; renderLibraryIdle(); });
      row.appendChild(approve);
      row.appendChild(cancel);
      box.appendChild(row);
    }
    renderLaunchStages(plan.stages || {});
    renderObservation(plan.observation);
  }

  function renderGameResult(result) {
    var box = byId("forgeLibraryResult");
    if (!box) return;
    box.innerHTML = "";
    setLibraryState(String(result.status || "result"), result.stages && result.stages.launch_verified ? "good" : "warn");
    box.appendChild(el("p", "forge-library-summary", String(result.summary || "")));
    var launch = (result.game || {}).launch || {};
    if (launch.pid) {
      var facts = el("dl", "forge-frame-meta");
      [["Process", String(launch.process_name || "") + " (pid " + String(launch.pid) + ")"],
        ["Started", String(launch.start_time || "not recorded")],
        ["Window", String(launch.window || "none detected")],
        ["Verification", String(launch.verification || "")]].forEach(function (pair) {
        var wrap = document.createElement("div");
        wrap.appendChild(el("dt", "", pair[0]));
        wrap.appendChild(el("dd", "", pair[1]));
        facts.appendChild(wrap);
      });
      box.appendChild(facts);
    }
    if (result.closed) {
      box.appendChild(el("p", "forge-limit-hint", "The test-launched process exited; the ownership claim was released."));
    }
    renderLaunchStages(result.stages || {});
    renderObservation(result.observation);
  }

  function renderLibraryIdle() {
    var box = byId("forgeLibraryResult");
    if (!box) return;
    box.innerHTML = "";
    box.appendChild(el("p", "muted",
      "Choose a launchable game to preview the exact installation, target and resource gate. Nothing starts until you approve the preview."));
    setLibraryState("idle", "");
    renderLaunchStages({});
  }

  function libraryRequestStillCurrent(token) {
    return token === forge.library.token;
  }

  async function loadLibrary() {
    var input = byId("forgeLibrarySearch");
    if (input) forge.library.query = String(input.value || "");
    var token = ++forge.library.token;
    try {
      var payload = await requestJson("/novaforge/library?q=" + encodeURIComponent(forge.library.query));
      if (!libraryRequestStillCurrent(token)) return;
      forge.library.payload = payload;
      if (!payload.available) {
        libraryNotice(String(payload.reason || "The catalog is not available."), "error");
        var list = byId("forgeLibraryList");
        if (list) { list.innerHTML = ""; list.appendChild(el("li", "muted", String(payload.reason || "No catalog."))); }
        return;
      }
      renderLibrary(payload);
    } catch (error) {
      if (!libraryRequestStillCurrent(token)) return;
      libraryNotice("The game catalog could not be read: " + requestErrorMessage(error), "error");
    }
  }

  async function planLibraryGame(name) {
    if (forge.library.busy) return;
    forge.library.busy = true;
    try {
      var plan = await requestJson("/novaforge/library/launch", { command: "launch " + name });
      forge.library.plan = plan;
      renderGamePlan(plan);
      libraryNotice(plan.approval && plan.approval.token
        ? "Preview ready for " + String(plan.target || name) + ". Nothing has started yet."
        : String(plan.summary || "This game cannot be launched."),
        plan.approval && plan.approval.token ? "" : "error");
    } catch (error) {
      libraryNotice("The launch preview was refused: " + requestErrorMessage(error), "error");
    } finally {
      forge.library.busy = false;
    }
  }

  async function approvePlannedGame() {
    var plan = forge.library.plan;
    if (!plan || !plan.approval || !plan.approval.token) return;
    if (forge.library.busy) return;
    forge.library.busy = true;
    setLibraryState("launching", "waiting");
    try {
      var result = await requestJson("/novaforge/library/launch", {
        command: plan.command,
        approval_token: plan.approval.token,
      });
      forge.library.plan = null;
      forge.library.result = result;
      renderGameResult(result);
      libraryNotice(result.summary || "The launch finished.",
        result.stages && result.stages.launch_verified ? "" : "error");
      loadLibrary();
    } catch (error) {
      libraryNotice("The launch did not run: " + requestErrorMessage(error), "error");
    } finally {
      forge.library.busy = false;
    }
  }

  async function closeOwnedGame(launchId) {
    if (forge.library.busy) return;
    forge.library.busy = true;
    try {
      var outcome = await requestJson("/novaforge/library/close",
        launchId ? { launch_id: String(launchId) } : {});
      var box = byId("forgeLibraryResult");
      if (box) {
        box.innerHTML = "";
        box.appendChild(el("p", "forge-library-summary", String(outcome.summary || outcome.reason || "")));
      }
      setLibraryState(String(outcome.status || "closed"), outcome.closed ? "good" : "warn");
      renderLaunchStages({});
      renderObservation(outcome.observation);
      libraryNotice(outcome.closed
        ? "The test-launched process exited; the ownership claim was released."
        : String(outcome.reason || "No owned game was closed."),
        outcome.closed ? "" : "error");
      loadLibrary();
    } catch (error) {
      libraryNotice("The close was refused: " + requestErrorMessage(error), "error");
    } finally {
      forge.library.busy = false;
    }
  }

  async function attachLibraryGame(name) {
    try {
      var outcome = await requestJson("/novaforge/library/attach", { name: String(name || "") });
      var box = byId("forgeLibraryResult");
      if (box) {
        box.innerHTML = "";
        box.appendChild(el("p", "forge-library-summary", String(outcome.summary || outcome.reason || "")));
        var running = outcome.running || ((outcome.entry || {}).running) || {};
        if (running.pid) {
          box.appendChild(el("p", "forge-library-evidence",
            String(running.name || "") + " · pid " + String(running.pid) +
            (running.window ? " · window " + String(running.window) : " · no window") +
            " · owned by the operator, never closed here"));
        }
      }
      setLibraryState(String(outcome.status || "attached"), outcome.attached ? "good" : "warn");
      renderLaunchStages({});
      renderObservation(outcome.observation);
    } catch (error) {
      libraryNotice("The running instance could not be read: " + requestErrorMessage(error), "error");
    }
  }

  function openLibrary() {
    loadLibrary();
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
    if (viewId === "forgeLibraryView") openLibrary();
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

    var librarySearch = byId("forgeLibrarySearch");
    if (librarySearch) {
      var libraryTimer = null;
      librarySearch.addEventListener("input", function () {
        if (libraryTimer) clearTimeout(libraryTimer);
        libraryTimer = setTimeout(loadLibrary, 250);
      });
      librarySearch.addEventListener("keydown", function (event) {
        if (event.key === "Enter") {
          if (libraryTimer) clearTimeout(libraryTimer);
          loadLibrary();
        }
      });
    }
    if (byId("forgeLibraryRefreshButton")) {
      byId("forgeLibraryRefreshButton").addEventListener("click", loadLibrary);
    }
    if (byId("forgeLibraryCloseButton")) {
      byId("forgeLibraryCloseButton").addEventListener("click", function () { closeOwnedGame(""); });
    }
    if (byId("forgeLibraryList")) {
      // One delegation for every row: a row's buttons are rebuilt on each
      // catalog read, so per-row listeners would leak as the list refreshes.
      byId("forgeLibraryList").addEventListener("click", function (event) {
        var target = event.target;
        if (!target || typeof target.closest !== "function") return;
        var launch = target.closest("[data-launch-game]");
        if (launch) { planLibraryGame(launch.dataset.launchGame); return; }
        var attach = target.closest("[data-attach-game]");
        if (attach) { attachLibraryGame(attach.dataset.attachGame); return; }
        var close = target.closest("[data-close-game]");
        if (close) closeOwnedGame(close.dataset.closeGame);
      });
    }

    // NF-15: observation controls. Each posts to the observer's own route; the
    // response is the authoritative state and is rendered as-is.
    if (byId("forgeObservationStart")) {
      byId("forgeObservationStart").addEventListener("click", startObservation);
      byId("forgeObservationPause").addEventListener("click", pauseObservation);
      byId("forgeObservationResume").addEventListener("click", resumeObservation);
      byId("forgeObservationStop").addEventListener("click", stopObservation);
      byId("forgeObservationOnce").addEventListener("click", observeOnce);
      byId("forgeObservationRefresh").addEventListener("click", function () {
        loadObservationStatus();
        loadObservationLadder();
      });
    }

    // NF-16: the state reader is on demand. Nothing here polls a reading; a
    // refresh re-reads the LAST result, and Analyze is the only door to a new
    // one.
    if (byId("forgeStateAnalyzeButton")) {
      byId("forgeStateAnalyzeButton").addEventListener("click", analyzeGameState);
      byId("forgeStateRefreshButton").addEventListener("click", loadGameState);
      byId("forgeStateResetButton").addEventListener("click", resetGameState);
    }

    // NF-17: the objectives card reads what NF-16 last interpreted. Reading
    // posts the same state analysis (one capture, one read, one interpretation)
    // so the two cards never disagree about which frame they describe.
    if (byId("forgeObjectivesReadButton")) {
      byId("forgeObjectivesReadButton").addEventListener("click", readObjectives);
      byId("forgeObjectivesRefreshButton").addEventListener("click", loadObjectives);
      byId("forgeObjectivesResetButton").addEventListener("click", resetObjectives);
    }

    // NF-18: the knowledge card is READ-ONLY. It retrieves what validated
    // readings stored; it never records an observation from a click.
    if (byId("forgeKnowledgeRefreshButton")) {
      byId("forgeKnowledgeRefreshButton").addEventListener("click", function () {
        loadKnowledge();
      });
      byId("forgeKnowledgeGame").addEventListener("change", function () {
        loadKnowledge();
      });
    }

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
    byId("forgeObjectiveKind").addEventListener("change", syncObjectiveFields);
    syncObjectiveFields();
    byId("forgeLoopRunButton").addEventListener("click", function () { runLoopFromPanel(); });
    byId("forgeLoopCancelButton").addEventListener("click", function () { cancelLoopFromPanel(); });
    byId("forgeDescribeFrame").addEventListener("click", function () { describeFrameFromPanel(); });
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
    // NF-19: one bounded input action at a time; the card's buttons are enabled
    // only from the server's own gate decision, never from the session state.
    if (byId("forgeControlRunButton")) {
      byId("forgeControlRunButton").addEventListener("click", function () { sendControlAction(); });
      byId("forgeControlCancelButton").addEventListener("click", function () { cancelControlAction(); });
      byId("forgeControlReleaseButton").addEventListener("click", function () { releaseControlInputs(); });
      byId("forgeControlKind").addEventListener("change", syncControlFields);
      syncControlFields();
    }
    // NF-20: build/approve/execute/cancel/replan. Build and Execute are
    // separate presses on purpose: planning never sends input.
    if (byId("forgePlanBuildButton")) {
      byId("forgePlanBuildButton").addEventListener("click", function () { buildPlan(); });
      byId("forgePlanExecuteButton").addEventListener("click", function () { executePlan(); });
      byId("forgePlanReplanButton").addEventListener("click", function () { replanPlan(); });
      byId("forgePlanCancelButton").addEventListener("click", function () { cancelPlan(); });
    }
    // NF-22: start (with the operator's approval), pause, resume, cancel and one
    // answer per parked plan. Start is the only control that can lead to input
    // being sent, and it is disabled whenever a run already holds the session.
    if (byId("forgeAutonomyStartButton")) {
      byId("forgeAutonomyStartButton").addEventListener("click", function () { startAutonomy(); });
      byId("forgeAutonomyPauseButton").addEventListener("click", function () { pauseAutonomy(); });
      byId("forgeAutonomyResumeButton").addEventListener("click", function () { resumeAutonomy(); });
      byId("forgeAutonomyCancelButton").addEventListener("click", function () { cancelAutonomy(); });
      byId("forgeAutonomyApproveButton").addEventListener("click", function () { answerAutonomy(true); });
      byId("forgeAutonomyRejectButton").addEventListener("click", function () { answerAutonomy(false); });
      byId("forgeAutonomyRefreshButton").addEventListener("click", loadAutonomy);
    }
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
      byId("forgeEvaluationApprove").addEventListener("change", updateEvaluationControls);

      // Controlled mode: the mode, objective and plan are all DIGEST inputs, so
      // any edit drops the previous validation and the previous approval.
      if (byId("forgeEvaluationMode")) {
        byId("forgeEvaluationMode").addEventListener("change", function () {
          setEvaluationMode(byId("forgeEvaluationMode").value);
        });
        byId("forgeEvaluationObjectiveKind").addEventListener("change", function () {
          syncEvaluationObjectiveFields();
          updateEvaluationObjectiveState();
          invalidateEvaluationDigest();
        });
        ["forgeEvaluationObjectiveMode", "forgeEvaluationObjectiveParameter",
          "forgeEvaluationObjectivePath", "forgeEvaluationObjectivePointer",
          "forgeEvaluationObjectiveAge", "forgeEvaluationBudget"].forEach(function (id) {
          var field = byId(id);
          field.addEventListener("input", function () {
            updateEvaluationObjectiveState();
            updateEvaluationPlanSummary();
            invalidateEvaluationDigest();
          });
          field.addEventListener("change", function () {
            updateEvaluationObjectiveState();
            updateEvaluationPlanSummary();
            invalidateEvaluationDigest();
          });
        });
        [["forgeEvaluationPlanAddKey", "key"], ["forgeEvaluationPlanAddHold", "hold"],
          ["forgeEvaluationPlanAddClick", "click"], ["forgeEvaluationPlanAddMove", "move"]
        ].forEach(function (pair) {
          byId(pair[0]).addEventListener("click", function () { addEvaluationPlanAction(pair[1]); });
        });
        renderEvaluationPlan();
        applyEvaluationMode();
      }

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
      if (document.hidden) return;
      if (forge.sessionId && forge.activeId) refreshSession();
      loadObservationStatus();
      loadGameState();
      loadObjectives();
      loadKnowledge();
    });
    byId("forgeFrame").addEventListener("error", function () {
      liveError("The captured frame could not be displayed.");
    });

    loadStatus();
    loadObservationStatus();
    loadObservationLadder();
    loadGameState();
    loadObjectives();
    loadKnowledge();
    loadLoopCapability();
    // NF-19: the card starts honest — no session has been read, so nothing can
    // be sent. It is never left on its "checking…" placeholder.
    renderControl(null);
    // NF-20: same rule for the plan card — no plan was read, so Build stays
    // disabled until a session's own gate says planning is possible.
    renderPlan(null);
    loadMission().then(loadWindows);
    loadHistory();
  }

  window.setupNovaForge = setupNovaForge;
  window.NovaForgeLogic = {
    frameFreshness: frameFreshness,
    frameLiveState: frameLiveState,
    sourceLabel: sourceLabel,
    backendLabel: backendLabel,
    validationLabel: validationLabel,
    targetVisibilityLabel: targetVisibilityLabel,
    targetLine: targetLine,
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
    // NF-17: the objective rows and the progress sentence are pure functions,
    // so a Node test can assert what a panel shows without a DOM.
    // NF-24: the scene sentence is what the card shows, so a Node test asserts
    // exactly it rather than scraping rendered markup.
    stateSceneText: stateSceneText,
    objectiveProgressText: objectiveProgressText,
    objectiveRow: objectiveRow,
    objectiveStatusSummary: objectiveStatusSummary,
    objectiveFacts: objectiveFacts,
    objectiveHint: objectiveHint,
    objectiveEventText: objectiveEventText,
    // NF-18: knowledge rows, facts and the scope sentence are pure functions, so
    // a Node test can assert exactly what the card will show.
    knowledgeRecordRow: knowledgeRecordRow,
    knowledgeFacts: knowledgeFacts,
    knowledgeHint: knowledgeHint,
    knowledgeHistoryRows: knowledgeHistoryRows,
    // Controlled-evaluation decisions are exported so a Node test can drive
    // them directly: guard, recovery, verdict translation and plan validation.
    controlStartGuard: controlStartGuard,
    controlStartRecovery: controlStartRecovery,
    controlVerdictText: controlVerdictText,
    controlVerdictTone: controlVerdictTone,
    controlObjectiveSummary: controlObjectiveSummary,
    controlActionSummary: controlActionSummary,
    controlPlanErrors: controlPlanErrors,
    controlObjectiveErrors: controlObjectiveErrors,
    controlMetricText: controlMetricText,
    controlStageText: controlStageText,
    controlPolicyText: controlPolicyText,
    evaluationPlanSummaryText: evaluationPlanSummaryText,
    evaluationPlanAction: evaluationPlanAction,
    evaluationMode: evaluationMode,
    evaluationModeText: evaluationModeText,
    renderEvaluationPlan: renderEvaluationPlan,
    renderEvaluationRun: renderEvaluationRun,
    evaluationState: function () { return forge.evaluation; },
    applyEvaluationMode: applyEvaluationMode,
    setEvaluationMode: setEvaluationMode,
    syncEvaluationObjectiveFields: syncEvaluationObjectiveFields,
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
    // NF-08: the loop's renderer and status translation are exported so a DOM
    // test asserts on the nodes built for a completed, refused and unavailable
    // loop, not on the source text that builds them.
    loopStatusText: loopStatusText,
    // NF-12: the library's status/stage translation is pure, so a Node test can
    // assert that a started process is never described as a verified launch.
    // NF-15: the observation card's pure decisions are exported so a DOM test
    // can assert that a paused or stale observation never presents its last
    // frame as a live feed, and that no unmeasured figure prints as zero.
    observationControls: observationControls,
    observationLiveState: observationLiveState,
    observationLadderHeadline: observationLadderHeadline,
    observationLadderRows: observationLadderRows,
    observationStageStateText: observationStageStateText,
    observationFreshness: observationFreshness,
    observationReasonText: observationReasonText,
    observationRateText: observationRateText,
    observationLatencyText: observationLatencyText,
    observationDelayText: observationDelayText,
    observationTargetText: observationTargetText,
    observationWindowText: observationWindowText,
    observationFactRows: observationFactRows,
    renderObservationStatus: renderObservationStatus,
    observationState: function () { return forge.observation; },
    // NF-16: the state card's pure translations are exported so a Node test can
    // assert that a missing field reads as absent, never as a zero or a fact.
    stateFactRows: stateFactRows,
    stateHudRows: stateHudRows,
    stateChangeText: stateChangeText,
    stateEntitySummary: stateEntitySummary,
    stateCoverageText: stateCoverageText,
    stateProfileText: stateProfileText,
    stateHintText: stateHintText,
    renderGameState: renderGameState,
    gameStatusLabel: gameStatusLabel,
    gameStatusTone: gameStatusTone,
    gameStageText: gameStageText,
    memoryText: memoryText,
    renderLoop: renderLoop,
    renderTrialHistory: renderTrialHistory,
    refreshTrialHistory: refreshTrialHistory,
    trialOutcomeText: trialOutcomeText,
    loadLoopCapability: loadLoopCapability,
    describeFrameFromPanel: describeFrameFromPanel,
    renderVisionInterpretation: renderVisionInterpretation,
    objectivePayload: objectivePayload,
    // NF-22: the autonomous card's own decisions and its fact list are pure, so
    // a Node test asserts the button states and the labels the operator sees
    // (start only without a run, approval only while parked, never a live badge
    // over simulated evidence) without a browser.
    autonomyView: autonomyView,
    autonomyFactList: autonomyFactList,
    autonomyRun: autonomyRun,
    autonomyBudgets: autonomyBudgets,
    autonomyState: function () { return forge.autonomy; },
    renderAutonomy: renderAutonomy,
    // NF-19: the control card's pure decisions and request builder, so a Node
    // test asserts that a blocked gate disables sending and that a half point
    // is omitted rather than guessed.
    inputControlState: inputControlState,
    planView: planView,
    renderPlan: renderPlan,
    controlPayloadFor: controlPayloadFor,
    controlState: function () { return forge.control; },
    syncObjectiveFields: syncObjectiveFields,
    loopState: function () { return forge.loop; },
  };
})();
