/* ── Explore / Research Rendering ───────────────────────────── */

/** Render a research report into `target`. Returns false when `data` has no report shape. */
function tryRenderResearch(target, data) {
  // /ask envelopes reports under `payload`; /explore returns the report directly.
  const candidates = [data.payload, data, data.report];
  const report = candidates.find((item) => item && (item.overview || item.key_points || item.sources || item.videos));
  if (!report) return false;
  renderAiAnswerPage(target, report, state.lastQuery || report.topic || "Research");
  return true;
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

/* ── Live Progress (SSE /explore/stream) ───────────────────── */

function trackExploredTopic(topic) {
  const isFollowup = /^(tell me more|go deeper|continue|elaborate|give me more|what else|more detail)/i.test(topic);
  if (!isFollowup || !state.lastExploredTopic) state.lastExploredTopic = topic;
}

function appendProgressStep(steps, data) {
  const text = data.detail || data.step || "Working...";
  const rows = steps.querySelectorAll(".progress-step");
  if (rows.length && rows[rows.length - 1].textContent === text) return; // no repeated rows
  steps.appendChild(el("p", "progress-step", text));
}

function parseSseBlock(block) {
  let name = "message";
  const dataLines = [];
  for (const line of block.split("\n")) {
    if (line.startsWith("event:")) name = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
  }
  if (!dataLines.length) return null;
  try {
    return { name, data: JSON.parse(dataLines.join("\n")) };
  } catch (_) {
    return { name, data: { error: dataLines.join("\n") } };
  }
}

/**
 * Research with live progress steps. Pure "task" for run(): it appends the
 * progressive status rows under run()'s loading scan-line, and resolves with
 * the report (run() renders it, toasts, and manages the button). All failures
 * surface as thrown errors so run()'s shared error card handles them.
 */
async function streamExploreResearch(topic) {
  const target = byId("exploreOutput");
  target.classList.remove("empty-state");
  const steps = el("div", "explore-progress");
  target.appendChild(steps);

  const payload = { topic, include_videos: true };
  if (state.lastExploredTopic) payload.last_topic = state.lastExploredTopic;
  const response = await fetch("/explore/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    let detail = response.statusText;
    try { detail = JSON.parse(await response.text()).detail || detail; } catch (_) { /* non-JSON body */ }
    throw new Error(detail);
  }
  if (!response.body) throw new Error("This browser does not support streaming responses.");

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let report = null;
  while (report === null) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let split = buffer.indexOf("\n\n");
    while (split !== -1) {
      const block = buffer.slice(0, split).trim();
      buffer = buffer.slice(split + 2);
      const event = parseSseBlock(block);
      split = buffer.indexOf("\n\n");
      if (!event) continue;
      if (event.name === "progress" && event.data.step && event.data.step !== "complete") {
        appendProgressStep(steps, event.data);
      } else if (event.name === "complete") {
        report = event.data;
      } else if (event.name === "error") {
        throw new Error(event.data.error || "Research failed.");
      }
    }
  }
  if (!report) throw new Error("Research stream ended before a report arrived.");
  return report;
}
