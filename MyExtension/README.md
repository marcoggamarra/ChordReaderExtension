# MyExtension Layout

This project is split into two parts:

- `chrome_extension/` -> load this folder in Chrome (`chrome://extensions` -> Load unpacked)
- `python_service/` -> local Python API for analysis

## Run Python Service (Python 3.11 venv)

From `d:\NewChrome\MyExtension`:

```powershell
.\.venv\Scripts\python.exe -m uvicorn local_service:app --host 127.0.0.1 --port 8765 --app-dir .\python_service
```

## Test Service

- `http://127.0.0.1:8765/`
- `http://127.0.0.1:8765/health`
- `http://127.0.0.1:8765/docs`

## Install Python deps

```powershell
.\.venv\Scripts\pip.exe install -r .\python_service\requirements.txt
```
