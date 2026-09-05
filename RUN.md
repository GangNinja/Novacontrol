# RUN — NovaControl (Windows + VS Code)

Everything below is run inside the **VS Code terminal** (View → Terminal,
or Ctrl+`).

## Project path

```powershell
cd "C:\Users\NARASIMHA\OneDrive\Desktop\NovaControl"
```

## 1. Activate the virtual environment

```powershell
.\.venv\Scripts\Activate.ps1
```

If PowerShell refuses (execution policy), run these two lines **once**:

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope Process
.\.venv\Scripts\Activate.ps1
```

You know it worked when the terminal prompt starts with `(.venv)`.

## 2. Make the `novacontrol` package importable

Your venv currently contains FastAPI + uvicorn but NOT `novacontrol` itself.
Pick **one** of these:

**Option A — install the package (recommended, one-time):**

```powershell
python -m pip install -e .
```

**Option B — point Python at the source folder (per terminal session):**

```powershell
$env:PYTHONPATH = "src"
```

Either way, verify with:

```powershell
python -c "import novacontrol; print(novacontrol.__file__)"
```

It must print a path under `...\NovaControl\src\...` (not an error).

## 3. Run the web server

```powershell
python -m uvicorn novacontrol.api.app:create_app --factory --host 127.0.0.1 --port 8000
```

- Uses `--factory` because `create_app()` is a factory function, not a FastAPI instance.
- Then open **http://127.0.0.1:8000/** in your browser.
- Stop the server with `Ctrl+C` in that same terminal.

> **Seeing an old interface?** Hard-refresh with `Ctrl+Shift+R` (or clear the
> browser cache). Asset URLs now carry a date-based cache-buster, so a normal
> refresh after that is enough.

## 4. Run the tests (optional)

```powershell
$env:PYTHONPATH = "src"   # only if you did NOT pip install -e .
python -m pytest tests -q
```

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `ModuleNotFoundError: No module named 'novacontrol'` | Do step 2 (Option A or B). |
| `(.venv)` missing from prompt | Re-run the activation command from step 1. |
| `port already in use` / old page loads | Another server is running. Stop it (`Ctrl+C`), or use `--port 8001`. |
| Old styling / scripts after an update | `Ctrl+Shift+R` hard refresh. |
