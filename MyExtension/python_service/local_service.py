from typing import List
import collections
import collections.abc
import threading

import numpy as np
from fastapi import FastAPI
from pydantic import BaseModel
from starlette.middleware.cors import CORSMiddleware

if not hasattr(np, "float"):
    np.float = float  # type: ignore[attr-defined]
if not hasattr(np, "int"):
    np.int = int  # type: ignore[attr-defined]
if not hasattr(np, "complex"):
    np.complex = complex  # type: ignore[attr-defined]

MADMOM_IMPORT_ERROR = None

for name in ("Mapping", "MutableMapping", "Sequence", "MutableSequence"):
    if not hasattr(collections, name):
        setattr(collections, name, getattr(collections.abc, name))

try:
    import madmom
except Exception as error:  # pragma: no cover
    madmom = None
    MADMOM_IMPORT_ERROR = str(error)


class AnalyzeRequest(BaseModel):
    sampleRate: int
    samples: List[float]
    spectrum: List[float] = []
    energy: float = 0.0
    timestampMs: int = 0


app = FastAPI(title="Local Music Analyzer")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Server-side state for BPM estimation
# Each audio chunk is ~0.5 s — too short on its own.  We accumulate onset
# strength vectors across up to 20 chunks (~6 s) before running autocorrelation,
# and keep a short history of confirmed BPM values to further smooth output.
# ---------------------------------------------------------------------------
_state_lock = threading.Lock()
_onset_accumulator: collections.deque = collections.deque(maxlen=20)
_bpm_history: collections.deque = collections.deque(maxlen=10)


@app.get("/")
def root() -> dict:
    return {
        "service": "Local Music Analyzer",
        "status": "running",
        "endpoints": {
            "health": "/health",
            "analyze": "/analyze",
            "reset": "/reset",
            "docs": "/docs",
        },
    }


