/* ── System telemetry (Command Center + NOVA CORE indicator) ──────────────
   One poll feeds both surfaces: the ribbon's NOVA CORE read-out and the
   Command Center's metric cards. Values come from /system/telemetry, which
   reads the real machine — CPU/RAM/disk/battery/uptime live per request, and
   GPU/network/temperature from the server's background sample, whose age is
   shown rather than hidden.

   Cards are built once and then *updated in place*, so the bars animate on the
   width transition instead of being torn down and rebuilt twice a second. */

const TELEMETRY_INTERVAL_MS = 2000;
let telemetryTimer = null;
let telemetryPayload = null;

/* ── Formatting: unmeasured values never get a number printed ──────────── */

function formatBytes(bytes, digits = 1) {
  if (typeof bytes !== "number" || !Number.isFinite(bytes)) return "";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(unit >= 3 ? digits : 0)} ${units[unit]}`;
}

function formatRate(bytesPerSecond) {
  if (typeof bytesPerSecond !== "number" || !Number.isFinite(bytesPerSecond)) return "";
  return `${formatBytes(bytesPerSecond, 1)}/s`;
}

function formatUptime(seconds) {
  if (typeof seconds !== "number" || !Number.isFinite(seconds)) return "";
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (days) return `${days}d ${hours}h`;
  if (hours) return `${hours}h ${minutes}m`;
  return `${minutes}m`;
}

function telemetryPercent(metric) {
  return metric && metric.available && typeof metric.percent === "number" ? metric.percent : null;
}

/* ── Cards ─────────────────────────────────────────────────────────────── */

// Order is usefulness for someone watching this machine work: what is loaded
// now, then memory and disk head-room, then the sampled extras.
const TELEMETRY_CARDS = [
  { key: "cpu", name: "CPU" },
  { key: "memory", name: "Memory" },
  { key: "storage", name: "Storage" },
  { key: "gpu", name: "GPU" },
  { key: "network", name: "Network" },
  { key: "battery", name: "Battery" },
  { key: "temperature", name: "Temp" },
  // Uptime last: real and always displayable, but the least actionable reading
  // on the board. It is a card rather than a details row so every metric this
  // section claims to show is visible without opening anything.
  { key: "uptime", name: "Uptime" },
];

function telemetryCardBody(key, hw) {
  const metric = hw[key] || {};
  if (!metric.available) {
    return {
      available: false,
      value: "Unavailable",
      percent: null,
      note: String(metric.reason || "this machine does not expose it"),
      state: "missing",
    };
  }
  switch (key) {
    case "cpu": {
      const percent = telemetryPercent(metric);
      return {
        available: true,
        value: `${percent.toFixed(0)}%`,
        percent,
        note: percent >= 85 ? "under heavy load" : "running",
        state: percent >= 85 ? "hot" : percent >= 60 ? "busy" : "ok",
      };
    }
    case "memory": {
      const percent = telemetryPercent(metric);
      return {
        available: true,
        value: `${formatBytes(metric.used_bytes)} / ${formatBytes(metric.total_bytes)}`,
        percent,
        note: `${formatBytes(metric.available_bytes)} available`,
        state: percent >= 88 ? "hot" : percent >= 70 ? "busy" : "ok",
      };
    }
    case "storage": {
      const percent = telemetryPercent(metric);
      return {
        available: true,
        value: `${formatBytes(metric.used_bytes)} / ${formatBytes(metric.total_bytes)}`,
        percent,
        note: `${formatBytes(metric.free_bytes)} free on ${metric.mount || "the system drive"}`,
        state: percent >= 92 ? "hot" : percent >= 80 ? "busy" : "ok",
      };
    }
    case "gpu": {
      const percent = telemetryPercent(metric);
      const memory = metric.memory || {};
      // Integrated GPUs have no dedicated VRAM: report what the adapter actually
      // reports (shared system memory) and name it as such.
      const memoryText = memory.available
        ? memory.total_bytes
          ? `${formatBytes(memory.used_bytes)} / ${formatBytes(memory.total_bytes)} VRAM`
          : `${formatBytes(memory.used_bytes)} ${memory.kind} GPU memory`
        : "";
      return {
        available: percent !== null,
        value: percent === null ? "Unavailable" : `${percent.toFixed(0)}%`,
        percent,
        note: [metric.name, memoryText].filter(Boolean).join(" · ") || String(metric.reason || ""),
        state: percent === null ? "missing" : percent >= 85 ? "hot" : percent >= 50 ? "busy" : "ok",
      };
    }
    case "network": {
      if (metric.rate_available) {
        return {
          available: true,
          value: `↓ ${formatRate(metric.download_bps)}`,
          percent: null,
          note: `↑ ${formatRate(metric.upload_bps)} over ${Number(metric.window_seconds || 0).toFixed(0)}s`,
          state: "ok",
        };
      }
      return {
        available: true,
        value: "measuring",
        percent: null,
        note: String(metric.reason || "byte rates need a second sample"),
        state: "busy",
      };
    }
    case "battery": {
      const percent = telemetryPercent(metric);
      const state = metric.charging ? "charging" : metric.on_ac ? "on AC" : "on battery";
      return {
        available: true,
        value: `${percent.toFixed(0)}%`,
        percent,
        note: state,
        state: !metric.on_ac && percent <= 20 ? "hot" : metric.charging ? "ok" : "busy",
      };
    }
    case "temperature": {
      return {
        available: true,
        value: `${metric.celsius.toFixed(0)}°C`,
        percent: null,
        note: String(metric.source || ""),
        state: metric.celsius >= 85 ? "hot" : "ok",
      };
    }
    case "uptime": {
      // Straight from the OS's boot clock (Win32 GetTickCount64 / psutil boot
      // time / /proc/uptime): no bar, because an uptime has no ceiling to be a
      // fraction of. `hw.uptime` is its own metric in the payload, so this card
      // goes through the same availability contract as every other card.
      const text = formatUptime(metric.seconds);
      return {
        available: Boolean(text),
        value: text || "Unavailable",
        percent: null,
        note: text
          ? `since last boot on ${(hw.host || {}).hostname || "this host"}`
          : String(metric.reason || "the OS did not report a boot time"),
        state: text ? "ok" : "missing",
      };
    }
    default:
      return { available: false, value: "Unavailable", percent: null, note: "", state: "missing" };
  }
}

function telemetryCard(key, name) {
  const card = el("article", "telemetry-card");
  card.dataset.metric = key;
  const top = el("div", "telemetry-card-top");
  top.appendChild(el("span", "telemetry-name", name));
  const dot = el("span", "telemetry-dot");
  dot.setAttribute("aria-hidden", "true");
  top.appendChild(dot);
  card.appendChild(top);
  card.appendChild(el("p", "telemetry-value", "—"));
  const bar = el("div", "telemetry-bar");
  bar.appendChild(el("span", "telemetry-fill"));
  card.appendChild(bar);
  card.appendChild(el("p", "telemetry-note", ""));
  return card;
}

function ensureTelemetryCards(hw) {
  const grid = byId("telemetryGrid");
  if (!grid) return;
  const missing = TELEMETRY_CARDS.filter((entry) => !grid.querySelector(`[data-metric="${entry.key}"]`));
  if (missing.length) {
    missing.forEach((entry) => grid.appendChild(telemetryCard(entry.key, entry.name)));
    // Keeping the sample-age row last means the grid never reorders underfoot.
    TELEMETRY_CARDS.forEach((entry) => {
      const card = grid.querySelector(`[data-metric="${entry.key}"]`);
      if (card) grid.appendChild(card);
    });
  }
}

function paintTelemetryCards(hw) {
  TELEMETRY_CARDS.forEach(({ key, name }) => {
    const card = document.querySelector(`#telemetryGrid [data-metric="${key}"]`);
    if (!card) return;
    const body = telemetryCardBody(key, hw);
    card.dataset.state = body.state;
    card.dataset.available = String(body.available);
    const value = card.querySelector(".telemetry-value");
    if (value.textContent !== body.value) value.textContent = body.value;
    const note = card.querySelector(".telemetry-note");
    if (note.textContent !== body.note) note.textContent = body.note;
    const fill = card.querySelector(".telemetry-fill");
    if (body.percent === null) {
      fill.style.width = "0%";
      card.dataset.bar = "none";
    } else {
      card.dataset.bar = "value";
      fill.style.width = `${Math.max(0, Math.min(100, body.percent))}%`;
    }
    card.setAttribute(
      "aria-label",
      `${name}: ${body.value}${body.note ? ` (${body.note})` : ""}`,
    );
    card.title = body.note;
  });
}

