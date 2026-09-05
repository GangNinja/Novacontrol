# How To Run NovaControl

This guide explains how to execute NovaControl through every completed phase.

The project is currently working through:

- Phase 1: Project foundation
- Phase 2: Core engine
- Phase 3: Memory
- Phase 4: Tool system
- Phase 5: Agents
- Phase 6: Planning
- Phase 7: Desktop automation
- Phase 8: Browser automation
- Phase 9: Vision
- Phase 10: Voice
- Phase 11: GUI
- Phase 12: API
- Phase 13: Plugin marketplace
- Phase 14: Performance optimization
- Phase 15: Documentation and release readiness
- Additional requested feature: Explore backend for online research, explanations, and videos

## 1. Open The Project

```powershell
cd "C:\Users\NARASIMHA\OneDrive\Desktop\NovaControl"
```

## 2. Activate Your Virtual Environment

If your venv is named `.venv`:

```powershell
.\.venv\Scripts\Activate.ps1
```

If PowerShell blocks activation, run this once in the same terminal:

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope Process
.\.venv\Scripts\Activate.ps1
```

## 3. Make The Source Package Available

If you installed the project with `pip install -e .`, you can run commands directly.

If not, set `PYTHONPATH`:

```powershell
$env:PYTHONPATH='src'
```

## 4. Verify The Completed Work

```powershell
python -m unittest discover -s tests
```

Expected result:

```text
OK
```

## 5. Check NovaControl Status

```powershell
python -m novacontrol status
```

This prints the current completed phases and useful commands.

## 6. List Phase Commands

```powershell
python -m novacontrol phases
```

This prints every completed phase and the command that demonstrates it.

## 7. Run Every Completed Phase Demo

```powershell
python -m novacontrol demo all
```

This runs safe demos for all completed phases. Desktop and browser automation intentionally deny sensitive execution by default until a real approval UI is connected.

## 8. Run One Phase At A Time

Phase 1 foundation:

```powershell
python -m novacontrol demo phase1
```

Phase 2 core engine:

```powershell
python -m novacontrol demo phase2
```

Phase 3 memory:

```powershell
python -m novacontrol demo phase3
```

Phase 4 tool system:

```powershell
python -m novacontrol demo phase4
```

Phase 5 agents:

```powershell
python -m novacontrol demo phase5
```

Phase 6 planning:

```powershell
python -m novacontrol demo phase6
```

Phase 7 desktop automation:

```powershell
python -m novacontrol demo phase7
```

Phase 8 browser automation:

```powershell
python -m novacontrol demo phase8
```

Phase 9 vision:

```powershell
python -m novacontrol demo phase9
```

Phase 10 voice:

```powershell
python -m novacontrol demo phase10
```

Phase 11 GUI:

```powershell
python -m novacontrol demo phase11
python -m novacontrol gui --dry-run
python -m novacontrol gui
```

Phase 12 API:

```powershell
python -m novacontrol demo phase12
python -m uvicorn novacontrol.api.app:create_app --factory --reload
```

Phase 13 plugin marketplace:

```powershell
python -m novacontrol demo phase13
```

Phase 14 performance optimization:

```powershell
python -m novacontrol demo phase14
```

Phase 15 release readiness:

```powershell
python -m novacontrol demo phase15
```

Integrated NovaControl request router:

```powershell
python -m novacontrol ask "build a plan for my project"
python -m novacontrol ask "create tests for the scheduler"
python -m novacontrol ask "explain transformers in AI"
```

Explore research backend:

```powershell
python -m novacontrol demo explore
```

## 9. Use The Planning System

Create a plan:

```powershell
python -m novacontrol plan "research options then implement the best one"
```

Create and execute the safe deterministic workflow:

```powershell
python -m novacontrol plan "research options then implement the best one" --execute
```

## 10. Use Explore For Online Research

Explore researches online through your internet connection, creates an understandable explanation, includes source links, and returns related video links.

```powershell
python -m novacontrol explore "transformers in AI"
```

Use a simpler explanation:

```powershell
python -m novacontrol explore "quantum computing" --depth simple
```

Limit sources and videos:

```powershell
python -m novacontrol explore "machine learning" --sources 4 --videos 3
```

Disable videos:

```powershell
python -m novacontrol explore "Python decorators" --no-videos
```

## 11. Use Vision

Understand an image-like source name:

```powershell
python -m novacontrol vision "nova_control_dashboard.png"
```

OCR a text file:

```powershell
python -m novacontrol vision "notes.txt" --task ocr
```

Understand a document-like text or Markdown file:

```powershell
python -m novacontrol vision "docs\ARCHITECTURE.md" --task document_understanding
```

## 12. Run The API

After installing dependencies in your venv:

```powershell
python -m uvicorn novacontrol.api.app:create_app --factory --reload
```

Open:

```text
http://127.0.0.1:8000/health
```

Expected response:

```json
{"status":"ok"}
```

## Current Safety Notes

- Desktop automation is approval-gated and deny-by-default.
- Browser navigation, form fill, downloads, and web tests are approval-gated and deny-by-default.
- Current desktop and browser demos use safe no-op runners.
- Explore uses online providers and may return provider errors if the internet or provider is unavailable.

## What Comes Next

Next work should focus on real external adapters, packaging, and deeper UI polish.
