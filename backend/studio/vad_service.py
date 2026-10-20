"""
TranStream Silero VAD Speech Segmentation Service (Phase 4).

Takes a normalized 16 kHz mono PCM WAV file and returns a list of
speech segments with start/end timestamps in seconds and milliseconds.

Uses the ONNX runtime (not PyTorch) for a ~30 MB memory footprint
instead of ~500 MB+. The model is loaded lazily on first use and
cached as a module-level singleton.
"""

import logging
from pathlib import Path

import numpy as np
import soundfile as sf
from django.conf import settings

logger = logging.getLogger(__name__)

# ─── Singleton model ───────────────────────────────────────────────────────

_vad_model = None
_vad_model_loaded = False


def _get_vad_model():
    """
    Lazily load the Silero VAD ONNX model.

    Returns the model object, or None if loading failed.
    Thread-safe in practice because Django's ASGI server runs
    a single event loop and sync_to_async serializes through
    one thread executor.
    """
    global _vad_model, _vad_model_loaded

    if _vad_model_loaded:
        return _vad_model

    try:
        # Correct API for silero-vad >= 5.x: pass onnx=True
        from silero_vad import load_silero_vad, get_speech_timestamps

        _vad_model = load_silero_vad(onnx=True)
        _vad_model_loaded = True
        logger.info("Silero VAD ONNX model loaded successfully")
        return _vad_model

    except ImportError as exc:
        logger.error(
            "silero-vad or onnxruntime not installed. "
            "Run: pip install silero-vad onnxruntime soundfile"
        )
        _vad_model_loaded = True
        return None

    except Exception as exc:
        logger.exception("Failed to load Silero VAD ONNX model: %s", exc)
        _vad_model_loaded = True
        return None


# ─── Public API ────────────────────────────────────────────────────────────

class VADError(Exception):
    """Raised when VAD processing fails."""
    pass


def extract_speech_segments(wav_path: Path) -> list[dict]:
    """
    Run Silero VAD on a 16 kHz mono PCM WAV file.

    Args:
        wav_path: Absolute path to the normalized WAV file.

    Returns:
        List of segment dicts, each with keys:
            index          int
            start_seconds  float  (e.g. 0.520)
            end_seconds    float  (e.g. 2.310)
            start_ms       int    (e.g. 520)
            end_ms         int    (e.g. 2310)
            duration_ms    int    (e.g. 1790)

    Raises:
        VADError on any failure (missing file, wrong format, model error).
    """
    wav_path = Path(wav_path).resolve()

    if not wav_path.exists():
        raise VADError(f"WAV file not found: {wav_path}")

    # ── Read audio ──
    try:
        audio, sample_rate = sf.read(str(wav_path), dtype="float32")
    except Exception as exc:
        raise VADError(f"Failed to read WAV file: {exc}") from exc

    if sample_rate != 16000:
        raise VADError(
            f"Expected 16000 Hz sample rate, got {sample_rate} Hz. "
            f"Run FFmpeg normalization first."
        )

    # Mono: if stereo, take first channel
    if audio.ndim > 1:
        audio = audio[:, 0]

    # Ensure contiguous float32 in [-1, 1]
    audio = np.ascontiguousarray(audio, dtype=np.float32)

    total_duration_s = len(audio) / sample_rate

    if total_duration_s < 0.1:
        logger.warning(
            "Audio too short for VAD (%.3f s): %s",
            total_duration_s,
            wav_path.name,
        )
        return []

    # ── Load model ──
    model = _get_vad_model()

    if model is None:
        raise VADError(
            "Silero VAD model is not available. "
            "Install with: pip install silero-vad onnxruntime soundfile"
        )

    # ── Run VAD ──
    try:
        from silero_vad import get_speech_timestamps

        raw_segments = get_speech_timestamps(
            audio,
            model,
            sampling_rate=sample_rate,
            threshold=getattr(settings, "VAD_THRESHOLD", 0.5),
            min_silence_duration_ms=getattr(settings, "VAD_MIN_SILENCE_MS", 500),
            speech_pad_ms=getattr(settings, "VAD_SPEECH_PAD_MS", 30),
            return_seconds=True,
        )

    except Exception as exc:
        raise VADError(f"Silero VAD inference failed: {exc}") from exc

    # ── Build result list ──
    segments = []

    for i, seg in enumerate(raw_segments):
        start_s = float(seg["start"])
        end_s = float(seg["end"])

        # Clamp to audio bounds
        start_s = max(0.0, min(start_s, total_duration_s))
        end_s = max(start_s, min(end_s, total_duration_s))

        segments.append({
            "index": i,
            "start_seconds": round(start_s, 3),
            "end_seconds": round(end_s, 3),
            "start_ms": int(round(start_s * 1000)),
            "end_ms": int(round(end_s * 1000)),
            "duration_ms": int(round((end_s - start_s) * 1000)),
        })

    logger.info(
        "VAD complete: %s → %d segments, %.3f s total speech",
        wav_path.name,
        len(segments),
        sum(s["duration_ms"] for s in segments) / 1000.0,
    )

    return segments


def vad_model_status() -> dict:
    """
    Return current VAD model status for diagnostics.

    Does NOT trigger model loading.
    """
    return {
        "loaded": _vad_model_loaded,
        "available": _vad_model is not None,
        "backend": "onnxruntime",
    }