/* ── Engine / vision / automation status ───────────────────────────────── */

function telemetryEnginePills(nova) {
  const holder = byId("telemetryEngines");
  if (!holder) return;
  clearNode(holder);
  if (!nova || !nova.available) {
    holder.appendChild(el("span", "pill warn", "NovaControl status unavailable"));
    return;
  }
  const engine = nova.ai_engine || {};
  const enginePill = el("span", `pill ${engine.provider ? "ok" : "warn"}`);
  enginePill.textContent = `AI engine · ${engine.effective_mode || engine.mode || "unknown"}${
    engine.provider ? ` (${engine.provider})` : ""
  }`;
  enginePill.title = `Mode ${engine.mode || "?"} · ${engine.provider || "no provider"}${
    engine.model ? ` · ${engine.model}` : ""
  }`;
  holder.appendChild(enginePill);

  const vision = nova.vision || {};
  const visionPill = el("span", `pill ${vision.available ? "ok" : ""}`);
  visionPill.textContent = vision.available ? "Vision ready" : "Vision unavailable";
  visionPill.title = vision.available ? "A vision model is installed." : String(vision.reason || "");
  holder.appendChild(visionPill);

  const automation = nova.automation || {};
  const automationPill = el("span", "pill ok");
  automationPill.textContent = `Automation · ${automation.workflows ?? 0} workflow(s)`;
  automationPill.title = [
    automation.desktop_runner ? `desktop: ${automation.desktop_runner}` : "",
    automation.browser_runner ? `browser: ${automation.browser_runner}` : "",
  ]
    .filter(Boolean)
    .join(" · ");
  holder.appendChild(automationPill);

  const tasks = el("span", `pill ${nova.active_tasks ? "warn" : "ok"}`);
  tasks.textContent = `${nova.active_tasks} active task(s)`;
  tasks.title = (nova.active_task_titles || []).join("\n") || "No task is pending or running.";
  holder.appendChild(tasks);
}

