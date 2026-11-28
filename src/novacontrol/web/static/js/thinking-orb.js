/* ── Extra 4 · Thinking Orb renderer (native canvas) ─────────
 *
 * A dependency-free spherical point-field renderer for NovaControl. It is
 * inspired by the MIT-licensed `thinking-orbs` visual language (nine dotted
 * states, two tuned scales, monochrome ink) but is an original implementation:
 * NovaControl serves hand-written ES modules, has no npm/React toolchain, and
 * the package is React-only. Nothing from that package is vendored or copied.
 *
 * Performance model:
 *   • one shared requestAnimationFrame loop for every instance;
 *   • 30 fps ceiling for large orbs, 24 fps for compact ones;
 *   • instances pause when offscreen, in a hidden tab, or in a static state;
 *   • device-pixel-ratio capped at 2;
 *   • `prefers-reduced-motion` draws one representative frame and never loops;
 *   • every draw is plain 2D canvas arcs (no filters, WebGL or SVG).
 */
(function () {
  "use strict";

  const FRAME_MS = 1000 / 30;
  const SMALL_FRAME_MS = 1000 / 24;
  const GOLDEN = Math.PI * (3 - Math.sqrt(5));
  const instances = new Set();
  const stats = { frames: 0, totalDrawMs: 0, maxDrawMs: 0, lastDrawMs: 0, instances: 0 };
  let rafId = null;
  let lastFrameAt = 0;
  let visibilityBound = false;

  function reducedMotion() {
    try {
      return Boolean(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
    } catch (_) {
      return false;
    }
  }

  function now() {
    return typeof performance !== "undefined" && performance.now ? performance.now() : Date.now();
  }

  function clamp(value, low, high) {
    return Math.max(low, Math.min(high, value));
  }

  function hexToRgb(hex) {
    let value = String(hex || "").trim().replace("#", "");
    if (value.length === 3) value = value.split("").map((ch) => ch + ch).join("");
    if (!/^[0-9a-f]{6}$/i.test(value)) return "103, 245, 228";
    return `${parseInt(value.slice(0, 2), 16)}, ${parseInt(value.slice(2, 4), 16)}, ${parseInt(value.slice(4, 6), 16)}`;
  }

  function rgba(rgb, alpha) {
    return `rgba(${rgb}, ${clamp(alpha, 0, 1).toFixed(3)})`;
  }

  function makePoints(count) {
    const points = [];
    for (let i = 0; i < count; i += 1) {
      const y = 1 - (i / Math.max(1, count - 1)) * 2;
      const radius = Math.sqrt(Math.max(0, 1 - y * y));
      const theta = GOLDEN * i;
      points.push({ x: Math.cos(theta) * radius, y, z: Math.sin(theta) * radius });
    }
    return points;
  }

  function rotate3(x, y, z, ax, ay) {
    const cosY = Math.cos(ay);
    const sinY = Math.sin(ay);
    const x1 = x * cosY - z * sinY;
    const z1 = x * sinY + z * cosY;
    const cosX = Math.cos(ax);
    const sinX = Math.sin(ax);
    const y1 = y * cosX - z1 * sinX;
    const z2 = y * sinX + z1 * cosX;
    return { x: x1, y: y1, z: z2 };
  }

  function smoothstep(edge0, edge1, value) {
    const t = clamp((value - edge0) / (edge1 - edge0 || 1), 0, 1);
    return t * t * (3 - 2 * t);
  }

  class ThinkingOrb {
    constructor(host, options = {}) {
      this.host = host;
      this.options = options;
      this.state = "breathing";
      this.stateSince = now();
      this.paused = Boolean(options.paused);
      this.visible = true;
      this.reduced = reducedMotion();
      this.destroyed = false;
      this.lastTick = 0;
      this.size = Number(options.size) || 160;
      this.points = makePoints(Number(options.dots) || 110);
      this.canvas = null;
      this.ctx = null;
      this.dpr = 1;
      this.frameInterval = this.size < 70 ? SMALL_FRAME_MS : FRAME_MS;
      this.colors = { accent: "103, 245, 228", soft: "47, 190, 210", error: "255, 107, 107", wait: "251, 191, 36" };
      this.mount();
    }

    mount() {
      const doc = this.host.ownerDocument || document;
      this.canvas = doc.createElement("canvas");
      this.canvas.className = "thinking-orb-canvas";
      this.canvas.setAttribute("aria-hidden", "true");
      this.host.appendChild(this.canvas);
      try {
        this.ctx = this.canvas.getContext("2d");
      } catch (_) {
        this.ctx = null;
      }
      this.readColors();
      this.resize();
      this.bindObservers();
      instances.add(this);
      stats.instances = instances.size;
      if (!this.ctx) {
        this.host.classList.add("thinking-orb--fallback");
        return;
      }
      this.draw(now(), true);
      ensureLoop();
    }

    readColors() {
      try {
        const computed = window.getComputedStyle ? window.getComputedStyle(this.host) : null;
        if (!computed) return;
        const accent = computed.getPropertyValue("--orb-accent").trim();
        const soft = computed.getPropertyValue("--orb-soft").trim();
        if (accent) this.colors.accent = hexToRgb(accent);
        if (soft) this.colors.soft = hexToRgb(soft);
      } catch (_) { /* defaults are valid */ }
    }

    resize() {
      if (!this.canvas) return;
      const rect = this.host.getBoundingClientRect ? this.host.getBoundingClientRect() : null;
      const width = rect && rect.width ? rect.width : this.size;
      const height = rect && rect.height ? rect.height : this.size;
      this.size = clamp(Math.round(Math.min(width, height) || this.size), 24, 320);
      this.dpr = Math.min(2, (window.devicePixelRatio || 1));
      this.canvas.width = Math.round(this.size * this.dpr);
      this.canvas.height = Math.round(this.size * this.dpr);
      this.canvas.style.width = `${this.size}px`;
      this.canvas.style.height = `${this.size}px`;
      if (this.ctx) this.ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
      this.frameInterval = this.size < 70 ? SMALL_FRAME_MS : FRAME_MS;
      this.draw(now(), true);
    }

    bindObservers() {
      if (typeof ResizeObserver !== "undefined") {
        this.resizeObserver = new ResizeObserver(() => this.resize());
        this.resizeObserver.observe(this.host);
      }
      if (typeof IntersectionObserver !== "undefined") {
        this.intersectionObserver = new IntersectionObserver((entries) => {
          const entry = entries && entries[0];
          if (!entry) return;
          this.visible = Boolean(entry.isIntersecting);
          if (this.visible) ensureLoop();
        }, { threshold: 0.01 });
        this.intersectionObserver.observe(this.host);
      }
      if (!visibilityBound && typeof document !== "undefined" && document.addEventListener) {
        visibilityBound = true;
        document.addEventListener("visibilitychange", () => {
          if (!document.hidden) ensureLoop();
        });
      }
    }

    setState(state, options = {}) {
      const next = String(state || "breathing");
      this.state = next === "idle" ? "breathing" : next;
      this.stateSince = now();
      if (options.paused != null) this.paused = Boolean(options.paused);
      if (options.colors) this.readColors();
      this.draw(now(), true);
      ensureLoop();
    }

    setPaused(paused) {
      this.paused = Boolean(paused);
      if (this.paused) this.draw(now(), true);
      else ensureLoop();
    }

    isStatic() {
      return this.reduced || this.paused || this.visible === false || this.state === "error" || this.state === "unknown" || this.state === "waiting";
    }

    tick(timestamp) {
      if (this.destroyed || !this.ctx) return;
      if (this.paused || this.visible === false || this.reduced) return;
      if (typeof document !== "undefined" && document.hidden) return;
      if (this.state === "error" || this.state === "unknown" || this.state === "waiting") return;
      if (timestamp - this.lastTick < this.frameInterval) return;
      this.lastTick = timestamp;
      this.draw(timestamp, false);
      ensureLoop();
    }

    draw(timestamp, force) {
      if (!this.ctx || !this.canvas) return;
      const started = now();
      const size = this.size;
      const ctx = this.ctx;
      ctx.clearRect(0, 0, size, size);
      const reducedTime = this.reduced || this.state === "waiting" || this.state === "error" || this.state === "unknown";
      const t = reducedTime ? 0.8 : (timestamp || now()) / 1000;
      const elapsed = (now() - this.stateSince) / 1000;
      this.drawGlow(ctx, size);
      if (this.state === "error") this.drawError(t);
      else if (this.state === "unknown") this.drawUnknown(t);
      else if (this.state === "listening") this.drawListening(t);
      else if (this.state === "searching") this.drawSearching(t);
      else if (this.state === "weaving" || this.state === "thinking") this.drawWeaving(t);
      else if (this.state === "shaping" || this.state === "planning") this.drawShaping(t);
      else if (this.state === "connecting") this.drawConnecting(t);
      else if (this.state === "working") this.drawWorking(t);
      else if (this.state === "solving" || this.state === "recovering") this.drawSolving(t);
      else if (this.state === "composing") this.drawComposing(t);
      else if (this.state === "completed") this.drawCompleted(t, elapsed);
      else this.drawBreathing(t);
      const duration = now() - started;
      stats.frames += 1;
      stats.lastDrawMs = duration;
      stats.totalDrawMs += duration;
      stats.maxDrawMs = Math.max(stats.maxDrawMs, duration);
    }

    drawGlow(ctx, size) {
      const center = size / 2;
      const radius = size * 0.52;
      const gradient = ctx.createRadialGradient(center, center, 0, center, center, radius);
      gradient.addColorStop(0, rgba(this.colors.accent, 0.1));
      gradient.addColorStop(0.55, rgba(this.colors.soft, 0.035));
      gradient.addColorStop(1, rgba(this.colors.accent, 0));
      ctx.fillStyle = gradient;
      ctx.beginPath();
      ctx.arc(center, center, radius, 0, Math.PI * 2);
      ctx.fill();
    }

    project(point, t, speed = 0.22, tilt = -0.3) {
      const center = this.size / 2;
      const radius = this.size * 0.34;
      const rotated = rotate3(point.x, point.y, point.z, tilt, t * speed);
      return {
        x: center + rotated.x * radius,
        y: center + rotated.y * radius,
        z: rotated.z,
      };
    }

    dot(x, y, radius, rgb, alpha) {
      const ctx = this.ctx;
      ctx.fillStyle = rgba(rgb, alpha);
      ctx.beginPath();
      ctx.arc(x, y, Math.max(0.45, radius), 0, Math.PI * 2);
      ctx.fill();
    }

    drawBreathing(t) {
      const scale = 1 + 0.022 * Math.sin(t * 1.15);
      const ctx = this.ctx;
      for (const point of this.points) {
        const p = this.project({ x: point.x * scale, y: point.y * scale, z: point.z * scale }, t, 0.18);
        const depth = (p.z + 1) / 2;
        this.dot(p.x, p.y, this.size * 0.0045 + depth * this.size * 0.003, this.colors.accent, 0.12 + depth * 0.7);
      }
      ctx.strokeStyle = rgba(this.colors.soft, 0.15);
      ctx.lineWidth = 0.75;
      ctx.beginPath();
      ctx.arc(this.size / 2, this.size / 2, this.size * 0.38, 0, Math.PI * 2);
      ctx.stroke();
    }

    drawListening(t) {
      const center = this.size / 2;
      const radius = this.size * 0.34;
      for (let ring = 0; ring < 3; ring += 1) {
        const ringRadius = radius * (0.72 + ring * 0.14);
        for (let i = 0; i < 30; i += 1) {
          const angle = (i / 30) * Math.PI * 2 + t * (0.22 + ring * 0.08);
          const wave = Math.sin(angle * 3 + t * 3.4 + ring) * 0.06;
          const x = center + Math.cos(angle) * ringRadius * (1 + wave);
          const y = center + Math.sin(angle) * ringRadius * 0.3 * (1 + wave * 2.4);
          this.dot(x, y, this.size * 0.0038, ring === 1 ? this.colors.soft : this.colors.accent, 0.24 + (ring === 1 ? 0.4 : 0.2));
        }
      }
      for (const point of this.points) {
        const wave = Math.sin(point.y * 8 + t * 4.2) * 0.055;
        const p = this.project({ x: point.x * (1 + wave), y: point.y, z: point.z }, t, 0.14);
        const depth = (p.z + 1) / 2;
        this.dot(p.x, p.y, this.size * 0.0038 + depth * this.size * 0.0026, this.colors.accent, 0.1 + depth * 0.55);
      }
    }

    drawSearching(t) {
      const sweep = t * 0.85;
      const scanY = Math.sin(t * 0.55) * 0.82;
      for (const point of this.points) {
        const p = this.project(point, t, 0.2);
        const depth = (p.z + 1) / 2;
        const longitude = Math.atan2(point.z, point.x);
        const distance = Math.abs(Math.atan2(Math.sin(longitude - sweep), Math.cos(longitude - sweep)));
        const near = smoothstep(0.6, 0.0, distance);
        const band = smoothstep(0.18, 0.0, Math.abs(point.y - scanY));
        this.dot(p.x, p.y, this.size * 0.0036 + depth * this.size * 0.0024 + near * this.size * 0.0018, near > 0.25 ? this.colors.accent : this.colors.soft, 0.1 + depth * 0.4 + near * 0.45 + band * 0.25);
      }
      const center = this.size / 2;
      this.ctx.strokeStyle = rgba(this.colors.accent, 0.28);
      this.ctx.lineWidth = 0.8;
      this.ctx.beginPath();
      this.ctx.ellipse(center, center + scanY * this.size * 0.34, this.size * 0.29, this.size * 0.075, 0, 0, Math.PI * 2);
      this.ctx.stroke();
    }

    drawWeaving(t) {
      const center = this.size / 2;
      const radius = this.size * 0.33;
      for (const point of this.points) {
        const p = this.project(point, t, 0.12);
        const depth = (p.z + 1) / 2;
        this.dot(p.x, p.y, this.size * 0.0034, this.colors.soft, 0.05 + depth * 0.16);
      }
      for (let strand = 0; strand < 3; strand += 1) {
        const tilt = -0.75 + strand * 0.75;
        for (let i = 0; i < 34; i += 1) {
          const angle = (i / 34) * Math.PI * 2 + t * (0.34 + strand * 0.11);
          const base = { x: Math.cos(angle), y: Math.sin(angle), z: 0 };
          const rotated = rotate3(base.x, base.y, base.z, tilt + Math.sin(t * 0.5 + strand) * 0.16, t * 0.18);
          this.dot(center + rotated.x * radius, center + rotated.y * radius, this.size * 0.0052, strand === 1 ? this.colors.accent : this.colors.soft, 0.34 + 0.2 * Math.sin(angle * 2 + t));
        }
      }
    }

    drawShaping(t) {
      const center = this.size / 2;
      const radius = this.size * 0.31;
      for (const point of this.points) {
        const p = this.project(point, t, 0.1);
        const depth = (p.z + 1) / 2;
        this.dot(p.x, p.y, this.size * 0.0032, this.colors.soft, 0.04 + depth * 0.14);
      }
      const morph = (Math.sin(t * 0.5) + 1) / 2;
      for (let i = 0; i < 72; i += 1) {
        const angle = (i / 72) * Math.PI * 2;
        const circle = 1;
        const triangle = 1 / Math.max(0.3, Math.cos(((angle + Math.PI / 2) % (Math.PI * 2)) / 2));
        const maxAxis = Math.max(Math.abs(Math.cos(angle)), Math.abs(Math.sin(angle)));
        const square = 1 / Math.max(0.4, maxAxis);
        const shape = circle * (1 - morph) + (triangle * 0.62 + square * 0.38) * morph;
        const x = center + Math.cos(angle) * radius * shape;
        const y = center + Math.sin(angle) * radius * shape;
        this.dot(x, y, this.size * 0.0042, this.colors.accent, 0.26 + 0.4 * morph);
      }
    }

    drawConnecting(t) {
      const points = this.points.filter((_, index) => index % 9 === 0);
      const projected = points.map((point) => this.project(point, t, 0.12));
      const ctx = this.ctx;
      for (let i = 0; i < projected.length; i += 1) {
        const a = projected[i];
        const b = projected[(i + 3) % projected.length];
        const pulse = 0.5 + 0.5 * Math.sin(t * 1.6 + i * 0.9);
        ctx.strokeStyle = rgba(this.colors.accent, 0.05 + pulse * 0.22);
        ctx.lineWidth = 0.7;
        ctx.beginPath();
        ctx.moveTo(a.x, a.y);
        ctx.lineTo(b.x, b.y);
        ctx.stroke();
      }
      projected.forEach((p, index) => {
        const depth = (p.z + 1) / 2;
        this.dot(p.x, p.y, this.size * 0.0045 + depth * this.size * 0.002, this.colors.accent, 0.2 + depth * 0.55);
      });
      for (const point of this.points) {
        const p = this.project(point, t, 0.08);
        this.dot(p.x, p.y, this.size * 0.0028, this.colors.soft, 0.05 + ((p.z + 1) / 2) * 0.18);
      }
    }

    drawWorking(t) {
      const center = this.size / 2;
      const radius = this.size * 0.33;
      for (let orbit = 0; orbit < 3; orbit += 1) {
        const tilt = -0.9 + orbit * 0.75;
        for (let i = 0; i < 26; i += 1) {
          const angle = (i / 26) * Math.PI * 2 + t * (0.55 + orbit * 0.13);
          const rotated = rotate3(Math.cos(angle), Math.sin(angle), 0, tilt, t * 0.22);
          this.dot(center + rotated.x * radius, center + rotated.y * radius, this.size * 0.0042, orbit === 1 ? this.colors.accent : this.colors.soft, 0.25 + 0.3 * ((rotated.z + 1) / 2));
        }
      }
      for (const point of this.points) {
        const p = this.project(point, t, 0.05);
        this.dot(p.x, p.y, this.size * 0.0028, this.colors.soft, 0.04 + ((p.z + 1) / 2) * 0.12);
      }
    }

    drawSolving(t) {
      const bands = 6;
      const settle = smoothstep(0.65, 0.98, (Math.sin(t * 0.7) + 1) / 2);
      for (const point of this.points) {
        const band = Math.min(bands - 1, Math.floor(((point.y + 1) / 2) * bands));
        const scramble = (1 - settle) * 0.34 * Math.sin(t * 2.1 + band * 1.7);
        const rotated = rotate3(point.x, point.y, point.z, -0.3, t * 0.18 + scramble);
        const center = this.size / 2;
        const radius = this.size * 0.34;
        const depth = (rotated.z + 1) / 2;
        this.dot(center + rotated.x * radius, center + rotated.y * radius, this.size * 0.0038 + depth * this.size * 0.0022, band % 2 ? this.colors.soft : this.colors.accent, 0.1 + depth * 0.55);
      }
    }

    drawComposing(t) {
      const center = this.size / 2;
      const radius = this.size * 0.32;
      for (let sash = 0; sash < 3; sash += 1) {
        for (let i = 0; i < 28; i += 1) {
          const x = (i / 27) * 2 - 1;
          const wave = Math.sin(x * 4.4 + t * 2.0 + sash * 1.3) * 0.26 + (sash - 1) * 0.28;
          const z = Math.sqrt(Math.max(0, 1 - x * x - wave * wave * 0.4));
          const rotated = rotate3(x, wave, z, -0.38, t * 0.16);
          this.dot(center + rotated.x * radius, center + rotated.y * radius, this.size * 0.0044, sash === 1 ? this.colors.accent : this.colors.soft, 0.2 + ((rotated.z + 1) / 2) * 0.4);
        }
      }
    }

    drawCompleted(t, elapsed) {
      this.drawBreathing(t);
      if (elapsed > 1.5) return;
      const progress = clamp(elapsed / 1.5, 0, 1);
      const radius = this.size * (0.35 + progress * 0.12);
      this.ctx.strokeStyle = rgba(this.colors.accent, (1 - progress) * 0.5);
      this.ctx.lineWidth = Math.max(1, this.size * 0.006);
      this.ctx.beginPath();
      this.ctx.arc(this.size / 2, this.size / 2, radius, 0, Math.PI * 2);
      this.ctx.stroke();
    }

    drawError(t) {
      const center = this.size / 2;
      for (const point of this.points) {
        const p = this.project(point, t, 0);
        const depth = (p.z + 1) / 2;
        this.dot(p.x, p.y, this.size * 0.0036, this.colors.error, 0.08 + depth * 0.32);
      }
      this.ctx.strokeStyle = rgba(this.colors.error, 0.42);
      this.ctx.lineWidth = 1;
      this.ctx.beginPath();
      this.ctx.arc(center, center, this.size * 0.37, -1.9, 1.1);
      this.ctx.stroke();
      this.ctx.beginPath();
      this.ctx.arc(center, center, this.size * 0.37, 2.2, 2.75);
      this.ctx.stroke();
    }

    drawUnknown(t) {
      const center = this.size / 2;
      for (const point of this.points) {
        const p = this.project(point, t, 0);
        const depth = (p.z + 1) / 2;
        this.dot(p.x, p.y, this.size * 0.0032, this.colors.soft, 0.05 + depth * 0.2);
      }
      this.ctx.setLineDash([3, 5]);
      this.ctx.strokeStyle = rgba(this.colors.soft, 0.25);
      this.ctx.lineWidth = 0.8;
      this.ctx.beginPath();
      this.ctx.arc(center, center, this.size * 0.37, 0, Math.PI * 2);
      this.ctx.stroke();
      this.ctx.setLineDash([]);
    }

    destroy() {
      this.destroyed = true;
      instances.delete(this);
      stats.instances = instances.size;
      if (this.resizeObserver) this.resizeObserver.disconnect();
      if (this.intersectionObserver) this.intersectionObserver.disconnect();
      if (this.canvas && this.canvas.parentNode) this.canvas.parentNode.removeChild(this.canvas);
      this.canvas = null;
      this.ctx = null;
    }
  }

  function activeInstances() {
    for (const orb of instances) {
      if (!orb.destroyed && !orb.paused && orb.visible && !orb.reduced && !orb.isStatic()) return true;
    }
    return false;
  }

  function ensureLoop() {
    if (rafId != null || typeof requestAnimationFrame === "undefined") return;
    if (!activeInstances()) return;
    rafId = requestAnimationFrame(loop);
  }

  function loop(timestamp) {
    rafId = null;
    if (typeof document !== "undefined" && document.hidden) {
      return;
    }
    if (timestamp - lastFrameAt >= FRAME_MS) {
      lastFrameAt = timestamp;
      for (const orb of instances) orb.tick(timestamp);
    }
    if (activeInstances()) ensureLoop();
  }

  function create(host, options = {}) {
    if (!host) return null;
    return new ThinkingOrb(host, options);
  }

  window.NovaOrb = {
    create,
    states: ["breathing", "listening", "searching", "weaving", "shaping", "connecting", "working", "solving", "composing", "waiting", "completed", "error", "unknown"],
    stats() {
      return {
        instances: instances.size,
        frames: stats.frames,
        avgDrawMs: stats.frames ? stats.totalDrawMs / stats.frames : 0,
        maxDrawMs: stats.maxDrawMs,
        lastDrawMs: stats.lastDrawMs,
      };
    },
    destroyAll() {
      [...instances].forEach((orb) => orb.destroy());
    },
    reducedMotion,
  };
})();
