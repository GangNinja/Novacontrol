/* ── Animated Companion (Extra 1) ──────────────────────────────
 *
 * The companion is a procedural SVG face whose expression is a function of
 * what NovaControl is ACTUALLY doing. There is no timer that pretends work is
 * happening: every state below is entered from a real application event —
 *
 *   run() start / success / failure          (render-utils.js, every action)
 *   live explore.progress / command.progress  (the /events/stream channel)
 *   command.completed / explore.completed     (the same channel)
 *   SpeechRecognition onstart / onend         (voice.js, real mic acquisition)
 *   SpeechSynthesisUtterance onstart / onend  (voice.js, real speech output)
 *
 * Ambient motion (breathing, blinking) is presentation only and never changes
 * the state label. When that motion is paused, or the OS asks for reduced
 * motion, the face still reflects real state — it simply stops moving.
 *
 * ── Resource note ────────────────────────────────────────────
 * The companion's visual language (procedural body surface + expressions +
 * eye geometry) follows Bible Strong Avatar Lab, which is AGPL-3.0-only and
 * therefore cannot be linked into this proprietary build. No Avatar Lab code
 * or asset is copied or loaded here. Instead the lab's own EXPORT format is
 * the integration seam: an `.avatar.json` authored and exported in the lab can
 * be imported below, and its body/eye colours and eye geometry drive this
 * renderer. See docs/EXTRA1_COMPANION_ACCEPTANCE_REPORT.md.
 */

const COMPANION_STATE_KEY = "novacontrol.companion.motion";
const COMPANION_AVATAR_KEY = "novacontrol.companion.avatar";

/* The Avatar Lab export schema this renderer understands. Its definition owns
 * a body surface, colours, expressions and animations; we consume the subset
 * we can render faithfully and say so when a field is ignored. */
const COMPANION_AVATAR_SCHEMA = "bible-strong/avatar-definition";

/* ── State table ───────────────────────────────────────────────
 *
 * ONE table: the label the reader sees, the expression the face wears, and the
 * tone the stage paints. Adding a state is a row here, never a branch in three
 * functions.
 */
const COMPANION_STATES = {
  idle: { label: "Idle", expression: "neutral", tone: "idle" },
  listening: { label: "Listening", expression: "listening", tone: "live" },
  thinking: { label: "Thinking", expression: "thinking", tone: "busy" },
  working: { label: "Working", expression: "working", tone: "busy" },
  waiting: { label: "Waiting", expression: "waiting", tone: "busy" },
  responding: { label: "Responding", expression: "speaking", tone: "live" },
  speaking: { label: "Speaking", expression: "speaking", tone: "live" },
  error: { label: "Interrupted", expression: "error", tone: "error" },
};

/* Eye openness multiplier, gaze offset, brow tilt and mouth shape per
 * expression. Numbers are in the SVG's user units (viewBox 0 0 120 120). */
const COMPANION_EXPRESSIONS = {
  neutral: { eyeOpen: 1, gazeX: 0, gazeY: 0, brow: 0, mouth: "rest" },
  listening: { eyeOpen: 1.14, gazeX: 0, gazeY: -1.2, brow: -2.4, mouth: "small" },
  thinking: { eyeOpen: 0.84, gazeX: 2.6, gazeY: -3.2, brow: -3.6, mouth: "flat" },
  working: { eyeOpen: 0.78, gazeX: -2.4, gazeY: 1.6, brow: 1.4, mouth: "focus" },
  waiting: { eyeOpen: 0.92, gazeX: 0, gazeY: -0.6, brow: -1.2, mouth: "small" },
  speaking: { eyeOpen: 1.04, gazeX: 0, gazeY: 0, brow: -1, mouth: "talk" },
  error: { eyeOpen: 0.7, gazeX: 0, gazeY: 1.8, brow: 4.4, mouth: "sad" },
};

/* Geometry the renderer owns. An imported avatar may change the eye scale,
 * spacing and angle within safe bounds; it can never move the face off canvas. */
const COMPANION_GEOMETRY = {
  cx: 60,
  eyeY: 56,
  mouthY: 78,
  eyeBaseRx: 5.4,
  eyeBaseRy: 6.6,
  eyeSpacing: 26,
  baseWidth: 34,
  baseHeight: 36,
};

