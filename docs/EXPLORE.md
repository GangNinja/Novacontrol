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
