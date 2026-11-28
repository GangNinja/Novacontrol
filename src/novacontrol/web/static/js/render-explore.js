/* ── Explore / Research Rendering ───────────────────────────── */

/** Render a research report into `target`. Returns false when `data` has no report shape.
 *
 * Callers pass one unwrapped body only: /ask consumers hand over `envelope.data`
 * (the handler payload) and /explore passes the flat report, so there is no
 * envelope-vs-flat probe to do here — a report simply is an object with report
 * fields.
 */
function tryRenderResearch(target, data) {
  if (!data || !(data.overview || data.key_points || data.sources || data.videos)) return false;
  renderAiAnswerPage(target, data, state.lastQuery || data.topic || "Research");
  return true;
}

/** Shape probe only: true when `data` looks like a research report.
 *
 * renderChatResult uses this to route /ask research payloads to the chat
 * bubble renderer without rendering the full Explore page inside the
 * conversation; renderExplore keeps the (target, data) page-render form.
 */
function looksLikeResearch(data) {
  return Boolean(data && (data.overview || data.key_points || data.sources || data.videos));
}

function renderExplore(target, data) {
  if (!tryRenderResearch(target, data)) {
    renderAiAnswerPage(target, data, state.lastQuery || data.topic || "Research");
  }
}

function renderAiAnswerPage(target, report, query) {
  const layout = el("article", "ai-answer-layout");
  const main = el("div", "answer-main");
  main.appendChild(el("div", "query-chip", query || report.topic || "Ask anything"));

  const answerCard = el("section", "answer-card");
  const answerLead = el("div", "answer-lead");
  answerLead.appendChild(renderMd(report.answer || report.overview || "Here is a clear answer based on available context."));
  answerCard.appendChild(answerLead);

  if (report.answer_highlights?.length) {
    const highlights = el("div", "highlight-grid");
    report.answer_highlights.slice(0, 4).forEach((highlight) => {
      const item = el("div", "highlight-item");
      item.appendChild(el("span", "highlight-mark", ""));
      const p = el("div", "highlight-text");
      p.appendChild(renderMd(highlight));
      item.appendChild(p);
      highlights.appendChild(item);
    });
    answerCard.appendChild(highlights);
  }

  if (report.source_chips?.length) {
    answerCard.appendChild(sourceChips(report.source_chips));
  }
  main.appendChild(answerCard);

  if (report.warnings?.length) {
    const notice = el("div", "answer-notice");
    report.warnings.slice(0, 2).forEach((warning) => notice.appendChild(el("p", "", warning)));
    main.appendChild(notice);
  }

  if (report.sections?.length) {
    renderAnswerSections(main, report.sections, report.sources || []);
  } else if (report.key_points?.length) {
    const section = el("section", "answer-section");
    section.appendChild(el("h3", "", "Key Takeaways"));
    const list = el("ul", "");
    report.key_points.forEach((point) => list.appendChild(richBullet(point)));
    section.appendChild(list);
    main.appendChild(section);
  }

  if (report.verification) {
    renderVerification(main, report.verification);
  }

  if (report.learning_path?.length) {
    const section = el("section", "answer-section");
    section.appendChild(el("h3", "", "Go Deeper"));
    const list = el("ul", "");
    report.learning_path.forEach((item) => list.appendChild(richBullet(item)));
    section.appendChild(list);
    main.appendChild(section);
  }

  if (report.follow_up_questions?.length) {
    renderFollowUps(main, report.follow_up_questions);
  }

  if (report.videos?.length) {
    const section = el("section", "answer-section");
    section.appendChild(el("h3", "", "Watch Next"));
    const row = el("div", "video-row");
    report.videos.slice(0, 3).forEach((video, index) => row.appendChild(videoCard(video, index)));
    section.appendChild(row);
    main.appendChild(section);
  }

  layout.appendChild(main);
  layout.appendChild(sourceRail(report));
  target.appendChild(layout);
}

function renderVerification(target, verification) {
  const section = el("section", "answer-section verification-section");
  section.appendChild(el("h3", "", "Verification"));
  const confidence = verification.confidence || "unknown";
  const row = el("div", "pill-row");
  row.appendChild(el("span", `pill ${confidence === "medium" ? "ok" : "warn"}`, `Confidence: ${label(confidence)}`));
  row.appendChild(el("span", "pill", `${verification.source_count || 0} sources`));
  row.appendChild(el("span", "pill", `${verification.independent_domains?.length || 0} domains`));
  section.appendChild(row);
  if (verification.notes?.length) {
    section.appendChild(el("p", "", verification.notes[0]));
  }
  target.appendChild(section);
}

function renderFollowUps(target, questions) {
  const section = el("section", "answer-section followup-panel");
  section.appendChild(el("h3", "", "Ask Next"));
  const row = el("div", "followup-row");
  questions.forEach((question) => {
    const button = el("button", "followup-chip", question);
    button.type = "button";
    button.addEventListener("click", () => {
      const exploreInput = byId("exploreInput");
      if (exploreInput) {
        exploreInput.value = question;
        // Use setTimeout to ensure value propagates before button click handler reads it
        setTimeout(() => byId("exploreButton").click(), 0);
      } else {
        byId("chatInput").value = question;
        submitChat();
      }
    });
    row.appendChild(button);
  });
  section.appendChild(row);
  target.appendChild(section);
}