const COMPANION_DEFAULT_AVATAR = {
  name: "Nova",
  body: "#0b2242",
  eyes: "#00eaff",
  eyeScaleX: 1,
  eyeScaleY: 1,
  eyeSpacing: 1,
  eyeAngle: 0,
  source: "built-in",
};

/* How long the face keeps the "responding" expression after a real success
 * before settling back to idle. This is presentation of an event that already
 * happened — it never starts on its own and never claims new work. */
const COMPANION_RESPOND_DWELL_MS = 2400;

const companionRuntime = {
  mounted: false,
  state: "idle",
  detail: "",
  // How many run() calls this page has in flight. The activity channel is
  // app-wide, so another client's completion must never settle the face while
  // this tab is still working on something.
  activeRuns: 0,
  paused: false,
  avatar: { ...COMPANION_DEFAULT_AVATAR },
  reducedMotion: false,
  rafId: null,
  lastFrame: 0,
  nextBlinkAt: 0,
  blinkUntil: 0,
  breathPhase: 0,
  talkPhase: 0,
  dwellTimer: null,
  bindings: [],
  motionQuery: null,
  onVisibility: null,
  onMotionChange: null,
};

/* ── Motion policy ───────────────────────────────────────────── */

function companionMotionAllowed() {
  if (companionRuntime.paused) return false;
  if (companionRuntime.reducedMotion) return false;
  if (typeof document !== "undefined" && document.hidden) return false;
  return true;
}

function companionStartLoop() {
  if (!companionMotionAllowed()) return;
  if (companionRuntime.rafId !== null) return;
  const step = (now) => {
    companionRuntime.rafId = null;
    companionPaint(now);
    if (companionMotionAllowed()) {
      companionRuntime.rafId = requestAnimationFrame(step);
    }
  };
  companionRuntime.rafId = requestAnimationFrame(step);
}

function companionStopLoop() {
  if (companionRuntime.rafId !== null) {
    cancelAnimationFrame(companionRuntime.rafId);
    companionRuntime.rafId = null;
  }
}

/* ── Painting ────────────────────────────────────────────────── */

function companionEyeGeometry() {
  const geometry = COMPANION_GEOMETRY;
  const avatar = companionRuntime.avatar;
  const rx = geometry.eyeBaseRx * avatar.eyeScaleX;
  const ry = geometry.eyeBaseRy * avatar.eyeScaleY;
  const half = (geometry.eyeSpacing * avatar.eyeSpacing) / 2;
  return { rx, ry, leftX: geometry.cx - half, rightX: geometry.cx + half };
}

function companionMouthPath(kind, openAmount) {
  const y = COMPANION_GEOMETRY.mouthY;
  switch (kind) {
    case "small":
      return `M54 ${y} Q60 ${y + 2.2} 66 ${y}`;
    case "flat":
      return `M53 ${y} L67 ${y}`;
    case "focus":
      return `M52 ${y} Q60 ${y + 3.4} 68 ${y - 0.6}`;
    case "sad":
      return `M53 ${y + 2.6} Q60 ${y - 1.8} 67 ${y + 2.6}`;
    case "talk":
      return `M52 ${y - 0.4} Q60 ${y + Math.max(0.8, openAmount)} 68 ${y - 0.4}`;
    default:
      return `M52 ${y} Q60 ${y + 2.6} 68 ${y}`;
  }
}

function companionSetAttr(id, name, value) {
  const node = byId(id);
  if (node) node.setAttribute(name, value);
}

/* Paint one frame: breathing, gaze, blink and (while speaking) mouth motion.
 * `now` is a millisecond timestamp; the phase terms are wall-clock so the loop
 * can start and stop (pause, hidden tab) without the face jumping. */
