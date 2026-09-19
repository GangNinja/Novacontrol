/* NovaControl OS shell behaviors — presentation only.
   Everything here reuses EXISTING controls: the palette "runs" the same nav
   buttons and header buttons a user could click by hand; the splash fades on
   the DOM's real readiness; the ribbon mirrors states the UI already renders.
   No data contracts, routes, or app logic are touched. */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);

  /* ── Splash: fade once the shell's own init has run ────────────────────── */
  const bootSplash = () => {
    requestAnimationFrame(() => {
      requestAnimationFrame(() => document.body.classList.add("nc-booted"));
    });
  };
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bootSplash, { once: true });
  } else {
    bootSplash();
  }

  /* ── Command palette: existing nav + existing header buttons ───────────── */
  const palette = $("commandPalette");
  const paletteInput = $("paletteInput");
  const paletteList = $("paletteList");
  if (!palette || !paletteInput || !paletteList) return;

  const NAV_ITEMS = Array.from(document.querySelectorAll(".nav-item")).map((btn) => ({
    label: btn.textContent.trim(),
    hint: "Panel",
    group: "Navigate",
    glyph: "◈",
    run: () => btn.click(),
  }));

  const COMMAND_ITEMS = [
    { id: "homeHealthButton", label: "Check System", hint: "Command Center", group: "Commands", glyph: "✚" },
    { id: "clearChatButton", label: "Clear Chat", hint: "Chat", group: "Commands", glyph: "⌫" },
    { id: "clearCliButton", label: "Clear CLI Output", hint: "CLI", group: "Commands", glyph: "⌫" },
    { id: "refreshButton", label: "Refresh Status", hint: "Runtime", group: "Commands", glyph: "↻" },
    { id: "focusModeButton", label: "Toggle Focus Mode", hint: "View", group: "Commands", glyph: "◎" },
  ]
    .map((item) => ({ ...item, run: () => { const el = $(item.id); if (el) el.click(); } }))
    .filter((item) => $(item.id));

  const ALL_ITEMS = [...NAV_ITEMS, ...COMMAND_ITEMS];
  let activeIndex = 0;
  let visible = [];

  const renderPalette = (query) => {
    const q = query.trim().toLowerCase();
    visible = ALL_ITEMS.filter(
      (item) => !q || item.label.toLowerCase().includes(q) || item.hint.toLowerCase().includes(q),
    );
    paletteList.textContent = "";
    let lastGroup = null;
    visible.forEach((item, index) => {
      if (item.group !== lastGroup) {
        lastGroup = item.group;
        const head = document.createElement("div");
        head.className = "cp-group";
        head.textContent = item.group;
        paletteList.appendChild(head);
      }
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "cp-result" + (index === activeIndex ? " active" : "");
      btn.setAttribute("role", "option");
      const glyph = document.createElement("span");
      glyph.className = "cp-glyph";
      glyph.textContent = item.glyph;
      const name = document.createElement("strong");
      name.textContent = item.label;
      const hint = document.createElement("span");
      hint.className = "cp-hint";
      hint.textContent = item.hint;
      btn.append(glyph, name, hint);
      btn.addEventListener("click", () => { closePalette(); item.run(); });
      paletteList.appendChild(btn);
    });
    if (!visible.length) {
      const empty = document.createElement("div");
      empty.className = "cp-empty";
      empty.textContent = "No matching commands";
      paletteList.appendChild(empty);
    }
    activeIndex = Math.min(activeIndex, Math.max(0, visible.length - 1));
    highlightActive();
  };

  const highlightActive = () => {
    paletteList.querySelectorAll(".cp-result").forEach((el, index) => {
      el.classList.toggle("active", index === activeIndex);
      if (index === activeIndex) el.scrollIntoView({ block: "nearest" });
    });
  };

  const openPalette = () => {
    palette.hidden = false;
    activeIndex = 0;
    renderPalette(paletteInput.value);
    paletteInput.value = "";
    renderPalette("");
    paletteInput.focus();
  };

  const closePalette = () => {
    palette.hidden = true;
  };

  const paletteOpen = () => !palette.hidden;

  document.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      paletteOpen() ? closePalette() : openPalette();
      return;
    }
    if (!paletteOpen()) return;
    if (event.key === "Escape") {
      event.preventDefault();
      closePalette();
    } else if (event.key === "ArrowDown") {
      event.preventDefault();
      activeIndex = Math.min(activeIndex + 1, visible.length - 1);
      highlightActive();
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      activeIndex = Math.max(activeIndex - 1, 0);
      highlightActive();
    } else if (event.key === "Enter") {
      event.preventDefault();
      const item = visible[activeIndex];
      if (item) { closePalette(); item.run(); }
    }
  });

  const backdrop = palette.querySelector(".cp-backdrop");
  if (backdrop) backdrop.addEventListener("click", closePalette);
  const paletteButton = $("paletteButton");
  if (paletteButton) paletteButton.addEventListener("click", openPalette);

  paletteInput.addEventListener("input", () => {
    activeIndex = 0;
    renderPalette(paletteInput.value);
  });

  /* ── Ribbon state: mirror what the Quick Command output already shows ──── */
  const ribbonState = $("ribbonState");
  const commandOutput = $("commandOutput");
  if (ribbonState && commandOutput && "MutationObserver" in window) {
    const setRibbon = (state, label) => {
      ribbonState.dataset.state = state;
      ribbonState.textContent = label;
    };
    const syncRibbon = () => {
      // Lifecycle words map 1:1 to real DOM state — nothing invented.
      const planBtn = document.getElementById("jarvisPlanButton");
      const runBtn = document.getElementById("jarvisRunButton");
      const jarvisOutput = document.getElementById("jarvisOutput");
      if (planBtn && planBtn.classList.contains("loading")) setRibbon("understanding", "Understanding");
      else if (runBtn && runBtn.classList.contains("loading")) setRibbon("executing", "Executing");
      else if (commandOutput.querySelector(".loading-indicator")) setRibbon("understanding", "Understanding");
      else if (commandOutput.querySelector(".error-card") || (jarvisOutput && jarvisOutput.querySelector(".error-card"))) setRibbon("error", "Error");
      else if (jarvisOutput && jarvisOutput.querySelector(".approval-banner.pending")) setRibbon("approval", "Approval");
      else if (!commandOutput.classList.contains("empty-state") || (jarvisOutput && !jarvisOutput.classList.contains("empty-state"))) setRibbon("complete", "Complete");
      else setRibbon("ready", "Ready");
    };
    new MutationObserver(syncRibbon).observe(commandOutput, { childList: true, subtree: true, attributes: true, attributeFilter: ["class"] });
    syncRibbon();
    // The JARVIS console lives outside commandOutput — watch it too so the
    // ribbon reacts to its plan/approve button states.
    const jarvisConsole = document.querySelector("#jarvisPanel .command-console");
    if (jarvisConsole) {
      new MutationObserver(syncRibbon).observe(jarvisConsole, { childList: true, subtree: true, attributes: true, attributeFilter: ["class"] });
    }
    const jarvisOutput = document.getElementById("jarvisOutput");
    if (jarvisOutput) {
      new MutationObserver(syncRibbon).observe(jarvisOutput, { childList: true, subtree: true });
    }
  }

  /* ── Focus Mode: class toggle styled by nc-os.css ─────────────────────── */
  const focusButton = $("focusModeButton");
  if (focusButton) {
    focusButton.addEventListener("click", () => {
      const on = document.body.classList.toggle("nc-focus-mode");
      focusButton.classList.toggle("active", on);
      focusButton.setAttribute("aria-pressed", on ? "true" : "false");
    });
  }
})();
