# Installation Guide

## Prerequisites

### Python 3.11 (Required)

madmom **requires Python 3.11**. It does not build on Python 3.12+.

**Install via winget (Windows):**
```powershell
winget install Python.Python.3.11
```

Verify the installation:
```powershell
py -3.11 --version
# Expected: Python 3.11.x
```

---

## Python Service Setup

All commands below should be run from the `MyExtension/` directory:

```powershell
cd D:\NewChrome\MyExtension
```

### Step 1 — Create a virtual environment with Python 3.11

```powershell
py -3.11 -m venv .venv
```

### Step 2 — Install build tools first

madmom requires Cython and NumPy to be present before it can be compiled:

```powershell
.\.venv\Scripts\pip.exe install cython numpy setuptools wheel
```

### Step 3 — Install FastAPI and Uvicorn

```powershell
.\.venv\Scripts\pip.exe install fastapi==0.115.0 "uvicorn[standard]==0.30.6"
```

### Step 4 — Install madmom (no build isolation)

madmom's `setup.py` uses Cython at build time and must be installed without pip's
default build isolation so it can see the already-installed Cython and NumPy:

```powershell
.\.venv\Scripts\pip.exe install --no-build-isolation madmom==0.16.1
```

### All-in-one command

```powershell
.\.venv\Scripts\pip.exe install cython numpy setuptools wheel ; `
.\.venv\Scripts\pip.exe install fastapi==0.115.0 "uvicorn[standard]==0.30.6" ; `
.\.venv\Scripts\pip.exe install --no-build-isolation madmom==0.16.1
```

### Verify installation

```powershell
.\.venv\Scripts\python.exe -c "import madmom; print('madmom OK')"
.\.venv\Scripts\python.exe -c "import fastapi; print('fastapi OK')"
```

---

## Running the Local Service

```powershell
cd D:\NewChrome\MyExtension
.\.venv\Scripts\python.exe -m uvicorn local_service:app --host 127.0.0.1 --port 8765
```

Check the service is running:
```powershell
curl http://127.0.0.1:8765/health
# Expected: {"ok":true,"madmom":true}
```

---

## Chrome Extension Setup

1. Open Chrome and navigate to `chrome://extensions/`
2. Enable **Developer mode** (top-right toggle)
3. Click **Load unpacked**
4. Select the `D:\NewChrome\MyExtension\` folder
5. The extension icon should appear in the toolbar

> The extension connects to `http://127.0.0.1:8765` — the local service **must be running** before pressing Start in the popup.

---

## Dependency Notes

| Package | Version | Notes |
|---|---|---|
| Python | **3.11.x** | 3.12+ breaks madmom's C extensions |
| madmom | 0.16.1 | Must be installed with `--no-build-isolation` |
| numpy | 2.1.1 | Install before madmom |
| cython | latest | Install before madmom (needed to compile .pyx files) |
| fastapi | 0.115.0 | REST API framework for the local service |
| uvicorn[standard] | 0.30.6 | ASGI server for FastAPI |

---

## Troubleshooting

**`madmom` fails to install with build errors**
- Make sure you installed `cython numpy setuptools wheel` first (Step 2)
- Make sure you used `--no-build-isolation` (Step 4)
- Confirm your venv is using Python 3.11: `.\.venv\Scripts\python.exe --version`

**Extension shows "service unavailable"**
- Confirm the local service is running on port 8765
- Check `http://127.0.0.1:8765/health` in a browser — should return `{"ok":true,...}`

**`madmom: false` in health response**
- madmom failed to import at runtime; check the service console output for the error
- Re-run the install steps above inside the `.venv`

**Port 8765 already in use**
- Find and stop the conflicting process: `netstat -ano | findstr :8765`
- Or change the port in both `local_service.py` startup command and `background.js` (`ANALYSIS_ENDPOINT`)