function companionPaint(now) {
  const runtime = companionRuntime;
  const stamp = typeof now === "number" ? now : Date.now();
  if (!runtime.mounted) return;

  const expression = COMPANION_EXPRESSIONS[(COMPANION_STATES[runtime.state] || COMPANION_STATES.idle).expression]
    || COMPANION_EXPRESSIONS.neutral;
  const eyes = companionEyeGeometry();
  const breathing = companionMotionAllowed()
    ? 1 + Math.sin(stamp / 1400) * 0.012
    : 1;

  companionSetAttr("companionBody", "transform", `translate(60 62) scale(${breathing.toFixed(4)}) translate(-60 -62)`);

  // Blink: an ambient micro-animation, scheduled only while motion is allowed.
  let openness = expression.eyeOpen;
  if (companionMotionAllowed()) {
    if (!runtime.nextBlinkAt) {
      runtime.nextBlinkAt = stamp + companionNextBlinkDelay();
    } else if (stamp >= runtime.nextBlinkAt) {
      runtime.blinkUntil = stamp + 110;
      runtime.nextBlinkAt = stamp + 110 + companionNextBlinkDelay();
    }
    if (runtime.blinkUntil && stamp < runtime.blinkUntil) openness *= 0.12;
  }

  ["companionEyeLeft", "companionEyeRight"].forEach((id, index) => {
    const cx = index === 0 ? eyes.leftX : eyes.rightX;
    companionSetAttr(id, "cx", (cx + expression.gazeX).toFixed(2));
    companionSetAttr(id, "cy", (COMPANION_GEOMETRY.eyeY + expression.gazeY).toFixed(2));
    companionSetAttr(id, "rx", eyes.rx.toFixed(2));
    companionSetAttr(id, "ry", (eyes.ry * openness).toFixed(2));
    companionSetAttr(
      id,
      "transform",
      `rotate(${companionRuntime.avatar.eyeAngle} ${cx.toFixed(2)} ${(COMPANION_GEOMETRY.eyeY + expression.gazeY).toFixed(2)})`
    );
  });

  companionSetAttr("companionBrowLeft", "transform", `rotate(${(-expression.brow).toFixed(2)} 47 45.5)`);
  companionSetAttr("companionBrowRight", "transform", `rotate(${expression.brow.toFixed(2)} 73 45.5)`);

  let openAmount = 2.6;
  if (expression.mouth === "talk" && companionMotionAllowed()) {
    runtime.talkPhase += 0.55;
    openAmount = 2.6 + Math.abs(Math.sin(runtime.talkPhase)) * 3.6;
  }
  companionSetAttr("companionMouth", "d", companionMouthPath(expression.mouth, openAmount));

  companionSetAttr("companionHead", "fill", runtime.avatar.body);
  companionSetAttr("companionEyeLeft", "fill", runtime.avatar.eyes);
  companionSetAttr("companionEyeRight", "fill", runtime.avatar.eyes);
}

function companionNextBlinkDelay() {
  return 2200 + Math.random() * 3600;
}

/* ── State ───────────────────────────────────────────────────── */

function companionSetState(name, detail) {
  if (!COMPANION_STATES[name]) name = "idle";
  const runtime = companionRuntime;
  const changed = runtime.state !== name;
  runtime.state = name;
  runtime.detail = typeof detail === "string" ? detail : "";
  if (runtime.dwellTimer) {
    clearTimeout(runtime.dwellTimer);
    runtime.dwellTimer = null;
  }
  if (!runtime.mounted) return;

  const row = COMPANION_STATES[name];
  const stage = byId("companionStage");
  if (stage) stage.dataset.state = name;
  const chip = byId("companionState");
  if (chip) {
    chip.textContent = row.label;
    chip.dataset.state = name;
    chip.dataset.tone = row.tone;
  }
  const detailNode = byId("companionDetail");
  if (detailNode) detailNode.textContent = runtime.detail;
  // An expression change repaints immediately even when ambient motion is
  // paused: the state is real, so the face must show it.
  companionPaint(Date.now());
  if (changed) companionStartLoop();
}

function companionReturnToIdle(delayMs) {
  if (companionRuntime.dwellTimer) clearTimeout(companionRuntime.dwellTimer);
  companionRuntime.dwellTimer = setTimeout(() => {
    companionRuntime.dwellTimer = null;
    companionSetState("idle");
  }, Math.max(0, delayMs));
}

/* ── Real application events ───────────────────────────────────
 *
 * These are called by the modules that already own the work. None of them
 * invents a transition the application did not cause.
 */

/* A run() started: chat is a question being thought about, every other action
 * family is the machine doing something. */
function companionOnRunStart(type, detail) {
  companionRuntime.activeRuns += 1;
  const thinking = type === "chat";
  companionSetState(thinking ? "thinking" : "working", detail || "");
}

