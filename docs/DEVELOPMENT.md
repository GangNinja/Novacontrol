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
