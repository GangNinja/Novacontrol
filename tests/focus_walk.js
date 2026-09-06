// Focus-ring walker for tests/test_focus_rings.py.
// Usage: node focus_walk.js <pageWebSocketDebuggerUrl> <appUrl>
//
// Drives a headless Chromium (Edge/Chrome) over CDP with trusted Tab key
// events — genuine keyboard modality — walking every focusable element in every
// panel and asserting :focus-visible matches with a 2px solid outline on each.
// Prints one JSON line: { ok, total, failures: [{tag, cls, text, fv,
// outlineStyle, outlineWidth}] } and exits non-zero when any element fails.

const wsUrl = process.argv[2];
const appUrl = process.argv[3];

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

class CDP {
  constructor(url) {
    this.ws = new WebSocket(url);
    this.id = 0;
    this.pending = new Map();
  }

  async open() {
    if (this.ws.readyState === WebSocket.OPEN) return;
    await new Promise((resolve, reject) => {
      this.ws.onopen = resolve;
      this.ws.onerror = () => reject(new Error("websocket connect failed"));
    });
    this.ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.id && this.pending.has(msg.id)) {
        const { resolve, reject } = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        msg.error ? reject(new Error(JSON.stringify(msg.error))) : resolve(msg.result);
      }
    };
  }

  send(method, params = {}) {
    const id = ++this.id;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }
}

const TAB_DOWN = {
  type: "keyDown", key: "Tab", code: "Tab",
  windowsVirtualKeyCode: 9, nativeVirtualKeyCode: 9,
};
const TAB_UP = {
  type: "keyUp", key: "Tab", code: "Tab",
  windowsVirtualKeyCode: 9, nativeVirtualKeyCode: 9,
};

const PROBE = `(() => {
  const el = document.activeElement;
  if (!el) return { tag: null };
  const cs = getComputedStyle(el);
  const r = el.getBoundingClientRect();
  return {
    tag: el.tagName,
    cls: typeof el.className === "string" ? el.className : "",
    text: (el.textContent || "").trim().slice(0, 40),
    visible: r.width > 0 && r.height > 0,
    index: Array.prototype.indexOf.call(document.querySelectorAll("*"), el),
    fv: el.matches(":focus-visible"),
    outlineStyle: cs.outlineStyle,
    outlineWidth: cs.outlineWidth,
    outer: el.outerHTML.slice(0, 80),
  };
})()`;

async function main() {
  const cdp = new CDP(wsUrl);
  await cdp.open();
  await cdp.send("Page.enable");
  await cdp.send("Runtime.enable");
  await cdp.send("Page.navigate", { url: appUrl });
  await cdp.send("Page.bringToFront"); // headless windows need explicit focus for Tab traversal

  let loaded = false;
  for (let i = 0; i < 80; i++) {
    await sleep(250);
    const res = await cdp.send("Runtime.evaluate", {
      expression:
        "document.readyState === 'complete' && !!document.querySelector('.nav-item') && !document.getElementById('statusText').textContent.includes('Initializing')",
      returnByValue: true,
    });
    if (res.result.value) { loaded = true; break; }
  }
  if (!loaded) throw new Error("app did not finish loading");

  const failures = [];
  let total = 0;

  // Headless windows sometimes attach without input focus; Tab traversal is
  // browser-level and silently no-ops then. Re-raise the window until the page
  // reports document.hasFocus(), which is what makes trusted key events land.
  async function ensureFocused() {
    for (let i = 0; i < 6; i++) {
      await cdp.send("Page.bringToFront");
      await sleep(120);
      const res = await cdp.send("Runtime.evaluate", {
        expression: "document.hasFocus()",
        returnByValue: true,
      });
      if (res.result.value === true) return true;
    }
    return false;
  }

  async function tabWalk() {
    let firstIndex = null;
    let hops = 0;
    let prev = null;
    let stuck = 0;
    while (hops < 400) {
      hops += 1;
      await cdp.send("Input.dispatchKeyEvent", TAB_DOWN);
      await cdp.send("Input.dispatchKeyEvent", TAB_UP);
      await sleep(25);
      const res = await cdp.send("Runtime.evaluate", { expression: PROBE, returnByValue: true });
      const el = res.result.value;
      if (!el || el.tag === null) break;
      if (el.tag === "BODY" || el.tag === "HTML") {
        if (firstIndex === null) {
          // focus has not moved yet — either the window lost input focus or the
          // tab cycle genuinely wraps to body before the first element is seen.
          stuck += 1;
          if (stuck > 4) {
            if (!(await ensureFocused())) {
              throw new Error("Tab key events never moved focus: page has no input focus");
            }
            stuck = 0;
            await cdp.send("Runtime.evaluate", {
              expression: "document.activeElement && document.activeElement.blur && document.activeElement.blur()",
            });
          }
          continue;
        }
        break; // focus wrapped back to the document body — walk complete
      }
      if (firstIndex === null) {
        firstIndex = el.index;
      } else if (el.index === firstIndex) {
        break; // focus wrapped around to the first element — walk complete
      }
      if (!el.visible) continue;
      total += 1;
      if (!el.fv || el.outlineStyle !== "solid" || el.outlineWidth !== "2px") {
        failures.push({
          hop: hops, tag: el.tag, cls: el.cls, text: el.text, outer: el.outer,
          prev: prev ? `${prev.tag}.${prev.cls || "-"} (${prev.text.slice(0, 20)})` : null,
          focusVisible: el.fv, outline: `${el.outlineStyle} ${el.outlineWidth}`,
        });
      }
      prev = el;
    }
  }

  if (!(await ensureFocused())) {
    throw new Error("page never reported document.hasFocus()");
  }
  await cdp.send("Runtime.evaluate", {
    expression: "document.activeElement && document.activeElement.blur && document.activeElement.blur()",
  });
  await tabWalk();

  const navRes = await cdp.send("Runtime.evaluate", {
    expression: "Array.from(document.querySelectorAll('.nav-item')).map(b => b.dataset.panel)",
    returnByValue: true,
  });
  for (const panel of navRes.result.value || []) {
    await cdp.send("Runtime.evaluate", {
      expression: `document.querySelector('.nav-item[data-panel="${panel}"]').click()`,
    });
    await sleep(250);
    await cdp.send("Runtime.evaluate", {
      expression: "document.activeElement && document.activeElement.blur && document.activeElement.blur()",
    });
    await tabWalk();
  }

  try { await cdp.send("Browser.close"); } catch (_) { /* python kills the process */ }
  console.log(JSON.stringify({ ok: failures.length === 0, total, failures }));
  process.exit(failures.length === 0 ? 0 : 1);
}

main().catch((err) => {
  console.error(String(err));
  process.exit(2);
});