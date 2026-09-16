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

  // Exact keys of novacontrol.brain.scratch._KNOWLEDGE — kept in sync by
  // tests/test_math_parity.py, which regenerates this list from Python and
  // fails the build on drift.
  var MIRROR_KNOWLEDGE_KEYS = [
    "how many planets", "speed of light", "speed of sound",
    "boiling point of water", "freezing point of water", "distance to the moon",
    "distance to the sun", "age of the earth", "age of the universe",
    "diameter of earth", "gravity on earth", "avogadro's number",
    "speed of internet", "who invented the lightbulb", "who invented the telephone",
    "who invented the internet", "who invented the airplane", "capital of france",
    "capital of japan", "capital of india", "capital of china",
    "capital of germany", "capital of united states", "capital of usa",
    "capital of uk", "capital of england", "capital of brazil",
    "capital of australia", "capital of canada", "capital of russia",
    "largest country", "most populated country", "longest river",
    "tallest mountain", "what is dna", "what is evolution",
    "what is gravity", "what is photosynthesis", "what is climate change",
    "what is ai", "what is machine learning", "what is quantum computing",
    "what is blockchain", "what is html", "what is pi",
    "what is euler's number", "what is the pythagorean theorem", "what is a prime number",
  ];;

  function mirrorScratchable(lower) {
    // The real scratch brain matches greetings on the WHOLE normalized text
    // ("hello" yes, "open notepad and type hello" no), so the mirror must too.
    var cleaned = lower.trim().replace(/\s+/g, " ");
    if (MIRROR_GREETINGS.indexOf(cleaned) !== -1) return "greeting";
    // Worded math: an operator word + a digit, or a unary row ('half of',
    // 'double', the fraction family) over a digit OR a spelled-out number —
    // mirroring the registry rows and _ADD_FORM grammar ('add five and seven'
    // counts too). Scale words (million/billion/trillion) count as number
    // words, so '3 million times 2' and 'half of three million' land here.
    var NUM_WORD = "\\d+|zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|million|billion|trillion";
    var NUM_SEQ = NUM_WORD + "(?:[\\s-]+(?:and[\\s-]+)?(?:" + NUM_WORD + "))*";
    if (/\d/.test(lower) && contains(lower, "plus", "minus", "times", "divided by", "multiplied by", "percent of", "squared", "cubed", "sqrt", "square root", "to the power of", "over", "+", "-", "*", "/")) {
      return "math";
    }
    // Worded binary over spelled-out operands ('three billion minus seven',
    // 'three million times two') — the digit gate above can't see these.
    if (new RegExp("\\b(?:" + NUM_SEQ + ")\\s+(?:plus|minus|times|over|multiplied by|divided by)\\s+(?:" + NUM_SEQ + ")\\b").test(lower)) {
      return "math";
    }
    // Unary rows: 'double 7', 'half of 10', 'one quarter of 8', 'a third of 90',
    // 'three quarters of 200' — article or spelled numerator, plural denominators.
    if (new RegExp("\\bdouble\\s+(?:" + NUM_SEQ + ")\\b").test(lower)) {
      return "math";
    }
    if (new RegExp("\\b(?:(a|an|" + NUM_SEQ + ")\\s+)?(?:half|halves|third|thirds|quarter|quarters|fourth|fourths|fifth|fifths|sixth|sixths|seventh|sevenths|eighth|eighths|ninth|ninths|tenth|tenths)\\s+of\\s+(?:" + NUM_SEQ + ")\\b").test(lower)) {
      return "math";
    }
    if (new RegExp("\\badd\\s+(?:" + NUM_SEQ + ")\\b").test(lower) && /\band\b/.test(lower)) {
      return "math";
    }
    // Other narrow (routing_safe) canned kinds, so match/breadth relations
    // stay honest in the mirror too.
    if (contains(lower, "what time", "current time", "the time now", "whats the time", "what's the time", "time in ") && /\d/.test(lower)) {
      return "time";
    }
    if (contains(lower, "convert ", " km", "km to", "miles", "celsius", "fahrenheit", "kg to", "pounds", "cm to", "inches", "usd", "euros", " inr")) {
      return "conversion";
    }
    // Narrow knowledge gate mirrors _knowledge_routing: an exact knowledge-base
    // key is the only thing that counts as a canned local answer. MIRROR_KNOWLEDGE
    // KEYS is pinned against the Python dict by tests/test_math_parity.py.
    for (var k = 0; k < MIRROR_KNOWLEDGE_KEYS.length; k++) {
      if (lower.indexOf(MIRROR_KNOWLEDGE_KEYS[k]) !== -1) return "knowledge";
    }
    if (contains(lower, "recommend", "suggest")) {
      return "recommendation";
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
    return contains(
      lower,
      "what is", "what are", "how does", "how do", "how to", "why is", "explain", "research", "tell me about",
      // Casual scaffold phrasing (kept in sync with brain.looks_like_research_question).
      "things to know about", "should i know about", "need to know about",
      "the deal with", "stuff about", "facts about", "info on",
      "information about", "basics of", "gist of", "lowdown on", "scoop on",
      "wtf is", "wth is", "what the heck is", "what the hell is",
    );
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
    { gate: "memory_store", intent: "memory", confidence: 0.85, reason: "Request asks to store something in memory.",
      // Explicit store phrasing sits ABOVE the plan/phone/desktop/browser
      // blocks: content words inside the remembered text ("remember this:
      // create a roadmap for the project") must never steal the routing.
      pred: function (lower, s) { return contains(lower, "remember this", "remember that", "remember for me"); } },
    { gate: "plan", intent: "plan", confidence: 0.84, reason: "Request asks for planning.",
      pred: function (lower, s) { return contains(lower, "roadmap", "milestone", "break down", "steps") || /\bplan\b/.test(lower); } },
    { gate: "phone_command", intent: "phone_control", confidence: 0.82, reason: "Request asks for phone control.",
      pred: function (lower, s) { return mirrorPhoneCommand(lower); } },
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

  /* ── Embedded mirror of the broad _classify() engine ──────────
   *
   * Approximates the scratch engine's historical row order (greeting →
   * phone → desktop → capabilities → time → conversion → math → knowledge →
   * recommendation) so the explorer can show the narrow-vs-broad comparison
   * offline too. Approximate by design: the real engine parses spelled-out
   * numbers and regex tables; the mirror uses phrase heuristics.
   */

  function mirrorClassifyBroad(lower) {
    var cleaned = lower.trim().replace(/\s+/g, " ");
    // Engine order mirrors _ENGINE_ORDER in scratch.py.
    if (MIRROR_GREETINGS.indexOf(cleaned) !== -1) return "greeting";
    if (mirrorPhoneCommand(lower)) return "phone_control";
    if (mirrorDesktop(lower)) return "desktop_control";
    if (contains(lower, "what can you do", "your capabilities", "who are you", "what are you")) return "capabilities";
    if (contains(lower, "what time", "current time", "the time now", "whats the time", "what's the time", "time in ")) return "time";
    if (contains(lower, " km", "km to", "miles", "celsius", "fahrenheit", "kg to", "pounds", "cm to", "inches", "usd", "euros", " inr", " minutes to ", " hours to ", "days to ")) return "conversion";
    if (mirrorScratchable(lower) === "math") return "math";
    if (contains(lower, "what is", "what are", "who is", "who was", "how many", "capital of", "invented ", "president of", "population of")) return "knowledge";
    if (contains(lower, "recommend", "suggest", "what should i", "movie", "book", "food", "eat", "watch")) return "recommendation";
    return "unknown";
  }

  // JS port of brain.py's _classifier_relation.
  function mirrorClassifierRelation(narrow, broad) {
    if (narrow !== null && narrow === broad) {
      return { narrow: narrow, broad: broad, relation: "match",
               note: "Both classifiers land on the same intent." };
    }
    if (narrow === null && (broad === "phone_control" || broad === "desktop_control" || broad === "capabilities")) {
      return { narrow: narrow, broad: broad, relation: "engine_only",
               note: "The broad engine sees " + broad + ", but its row is routing_safe=False: the narrow router deliberately hides it so the request is dispatched to real device automation instead of a canned local answer." };
    }
    if (narrow === null && broad === "unknown") {
      return { narrow: narrow, broad: broad, relation: "unknown",
               note: "Neither classifier has a local answer — the request falls through to research or the configured model." };
    }
    if (narrow === null) {
      return { narrow: narrow, broad: broad, relation: "breadth",
               note: "The broad engine matches " + broad + " with its wide detect, but the narrow router requires an exact canned key, so routing deliberately sends it onward (usually Explore) instead of answering locally." };
    }
    return { narrow: narrow, broad: broad, relation: "order",
             note: "The narrow router promotes " + narrow + " ahead of time/conversion, while the broad engine keeps its historical order and lands on " + broad + ". Routing intentionally uses the narrow order." };
  }

  function mirrorTrace(text) {
    var lower = text.trim().toLowerCase();
    var s = mirrorScratchable(lower);
    var broad = mirrorClassifyBroad(lower);
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
      trace: trace, source: "mirror", preview: mirrorPreview(text, landing.intent),
      classifiers: mirrorClassifierRelation(s, broad)
    };
  }

  /* ── Rendering ─────────────────────────────────────────────── */

  function esc(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function renderClassifiers(cls) {
    if (!cls) return "";
    var relation = cls.relation || "unknown";
    var disagree = relation !== "match";
    var labels = { match: "Agree", order: "Order differs", engine_only: "Engine-only", breadth: "Breadth", unknown: "Neither" };
    var label = labels[relation] || relation;
    var html = "";
    html += '<div class="routing-classifiers' + (disagree ? " disagree" : "") + '">';
    html += '<div class="routing-preview-head">Broad vs narrow classifiers <span class="routing-rel-badge ' + esc(relation) + '">' + esc(label) + '</span></div>';
    html += '<div class="routing-cmp">';
    html += '<span class="routing-cmp-cell"><span class="routing-cmp-label">narrow (gate)</span><span class="routing-cmp-val">' + esc(cls.narrow || "—") + '</span></span>';
    html += '<span class="routing-cmp-vs">vs</span>';
    html += '<span class="routing-cmp-cell"><span class="routing-cmp-label">broad (engine)</span><span class="routing-cmp-val">' + esc(cls.broad || "unknown") + '</span></span>';
    html += '</div>';
    html += '<div class="routing-preview-note">' + esc(cls.note || "") + '</div>';
    html += '</div>';
    return html;
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

    if (byId("routingBroadToggle") && byId("routingBroadToggle").checked) {
      html += renderClassifiers(result.classifiers);
    }

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

  var lastTrace = null;  // { result, source, text } so the toggle can re-render

  function traceUtterance() {
    var text = byId("routingInput").value.trim();
    if (!text) { showToast("Type an utterance first"); return; }
    var button = byId("routingTraceButton");
    var original = button.textContent;
    button.disabled = true;
    button.textContent = "Tracing…";

    requestJson("/brain/decide", { text: text })
      .then(function (data) {
        lastTrace = { result: data, source: "live", text: text };
        renderTrace(data, "live", text);
      })
      .catch(function () {
        // Server unreachable (or erroring): the embedded mirror takes over.
        var mirrored = mirrorTrace(text);
        lastTrace = { result: mirrored, source: "mirror", text: text };
        renderTrace(mirrored, "mirror", text);
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
    var toggle = byId("routingBroadToggle");
    if (toggle) {
      toggle.addEventListener("change", function () {
        // Re-render the cached trace so the comparison appears/disappears
        // without another round trip; re-trace when the input changed.
        if (lastTrace) {
          renderTrace(lastTrace.result, lastTrace.source, lastTrace.text);
        } else {
          traceUtterance();
        }
      });
    }
  }

  if (typeof window.setupRoutingExplorer !== "function") {
    window.setupRoutingExplorer = setupRoutingExplorer;
  }
  // Also expose the pure mirror for tests and for the offline fallback path.
  window.routingExplorerMirror = {
    mirrorTrace: mirrorTrace,
    mirrorPreview: mirrorPreview,
    mirrorClassifyBroad: mirrorClassifyBroad,
    mirrorClassifierRelation: mirrorClassifierRelation
  };
})();