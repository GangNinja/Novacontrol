# Explore

Explore is the research and learning workflow requested by the user. It ships as the visible `Explore` tab in both the web platform and the desktop GUI, backed by the same `ExploreService`.

## Purpose

Explore lets the user ask about any topic and receive:

- Online research through the user's internet connection
- Understandable explanations
- Key points
- Detailed learning path
- Source links
- Related video links
- Follow-up questions to continue learning

## Components

- `ExploreRequest`: topic, depth, source limits, and video settings
- `ResearchSource`: web source title, URL, snippet, and type
- `VideoResult`: video title, URL, channel, duration, and reason
- `ExploreReport`: complete explanation package
- `SearchProvider`: online search adapter interface
- `VideoProvider`: video search adapter interface
- `DuckDuckGoLiteSearchProvider`, `BingSearchProvider`, `GoogleSearchProvider`: standard-library web search providers
- `ResilientSearchProvider`: the default provider — tries the engines in order and never raises
- `WikipediaSearchProvider`: fallback used when web results come back too thin
- `YouTubeSearchVideoProvider`: related video link provider
- `TrendingTopicsProvider`: live top-story news headlines that feed the Explore panel's suggestion chips (see below)
- `PageReader`: fetches a source URL and extracts its readable prose (`explore/page_reader.py`)
- `Evidence`: the ranked, de-duplicated sentence pool an answer is composed from (`explore/evidence.py`)
- `ResearchExplainer`: creates clear explanations from sources
- `ExploreService`: orchestrates research, videos, and explanation
- `ExploreModule`: event-driven runtime module

## Answer quality: the site-chrome filter

Search snippets glue site furniture onto real content — consent banners ("By
using this site, you agree to the Terms of Use"), Wikipedia footers ("This page
was last edited on …"), trademark lines, newsletter and sign-in frames. The
synthesizer used to treat those as facts, so a researched answer could open
with a Terms-of-Use sentence instead of an explanation.

`synthesizer.clean_snippet()` now runs every snippet through
`strip_boilerplate()` first: chrome is detected **per sentence**, so a snippet
that mixes chrome with real content keeps the content, and a snippet that is
nothing but chrome cleans to `""` and every consumer skips it (facts, key
points, highlights, section items, source references, and the LLM prompt).
`is_boilerplate(text)` exposes the same verdict for tests and callers.

This is shared with Chat: a research question asked in Chat runs the identical
pipeline, so both surfaces answer from the same cleaned facts. The behaviour is
pinned by `SiteChromeFilterTests` in `tests/test_synthesizer.py`, which uses the
verbatim snippets behind the original failure and also guards against
over-filtering (a legitimate sentence *about* privacy policies must survive).

## Answer quality: question-first research (`planner.py`)

Keyword-shaped answers are the second failure of the same kind. Asking *"why do
cats purr"* searched the words `cats purr`, and the pages that came back were
titled *Why Do Cats Purr?* with teaser blurbs — which synthesis then re-listed
under "here is why cats purr:". The report was about the topic; it never
answered the question.

`explore/planner.py` fixes that at the source. When a synthesis model is
connected, `plan_research()` hands it the user's own question (plus the recent
conversation topics) and asks for JSON only:

```json
{"focus": "<what the user is actually asking for>", "queries": ["…", "…"]}
```

The plan drives the search, and **relevance is judged against the plan** — an
angle the model chose ("cat purr science vocal folds") is exactly the evidence
the answer needs, so the keyword gate must not reject it for failing to repeat
the user's phrasing. Answer synthesis is question-first too: the prompt carries
the question and the planner's `focus`, and the model is told to answer
directly, in prose, without listing source titles, pasting snippets, or
restating the question.

Nothing here inspects keywords. The rules stay where they are —
`query.build_search_queries()` is now the *fallback* used when no model is
connected (scratch brain, no Ollama, no cloud key), and a reply that is not
valid JSON is rejected rather than mined for query-looking lines, because
searching a model's prose searches nonsense.

## Answer quality: reading the sources (`page_reader.py`)

Everything above still worked on a 150-character search blurb, so an answer
could never say more than what the blurbs repeated: no synthesis rule can turn
*"Introduction Among the many offences which are delineated by the Code, there
exists one which is known as …"* into an answer. The fix is better evidence, not
another rule.

`explore/page_reader.py` fetches each source URL (standard library only —
`urllib` + `html.parser`, no new dependency) and extracts its readable prose:
script/style/nav/header/footer/aside/form blocks are skipped entirely, blocks
shorter than 45 characters (labels, crumbs) are dropped, blocks that have no
sentence in them are dropped, repeats collapse, and HTML entities, citation
markers (`[ 3 ]`), and spacing artifacts from link parsing are tidied away.
Fetches run on worker threads behind one deadline, and each page is cached.

