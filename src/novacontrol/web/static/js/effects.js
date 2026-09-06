/* ── 3D Animated Background ────────────────────────────────── */

(function initBackground() {
  // Companion to the CSS prefers-reduced-motion block: readers who set the
  // OS reduced-motion signal should also get no JS animation, because the
  // canvas loop is motion (constant redraw, particle updates, connection
  // lines) even when CSS transitions are collapsed.
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    return;
  }

  const canvas = document.getElementById("bgCanvas");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  let w, h, particles = [], mouseX = -1000, mouseY = -1000;

  function resize() {
    w = canvas.width = window.innerWidth;
    h = canvas.height = window.innerHeight;
  }
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

  for (let i = 0; i < 80; i++) particles.push(new Particle());

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

  function animate() {
    ctx.clearRect(0, 0, w, h);
    particles.forEach((p) => { p.update(); p.draw(); });
    drawConnections();
    requestAnimationFrame(animate);
  }
  animate();
})();

/* ── 3D Card Tilt Effect ───────────────────────────────────── */

(function initTiltEffect() {
  // Per-card tilt is motion too (the transform updates on every mousemove
  // frame). Keep it disabled under the same OS reduced-motion signal that
  // the CSS block uses, so the whole-page motion policy is consistent.
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    return;
  }

  document.addEventListener("mousemove", (e) => {
    document.querySelectorAll(".surface, .command-console, .info-card, .metric-card").forEach((card) => {
      const rect = card.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const y = e.clientY - rect.top;
      if (x >= -50 && x <= rect.width + 50 && y >= -50 && y <= rect.height + 50) {
        const rotateX = ((y / rect.height) - 0.5) * -4;
        const rotateY = ((x / rect.width) - 0.5) * 4;
        card.style.transform = `perspective(800px) rotateX(${rotateX}deg) rotateY(${rotateY}deg) translateZ(4px)`;
      }
    });
  });

  document.addEventListener("mouseleave", () => {
    document.querySelectorAll(".surface, .command-console, .info-card, .metric-card").forEach((card) => {
      card.style.transform = "";
    });
  }, true);
})();
