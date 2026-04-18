# YouTube Music Analyzer

A Chrome extension + local Python service that captures audio from YouTube tabs and performs real-time music analysis — displaying BPM, chord, confidence, and a waveform visualizer in the extension popup.

---

## How It Works

```
YouTube Tab
    |
    | (tabCapture API)
    v
offscreen.html  <-- Web Audio API decodes raw PCM samples
    |
    | (chrome.runtime.sendMessage)
    v
background.js   <-- forwards audio payload
    |
    | (HTTP POST /analyze)
    v
local_service.py  <-- FastAPI server on http://127.0.0.1:8765
    |
    | uses madmom (RNN beat detection) + chroma analysis
    v
popup.html  <-- displays BPM / Chord / Confidence / Waveform
```

### Components

| Component | Description |
|---|---|
| `manifest.json` | Chrome Extension (Manifest V3), permissions: `activeTab`, `scripting`, `tabCapture`, `offscreen` |
| `background.js` | Service worker — starts/stops analysis, manages offscreen document, relays results to popup |
| `offscreen.html/js` | Offscreen document — captures tab audio stream, runs Web Audio API, sends PCM chunks to background |
| `popup.js / index.html` | Extension popup — shows BPM, chord, confidence, waveform canvas, start/stop button |
| `local_service.py` | FastAPI server — receives audio samples, runs BPM + chord estimation, returns JSON |

### Analysis Pipeline

**BPM Detection (primary — madmom)**
- Uses `madmom.features.beats.RNNBeatProcessor` (RNN-based beat tracking)
- Falls back to autocorrelation-based envelope analysis if madmom is unavailable

**Chord Detection (custom)**
- Builds a 12-bin chroma vector from FFT magnitudes (40–5000 Hz)
- Scores all 24 major/minor chord templates via dot product
- Returns root note + quality (e.g. `C`, `Am`) with normalized confidence

**Local Service Endpoint**
- `GET /health` — returns `{ ok: true, madmom: true/false }`
- `POST /analyze` — accepts `{ sampleRate, samples[], spectrum[], energy, timestampMs }`, returns `{ bpm, chord, confidence, energy, timestampMs }`

---

## Requirements

### Chrome Extension
- Google Chrome (Manifest V3 support — Chrome 88+)
- No build step required; load unpacked from `MyExtension/`

### Python Service
- **Python 3.11** (required — madmom is not compatible with Python 3.12+)
- See [INSTALL.md](INSTALL.md) for full setup instructions

#### Python packages (`MyExtension/requirements.txt`)
```
fastapi==0.115.0
uvicorn[standard]==0.30.6
numpy==2.1.1
cython
madmom==0.16.1
```

---

## Quick Start

1. **Set up the Python service** — follow [INSTALL.md](INSTALL.md)
2. **Start the service:**
   ```powershell
   .\.venv\Scripts\python.exe local_service.py
   ```
   Or with uvicorn directly:
   ```powershell
   .\.venv\Scripts\uvicorn.exe local_service:app --host 127.0.0.1 --port 8765
   ```
3. **Load the Chrome extension:**
   - Go to `chrome://extensions/`
   - Enable **Developer mode**
   - Click **Load unpacked** → select the `MyExtension/` folder
4. **Open a YouTube tab** and click the extension icon → press **Start**

---

## Project Structure

```
NewChrome/
├── README.md
├── INSTALL.md
├── MyExtension/
│   ├── manifest.json       # Chrome extension manifest (MV3)
│   ├── background.js       # Service worker
│   ├── offscreen.html      # Offscreen document host
│   ├── offscreen.js        # Web Audio capture + PCM extraction
│   ├── index.html          # Popup UI
│   ├── popup.js            # Popup logic
│   ├── local_service.py    # FastAPI music analysis server
│   ├── requirements.txt    # Python dependencies
│   └── .venv/              # Python virtual environment (not committed)
└── madmom/                 # madmom library source (reference/dev)
```
