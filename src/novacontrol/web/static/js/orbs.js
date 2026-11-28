/* ── Extra 4 · Orb placements ────────────────────────────────
 *
 * Binds the four approved placements to the ONE activity adapter:
 *   main (Chat workspace) · explore · voice · task (System)
 * The orb is presentation only: every label and state comes from
 * `NovaActivity`, which is fed by real runtime events.
 */
(function () {
  "use strict";

  const PLACEMENTS = [
    { scope: "main", host: "novaOrbMain", label: "novaOrbMainLabel", detail: "novaOrbMainDesc", status: "novaOrbMainStatus", card: "novaOrbMainCard" },
    { scope: "explore", host: "novaOrbExplore", label: "novaOrbExploreLabel", detail: "novaOrbExploreDesc", status: "novaOrbExploreStatus", card: "novaOrbExploreCard" },
    { scope: "voice", host: "novaOrbVoice", label: "novaOrbVoiceLabel", detail: "novaOrbVoiceDesc", status: "novaOrbVoiceStatus", card: "novaOrbVoiceCard" },
    { scope: "task", host: "novaOrbTask", label: "novaOrbTaskLabel", detail: "novaOrbTaskDesc", status: "novaOrbTaskStatus", card: "novaOrbTaskCard" },
  ];

  const mounted = [];
  let unsubscribe = null;
  let started = false;

  function byId(id) {
    return document.getElementById(id);
  }

  function toneFor(state) {
    if (state === "error") return "error";
    if (state === "completed") return "ok";
    if (state === "waiting" || state === "cancelled") return "warn";
    if (state === "unknown") return "muted";
    if (state === "idle") return "idle";
    return "live";
  }

  function paint(scope, view) {
    const placement = PLACEMENTS.find((item) => item.scope === scope);
    if (!placement) return;
    const host = byId(placement.host);
    const label = byId(placement.label);
    const detail = byId(placement.detail);
    const status = byId(placement.status);
    const card = byId(placement.card);
    if (label) label.textContent = view.label || "Ready";
    if (detail) detail.textContent = view.description || "";
    if (status) {
      status.textContent = view.state === "idle" ? "Idle" : view.label || view.state;
      status.dataset.tone = toneFor(view.state);
      status.setAttribute("data-tone", toneFor(view.state));
    }
    if (card) {
      card.dataset.state = view.state;
      card.dataset.source = view.source || "";
      if (view.taskId) card.dataset.taskId = view.taskId;
      else delete card.dataset.taskId;
    }
    if (host) {
      host.dataset.state = view.state;
      host.setAttribute("aria-label", `${view.label || "Ready"}. ${view.description || ""}`.trim());
      const orb = mounted.find((item) => item.scope === scope);
      if (orb && orb.controller) {
        orb.controller.setState(view.state === "idle" ? "breathing" : view.state, {
          colors: true,
        });
      }
    }
  }

  function mount() {
    if (started) return;
    started = true;
    if (!window.NovaActivity || !window.NovaOrb) {
      PLACEMENTS.forEach((placement) => {
        const card = byId(placement.card);
        if (card) card.dataset.state = "unknown";
        const label = byId(placement.label);
        if (label) label.textContent = "Activity unavailable";
        const detail = byId(placement.detail);
        if (detail) detail.textContent = "The activity adapter did not load, so no orb state is being shown.";
      });
      return;
    }
    PLACEMENTS.forEach((placement) => {
      const host = byId(placement.host);
      if (!host) return;
      const controller = window.NovaOrb.create(host, { size: host.dataset.size || undefined });
      mounted.push({ scope: placement.scope, controller });
    });
    unsubscribe = window.NovaActivity.subscribe(paint);
  }

  function destroy() {
    if (unsubscribe) unsubscribe();
    unsubscribe = null;
    mounted.forEach((item) => item.controller.destroy());
    mounted.length = 0;
    started = false;
  }

  window.NovaOrbs = {
    mount,
    destroy,
    placements: PLACEMENTS.map((item) => item.scope),
    stats() {
      return window.NovaOrb ? window.NovaOrb.stats() : null;
    },
    state(scope = "main") {
      return window.NovaActivity ? window.NovaActivity.getState(scope) : null;
    },
  };
})();