@app.post("/reset")
def reset_state() -> dict:
    """Clear accumulated BPM state. Call when a new song starts."""
    with _state_lock:
        _onset_accumulator.clear()
        _bpm_history.clear()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _compute_onset_strength(samples: np.ndarray, hop: int = 441) -> np.ndarray:
    """
    Spectral-flux onset strength: half-wave-rectified sum of positive magnitude
    differences between successive STFT frames.  Much more discriminative than
    raw amplitude envelope — different rhythmic patterns produce genuinely
    different autocorrelation profiles.
    """
    fft_size = 1024
    num_frames = max(0, (len(samples) - fft_size) // hop)
    if num_frames < 2:
        return np.zeros(1)

    win = np.hanning(fft_size)
    prev_mag = None
    flux: List[float] = []

    for i in range(num_frames):
        frame = samples[i * hop : i * hop + fft_size]
        if len(frame) < fft_size:
            break
        mag = np.abs(np.fft.rfft(frame * win))
        if prev_mag is not None:
            flux.append(float(np.sum(np.maximum(0.0, mag - prev_mag))))
        else:
            flux.append(0.0)
        prev_mag = mag

    return np.array(flux, dtype=np.float64)


def _smooth_bpm(history: collections.deque) -> float:
    """
    Median of recent BPM values with octave folding.
    Folds every value into the 80-160 BPM reference band before taking the
    median, then maps the result back to the original octave — this prevents
    half-time / double-time flip-flops from inflating the reported value.
    """
    if not history:
        return 0.0

    values = np.array(list(history), dtype=np.float64)
    if len(values) == 1:
        return float(values[0])

    folded = values.copy()
    for i in range(len(folded)):
        while folded[i] > 160.0:
            folded[i] /= 2.0
        while folded[i] < 80.0:
            folded[i] *= 2.0

    median_fold = float(np.median(folded))

    result = [v * (median_fold / f) for v, f in zip(values, folded)]
    return float(np.median(result))


# ---------------------------------------------------------------------------
# BPM estimation
# ---------------------------------------------------------------------------

def estimate_bpm(samples: np.ndarray, sample_rate: int) -> tuple[float, float]:
    # --- madmom path (best quality) ---
    if madmom is not None:
        try:
            signal = madmom.audio.signal.Signal(samples, sample_rate=sample_rate, num_channels=1)
            activations = madmom.features.beats.RNNBeatProcessor()(signal)
            tempo_candidates = madmom.features.tempo.TempoEstimationProcessor(fps=100)(activations)

            if len(tempo_candidates) > 0:
                top = tempo_candidates[0]
                bpm = float(top[0]) if isinstance(top, (list, tuple, np.ndarray)) else float(top)
                confidence = (
                    float(top[1])
                    if isinstance(top, (list, tuple, np.ndarray)) and len(top) > 1
                    else 0.8
                )
                with _state_lock:
                    _bpm_history.append(bpm)
                    smoothed = _smooth_bpm(_bpm_history)
                return smoothed, float(np.clip(confidence, 0.0, 1.0))
        except Exception:
            pass

    # --- fallback: spectral-flux onset autocorrelation ---
    if samples.size < 1024:
        return 0.0, 0.0

    # hop ~10 ms at 44100 Hz — fine enough for beat-level autocorrelation
    hop = max(1, sample_rate // 100)
    onset = _compute_onset_strength(samples, hop=hop)

    with _state_lock:
        _onset_accumulator.append(onset)

        # Wait for at least ~1.5 s of accumulated data before first estimate
        if len(_onset_accumulator) < 5:
            if _bpm_history:
                return float(_bpm_history[-1]), 0.3
            return 0.0, 0.0

        accumulated = np.concatenate(list(_onset_accumulator))

    accumulated -= np.mean(accumulated)
    std = np.std(accumulated)
    if std < 1e-9:
        return 0.0, 0.0
    accumulated /= std

    fps = sample_rate / hop
    min_lag = max(1, int(fps * 60.0 / 200.0))
    max_lag = min(len(accumulated) - 1, int(fps * 60.0 / 60.0))

    if max_lag <= min_lag:
        return 0.0, 0.0

    corr = np.correlate(accumulated, accumulated, mode="full")
    corr = corr[corr.size // 2 :]

    search = corr[min_lag : max_lag + 1]
    best_idx = int(np.argmax(search))
    best_lag = min_lag + best_idx
    bpm = 60.0 * fps / best_lag
    confidence = float(search[best_idx] / (corr[0] + 1e-9))

    # Prefer half-time or double-time if they score noticeably higher
    for mult in (0.5, 2.0):
        alt_bpm = bpm * mult
        if 60.0 <= alt_bpm <= 200.0:
            alt_lag = int(fps * 60.0 / alt_bpm)
            if min_lag <= alt_lag <= max_lag and alt_lag < len(corr):
                alt_score = float(corr[alt_lag] / (corr[0] + 1e-9))
                if alt_score > confidence * 1.05:
                    bpm = alt_bpm
                    confidence = alt_score

    with _state_lock:
        _bpm_history.append(bpm)
        smoothed = _smooth_bpm(_bpm_history)

    return smoothed, float(np.clip(confidence, 0.0, 1.0))


# ---------------------------------------------------------------------------
# Chord estimation
# ---------------------------------------------------------------------------

def estimate_chord(samples: np.ndarray, sample_rate: int) -> tuple[str, float]:
    if samples.size < 2048:
        return "N", 0.0

    window = np.hanning(samples.size)
    spectrum = np.fft.rfft(samples * window)
    # Log magnitude compresses the dynamic range so soft harmonics aren't
    # drowned out by the loudest partial — yields cleaner chroma vectors.
    log_mag = np.log1p(np.abs(spectrum))
    freqs = np.fft.rfftfreq(samples.size, d=1.0 / sample_rate)

    chroma = np.zeros(12, dtype=np.float64)

    for i in range(1, len(freqs)):
        freq = freqs[i]
        # 65 Hz ≈ C2, 2000 Hz covers fundamentals + first few harmonics up to ~C7
        if freq < 65.0 or freq > 2000.0:
            continue
        midi = 69.0 + 12.0 * np.log2(freq / 440.0)
        pitch_class = int(round(midi)) % 12
        chroma[pitch_class] += log_mag[i]

    chroma_sum = np.sum(chroma)
    if chroma_sum < 1e-9:
        return "N", 0.0

    # L1-normalize so template matching is independent of absolute loudness
    chroma /= chroma_sum

    note_names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

    # Slightly weight the fifth (index 7) more than the third — the fifth is
    # more acoustically stable and less ambiguous between major and minor.
    major_template = np.zeros(12)
    major_template[[0, 4, 7]] = [1.0, 0.8, 0.9]
    minor_template = np.zeros(12)
    minor_template[[0, 3, 7]] = [1.0, 0.8, 0.9]

    best_name = "N"
    best_score = -1.0

    for root in range(12):
        maj = float(np.dot(chroma, np.roll(major_template, root)))
        min_ = float(np.dot(chroma, np.roll(minor_template, root)))

        if maj > best_score:
            best_score = maj
            best_name = note_names[root]

        if min_ > best_score:
            best_score = min_
            best_name = f"{note_names[root]}m"

    # A perfect 3-note chord in isolation scores ~(1+0.8+0.9)/(3*12) ≈ 0.075
    # after L1 normalization over 12 pitch classes.  Scale so that score gives ~1.0.
    confidence = float(np.clip(best_score / 0.075, 0.0, 1.0))
    return best_name, confidence


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "madmom": madmom is not None,
        "madmomError": MADMOM_IMPORT_ERROR,
    }


@app.post("/analyze")
def analyze(payload: AnalyzeRequest) -> dict:
    samples = np.array(payload.samples, dtype=np.float32)

    if samples.size == 0:
        return {
            "bpm": 0.0,
            "chord": "N",
            "confidence": 0.0,
            "energy": 0.0,
            "timestampMs": payload.timestampMs,
        }

    bpm, bpm_confidence = estimate_bpm(samples, payload.sampleRate)
    chord, chord_confidence = estimate_chord(samples, payload.sampleRate)

    confidence = float(np.clip((bpm_confidence + chord_confidence) / 2.0, 0.0, 1.0))

    return {
        "bpm": bpm,
        "chord": chord,
        "confidence": confidence,
        "energy": float(payload.energy),
        "timestampMs": payload.timestampMs,
        "madmomAvailable": madmom is not None,
    }
