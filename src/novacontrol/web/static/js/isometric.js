/* ── Extra 2 — Atrium: an interactive isometric workspace map ────────────
 *
 * The Atrium is a real navigation surface, not an illustration. Every object
 * is a button that opens the workspace it represents through the SAME nav
 * element the sidebar uses, so panel switching, persistence and scroll reset
 * all stay in the one existing implementation.
 *
 * Live badges are read from the application itself:
 *   /status            runtime, brain, phone bridge, vision, modules, tasks
 *   /novaforge/status  mission readiness and the active session
 * A failed read is reported as unavailable; nothing is filled in with a
 * plausible-looking default. Objects without a metered state say what they
 * open and are marked non-live (`data-live="false"`), never given invented
 * numbers.
 *
 * The figures are Hairline (MIT © Lucas Marques), vendored under
 * static/vendor/hairline and imported LAZILY on the first Atrium visit, so the
 * 214 KB figure engine is not part of the initial page load. If that import
 * fails the map still renders as buttons and still navigates — the failure is
 * stated, not hidden.
 */

const ISO_STAGE_W = 1200;
const ISO_STAGE_H = 760;
const ISO_POLL_MS = 20000;
const ISO_FETCH_TIMEOUT_MS = 8000;
const HAIRLINE_MODULE_URL = "/static/vendor/hairline/index.js";
const ISO_FIGURE_OPTIONS = { theme: "dark", intensity: 0.62, play: false };

/* ONE table: the real workspace each object opens, the Hairline figure that
 * draws it, and its position on the 1200×760 stage. `live` is true only when a
 * badge below has a real application field behind it. */
const ISO_OBJECTS = [
  { key: "core", label: "Command Center", figure: "hub", target: "homePanel", x: 50, y: 50, blurb: "runtime", live: true },
  { key: "chat", label: "Chat", figure: "query", target: "chatPanel", x: 22, y: 40, blurb: "brain", live: true },
  { key: "explore", label: "Explore", figure: "dish", target: "explorePanel", x: 50, y: 18, blurb: "research desk", live: false },
  { key: "jarvis", label: "J.A.R.V.I.S", figure: "phone", target: "jarvisPanel", x: 78, y: 40, blurb: "devices", live: true },
  { key: "vision", label: "Vision", figure: "loupe", target: "visionPanel", x: 50, y: 80, blurb: "screen reading", live: true },
  { key: "novaforge", label: "NovaForge", figure: "terrain", target: "novaforgePanel", x: 7, y: 20, blurb: "missions", live: true },
  { key: "build", label: "Build", figure: "laptop", target: "buildPanel", x: 7, y: 80, blurb: "code", live: true },
  { key: "learn", label: "Learn", figure: "drawer", target: "learnPanel", x: 28, y: 86, blurb: "knowledge & training", live: false },
  { key: "system", label: "System", figure: "cabinet", target: "systemPanel", x: 72, y: 86, blurb: "modules & tasks", live: true },
  { key: "settings", label: "Settings", figure: "vault", target: "settingsPanel", x: 93, y: 80, blurb: "preferences", live: false },
  { key: "cli", label: "CLI", figure: "terminal", target: "cliPanel", x: 93, y: 20, blurb: "raw responses", live: false },
];

const isoRuntime = {
  panel: null,
  active: false,
  built: false,
  bound: false,
  poll: null,
  refreshing: false,
  artState: "idle", // idle | loading | ready | failed
  artPromise: null,
  artRetry: 0,
  handles: [],
};

/* ── Small, defensive readers ───────────────────────────────────────────
 * A status surface must never throw because one optional subsystem is absent,
 * and it must never turn "not reported" into a zero. */

function isoApp(status) {
  return status && typeof status === "object" && status.app && typeof status.app === "object" ? status.app : null;
}

function isoText(value, fallback) {
  return typeof value === "string" && value.trim() ? value.trim() : fallback;
}