Reading is an **upgrade, never a requirement**: a blocked, slow, script-only, or
non-HTML page yields `""`, the source keeps its blurb, and the report says the
pages could not be read. Search engines hand back wiki titles with `+` for the
spaces (`/wiki/Noise-cancelling+headphones`), which 404s, so a failed read of a
`+` path is retried once with the separator the wiki actually uses.

`ExploreService.read_pages` (default on) controls the step; `page_reader=` lets
a caller inject a reader, which is how the test suite stays offline.
`ResearchSource.content` carries the text and is deliberately **not** part of
`to_dict()` — it is evidence for synthesis, not payload for the browser.

## Answer quality: ranked sentences, not keyword buckets (`evidence.py`)

The local answer used to be assembled by keyword buckets ("cause", "step",
"definition" word lists) filled from blurbs, which is how a question about cats
purring produced a list of pages that mentioned purring. `explore/evidence.py`
replaces that with one ranked pool of the sentences the sources actually
contain, and nothing in it knows any topic:

- **coverage** — the question's own words, weighted by how RARE each is across
the result set, so a sentence naming the specific thing beats one that repeats
the topic's common words.
- **information** — the mean inverse document frequency of a sentence's content
  words across the pool. A sentence built from words every source repeats says
  almost nothing; one that names things nothing else does says a lot. This is
  what stops "Purring is common in cats of every age" from leading an answer.
- **agreement** — a sentence whose distinctive words recur in a *different*
source is likelier to be true.
- **specificity / position** — digits, units, and names read as facts; early
  sentences answer, late ones trail off into related links.

`compose_answer()` then writes the answer from the top sentences: a prose lead,
then **Supporting detail** bullets with a score floor, then attribution at the
END (`Sources: a.com, b.com`). Two things it never does: echo the question back
as a framing line ("here is what the sources say about X"), and quote a search
blurb as if it were a finished sentence (blurbs end in "…").

The pool is sliced **disjointly**: the answer takes the best sentences and tells
the report what it spent, and the highlight cards, key points, and sections take
the next best. The reader saw the same blurbs once inside the answer and again
as cards beneath it; now each surface shows something new, and a surface goes
empty rather than repeating the answer.

Page-chrome detection gained meta-discourse alongside the consent banners:
sentences about the *page* rather than the subject ("In this article they will
discuss…", "Purring is discussed in this overview…", "The information is
current and up-to-date…") are dropped, as are list lead-ins (a colon now ends a
sentence) and mid-sentence fragments.

## Answer quality: saying who wrote the answer

A research run whose brain is unreachable (a rejected cloud key, a stopped
Ollama) used to look exactly like a healthy one: keyword-matched source blurbs,
no explanation of why the prose never answered anything. `ResearchExplainer`
now appends a provenance note to `ExploreReport.warnings` whenever the answer
came from the local fallback instead of a model:

- a connected-but-broken brain is named with its own error (`last_error`),
- no model at all says the answer is assembled from the sources' own sentences
  (or, when the pages could not be read either, that it is a digest of search
  summaries rather than an answer),
- a model-written answer adds no note.

The fallback itself no longer dresses keyword matches up as an answer: video
pages are excluded from the fact pool (a video has its own section), scraped URLs
(`https:/…`) are stripped, a leading rhetorical teaser sentence is dropped, and
a truncated snippet keeps its ellipsis instead of pretending to be a finished
sentence. Pinned by `tests/test_explore_question_first.py`,
`tests/test_evidence_ranking.py`, and `tests/test_page_reading.py`.

## CLI

```powershell
python -m novacontrol explore "transformers in AI"
```

Simple explanation:

```powershell
python -m novacontrol explore "quantum computing" --depth simple
```

Limit sources and videos:

```powershell
python -m novacontrol explore "machine learning" --sources 4 --videos 3
```

## Events

- `explore.topic_requested`: request online research
- `explore.report_created`: emitted with the complete research report

## Topic suggestions: trending daily updates

The Explore panel's example chips are not a fixed list. `TrendingTopicsProvider`
fetches top-story headlines from Google News RSS (standard library, no API
key), trims each headline into a research topic (publisher suffix stripped,
LIVE blogs and opinion pieces skipped), caches for 30 minutes, invalidates the
pool per day, and rotates the visible window hourly so repeat visits offer
different topics. When the feed is unreachable the endpoint reports
`source: "unavailable"` and the panel keeps static help chips. The surface is
`GET /explore/trending`, pinned by `tests/test_explore_trending.py`.

## GUI

The `Explore` tab ships in both interfaces: the web platform's `Explore` nav
destination (serving `#explorePanel`) and the desktop Qt app's Explore tab
(`DashboardTab.EXPLORE`). Both call the same service, so behavior matches
across surfaces.