function companionFinishRun() {
  companionRuntime.activeRuns = Math.max(0, companionRuntime.activeRuns - 1);
  return companionRuntime.activeRuns;
}

/* States that belong to THIS client and therefore must not be overwritten by
 * an app-wide frame from another client. The approval gate matters most: a plan
 * this tab is showing as "waiting" cannot be relabelled by someone else's
 * progress, and a live microphone or utterance is more current than remote work.
 */
function companionLocalStateHolds() {
  return ["waiting", "listening", "speaking"].includes(companionRuntime.state);
}

/* One live step from the SSE activity channel, for this run or any other
 * client's — the channel is app-wide. */
function companionOnProgress(data) {
  if (companionLocalStateHolds()) return;
  const step = data && (data.detail || data.step || data.message);
  companionSetState("working", typeof step === "string" ? step : "");
}

/* A run finished successfully.
 *
 * A plan that came back NEEDING approval is not "done": the companion holds
 * the waiting state, with no dwell, until the user acts or new work starts.
 * That is the real gate (waiting_for_approval / waiting_for_phone_bridge), not
 * a guess from the request finishing. */
function companionOnRunResult(type, options = {}) {
  if (companionFinishRun() > 0) return; // another local run is still working
  if (options && options.waiting) {
    companionSetState("waiting", "Waiting for your approval");
    return;
  }
  companionSetState("responding", type === "chat" ? "" : `${type} complete`);
  companionReturnToIdle(COMPANION_RESPOND_DWELL_MS);
}

/* A run failed. The face holds the failure until the next real activity; an
 * error that silently fades is how a broken run looks like a healthy one. */
function companionOnRunError(message) {
  companionFinishRun();
  companionSetState("error", typeof message === "string" ? message : "");
}

function companionOnCompleted(family) {
  // Another client's job finishing does not mean THIS tab is done: while a
  // local run is in flight the companion keeps showing that work, and the
  // local run reports its own completion when it really ends.
  if (companionRuntime.activeRuns > 0) return;
  // Nor can a remote success clear a local state: the approval gate stays up
  // until this tab acts, a live microphone/utterance is still the current
  // state, and this tab's failure is not repaired by another tab's completion.
  if (companionLocalStateHolds() || companionRuntime.state === "error") return;
  companionSetState("responding", `${family} complete`);
  companionReturnToIdle(COMPANION_RESPOND_DWELL_MS);
}

function companionOnListening() {
  companionSetState("listening");
}

function companionOnListeningEnd() {
  if (companionRuntime.state !== "listening") return;
  // Dictation can end while a run is still in flight; returning to idle would
  // claim the work stopped when it did not.
  if (companionRuntime.activeRuns > 0) {
    companionSetState("working", "Working");
    return;
  }
  companionSetState("idle");
}

function companionOnSpeechStart() {
  companionSetState("speaking");
}

function companionOnSpeechEnd() {
  if (companionRuntime.state !== "speaking") return;
  if (companionRuntime.activeRuns > 0) {
    companionSetState("working", "Working");
    return;
  }
  companionSetState("idle");
}

/* ── Avatar Lab import ─────────────────────────────────────────
 *
 * The Avatar Lab exports a portable definition. We accept its declared schema,
 * never guess at an arbitrary JSON file, and report exactly which fields drove
 * the render and which were ignored — an import that silently drops half a
 * definition is a lie about what the companion looks like.
 */

function companionHex(value) {
  return typeof value === "string" && /^#[0-9a-f]{6}$/i.test(value.trim()) ? value.trim() : "";
}

function companionNumber(value, min, max) {
  const number = typeof value === "number" ? value : Number.NaN;
  if (!Number.isFinite(number)) return null;
  if (number < min || number > max) return Number.isFinite(number) ? Math.min(max, Math.max(min, number)) : null;
  return number;
}

/* Build the render-ready avatar from an Avatar Lab definition. Pure: no DOM
 * access, so it is the one thing a test can drive directly. */
