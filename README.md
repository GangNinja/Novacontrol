# NovaControl

NovaControl is a modular AI operating system designed to coordinate agents, tools, memory, desktop automation, browser automation, voice, vision, projects, and plugins through event-driven interfaces.

This repository currently contains **Phase 1: Project Foundation**. The implementation focuses on stable boundaries, configuration, core runtime primitives, security policy scaffolding, and tests. Later phases can add concrete FastAPI, PySide6, LangGraph, SQLAlchemy, Redis, Celery, vision, voice, and automation implementations without changing the core contracts.

## Status

- Phase 1 foundation: complete
- Core runtime skeleton: included
- Event bus: included
- Interface contracts: included
- Security approval primitives: included
- API/GUI/agent/memory modules: scaffolded
- Tests: standard-library smoke/unit tests

## Quick Start

NovaControl is a pure-Python FastAPI app (no package.json, no build step — edits under `src/novacontrol` are live on server restart).

Create the virtual environment and install the project (editable, with dev/test extras) once:

```powershell
python -m venv .venv
.\ .venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

Run the web app with uvicorn, using the `--factory` flag so FastAPI calls `create_app()`:

```powershell
python -m uvicorn novacontrol.api.app:create_app --factory --host 127.0.0.1 --port 8000
```

Then open http://127.0.0.1:8000/ in your browser. If port 8000 is already taken (e.g. by another session), pick a free one such as 8001.

Run the test suite (imports need `PYTHONPATH=src`, which the editable install does not set):

```powershell
$env:PYTHONPATH='src'
python -m pytest tests/ -q
```

## Architecture Rule

Modules communicate through events and interfaces. A module may depend on core contracts, but it must not import another feature module directly.

## Repository Layout

```text
src/novacontrol/
  core/          Event bus, runtime, configuration, contracts, security
  agents/        Agent contracts and future implementations
  api/           FastAPI entrypoint scaffold
  automation/    Workflow automation scaffold
  browser/       Browser controller scaffold
  desktop/       Desktop controller scaffold
  gui/           PySide6 GUI scaffold
  integrations/  External service integration scaffold
  knowledge/     Knowledge base scaffold
  memory/        Memory subsystem scaffold
  planning/      Planner scaffold
  plugins/       Plugin manager scaffold
  projects/      Project manager scaffold
  scheduler/     Scheduler scaffold
  skills/        Skill subsystem scaffold
  tools/         Tool manager scaffold
  vision/        Vision subsystem scaffold
  voice/         Voice subsystem scaffold
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and [docs/PHASES.md](docs/PHASES.md) for the implementation roadmap.

For execution commands through each completed phase, see [docs/HOW_TO_RUN.md](docs/HOW_TO_RUN.md).
