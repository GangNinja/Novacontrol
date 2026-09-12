# Explore

Explore is the research and learning workflow requested by the user. It is designed to become a visible GUI tab named `Explore`.

## Purpose

Explore should let the user ask about any topic and receive:

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
- `DuckDuckGoLiteSearchProvider`: standard-library web search provider
- `YouTubeSearchVideoProvider`: related video link provider
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

## GUI

The GUI phase must expose this as a tab named `Explore`.