function companionAvatarFromDefinition(definition) {
  if (!definition || typeof definition !== "object" || Array.isArray(definition)) {
    return { ok: false, reason: "the file is not a JSON object." };
  }
  if (definition.schema !== COMPANION_AVATAR_SCHEMA) {
    return {
      ok: false,
      reason: `expected the Avatar Lab schema "${COMPANION_AVATAR_SCHEMA}", found ${JSON.stringify(definition.schema || "")}.`,
    };
  }
  if (definition.schemaVersion !== undefined && definition.schemaVersion !== 1) {
    return { ok: false, reason: `schema version ${String(definition.schemaVersion)} is not supported (expected 1).` };
  }

  const colors = definition.colors && typeof definition.colors === "object" ? definition.colors : {};
  const body = companionHex(colors.body);
  const eyes = companionHex(colors.eyes);
  if (colors.body !== undefined && !body) {
    return { ok: false, reason: "colors.body is not a #rrggbb colour." };
  }
  if (colors.eyes !== undefined && !eyes) {
    return { ok: false, reason: "colors.eyes is not a #rrggbb colour." };
  }

  const neutral = definition.expressions && definition.expressions.neutral;
  const eyeDef = neutral && neutral.eyes && typeof neutral.eyes === "object" ? neutral.eyes : {};
  const left = eyeDef.left && typeof eyeDef.left === "object" ? eyeDef.left : {};
  const right = eyeDef.right && typeof eyeDef.right === "object" ? eyeDef.right : {};

  const ignored = [];
  const geometry = COMPANION_GEOMETRY;
  const width = companionNumber(left.width ?? right.width, 0.05, 40);
  const height = companionNumber(left.height ?? right.height, 0.05, 40);
  const spacing = companionNumber(eyeDef.spacing, 1, 80);
  // The lab's eye units are its own scale, so an absolute comparison with this
  // viewBox is meaningless. Normalise against the eye's size RELATIVE to the
  // gap it sits in: that ratio is unit-free, so a wide-set or narrow-set avatar
  // keeps its proportions here without guessing a conversion factor.
  const baseRatioX = geometry.eyeBaseRx / geometry.eyeSpacing;
  const baseRatioY = geometry.eyeBaseRy / geometry.eyeSpacing;
  const clampScale = (value) => Number(Math.min(1.45, Math.max(0.65, value)).toFixed(3));
  const eyeScaleX = width === null || spacing === null
    ? 1
    : clampScale((width / spacing) / baseRatioX);
  const eyeScaleY = height === null || spacing === null
    ? 1
    : clampScale((height / spacing) / baseRatioY);
  const spacingScale = spacing === null ? 1 : Math.min(1.6, Math.max(0.7, spacing / geometry.eyeSpacing));
  const angle = companionNumber(left.angle ?? right.angle, -45, 45) || 0;

  if (Array.isArray(definition.animations) || (definition.animations && Object.keys(definition.animations).length)) {
    ignored.push("animations");
  }
  if (definition.body && definition.body.nodes && definition.body.nodes.length) {
    ignored.push("body.nodes");
  }

  const name = typeof definition.name === "string" && definition.name.trim() ? definition.name.trim().slice(0, 32) : "";

  return {
    ok: true,
    avatar: {
      name: name || COMPANION_DEFAULT_AVATAR.name,
      body: body || COMPANION_DEFAULT_AVATAR.body,
      eyes: eyes || COMPANION_DEFAULT_AVATAR.eyes,
      eyeScaleX,
      eyeScaleY,
      eyeSpacing: Number(spacingScale.toFixed(3)),
      eyeAngle: angle,
      source: "avatar-lab",
    },
    ignored,
  };
}

/* Apply an imported definition to the live companion. */
function companionApplyAvatarDefinition(definition, options = {}) {
  const result = companionAvatarFromDefinition(definition);
  if (!result.ok) return result;
  companionRuntime.avatar = { ...result.avatar };
  companionApplyAvatarName();
  companionPaint(Date.now());
  if (options.persist !== false) {
    try {
      localStorage.setItem(COMPANION_AVATAR_KEY, JSON.stringify(definition));
    } catch (_) { /* storage unavailable: the import still applies this session */ }
  }
  return { ok: true, name: companionRuntime.avatar.name, ignored: result.ignored, source: "avatar-lab" };
}

function companionApplyAvatarName() {
  const node = byId("companionName");
  if (node) node.textContent = companionRuntime.avatar.name;
  const stage = byId("companionStage");
  if (stage) stage.dataset.avatarSource = companionRuntime.avatar.source;
}