function isoCount(value) {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function isoHumanize(value) {
  return isoText(value, "unknown").replace(/_/g, " ");
}

function isoErrorText(error) {
  if (!error) return "unknown error";
  if (error.name === "AbortError") return "request timed out";
  return String(error.message || error);
}

function isoUnavailable(text) {
  return { text: text || "status unavailable", tone: "warn", live: true };
}

/* One badge per object, derived only from fields the application reported.
 * This is pure so the phase tests can drive it with real payload shapes. */
function isoBadge(object, status, novaforge, options = {}) {
  if (options.error) {
    return object.live
      ? { text: "status unavailable", tone: "error", live: true }
      : { text: object.blurb, tone: "idle", live: false };
  }
  const app = isoApp(status);
  switch (object.key) {
    case "core": {
      if (!app || typeof app.runtime_started !== "boolean") return isoUnavailable("runtime state unavailable");
      const modules = Array.isArray(app.modules) ? app.modules.length : null;
      return {
        text: app.runtime_started
          ? `runtime online${modules === null ? "" : ` · ${modules} modules`}`
          : "runtime stopped",
        tone: app.runtime_started ? "ok" : "warn",
        live: true,
      };
    }
    case "chat": {
      if (!app || !app.brain || typeof app.brain !== "object") return isoUnavailable("brain unavailable");
      const brain = app.brain;
      const mode = isoText(brain.effective_mode, isoText(brain.mode, "unknown"));
      const configured = brain.model_configured === true;
      return {
        text: configured
          ? `${mode} brain`
          : mode === "scratch"
            ? "scratch brain · no model"
            : `${mode} brain · no model`,
        tone: configured || mode === "scratch" ? "ok" : "warn",
        live: true,
      };
    }
    case "jarvis": {
      if (!app || !app.phone_bridge || typeof app.phone_bridge !== "object") return isoUnavailable("bridge unavailable");
      const bridge = app.phone_bridge;
      const devices = Array.isArray(bridge.devices) ? bridge.devices.length : null;
      let text;
      if (devices !== null && devices > 0) {
        text = `${isoHumanize(bridge.state)} · ${devices} device${devices === 1 ? "" : "s"}`;
      } else if (bridge.state === "no_device") {
        text = "no device paired";
      } else {
        text = isoHumanize(bridge.state);
      }
      return { text, tone: bridge.available === true ? "ok" : "warn", live: true };
    }
    case "vision": {
      if (!app || !app.vision_pipeline || typeof app.vision_pipeline !== "object") return isoUnavailable("vision unavailable");
      const pipeline = app.vision_pipeline;
      const provider = pipeline.provider || {};
      const ocr = pipeline.ocr || {};
      if (provider.available === true) {
        return { text: `vision · ${isoText(provider.name, "provider")}`, tone: "ok", live: true };
      }
      if (ocr.available === true) return { text: "OCR only", tone: "warn", live: true };
      return { text: "vision unavailable", tone: "warn", live: true };
    }
    case "novaforge": {
      if (options.novaforgeError) return { text: "status unavailable", tone: "error", live: true };
      if (!novaforge || !novaforge.status || typeof novaforge.status !== "object") {
        return isoUnavailable("status unavailable");
      }
      const forge = novaforge.status;
      if (isoText(forge.active_session, "")) return { text: "mission running", tone: "ok", live: true };
      if (forge.capture_supported === true) return { text: "ready · capture", tone: "ok", live: true };
      if (forge.capture_supported === false) return { text: "capture unavailable", tone: "warn", live: true };
      return isoUnavailable("status unavailable");
    }
    case "build": {
      if (!app || !app.brain || typeof app.brain !== "object") return isoUnavailable("model state unavailable");
      const brain = app.brain;
      if (brain.model_configured === true) {
        return {
          text: `coding model · ${isoText(brain.model, isoText(brain.provider, "model"))}`,
          tone: "ok",
          live: true,
        };
      }
      return { text: "scaffold only", tone: "warn", live: true };
    }
    case "system": {
      if (!app || !Array.isArray(app.modules)) return isoUnavailable("state unavailable");
      const tasks = isoCount(app.tracked_tasks);
      return {
        text: `${app.modules.length} modules${tasks === null ? "" : ` · ${tasks} tasks`}`,
        tone: "ok",
        live: true,
      };
    }
    default:
      // No live field backs this object, so it states what it opens.
      return { text: object.blurb, tone: "idle", live: false };
  }
}

/* ── Scene ───────────────────────────────────────────────────────────── */

function isoBuildScene() {
  const scene = document.getElementById("isoScene");
  if (!scene) return false;
  if (scene.dataset.isoBuilt === "true") return true;
  scene.dataset.isoBuilt = "true";

  const ns = "http://www.w3.org/2000/svg";
  const links = document.createElementNS(ns, "svg");
  links.setAttribute("class", "iso-links");
  links.setAttribute("viewBox", `0 0 ${ISO_STAGE_W} ${ISO_STAGE_H}`);
  links.setAttribute("preserveAspectRatio", "none");
  links.setAttribute("aria-hidden", "true");
  scene.appendChild(links);

  const core = ISO_OBJECTS[0];
  ISO_OBJECTS.forEach((object) => {
    if (object.key !== core.key) {
      const link = document.createElementNS(ns, "line");
      link.setAttribute("class", "iso-link");
      link.setAttribute("x1", String(core.x * 12));
      link.setAttribute("y1", String(core.y * 7.6));
      link.setAttribute("x2", String(object.x * 12));
      link.setAttribute("y2", String(object.y * 7.6));
      links.appendChild(link);
    }

    const button = document.createElement("button");
    button.type = "button";
    button.className = object.key === "core" ? "iso-object iso-object-core" : "iso-object";
    button.dataset.isoKey = object.key;
    button.dataset.isoTarget = object.target;
    button.style.setProperty("--iso-x", `${object.x}%`);
    button.style.setProperty("--iso-y", `${object.y}%`);
    button.setAttribute("aria-label", `Open ${object.label}`);
    button.setAttribute("aria-describedby", `isoState-${object.key}`);
    button.title = `${object.label} — ${object.blurb}`;

    const figure = document.createElement("span");
    figure.className = "iso-figure";
    figure.dataset.isoFigure = object.figure;
    figure.dataset.isoOwner = object.key;
    figure.setAttribute("aria-hidden", "true");

    const label = document.createElement("span");
    label.className = "iso-label";
    label.textContent = object.label;

    const state = document.createElement("span");
    state.className = "iso-state";
    state.id = `isoState-${object.key}`;
    state.dataset.tone = "idle";
    state.dataset.live = object.live ? "true" : "false";
    state.textContent = object.blurb;

    button.appendChild(figure);
    button.appendChild(label);
    button.appendChild(state);
    button.addEventListener("click", () => isoNavigate(object.target));
    scene.appendChild(button);
  });
  isoRuntime.built = true;
  return true;
}

function isoNavFor(target) {
  return document.querySelector(`.nav-item[data-panel="${target}"]`);
}

/* Every object's action is the SAME nav click the sidebar performs. */
function isoNavigate(target) {
  const nav = isoNavFor(target);
  if (!nav) {
    if (typeof showToast === "function") {
      showToast("That workspace is not available in this build.");
    }
    return false;
  }
  nav.click();
  return true;
}

function isoStatusNode(key) {
  return document.getElementById(`isoState-${key}`);
}

function isoRenderStatus(status, novaforge, options = {}) {
  ISO_OBJECTS.forEach((object) => {
    const node = isoStatusNode(object.key);
    if (!node) return;
    const badge = isoBadge(object, status, novaforge, options);
    node.textContent = badge.text;
    node.dataset.tone = badge.tone;
    node.dataset.live = badge.live ? "true" : "false";
  });
}

function isoSetSource(text, tone) {
  const source = document.getElementById("isoStatusSource");
  if (!source) return;
  source.textContent = text;
  source.className = tone === "error" ? "pill error" : tone === "warn" ? "pill warn" : tone === "ok" ? "pill ok" : "pill";
}

function isoRenderStatusError(error) {
  isoRenderStatus(null, null, { error: isoErrorText(error) });
  isoSetSource("status unavailable — navigation still works", "error");
}

/* ── Live status ─────────────────────────────────────────────────────── */

async function isoFetchJson(path) {
  const controller = typeof AbortController !== "undefined" ? new AbortController() : null;
  const timer = controller ? setTimeout(() => controller.abort(), ISO_FETCH_TIMEOUT_MS) : null;
  try {
    const tokenInput = document.getElementById("tokenInput");
    const token = tokenInput && tokenInput.value ? tokenInput.value.trim() : "";
    const headers = { Accept: "application/json" };
    if (token) headers.Authorization = `Bearer ${token}`;
    const response = await fetch(path, { headers, signal: controller ? controller.signal : undefined });
    if (!response.ok) throw new Error(`${path} returned HTTP ${response.status}`);
    return await response.json();
  } finally {
    if (timer) clearTimeout(timer);
  }
}

async function isoRefresh() {
  if (isoRuntime.refreshing) return;
  // A hidden tab needs no status: the poll is paused until it is visible again.
  if (typeof document !== "undefined" && document.hidden) return;
  isoRuntime.refreshing = true;
  isoSetSource("reading status…", "loading");
  try {
    const status = await isoFetchJson("/status");
    let novaforge = null;
    let novaforgeError = "";
    try {
      novaforge = await isoFetchJson("/novaforge/status");
    } catch (error) {
      novaforgeError = isoErrorText(error);
    }
    isoRenderStatus(status, novaforge, { novaforgeError });
    isoSetSource(
      `live · ${new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}`,
      novaforgeError ? "warn" : "ok"
    );
  } catch (error) {
    isoRenderStatusError(error);
  } finally {
    isoRuntime.refreshing = false;
  }
}

/* ── Hairline figures (lazy) ─────────────────────────────────────────── */

function isoLoadHairline() {
  if (!isoRuntime.artPromise) {
    // A failed module import is cached by URL in the browser's module map, so
    // a retry after the asset is restored needs a new URL. The query is a
    // cache-buster only; static serving ignores it.
    const url = isoRuntime.artRetry
      ? `${HAIRLINE_MODULE_URL}?retry=${isoRuntime.artRetry}`
      : HAIRLINE_MODULE_URL;
    isoRuntime.artPromise = import(url).catch((error) => {
      isoRuntime.artPromise = null;
      isoRuntime.artRetry += 1;
      throw error;
    });
  }
  return isoRuntime.artPromise;
}

function isoSetArtStatus(text) {
  const node = document.getElementById("isoArtStatus");
  if (node) node.textContent = text;
}

/* Mount one figure per object. Public so the phase tests can inject a fake
 * module and pin the seam without a browser. */
function isoMountArt(module) {
  if (!module) return 0;
  if (isoRuntime.handles.length) return isoRuntime.handles.length;
  let mounted = 0;
  ISO_OBJECTS.forEach((object) => {
    const container = document.querySelector(`[data-iso-owner="${object.key}"]`);
    const factory = module[object.figure];
    if (!container || typeof factory !== "function") return;
    try {
      const handle = factory(container, { ...ISO_FIGURE_OPTIONS, label: `${object.label} isometric figure` });
      isoRuntime.handles.push({ owner: object.key, handle });
      container.dataset.art = "mounted";
      mounted += 1;
    } catch (error) {
      container.dataset.art = "failed";
    }
  });
  isoRuntime.artState = mounted ? "ready" : "failed";
  isoSetArtStatus(
    mounted === ISO_OBJECTS.length
      ? "figures live"
      : mounted
        ? `${mounted} of ${ISO_OBJECTS.length} figures live`
        : "figures unavailable — the map still navigates"
  );
  return mounted;
}

function isoUnmountArt() {
  const count = isoRuntime.handles.length;
  isoRuntime.handles.forEach(({ handle }) => {
    try { handle.destroy(); } catch (_) { /* a figure that cannot clean itself must not block leaving */ }
  });
  isoRuntime.handles = [];
  document.querySelectorAll("[data-iso-owner]").forEach((node) => {
    if (node.dataset) delete node.dataset.art;
  });
  isoRuntime.artState = "idle";
  return count;
}

async function isoEnsureArt() {
  if (isoRuntime.artState === "loading" || isoRuntime.artState === "ready") return;
  isoRuntime.artState = "loading";
  isoSetArtStatus("loading figures…");
  try {
    const module = await isoLoadHairline();
    if (!isoRuntime.active) {
      isoRuntime.artState = "idle";
      return;
    }
    isoMountArt(module);
  } catch (error) {
    isoRuntime.artState = "failed";
    isoSetArtStatus("figures unavailable — the map still navigates");
  }
}

/* ── Activation ──────────────────────────────────────────────────────── */

function isoActivate() {
  if (!isoRuntime.panel) return false;
  if (isoRuntime.active) return true;
  isoRuntime.active = true;
  isoBuildScene();
  if (isoRuntime.poll) clearInterval(isoRuntime.poll);
  isoRuntime.poll = setInterval(isoRefresh, ISO_POLL_MS);
  isoRefresh();
  isoEnsureArt();
  return true;
}

function isoDeactivate() {
  if (!isoRuntime.active) return false;
  isoRuntime.active = false;
  if (isoRuntime.poll) {
    clearInterval(isoRuntime.poll);
    isoRuntime.poll = null;
  }
  isoUnmountArt();
  return true;
}

function isoOnDocumentClick(event) {
  const target = event && event.target;
  const nav = target && typeof target.closest === "function" ? target.closest(".nav-item") : null;
  if (!nav || !nav.dataset) return;
  if (nav.dataset.panel === "atriumPanel") isoActivate();
  else if (isoRuntime.active) isoDeactivate();
}

/* Called once from app.js init. Idempotent: a re-run cannot bind twice. */
function setupAtrium() {
  const panel = document.getElementById("atriumPanel");
  if (!panel) return null;
  isoRuntime.panel = panel;
  isoBuildScene();
  if (!isoRuntime.bound) {
    isoRuntime.bound = true;
    const refresh = document.getElementById("isoRefreshButton");
    if (refresh) refresh.addEventListener("click", isoRefresh);
    document.addEventListener("click", isoOnDocumentClick);
    document.addEventListener("visibilitychange", () => {
      if (isoRuntime.active && !document.hidden) isoRefresh();
    });
  }
  // restoreActivePanel() may have opened this panel before init reached here.
  if (panel.classList.contains("active")) isoActivate();
  return panel;
}

function isoDebug() {
  return {
    active: isoRuntime.active,
    built: isoRuntime.built,
    polling: Boolean(isoRuntime.poll),
    artState: isoRuntime.artState,
    figures: isoRuntime.handles.length,
    objects: ISO_OBJECTS.length,
  };
}

/* ONE global door, like the companion: a module that failed to load leaves the
 * map usable, and the tests can drive the real scene without a browser. */
const NovaAtrium = {
  setup: setupAtrium,
  objects: ISO_OBJECTS,
  badge: isoBadge,
  renderStatus: isoRenderStatus,
  renderStatusError: isoRenderStatusError,
  refresh: isoRefresh,
  navigate: isoNavigate,
  mountArt: isoMountArt,
  unmountArt: isoUnmountArt,
  activate: isoActivate,
  deactivate: isoDeactivate,
  isActive: () => isoRuntime.active,
  debug: isoDebug,
};

if (typeof window !== "undefined") window.NovaAtrium = NovaAtrium;