/* ── Details table ─────────────────────────────────────────────────────── */

function telemetryDetailRows(hw, nova) {
  const host = hw.host || {};
  const sampler = hw.sampler || {};
  const network = hw.network || {};
  const gpu = hw.gpu || {};
  const rows = [];
  const add = (label, value) => {
    if (value !== "" && value !== null && value !== undefined) rows.push([label, String(value)]);
  };

  add("Host", host.hostname);
  add("System", [host.platform, host.platform_release].filter(Boolean).join(" "));
  add("CPU", host.cpu_model);
  add("Logical cores", host.cpu_count);
  add("Uptime", formatUptime(host.uptime_seconds));
  add("Python", host.python);
  add(
    "Sensor backend",
    host.psutil
      ? "psutil"
      : "native OS APIs (psutil is not installed — GPU, network and thermal values use the OS's own counters)",
  );
  // The GPU row is always present: before the slow sample completes there is no
  // adapter name yet, so the row states that rather than vanishing.
  add("GPU", gpu.name || String(gpu.reason || "waiting for the slow sensor sample"));
  const gpuMemory = gpu.memory || {};
  if (gpuMemory.available) {
    add(
      "GPU memory",
      gpuMemory.total_bytes
        ? `${formatBytes(gpuMemory.used_bytes)} / ${formatBytes(gpuMemory.total_bytes)} VRAM (dedicated)`
        : `${formatBytes(gpuMemory.used_bytes)} ${gpuMemory.kind} GPU memory`,
    );
  }
  // A single-adapter host once arrived as a bare string, which has no .join: the
  // list is normalized here too, so the read-out can never break on its shape.
  const interfaces = Array.isArray(network.interfaces)
    ? network.interfaces
    : network.interfaces
      ? [String(network.interfaces)]
      : [];
  add("Network interfaces", interfaces.length ? interfaces.join(", ") : "none reported");
  add(
    "Slow sensors",
    sampler.expensive_sampled
      ? sampler.expensive_age_seconds === null
        ? "sampled"
        : `sampled ${sampler.expensive_age_seconds}s ago`
      : String(sampler.note || "not sampled yet"),
  );
  add("Storage mount", (hw.storage || {}).mount);

  if (nova && nova.available) {
    const engine = nova.ai_engine || {};
    add("AI engine", [engine.mode, engine.effective_mode].filter(Boolean).join(" → "));
    add("AI provider", engine.provider);
    add("AI model", engine.model);
    add("Cloud brain", engine.cloud_configured ? `configured (${engine.cloud_state || "idle"})` : "not configured");
    add("Vision", (nova.vision || {}).available ? "model installed" : (nova.vision || {}).reason || "unavailable");
    add("Automation", `${(nova.automation || {}).workflows ?? 0} workflow(s)`);
    add("Tracked tasks", nova.tracked_tasks);
    add("Modules", nova.modules);
    add("Projects", nova.projects);
  }
  return rows;
}

function paintTelemetryDetails(hw, nova) {
  const holder = byId("telemetryDetails");
  if (!holder || holder.hidden) return;
  clearNode(holder);
  const list = el("dl", "telemetry-detail-list");
  telemetryDetailRows(hw, nova).forEach(([label, value]) => {
    list.appendChild(el("dt", "", label));
    list.appendChild(el("dd", "", value));
  });
  holder.appendChild(list);
}