function companionRestoreAvatar() {
  let raw = "";
  try { raw = localStorage.getItem(COMPANION_AVATAR_KEY) || ""; } catch (_) { return; }
  if (!raw) return;
  try {
    const definition = JSON.parse(raw);
    const result = companionAvatarFromDefinition(definition);
    if (result.ok) {
      companionRuntime.avatar = { ...result.avatar };
      return;
    }
    localStorage.removeItem(COMPANION_AVATAR_KEY); // a bad stored blob must not stick
  } catch (_) {
    try { localStorage.removeItem(COMPANION_AVATAR_KEY); } catch (_) { /* nothing to clear */ }
  }
}

async function companionImportFile(file) {
  if (!file) return;
  let text = "";
  try {
    text = await file.text();
  } catch (error) {
    showToast("Avatar not imported — the file could not be read.");
    return;
  }
  let definition = null;
  try {
    definition = JSON.parse(text);
  } catch (error) {
    showToast("Avatar not imported — the file is not valid JSON.");
    return;
  }
  const result = companionApplyAvatarDefinition(definition);
  if (!result.ok) {
    showToast(`Avatar not imported — ${result.reason}`);
    return;
  }
  const ignored = result.ignored && result.ignored.length ? ` (kept built-in ${result.ignored.join(", ")})` : "";
  showToast(`Companion now wearing ${result.name} from the Avatar Lab${ignored}.`);
}

/* ── Motion controls ─────────────────────────────────────────── */

function companionSetPaused(paused, options = {}) {
  companionRuntime.paused = Boolean(paused);
  const button = byId("companionPauseButton");
  if (button) {
    button.setAttribute("aria-pressed", companionRuntime.paused ? "true" : "false");
    button.textContent = companionRuntime.paused ? "Motion off" : "Motion on";
    button.title = companionRuntime.paused
      ? "Ambient motion is off. The face still follows real NovaControl activity."
      : "Pause the companion's ambient motion (breathing, blinking).";
  }
  const stage = byId("companionStage");
  if (stage) stage.dataset.motion = companionRuntime.paused ? "paused" : "on";
  if (options.persist !== false) {
    try {
      localStorage.setItem(COMPANION_STATE_KEY, companionRuntime.paused ? "paused" : "on");
    } catch (_) { /* storage unavailable */ }
  }
  if (companionRuntime.paused) {
    companionStopLoop();
  } else {
    companionStartLoop();
  }
  companionPaint(Date.now());
  return companionRuntime.paused;
}

function companionTogglePaused() {
  return companionSetPaused(!companionRuntime.paused);
}

/* ── Mount ───────────────────────────────────────────────────── */

function companionBind(node, event, handler) {
  if (!node) return;
  node.addEventListener(event, handler);
  companionRuntime.bindings.push({ node, event, handler });
}

function companionUnmount() {
  companionRuntime.bindings.forEach(({ node, event, handler }) => {
    node.removeEventListener(event, handler);
  });
  companionRuntime.bindings = [];
  companionStopLoop();
  if (companionRuntime.dwellTimer) {
    clearTimeout(companionRuntime.dwellTimer);
    companionRuntime.dwellTimer = null;
  }
  companionRuntime.activeRuns = 0;
  if (companionRuntime.onVisibility) {
    document.removeEventListener("visibilitychange", companionRuntime.onVisibility);
    companionRuntime.onVisibility = null;
  }
  if (companionRuntime.motionQuery && companionRuntime.onMotionChange) {
    const query = companionRuntime.motionQuery;
    if (typeof query.removeEventListener === "function") {
      query.removeEventListener("change", companionRuntime.onMotionChange);
    } else if (typeof query.removeListener === "function") {
      query.removeListener(companionRuntime.onMotionChange);
    }
    companionRuntime.onMotionChange = null;
  }
  companionRuntime.mounted = false;
}

function companionOpenChat() {
  const nav = document.querySelector('.nav-item[data-panel="chatPanel"]');
  if (nav) nav.click();
  const input = byId("chatInput");
  if (input && typeof input.focus === "function") input.focus();
}