/* ── Source Chips ───────────────────────────────────────────── */

function sourceChips(chips) {
  const row = el("div", "source-chip-row");
  chips.slice(0, 5).forEach((chip) => {
    const link = el("a", "source-chip", chip.label || chip.title || "source");
    link.href = chip.url || "#";
    link.target = "_blank";
    link.rel = "noreferrer";
    row.appendChild(link);
  });
  return row;
}

/* ── Answer Sections ────────────────────────────────────────── */

function renderAnswerSections(target, sections, sources) {
  sections.forEach((entry) => {
    const section = el("section", "answer-section structured-section");
    section.appendChild(el("h3", "", entry.title || "Explanation"));
    const list = el("ol", "");
    (entry.items || []).forEach((item) => {
      const text = typeof item === "string" ? item : item.text || "";
      const bullet = el("li", "");
      bullet.appendChild(renderMd(text));
      const citations = typeof item === "string" ? [] : item.source_indices || [];
      if (citations.length) bullet.appendChild(citationRow(citations, sources));
      list.appendChild(bullet);
    });
    section.appendChild(list);
    target.appendChild(section);
  });
}

function citationRow(indices, sources) {
  const row = el("span", "citation-row");
  indices.slice(0, 3).forEach((index) => {
    const source = sources[index - 1] || {};
    const chip = el("a", "citation-chip", source.url ? `${index} ${domainLabel(source.url)}` : `${index}`);
    chip.href = source.url || "#";
    chip.target = "_blank";
    chip.rel = "noreferrer";
    row.appendChild(chip);
  });
  return row;
}

/* ── Source Rail ────────────────────────────────────────────── */

function sourceRail(report) {
  const rail = el("aside", "source-rail");
  rail.appendChild(el("h3", "", "Sources And Videos"));

  if (report.verification) {
    const meta = el("div", "rail-summary");
    meta.appendChild(el("span", "pill ok", `Confidence: ${label(report.verification.confidence || "unknown")}`));
    meta.appendChild(el("span", "pill", `${report.verification.independent_domains?.length || 0} domains`));
    rail.appendChild(meta);
  }

  const videos = (report.videos || []).slice(0, 3);
  const sources = (report.sources || []).filter((s) => s.source_type !== "error").slice(0, 5);

  videos.forEach((video) => rail.appendChild(railCard(video, "YouTube", video.reason || video.channel || "")));
  sources.forEach((source) => rail.appendChild(railCard(source, "Source", source.snippet || "")));

  (report.warnings || []).slice(0, sources.length || videos.length ? 1 : 2).forEach((warning) => {
    const card = el("div", "rail-card muted-card");
    card.appendChild(el("div", "rail-meta", "Status"));
    card.appendChild(el("p", "", warning));
    rail.appendChild(card);
  });

  if (!videos.length && !sources.length) {
    rail.appendChild(el("div", "rail-card", "No external links were returned for this answer."));
  }
  return rail;
}

function railCard(item, type, description) {
  const card = el("div", "rail-card");
  if (item.thumbnail_url) card.classList.add("media-card");

  const meta = el("div", "rail-meta");
  const logo = faviconUrl(item, type);
  if (logo) {
    const image = el("img", "rail-logo");
    image.src = logo;
    image.alt = "";
    image.loading = "lazy";
    meta.appendChild(image);
  }
  meta.appendChild(document.createTextNode(sourceLabel(item, type)));
  card.appendChild(meta);

  const link = el("a", "", item.title || item.url || type);
  link.href = item.url || "#";
  link.target = "_blank";
  link.rel = "noreferrer";
  card.appendChild(link);

  if (item.thumbnail_url) {
    const image = el("img", "rail-thumb");
    image.src = item.thumbnail_url;
    image.alt = "";
    image.loading = "lazy";
    card.appendChild(image);
  }
  if (description) card.appendChild(el("p", "", description));
  return card;
}

/* ── Video Cards ────────────────────────────────────────────── */

function videoCard(video, index) {
  const card = el("a", "video-card");
  card.href = video.url;
  card.target = "_blank";
  card.rel = "noreferrer";

  const thumb = el("div", "video-thumb");
  if (video.thumbnail_url) {
    const image = el("img", "video-image");
    image.src = video.thumbnail_url;
    image.alt = "";
    image.loading = "lazy";
    thumb.appendChild(image);
    thumb.appendChild(el("span", "video-badge", video.duration || "Watch"));
  } else {
    thumb.textContent = index === 0 ? "YouTube" : "Video";
  }
  card.appendChild(thumb);
  card.appendChild(el("strong", "", video.title || "Related video"));
  card.appendChild(el("span", "", video.channel || video.reason || "Video"));
  return card;
}

/* ── Topic tracking ────────────────────────────────────────── */

