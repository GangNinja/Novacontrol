# Release Checklist

Use this checklist before treating NovaControl as ready for local use.

## Required Checks

```powershell
python -m unittest discover -s tests
python -m novacontrol demo all
python -m novacontrol demo phase15
```

## Required Docs

- README
- How to run guide
- Architecture guide
- Developer guide
- Plugin guide
- API guide
- Deployment guide
- Phase documentation
- Status document

## Safety Defaults

- Sensitive desktop automation is deny-by-default.
- Sensitive browser automation is deny-by-default.
- Sensitive tool execution is deny-by-default.
- Sensitive plugin installation is deny-by-default.
- API token authentication is available through `NOVACONTROL_API_TOKEN`.

## Current Release Scope

This release is a local production-shaped foundation. It includes working module boundaries, CLI demos, GUI launcher, API routes, tests, and documentation. Real model providers, real desktop control, full browser automation, persistent multi-user services, and packaged installers are future hardening steps.
