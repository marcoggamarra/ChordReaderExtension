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
# We accumulate raw audio samples in a sliding window (~10 s) so that both
# madmom and the fallback autocorrelation have enough context for reliable
# tempo estimation.  BPM is re-computed at most every 2 seconds to save CPU;
# intermediate /analyze calls return the cached value.
# ---------------------------------------------------------------------------
_state_lock = threading.Lock()
_onset_accumulator: collections.deque = collections.deque(maxlen=20)
_bpm_history: collections.deque = collections.deque(maxlen=10)

# Raw sample sliding window — keeps the last ~10 s of audio
_SAMPLE_WINDOW_SECONDS = 10
_sample_buffer: list[float] = []
_sample_rate_hint: int = 48000  # updated on first chunk

# BPM computation throttle
_last_bpm_time: float = 0.0
_BPM_RECOMPUTE_INTERVAL: float = 2.0  # seconds
_cached_bpm: float = 0.0
_cached_bpm_confidence: float = 0.0

# Chord stabilisation state
_chroma_ema: np.ndarray | None = None       # exponential moving avg of chroma
_CHROMA_EMA_ALPHA: float = 0.6              # blend factor for new chroma (higher = more responsive)
_current_chord: str = "N"
_current_chord_confidence: float = 0.0
_candidate_chord: str = "N"                 # chord that is trying to replace the current one
_candidate_count: int = 0                   # consecutive frames the candidate has won
_CHORD_CONFIRM_FRAMES: int = 1             # just 1 frame confirmation (~200ms)
_CHORD_HYSTERESIS: float = 0.008           # very small margin to prevent micro-flicker


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
    global _sample_buffer, _sample_rate_hint
    global _last_bpm_time, _cached_bpm, _cached_bpm_confidence
    global _chroma_ema, _current_chord, _current_chord_confidence
    global _candidate_chord, _candidate_count
    with _state_lock:
        _onset_accumulator.clear()
        _bpm_history.clear()
        _sample_buffer = []
        _sample_rate_hint = 48000
        _last_bpm_time = 0.0
        _cached_bpm = 0.0
        _cached_bpm_confidence = 0.0
        _chroma_ema = None
        _current_chord = "N"
        _current_chord_confidence = 0.0
        _candidate_chord = "N"
        _candidate_count = 0
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
# BPM estimation — uses the sliding-window sample buffer
# ---------------------------------------------------------------------------

def _accumulate_samples(samples: np.ndarray, sample_rate: int) -> np.ndarray:
    """Append *samples* to the global sliding window and return the full buffer."""
    global _sample_buffer, _sample_rate_hint
    _sample_rate_hint = sample_rate
    max_samples = int(_SAMPLE_WINDOW_SECONDS * sample_rate)

    _sample_buffer.extend(samples.tolist())
    if len(_sample_buffer) > max_samples:
        _sample_buffer = _sample_buffer[-max_samples:]

    return np.array(_sample_buffer, dtype=np.float32)


