from typing import List
import collections
import collections.abc

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


@app.get("/")
def root() -> dict:
    return {
        "service": "Local Music Analyzer",
        "status": "running",
        "endpoints": {
            "health": "/health",
            "analyze": "/analyze",
            "docs": "/docs",
        },
    }


def estimate_bpm(samples: np.ndarray, sample_rate: int) -> tuple[float, float]:
    if madmom is not None:
        try:
            signal = madmom.audio.signal.Signal(samples, sample_rate=sample_rate, num_channels=1)
            activations = madmom.features.beats.RNNBeatProcessor()(signal)
            tempo_candidates = madmom.features.tempo.TempoEstimationProcessor(fps=100)(activations)

            if len(tempo_candidates) > 0:
                top = tempo_candidates[0]
                bpm = float(top[0]) if isinstance(top, (list, tuple, np.ndarray)) else float(top)
                confidence = float(top[1]) if isinstance(top, (list, tuple, np.ndarray)) and len(top) > 1 else 0.8
                return bpm, float(np.clip(confidence, 0.0, 1.0))
        except Exception:
            pass

    if samples.size < 1024:
        return 0.0, 0.0

    centered = samples - np.mean(samples)
    envelope = np.abs(centered)

    hop = max(1, int(sample_rate * 0.02))
    windowed = envelope[: len(envelope) - (len(envelope) % hop)].reshape(-1, hop).mean(axis=1)

    if windowed.size < 8:
        return 0.0, 0.0

    windowed = windowed - np.mean(windowed)
    corr = np.correlate(windowed, windowed, mode="full")
    corr = corr[corr.size // 2 :]

    min_bpm = 60
    max_bpm = 180

    min_lag = max(1, int((60 / max_bpm) * (sample_rate / hop)))
    max_lag = max(min_lag + 1, int((60 / min_bpm) * (sample_rate / hop)))

    search = corr[min_lag:max_lag]
    if search.size == 0:
        return 0.0, 0.0

    best_idx = int(np.argmax(search))
    best_lag = min_lag + best_idx

    bpm = 60.0 * (sample_rate / hop) / best_lag
    confidence = float(search[best_idx] / (np.max(corr) + 1e-9))

    return float(bpm), float(np.clip(confidence, 0.0, 1.0))


def estimate_chord(samples: np.ndarray, sample_rate: int) -> tuple[str, float]:
    if samples.size < 2048:
        return "N", 0.0

    window = np.hanning(samples.size)
    spectrum = np.fft.rfft(samples * window)
    magnitude = np.abs(spectrum)
    freqs = np.fft.rfftfreq(samples.size, d=1.0 / sample_rate)

    chroma = np.zeros(12, dtype=np.float64)

    for i in range(1, len(freqs)):
        freq = freqs[i]
        if freq < 40.0 or freq > 5000.0:
            continue

        midi = 69 + 12 * np.log2(freq / 440.0)
        pitch_class = int(round(midi)) % 12
        chroma[pitch_class] += magnitude[i]

    if np.allclose(chroma, 0):
        return "N", 0.0

    note_names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

    major_template = np.zeros(12)
    major_template[[0, 4, 7]] = 1.0
    minor_template = np.zeros(12)
    minor_template[[0, 3, 7]] = 1.0

    best_name = "N"
    best_score = -1.0
    total_energy = np.sum(chroma) + 1e-9

    for root in range(12):
        maj_score = float(np.dot(chroma, np.roll(major_template, root)))
        min_score = float(np.dot(chroma, np.roll(minor_template, root)))

        if maj_score > best_score:
            best_score = maj_score
            best_name = f"{note_names[root]}"

        if min_score > best_score:
            best_score = min_score
            best_name = f"{note_names[root]}m"

    confidence = float(np.clip(best_score / total_energy, 0.0, 1.0))
    return best_name, confidence


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
