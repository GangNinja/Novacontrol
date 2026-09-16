/* ── DOM Utilities ────────────────────────────────────────── */

const byId = (id) => document.getElementById(id);

function clearNode(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function label(value) {
  return String(value)
    .replace(/[_-]/g, " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function showToast(message) {
  let toast = document.querySelector(".action-toast");
  if (!toast) {
    toast = el("div", "action-toast");
    toast.setAttribute("role", "status");
    toast.setAttribute("aria-live", "polite");
    document.body.appendChild(toast);
  }
  toast.textContent = message;
  toast.classList.add("show");
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => toast.classList.remove("show"), 2500);
}

// Toast with a timed action button ("Undo"): the toast stays up for the whole
// window (not the default 2.5s), the countdown shows in the button, and the
// action can only fire once. A new toast (plain or actioned) supersedes an
// older pending action — its onExpire still runs so callers can react.
function showToastWithAction({ message, actionLabel = "Undo", seconds = 30, onAction, onExpire }) {
  const toast = document.querySelector(".action-toast") || (() => {
    const node = el("div", "action-toast");
    node.setAttribute("role", "status");
    node.setAttribute("aria-live", "polite");
    document.body.appendChild(node);
    return node;
  })();
  const prior = toast._pendingAction;
  if (prior) prior.settle("superseded");

  toast.textContent = message;
  const action = el("button", "action-toast-btn", `${actionLabel} (${seconds}s)`);
  action.type = "button";
  toast.appendChild(action);
  toast.classList.add("show", "has-action");

  let done = false;
  let countdown = seconds;
  const tick = setInterval(() => {
    countdown -= 1;
    if (countdown > 0) action.textContent = `${actionLabel} (${countdown}s)`;
  }, 1000);
  const timer = setTimeout(() => finish("expired"), seconds * 1000);

  function finish(reason) {
    if (done) return;
    done = true;
    clearInterval(tick);
    clearTimeout(timer);
    toast.classList.remove("has-action");
    action.remove();
    toast._pendingAction = null;
    if (reason === "action") {
      toast.classList.remove("show");
      if (onAction) onAction();
    } else {
      toast._timer = setTimeout(() => toast.classList.remove("show"), 2500);
      if (reason === "expired" && onExpire) onExpire();
    }
  }

  action.addEventListener("click", () => finish("action"));
  toast._pendingAction = {
    settle(reason) {
      // Only expiry fires the caller's onExpire; superseded actions just die.
      if (done) return;
      done = true;
      clearInterval(tick);
      clearTimeout(timer);
      action.remove();
      if (reason === "expired" && onExpire) onExpire();
    },
  };
  toast._pendingAction.finish = finish;
}

function setButtonLoading(buttonId, loading) {
  const button = byId(buttonId);
  if (!button) return;
  if (loading) {
    button.classList.add("loading");
    button.dataset._text = button.textContent;
    button.textContent = "Working";
  } else {
    button.classList.remove("loading");
    if (button.dataset._text) button.textContent = button.dataset._text;
  }
}

function domainLabel(url) {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return "source";
  }
}

function sourceLabel(item, type) {
  if (type === "YouTube") {
    return item.channel ? `${item.channel} · YouTube` : "YouTube";
  }
  const domain = domainLabel(item.url || "");
  if (domain !== "source") return domain;
  return item.source_name || item.site || label(type || "Source");
}

function faviconUrl(item, type) {
  const fallback = type === "YouTube" ? "https://www.youtube.com/" : item.url || "";
  try {
    const host = new URL(fallback).hostname.replace(/^www\./, "");
    return `https://www.google.com/s2/favicons?domain=${encodeURIComponent(host)}&sz=32`;
  } catch {
    return "";
  }
}

/* ── Lightweight Markdown Renderer ──────────────────────── */

function renderMd(text) {
  if (!text) return document.createTextNode("");
  const frag = document.createDocumentFragment();
  const lines = String(text).split("\n");
  let inList = false;

  lines.forEach((line) => {
    const trimmed = line.trim();

    // Bullet points: "• " or "- " or "* " at start of line
    const bulletMatch = trimmed.match(/^[•\-\*]\s+(.*)/);
    if (bulletMatch) {
      if (!inList) {
        inList = true;
        const ul = document.createElement("ul");
        ul.className = "md-list";
        frag.appendChild(ul);
      }
      const li = document.createElement("li");
      _renderInline(li, bulletMatch[1]);
      frag.lastChild.appendChild(li);
      return;
    }

    // Numbered list: "1. " etc
    const numMatch = trimmed.match(/^\d+\.\s+(.*)/);
    if (numMatch) {
      if (!inList) {
        inList = true;
        const ol = document.createElement("ol");
        ol.className = "md-list";
        frag.appendChild(ol);
      }
      const li = document.createElement("li");
      _renderInline(li, numMatch[1]);
      frag.lastChild.appendChild(li);
      return;
    }

    // End list block if we were in one
    if (inList) inList = false;

    // Empty line → paragraph break
    if (!trimmed) {
      frag.appendChild(document.createElement("br"));
      return;
    }

    // Regular paragraph line
    const p = document.createElement("p");
    _renderInline(p, trimmed);
    frag.appendChild(p);
  });

  return frag;
}

/** Render inline Markdown: **bold**, *italic*, `code` */
function _renderInline(parent, text) {
  // Bold content may contain single asterisks (e.g. "**15*3**") but not a nested "**".
  const parts = text.split(/(\*\*(?:(?!\*\*).)+?\*\*|\*[^*]+\*|`[^`]+`)/g);
  parts.forEach((part) => {
    if (part.startsWith("**") && part.endsWith("**")) {
      const strong = document.createElement("strong");
      strong.textContent = part.slice(2, -2);
      parent.appendChild(strong);
    } else if (part.startsWith("*") && part.endsWith("*") && !part.startsWith("**")) {
      const em = document.createElement("em");
      em.textContent = part.slice(1, -1);
      parent.appendChild(em);
    } else if (part.startsWith("`") && part.endsWith("`")) {
      const code = document.createElement("code");
      code.textContent = part.slice(1, -1);
      parent.appendChild(code);
    } else {
      parent.appendChild(document.createTextNode(part));
    }
  });
}