def estimate_bpm(samples: np.ndarray, sample_rate: int) -> tuple[float, float]:
    import time as _time

    global _last_bpm_time, _cached_bpm, _cached_bpm_confidence

    with _state_lock:
        full_buffer = _accumulate_samples(samples, sample_rate)
        buffer_duration = len(full_buffer) / sample_rate

        # Return cached BPM while we're still accumulating (<5 s)
        # or if we re-computed recently (<2 s)
        now = _time.monotonic()
        need_recompute = (
            now - _last_bpm_time >= _BPM_RECOMPUTE_INTERVAL
            or _cached_bpm <= 0.0
        )

        if buffer_duration < 5.0:
            if _cached_bpm > 0.0:
                return _cached_bpm, _cached_bpm_confidence
            return 0.0, 0.0

        if not need_recompute:
            return _cached_bpm, _cached_bpm_confidence

    # --- madmom path (best quality) — run on full accumulated buffer ---
    if madmom is not None:
        try:
            signal = madmom.audio.signal.Signal(
                full_buffer, sample_rate=sample_rate, num_channels=1
            )
            activations = madmom.features.beats.RNNBeatProcessor()(signal)
            tempo_candidates = madmom.features.tempo.TempoEstimationProcessor(
                fps=100
            )(activations)

            if len(tempo_candidates) > 0:
                top = tempo_candidates[0]
                bpm = (
                    float(top[0])
                    if isinstance(top, (list, tuple, np.ndarray))
                    else float(top)
                )
                confidence = (
                    float(top[1])
                    if isinstance(top, (list, tuple, np.ndarray)) and len(top) > 1
                    else 0.8
                )
                with _state_lock:
                    _bpm_history.append(bpm)
                    smoothed = _smooth_bpm(_bpm_history)
                    _cached_bpm = smoothed
                    _cached_bpm_confidence = float(np.clip(confidence, 0.0, 1.0))
                    _last_bpm_time = _time.monotonic()
                return _cached_bpm, _cached_bpm_confidence
        except Exception:
            pass

    # --- fallback: spectral-flux onset autocorrelation on full buffer ---
    if full_buffer.size < 1024:
        return 0.0, 0.0

    hop = max(1, sample_rate // 100)
    onset = _compute_onset_strength(full_buffer, hop=hop)

    if onset.size < 2:
        return 0.0, 0.0

    onset -= np.mean(onset)
    std = np.std(onset)
    if std < 1e-9:
        return 0.0, 0.0
    onset /= std

    fps = sample_rate / hop
    # Search 40–220 BPM (covers slow ballads through fast punk)
    min_lag = max(1, int(fps * 60.0 / 220.0))
    max_lag = min(len(onset) - 1, int(fps * 60.0 / 40.0))

    if max_lag <= min_lag:
        return 0.0, 0.0

    corr = np.correlate(onset, onset, mode="full")
    corr = corr[corr.size // 2 :]

    search = corr[min_lag : max_lag + 1]
    best_idx = int(np.argmax(search))
    best_lag = min_lag + best_idx
    bpm = 60.0 * fps / best_lag
    confidence = float(search[best_idx] / (corr[0] + 1e-9))

    # Prefer half-time or double-time if they score noticeably higher
    for mult in (0.5, 2.0):
        alt_bpm = bpm * mult
        if 40.0 <= alt_bpm <= 220.0:
            alt_lag = int(fps * 60.0 / alt_bpm)
            if min_lag <= alt_lag <= max_lag and alt_lag < len(corr):
                alt_score = float(corr[alt_lag] / (corr[0] + 1e-9))
                if alt_score > confidence * 1.05:
                    bpm = alt_bpm
                    confidence = alt_score

    with _state_lock:
        _bpm_history.append(bpm)
        smoothed = _smooth_bpm(_bpm_history)
        _cached_bpm = smoothed
        _cached_bpm_confidence = float(np.clip(confidence, 0.0, 1.0))
        _last_bpm_time = _time.monotonic()

    return _cached_bpm, _cached_bpm_confidence


# ---------------------------------------------------------------------------
# Chord estimation — with chroma smoothing, hysteresis and confirmation
# ---------------------------------------------------------------------------

_NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

_MAJOR_TEMPLATE = np.zeros(12)
_MAJOR_TEMPLATE[[0, 4, 7]] = [1.0, 0.8, 0.9]
_MINOR_TEMPLATE = np.zeros(12)
_MINOR_TEMPLATE[[0, 3, 7]] = [1.0, 0.8, 0.9]


def _raw_chroma(samples: np.ndarray, sample_rate: int) -> np.ndarray | None:
    """Compute an L1-normalised 12-bin chroma vector from *samples*."""
    if samples.size < 2048:
        return None

    window = np.hanning(samples.size)
    spectrum = np.fft.rfft(samples * window)
    log_mag = np.log1p(np.abs(spectrum))
    freqs = np.fft.rfftfreq(samples.size, d=1.0 / sample_rate)

    chroma = np.zeros(12, dtype=np.float64)
    for i in range(1, len(freqs)):
        freq = freqs[i]
        if freq < 65.0 or freq > 2000.0:
            continue
        midi = 69.0 + 12.0 * np.log2(freq / 440.0)
        pitch_class = int(round(midi)) % 12
        chroma[pitch_class] += log_mag[i]

    chroma_sum = np.sum(chroma)
    if chroma_sum < 1e-9:
        return None
    chroma /= chroma_sum
    return chroma


def _best_chord(chroma: np.ndarray) -> tuple[str, float, float]:
    """Template-match a chroma vector against major/minor triads.
    Returns (name, raw_score, confidence_0_to_1)."""
    best_name = "N"
    best_score = -1.0

    for root in range(12):
        maj = float(np.dot(chroma, np.roll(_MAJOR_TEMPLATE, root)))
        min_ = float(np.dot(chroma, np.roll(_MINOR_TEMPLATE, root)))

        if maj > best_score:
            best_score = maj
            best_name = _NOTE_NAMES[root]

        if min_ > best_score:
            best_score = min_
            best_name = f"{_NOTE_NAMES[root]}m"

    confidence = float(np.clip(best_score / 0.075, 0.0, 1.0))
    return best_name, best_score, confidence


def _chord_score(chord_name: str, chroma: np.ndarray) -> float:
    """Return the raw template dot-product score for a specific chord."""
    for root in range(12):
        name = _NOTE_NAMES[root]
        if chord_name == name:
            return float(np.dot(chroma, np.roll(_MAJOR_TEMPLATE, root)))
        if chord_name == f"{name}m":
            return float(np.dot(chroma, np.roll(_MINOR_TEMPLATE, root)))
    return 0.0


def estimate_chord(samples: np.ndarray, sample_rate: int) -> tuple[str, float]:
    global _chroma_ema, _current_chord, _current_chord_confidence
    global _candidate_chord, _candidate_count

    raw = _raw_chroma(samples, sample_rate)
    if raw is None:
        return _current_chord, _current_chord_confidence

    # --- Exponential moving average on chroma ---
    with _state_lock:
        if _chroma_ema is None:
            _chroma_ema = raw.copy()
        else:
            _chroma_ema = _CHROMA_EMA_ALPHA * raw + (1.0 - _CHROMA_EMA_ALPHA) * _chroma_ema

        smoothed_chroma = _chroma_ema.copy()

    new_chord, new_score, new_confidence = _best_chord(smoothed_chroma)

    # --- Hysteresis + confirmation window ---
    with _state_lock:
        if new_chord == _current_chord:
            # Same chord — just refresh confidence, reset candidate
            _current_chord_confidence = new_confidence
            _candidate_chord = "N"
            _candidate_count = 0
            return _current_chord, _current_chord_confidence

        # Different chord — check if it beats the current one by the
        # hysteresis margin (so transient melody notes don't cause flicker)
        current_score = _chord_score(_current_chord, smoothed_chroma)

        if new_score < current_score + _CHORD_HYSTERESIS:
            # New chord doesn't win by enough — keep current
            _candidate_chord = "N"
            _candidate_count = 0
            return _current_chord, _current_chord_confidence

        # New chord wins by the margin — start/continue confirmation
        if new_chord == _candidate_chord:
            _candidate_count += 1
        else:
            _candidate_chord = new_chord
            _candidate_count = 1

        if _candidate_count >= _CHORD_CONFIRM_FRAMES:
            _current_chord = new_chord
            _current_chord_confidence = new_confidence
            _candidate_chord = "N"
            _candidate_count = 0

        return _current_chord, _current_chord_confidence


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
