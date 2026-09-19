/* ── 3D Animated Background ────────────────────────────────── */

(function initBackground() {
  // Companion to the CSS prefers-reduced-motion block: readers who set the
  // OS reduced-motion signal should also get no JS animation, because the
  // canvas loop is motion (constant redraw, particle updates, connection
  // lines) even when CSS transitions are collapsed.
  //
  // The guard is LIVE: a MediaQueryList "change" listener reacts to the OS
  // toggle mid-session — reduced motion turning on stops the running loop
  // (rAF cancelled, canvas blanked) and turning it off restarts the same
  // loop. The boot-time check only skips STARTUP, never the policy.
  const motionQuery = window.matchMedia("(prefers-reduced-motion: reduce)");
  const canvas = document.getElementById("bgCanvas");
  if (!canvas) return;

  const ctx = canvas.getContext("2d");
  let w, h, particles = [], mouseX = -1000, mouseY = -1000;
  let rafId = null;

  function resize() {
    w = canvas.width = window.innerWidth;
    h = canvas.height = window.innerHeight;
  }
  // Size before seeding: Particle.reset() scatters within [0,w]x[0,h].
  resize();
  window.addEventListener("resize", resize);
  document.addEventListener("mousemove", (e) => { mouseX = e.clientX; mouseY = e.clientY; });

  class Particle {
    constructor() { this.reset(); }
    reset() {
      this.x = Math.random() * w;
      this.y = Math.random() * h;
      this.z = Math.random() * 200;
      this.vx = (Math.random() - 0.5) * 0.3;
      this.vy = (Math.random() - 0.5) * 0.3;
      this.vz = (Math.random() - 0.5) * 0.5;
      this.size = Math.random() * 2 + 0.5;
      this.alpha = Math.random() * 0.5 + 0.1;
      this.hue = Math.random() > 0.7 ? 280 : 185;
    }
    update() {
      this.x += this.vx;
      this.y += this.vy;
      this.z += this.vz;
      if (this.x < 0 || this.x > w) this.vx *= -1;
      if (this.y < 0 || this.y > h) this.vy *= -1;
      if (this.z < 0 || this.z > 200) this.vz *= -1;
      const dx = mouseX - this.x, dy = mouseY - this.y;
      const dist = Math.sqrt(dx * dx + dy * dy);
      if (dist < 200) { this.x -= dx * 0.001; this.y -= dy * 0.001; }
    }
    draw() {
      const scale = 1 + this.z / 400;
      const alpha = this.alpha * (1 - this.z / 300);
      ctx.beginPath();
      ctx.arc(this.x, this.y, this.size * scale, 0, Math.PI * 2);
      ctx.fillStyle = `hsla(${this.hue}, 100%, 70%, ${Math.max(0, alpha)})`;
      ctx.fill();
    }
  }

  function drawConnections() {
    for (let i = 0; i < particles.length; i++) {
      for (let j = i + 1; j < particles.length; j++) {
        const dx = particles[i].x - particles[j].x;
        const dy = particles[i].y - particles[j].y;
        const dist = Math.sqrt(dx * dx + dy * dy);
        if (dist < 150) {
          const alpha = (1 - dist / 150) * 0.15;
          ctx.beginPath();
          ctx.moveTo(particles[i].x, particles[i].y);
          ctx.lineTo(particles[j].x, particles[j].y);
          ctx.strokeStyle = `rgba(0, 240, 255, ${alpha})`;
          ctx.lineWidth = 0.5;
          ctx.stroke();
        }
      }
    }
  }

  // The loop owns its lifecycle so the change listener can start/stop it.
  // rafId doubles as the "running" flag: null = stopped.
  for (let i = 0; i < 80; i++) particles.push(new Particle());

  function animate() {
    ctx.clearRect(0, 0, w, h);
    particles.forEach((p) => { p.update(); p.draw(); });
    drawConnections();
    rafId = requestAnimationFrame(animate);
  }

  function stopLoop() {
    if (rafId !== null) { cancelAnimationFrame(rafId); rafId = null; }
    ctx.clearRect(0, 0, canvas.width, canvas.height);
  }

  function applyMotionPolicy() {
    if (motionQuery.matches) {
      stopLoop();
      return;
    }
    if (rafId === null) {
      resize();
      rafId = requestAnimationFrame(animate);
    }
  }

  if (motionQuery.matches) {
    // Reduced motion at boot: stay stopped (the pinned guard contract) but
    // keep the change listener armed below so motion can start later.
    stopLoop();
  } else {
    rafId = requestAnimationFrame(animate);
  }

  // React to live OS toggles. addEventListener on MediaQueryList is standard
  // (Safari 14+); addListener is the legacy fallback for very old Safari.
  if (typeof motionQuery.addEventListener === "function") {
    motionQuery.addEventListener("change", applyMotionPolicy);
  } else if (typeof motionQuery.addListener === "function") {
    motionQuery.addListener(applyMotionPolicy);
  }
})();

/* ── 3D Card Tilt Effect ───────────────────────────────────── */

(function initTiltEffect() {
  // Per-card tilt is motion too (the transform updates on every mousemove
  // frame). Keep it disabled under the same OS reduced-motion signal that
  // the CSS block uses, so the whole-page motion policy is consistent —
  // including LIVE: reduced motion turning on mid-session must detach the
  // tilt listeners and clear any transforms left on the cards.
  const motionQuery = window.matchMedia("(prefers-reduced-motion: reduce)");
  // `.telemetry-card` belongs in the same family: the Command Center's live
  // metric cards are cards, and they respond to the pointer like the rest.
  const CARD_SELECTOR = ".surface, .command-console, .info-card, .metric-card, .telemetry-card";

  function clearTilt() {
    document.querySelectorAll(CARD_SELECTOR).forEach((card) => {
      card.style.transform = "";
    });
  }

  function onMouseMove(e) {
    document.querySelectorAll(CARD_SELECTOR).forEach((card) => {
      const rect = card.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const y = e.clientY - rect.top;
      if (x >= -50 && x <= rect.width + 50 && y >= -50 && y <= rect.height + 50) {
        const rotateX = ((y / rect.height) - 0.5) * -4;
        const rotateY = ((x / rect.width) - 0.5) * 4;
        card.style.transform = `perspective(800px) rotateX(${rotateX}deg) rotateY(${rotateY}deg) translateZ(4px)`;
      }
    });
  }

  function bindTilt() {
    document.addEventListener("mousemove", onMouseMove);
    document.addEventListener("mouseleave", onMouseLeave, true);
  }

  function unbindTilt() {
    document.removeEventListener("mousemove", onMouseMove);
    document.removeEventListener("mouseleave", onMouseLeave, true);
  }

  function onMouseLeave() {
    clearTilt();
  }

  function applyMotionPolicy() {
    if (motionQuery.matches) {
      unbindTilt();
      clearTilt();
    } else {
      // remove-then-add keeps this idempotent across repeat toggles.
      unbindTilt();
      bindTilt();
    }
  }

  if (!motionQuery.matches) {
    bindTilt();
  }

  // Same live toggle contract as the background loop above.
  if (typeof motionQuery.addEventListener === "function") {
    motionQuery.addEventListener("change", applyMotionPolicy);
  } else if (typeof motionQuery.addListener === "function") {
    motionQuery.addListener(applyMotionPolicy);
  }
})();