// Remember the last researched topic so follow-ups ("tell me more about that")
// and re-searches can reference it. Live progress now arrives from the single
// activity channel (see appendLiveStep in render-utils.js), not this file.
function trackExploredTopic(topic) {
  const isFollowup = /^(tell me more|go deeper|continue|elaborate|give me more|what else|more detail)/i.test(topic);
  if (!isFollowup || !state.lastExploredTopic) state.lastExploredTopic = topic;
}

/* ── Research history ──────────────────────────────────────────
 *
 * What the reader actually researched, newest first, in THIS browser's
 * localStorage (the same local-first pattern as the chat and JARVIS histories).
 * A row re-runs its search; each row and the whole list can be deleted, and
 * every mutation rewrites storage so a reload agrees with what is on screen.
 */
const EXPLORE_HISTORY_KEY = "novacontrol.exploreHistory";
const EXPLORE_HISTORY_MAX = 30;

function loadExploreHistory() {
  let raw = [];
  try {
    raw = JSON.parse(localStorage.getItem(EXPLORE_HISTORY_KEY) || "[]");
  } catch (_) {
    return []; // unreadable storage is empty history, never a crash
  }
  if (!Array.isArray(raw)) return [];
  return raw
    .filter((entry) => entry && typeof entry.topic === "string" && entry.topic.trim())
    .slice(0, EXPLORE_HISTORY_MAX);
}

function saveExploreHistory(items) {
  const trimmed = items.slice(0, EXPLORE_HISTORY_MAX);
  try { localStorage.setItem(EXPLORE_HISTORY_KEY, JSON.stringify(trimmed)); } catch (_) { /* storage unavailable */ }
  return trimmed;
}

function recordExploreHistory(topic) {
  const clean = String(topic || "").trim();
  const items = loadExploreHistory();
  if (!clean) return items;
  // An immediate repeat UPDATES the existing row instead of duplicating it.
  const withoutRepeat = items.filter((entry) => String(entry.topic).toLowerCase() !== clean.toLowerCase());
  withoutRepeat.unshift({ topic: clean, at: new Date().toISOString() });
  const saved = saveExploreHistory(withoutRepeat);
  renderExploreHistory(saved);
  return saved;
}

function deleteExploreHistoryEntry(topic) {
  const target = String(topic || "").toLowerCase();
  const saved = saveExploreHistory(loadExploreHistory().filter((entry) => String(entry.topic).toLowerCase() !== target));
  renderExploreHistory(saved);
  showToast("Search removed from history");
  return saved;
}

function clearExploreHistory() {
  const saved = saveExploreHistory([]);
  renderExploreHistory(saved);
  showToast("Research history cleared");
  return saved;
}

function exploreHistoryWhen(iso) {
  const then = Date.parse(iso);
  if (!Number.isFinite(then)) return "";
  const minutes = Math.max(0, Math.round((Date.now() - then) / 60000));
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  return days === 1 ? "yesterday" : `${days}d ago`;
}

function runExploreFromHistory(topic) {
  byId("exploreInput").value = topic;
  byId("exploreButton").click();
}

function renderExploreHistory(items) {
  const list = byId("exploreHistoryList");
  if (!list) return;
  const entries = Array.isArray(items) ? items : loadExploreHistory();
  clearNode(list);
  entries.forEach((entry) => {
    const row = el("li", "explore-history-row");
    const main = el("button", "explore-history-main");
    main.type = "button";
    main.title = "Research this again";
    main.appendChild(el("span", "explore-history-text", entry.topic));
    main.appendChild(el("span", "explore-history-meta", exploreHistoryWhen(entry.at)));
    main.addEventListener("click", () => runExploreFromHistory(entry.topic));
    row.appendChild(main);
    const remove = el("button", "explore-history-delete", "Delete");
    remove.type = "button";
    remove.setAttribute("aria-label", `Delete "${entry.topic}" from research history`);
    remove.addEventListener("click", (event) => {
      event.stopPropagation();
      deleteExploreHistoryEntry(entry.topic);
    });
    row.appendChild(remove);
    list.appendChild(row);
  });
  const empty = byId("exploreHistoryEmpty");
  if (empty) empty.hidden = entries.length > 0;
  const clear = byId("exploreHistoryClearButton");
  if (clear) clear.disabled = entries.length === 0;
  const badge = byId("exploreHistoryCount");
  if (badge) {
    badge.textContent = String(entries.length);
    badge.hidden = entries.length === 0;
  }
}

function setExploreHistoryOpen(open) {
  const panel = byId("exploreHistory");
  const button = byId("exploreHistoryButton");
  if (!panel) return;
  panel.hidden = !open;
  if (button) button.setAttribute("aria-expanded", open ? "true" : "false");
  if (open) renderExploreHistory();
}

function setupExploreHistory() {
  const button = byId("exploreHistoryButton");
  const panel = byId("exploreHistory");
  if (!button || !panel) return;
  button.addEventListener("click", () => setExploreHistoryOpen(panel.hidden));
  byId("exploreHistoryClearButton").addEventListener("click", clearExploreHistory);
  renderExploreHistory();
}