function setupCompanion() {
  const stage = byId("companionStage");
  if (!stage) return null;
  // Mounting twice (a script re-run, or a future hot reload) must not leave two
  // loops and two sets of listeners behind.
  if (companionRuntime.mounted) companionUnmount();

  companionRuntime.motionQuery = window.matchMedia
    ? window.matchMedia("(prefers-reduced-motion: reduce)")
    : null;
  companionRuntime.reducedMotion = Boolean(companionRuntime.motionQuery && companionRuntime.motionQuery.matches);

  let storedMotion = "";
  try { storedMotion = localStorage.getItem(COMPANION_STATE_KEY) || ""; } catch (_) { /* storage unavailable */ }
  companionRuntime.paused = storedMotion === "paused";
  companionRestoreAvatar();
  companionRuntime.mounted = true;
  companionApplyAvatarName();
  companionSetState("idle");

  companionBind(byId("companionPauseButton"), "click", () => {
    const paused = companionTogglePaused();
    showToast(paused ? "Companion motion paused — state still follows real activity" : "Companion motion resumed");
  });

  companionBind(byId("companionSpeakButton"), "click", () => {
    if (typeof speakLast !== "function") {
      showToast("Speech output is not available in this browser.");
      return;
    }
    if (!state.lastText) {
      showToast("Nothing to read yet — send a message first.");
      return;
    }
    speakLast();
  });

  const fileInput = byId("companionAvatarInput");
  companionBind(byId("companionAvatarButton"), "click", () => {
    if (fileInput && typeof fileInput.click === "function") fileInput.click();
  });
  companionBind(fileInput, "change", () => {
    const file = fileInput && fileInput.files ? fileInput.files[0] : null;
    if (file) companionImportFile(file);
    if (fileInput) fileInput.value = "";
  });

  companionBind(byId("companionFace"), "click", () => companionOpenChat());
  companionBind(byId("companionFace"), "keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      companionOpenChat();
    }
  });

  // The OS can flip reduced motion mid-session; the face must follow it live.
  if (companionRuntime.motionQuery) {
    companionRuntime.onMotionChange = (event) => {
      companionRuntime.reducedMotion = Boolean(event.matches);
      if (companionRuntime.reducedMotion) companionStopLoop();
      else companionStartLoop();
      companionPaint(Date.now());
    };
    if (typeof companionRuntime.motionQuery.addEventListener === "function") {
      companionRuntime.motionQuery.addEventListener("change", companionRuntime.onMotionChange);
    } else if (typeof companionRuntime.motionQuery.addListener === "function") {
      companionRuntime.motionQuery.addListener(companionRuntime.onMotionChange);
    }
  }

  // A hidden tab renders nothing; stopping the loop there is what keeps the
  // companion from burning a frame budget in the background.
  companionRuntime.onVisibility = () => {
    if (document.hidden) companionStopLoop();
    else companionStartLoop();
  };
  document.addEventListener("visibilitychange", companionRuntime.onVisibility);

  companionSetPaused(companionRuntime.paused, { persist: false });
  companionStartLoop();
  return stage;
}

/* ONE global door for the modules that already own the real work. */
const NovaCompanion = {
  setState: companionSetState,
  onRunStart: companionOnRunStart,
  onProgress: companionOnProgress,
  onRunResult: companionOnRunResult,
  onRunError: companionOnRunError,
  onCompleted: companionOnCompleted,
  onListening: companionOnListening,
  onListeningEnd: companionOnListeningEnd,
  onSpeechStart: companionOnSpeechStart,
  onSpeechEnd: companionOnSpeechEnd,
  applyAvatarDefinition: companionApplyAvatarDefinition,
  avatarFromDefinition: companionAvatarFromDefinition,
  setPaused: companionSetPaused,
  togglePaused: companionTogglePaused,
  isPaused: () => companionRuntime.paused,
  state: () => companionRuntime.state,
  detail: () => companionRuntime.detail,
  avatar: () => ({ ...companionRuntime.avatar }),
  avatarSchema: COMPANION_AVATAR_SCHEMA,
  unmount: companionUnmount,
  mount: setupCompanion,
  states: COMPANION_STATES,
};

// A top-level `const` is a global lexical binding, not a window property; the
// modules that call into the companion read `window.NovaCompanion` so a script
// that failed to load degrades to "no companion updates" instead of throwing
// a ReferenceError inside run().
if (typeof window !== "undefined") window.NovaCompanion = NovaCompanion;
