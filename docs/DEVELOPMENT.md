# Development Guide

## Requirements

- Python 3.12 for production development
- Python 3.13 is acceptable for current foundation tests
- Docker for infrastructure-backed phases

## Local Checks

```powershell
$env:PYTHONPATH='src'
python -m unittest discover -s tests
python -m novacontrol
```

Once dependencies are installed:

```powershell
python -m pip install -e ".[dev]"
pytest
ruff check .
mypy src
```

CI runs four checks on every push and pull request (`.github/workflows/ci.yml`), all of them reproducible locally:

```powershell
$env:PYTHONPATH='src'
python -m pytest tests/ -q                                  # matrix: Python 3.12 and 3.13
python scripts/generate_api_reference.py --check            # docs/API.md matches the route registry
python -m mypy src                                          # linux view
python -m mypy src --platform win32                         # windows view
```

The API-reference check is the reason a new route is not done when it answers: register it in `novacontrol/api/app.py`, add it to the `ApiSurface` in `api/models.py`, declare how the web UI consumes it in `api/route_consumers.py`, then run `python scripts/generate_api_reference.py` to regenerate `docs/API.md`.

## Coding Standards

- Keep feature modules decoupled from each other.
- Put shared contracts in `novacontrol.core.interfaces`.
- Publish events for cross-module communication.
- Require approval for sensitive actions.
- Prefer typed dataclasses, Pydantic models, and Protocols for boundaries.
- Add tests with every behavioral change.

## Configuration

Configuration can come from:

- `configs/default.yaml`
- JSON config files
- Environment variables prefixed with `NOVACONTROL_`

The current foundation loader supports JSON with the standard library and YAML when PyYAML is installed.
