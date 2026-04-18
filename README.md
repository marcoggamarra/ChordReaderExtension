# YouTube Music Analyzer

Chrome extension + local Python service for real-time music analysis (BPM, chord, confidence, and visualization).

## Project Layout

- `MyExtension/chrome_extension/` : load this in Chrome as unpacked extension
- `MyExtension/python_service/` : local FastAPI analysis service
- `MyExtension/.venv/` : Python 3.11 virtual environment

## Quick Start

1. Install Python dependencies:

```powershell
cd D:\NewChrome\MyExtension
.\.venv\Scripts\pip.exe install -r .\python_service\requirements.txt
```

2. Run local API:

```powershell
cd D:\NewChrome\MyExtension
.\.venv\Scripts\python.exe -m uvicorn local_service:app --host 127.0.0.1 --port 8765 --app-dir .\python_service
```

3. Load extension in Chrome:

- Open `chrome://extensions`
- Enable Developer mode
- Click Load unpacked
- Select `D:\NewChrome\MyExtension\chrome_extension`

4. Open a YouTube watch page and click Start in the extension popup.

## API Endpoints

- `GET /`
- `GET /health`
- `POST /analyze`
- `GET /docs`