/* ── NOVA CORE indicator ───────────────────────────────────────────────── */

function paintCoreIndicator(hw, nova) {
  const readout = byId("coreReadout");
  const pulse = byId("corePulse");
  if (!readout) return;
  const cpu = telemetryPercent(hw.cpu);
  const memory = telemetryPercent(hw.memory);
  const tasks = nova && nova.available ? nova.active_tasks : null;
  // Every segment is a real reading; a metric this machine cannot report is
  // left out of the read-out instead of being printed as a zero.
  const parts = [];
  if (cpu !== null) parts.push(`CPU ${cpu.toFixed(0)}%`);
  if (memory !== null) parts.push(`RAM ${memory.toFixed(0)}%`);
  if (tasks !== null) parts.push(`${tasks} TASK${tasks === 1 ? "" : "S"}`);
  readout.textContent = parts.length ? parts.join(" · ") : "no metrics available";

  const hot = [cpu, memory].some((value) => value !== null && value >= 90);
  const busy = [cpu, memory].some((value) => value !== null && value >= 70);
  if (pulse) {
    pulse.dataset.state = hot ? "hot" : busy ? "busy" : "ok";
  }
  const indicator = byId("coreIndicator");
  if (indicator) {
    indicator.dataset.state = hot ? "hot" : busy ? "busy" : "ok";
    indicator.title = `NOVA CORE — live system telemetry\n${parts.join(" · ") || "no metrics available"}\nClick for the full read-out.`;
  }
}

/* ── Poll loop ─────────────────────────────────────────────────────────── */

function renderTelemetry(payload) {
  telemetryPayload = payload;
  const hw = payload.hardware || {};
  const nova = payload.novacontrol || {};
  ensureTelemetryCards(hw);
  paintTelemetryCards(hw);
  paintCoreIndicator(hw, nova);
  telemetryEnginePills(nova);
  paintTelemetryDetails(hw, nova);

  const fresh = byId("telemetryFreshness");
  if (fresh) {
    const sampler = hw.sampler || {};
    if (!sampler.expensive_sampled) {
      fresh.className = "pill warn";
      fresh.textContent = "live metrics · slow sensors pending";
    } else {
      fresh.className = "pill ok";
      fresh.textContent = `sampled ${(sampler.expensive_age_seconds ?? 0).toFixed(0)}s ago`;
    }
    fresh.title = "CPU, memory, disk, battery and uptime are read on every poll; GPU, network rate and temperature come from the server's slow-sensor sample, shown with its age.";
  }
  const grid = byId("telemetryGrid");
  if (grid) grid.setAttribute("aria-busy", "false");
}

async function pollTelemetry() {
  try {
    const payload = await requestJson("/system/telemetry");
    renderTelemetry(payload);
  } catch (error) {
    const grid = byId("telemetryGrid");
    if (grid && !telemetryPayload) {
      clearNode(grid);
      grid.appendChild(el("p", "telemetry-error", `Telemetry unavailable: ${error.message}`));
      grid.setAttribute("aria-busy", "false");
    }
  }
}

// Polling stops while the tab is hidden: a background tab has no reader, and the
// server would keep reading this machine for nobody.
function startTelemetry() {
  if (telemetryTimer !== null) return;
  pollTelemetry();
  telemetryTimer = window.setInterval(() => {
    if (!document.hidden) pollTelemetry();
  }, TELEMETRY_INTERVAL_MS);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) pollTelemetry();
  });
}

function setupTelemetrySection() {
  const toggle = byId("telemetryToggle");
  const details = byId("telemetryDetails");
  const indicator = byId("coreIndicator");
  if (!toggle || !details) return;

  const setOpen = (open) => {
    details.hidden = !open;
    toggle.setAttribute("aria-expanded", String(open));
    toggle.textContent = open ? "Hide details" : "Details";
    if (indicator) indicator.setAttribute("aria-expanded", String(open));
    if (open && telemetryPayload) {
      paintTelemetryDetails(telemetryPayload.hardware || {}, telemetryPayload.novacontrol || {});
    }
  };

  toggle.addEventListener("click", () => setOpen(details.hidden));

  // The header indicator is a shortcut into the same read-out: show the Command
  // tab (where telemetry lives), open the details, and scroll them into view.
  if (indicator) {
    indicator.addEventListener("click", () => {
      const nav = document.querySelector('.nav-item[data-panel="homePanel"]');
      if (nav && !nav.classList.contains("active")) nav.click();
      setOpen(true);
      byId("telemetrySection")?.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  }
}
