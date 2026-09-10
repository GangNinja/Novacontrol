/* Routing Explorer — trace an utterance through NovaBrain's intent gates.
 *
 * Live mode POSTs to /brain/decide while the server is up and renders the
 * full gate trace plus a preview of what the user would actually see (scratch
 * answer text, research headline, or plan outline). When the server is
 * unreachable, an EMBEDDED MIRROR of the routing gates takes over so the
 * explorer still answers — it is approximate (a hand-kept JS copy of the
 * Python gate table) and is labelled as such in the UI.
 */
(function () {
  "use strict";

  function byId(id) { return document.getElementById(id); }

  function contains(text) {
    for (var i = 1; i < arguments.length; i++) {
      if (text.indexOf(arguments[i]) !== -1) return true;
    }
    return false;
  }

  /* ── Embedded mirror of the routing predicates ─────────────── */

  var MIRROR_GREETINGS = ["hi", "hello", "hey", "hai", "good morning", "good afternoon", "good evening"];

  function mirrorScratchable(lower) {
    // The real scratch brain matches greetings on the WHOLE normalized text
    // ("hello" yes, "open notepad and type hello" no), so the mirror must too.
    var cleaned = lower.trim().replace(/\s+/g, " ");
    if (MIRROR_GREETINGS.indexOf(cleaned) !== -1) return "greeting";
    // Worded math: an operator word + a digit (approximate — the real scratch
    // brain parses spelled-out numbers too).
    if (/\d/.test(lower) && contains(lower, "plus", "minus", "times", "divided by", "percent of", "squared", "cubed", "sqrt", "square root", "+", "-", "*", "/")) {
      return "math";
    }
    return null;
  }

  function mirrorPhoneCommand(lower) {
    if (contains(lower, "phone", "android", "mobile", "sms")) {
      return contains(lower, "open ", "launch ", "search", "send", "call ", "text ", "message", "screenshot", "dial ");
    }
    if (lower.indexOf("whatsapp") !== -1) return true;
    return lower.indexOf("call ") === 0 || lower.indexOf("dial ") === 0 ||
      lower.indexOf("text ") === 0 || lower.indexOf("sms ") === 0 ||
      contains(lower, "send a text", "send text", "take a screenshot", "take screenshot");
  }

  function mirrorWebSearch(lower) {
    return contains(lower, "google ", "search the web", "web search", "search for ") && !mirrorPhoneCommand(lower);
  }

  function mirrorResearch(lower) {
    return contains(lower, "what is", "what are", "how does", "how do", "why is", "explain", "research", "tell me about");
  }

  function mirrorDesktop(lower) {
    return contains(lower, "open ", "launch ", "close ", "type ", "press ", "click ", "open folder", "open file", "open the folder", "go to", "navigate to") && !mirrorPhoneCommand(lower);
  }

  var MIRROR_GATES = [
    { gate: "scratch_greeting", intent: "chat", confidence: 0.9, reason: "Request is a simple chat message.",
      pred: function (lower, s) { return s === "greeting"; } },
    { gate: "self_improvement", intent: "self_improvement", confidence: 0.9, reason: "Request asks NovaControl to inspect and improve its own code.",
      pred: function (lower, s) { return contains(lower, "self improve", "self-improve", "improve yourself", "improve itself", "code itself", "code yourself", "upgrade yourself", "improve your code", "make it intelligent", "make yourself intelligent"); } },
    { gate: "scratch_math", intent: "chat", confidence: 0.88, reason: "Worded arithmetic is computed locally by the scratch brain.",
      pred: function (lower, s) { return s === "math"; } },
    { gate: "phone_anchored_action", intent: "phone_control", confidence: 0.84, reason: "Request asks for phone control.",
      pred: function (lower, s) { return mirrorPhoneCommand(lower) && contains(lower, "phone", "android", "mobile") && contains(lower, "open ", "launch ", "search", "find ", "look up", "look for"); } },
    { gate: "web_search", intent: "browser_automation", confidence: 0.82, reason: "Request asks to search the web in a browser.",
      pred: function (lower, s) { return mirrorWebSearch(lower); } },
    { gate: "research_question", intent: "explore", confidence: 0.86, reason: "Request asks for explanation or research.",
      pred: function (lower, s) { return mirrorResearch(lower) && s === null; } },
    { gate: "plan", intent: "plan", confidence: 0.84, reason: "Request asks for planning.",
      pred: function (lower, s) { return contains(lower, "roadmap", "milestone", "break down", "steps") || /\bplan\b/.test(lower); } },
    { gate: "phone_command", intent: "phone_control", confidence: 0.82, reason: "Request asks for phone control.",
      pred: function (lower, s) { return mirrorPhoneCommand(lower); } },
    { gate: "memory_store", intent: "memory", confidence: 0.85, reason: "Request asks to store something in memory.",
      pred: function (lower, s) { return contains(lower, "remember this", "remember that", "remember for me"); } },
    { gate: "desktop_command", intent: "desktop_automation", confidence: 0.8, reason: "Request asks for desktop automation.",
      pred: function (lower, s) { return mirrorDesktop(lower); } },
    { gate: "browser", intent: "browser_automation", confidence: 0.8, reason: "Request asks for browser automation.",
      pred: function (lower, s) { return contains(lower, "browser", "website", "navigate", "fill form", "fill the form", "download"); } },
    { gate: "memory", intent: "memory", confidence: 0.72, reason: "Request refers to memory.",
      pred: function (lower, s) { return contains(lower, "remember", "recall", "memory"); } },
    { gate: "agent", intent: "agent", confidence: 0.78, reason: "Request should be delegated to a specialized agent.",
      pred: function (lower, s) { return contains(lower, "code", "test", "debug", "review", "implement", "fix", "document"); } },
    { gate: "project", intent: "project", confidence: 0.72, reason: "Request refers to project management.",
      pred: function (lower, s) { return contains(lower, "project", "task", "bug", "progress"); } },
    { gate: "chat_fallback", intent: "chat", confidence: 0.55, reason: "Fallback to model-backed chat.",
      pred: function (lower, s) { return true; } }
  ];

  // The mirror cannot run the Python scratch engine, so its preview is a
  // phrase-level stand-in; the live server returns the real answer.
  function mirrorPreview(text, intent) {
    var lower = text.toLowerCase();
    var s = mirrorScratchable(lower);
    if (s) {
      return { kind: "scratch", text: "(mirror) The scratch brain answers this locally — start the server for the exact answer." };
    }
    if (intent === "explore") {
      return { kind: "explore", headline: "Research on " + text + ": sources analyzed with key findings below.", note: "Full research adds sources, key points, and a detailed explanation." };
    }
    if (intent === "plan") {
      return { kind: "plan", outline: ["Understand request", "Execute task", "Verify result"], needs_clarification: false };
    }
    if (intent === "desktop_automation" || intent === "phone_control" || intent === "browser_automation") {
      return { kind: "plan", outline: ["planned action"], target: "", summary: "(mirror) The live server would show the real plan with an Approve And Run button." };
    }
    if (intent === "chat") {
      return { kind: "info", text: "Answered by the configured chat model." };
    }
    return { kind: "info", text: "Routed to " + intent + ". Open that panel to run it." };
  }

  function mirrorTrace(text) {
    var lower = text.trim().toLowerCase();
    var s = mirrorScratchable(lower);
    var trace = [];
    var landing = null;
    for (var i = 0; i < MIRROR_GATES.length; i++) {
      var g = MIRROR_GATES[i];
      var matched = !!g.pred(lower, s);
      trace.push({
        gate: g.gate, matched: matched,
        intent: matched ? g.intent : null,
        confidence: matched ? g.confidence : null,
        reason: matched ? g.reason : null
      });
      if (matched) { landing = g; break; }
    }
    if (!landing) landing = MIRROR_GATES[MIRROR_GATES.length - 1];
    return {
      intent: landing.intent, confidence: landing.confidence, reason: landing.reason,
      trace: trace, source: "mirror", preview: mirrorPreview(text, landing.intent)
    };
  }

  /* ── Rendering ─────────────────────────────────────────────── */

  function esc(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function renderTrace(result, source, text) {
    var out = byId("routingOutput");
    out.classList.remove("empty-state");
    out.classList.add("routing-result");

    var sourceLabel = source === "live"
      ? "Traced by NovaBrain (live server)"
      : "Traced by the embedded mirror (server unreachable — approximate)";
    byId("routingSource").textContent = sourceLabel;

    var html = "";
    html += '<div class="routing-landing">';
    html += '<span class="routing-label">Landing intent</span>';
    html += '<span class="routing-intent">' + esc(result.intent) + '</span>';
    html += '<span class="routing-conf">' + Math.round((result.confidence || 0) * 100) + '% confident</span>';
    html += '</div>';

    html += '<ol class="routing-gates">';
    result.trace.forEach(function (row) {
      var cls = row.matched ? "matched" : "skipped";
      html += '<li class="routing-gate ' + cls + '">';
      html += '<span class="routing-gate-mark">' + (row.matched ? "&#10003;" : "&#10007;") + '</span>';
      html += '<span class="routing-gate-name">' + esc(row.gate) + '</span>';
      if (row.matched) {
        html += '<span class="routing-gate-intent">' + esc(row.intent) + '</span>';
        html += '<span class="routing-gate-reason">' + esc(row.reason || "") + '</span>';
      }
      html += '</li>';
    });
    html += '</ol>';

    var pv = result.preview || {};
    html += '<div class="routing-preview">';
    html += '<div class="routing-preview-head">Preview of what you would see</div>';
    if (pv.kind === "scratch") {
      html += '<blockquote class="routing-preview-body">' + esc(pv.text) + '</blockquote>';
    } else if (pv.kind === "explore") {
      html += '<div class="routing-preview-body"><strong>Report headline:</strong> ' + esc(pv.headline) + '</div>';
      if (pv.note) html += '<div class="routing-preview-note">' + esc(pv.note) + '</div>';
    } else if (pv.kind === "plan") {
      html += '<ol class="routing-outline">';
      (pv.outline || []).forEach(function (step) { html += "<li>" + esc(step) + "</li>"; });
      html += '</ol>';
      if (pv.summary) html += '<div class="routing-preview-note">' + esc(pv.summary) + '</div>';
    } else {
      html += '<div class="routing-preview-body">' + esc(pv.text || "") + '</div>';
    }
    html += '</div>';

    html += '<div class="routing-utterance">"' + esc(text) + '"</div>';
    out.innerHTML = html;
  }

  function traceUtterance() {
    var text = byId("routingInput").value.trim();
    if (!text) { showToast("Type an utterance first"); return; }
    var button = byId("routingTraceButton");
    var original = button.textContent;
    button.disabled = true;
    button.textContent = "Tracing…";

    requestJson("/brain/decide", { text: text })
      .then(function (data) {
        renderTrace(data, "live", text);
      })
      .catch(function () {
        // Server unreachable (or erroring): the embedded mirror takes over.
        renderTrace(mirrorTrace(text), "mirror", text);
        showToast("Server unreachable — used the embedded routing mirror");
      })
      .finally(function () {
        button.disabled = false;
        button.textContent = original;
      });
  }

  function setupRoutingExplorer() {
    var input = byId("routingInput");
    if (!input) return;
    byId("routingTraceButton").addEventListener("click", traceUtterance);
    input.addEventListener("keydown", function (event) {
      if (event.key === "Enter") traceUtterance();
    });
  }

  if (typeof window.setupRoutingExplorer !== "function") {
    window.setupRoutingExplorer = setupRoutingExplorer;
  }
  // Also expose the pure mirror for tests and for the offline fallback path.
  window.routingExplorerMirror = { mirrorTrace: mirrorTrace, mirrorPreview: mirrorPreview };
})();