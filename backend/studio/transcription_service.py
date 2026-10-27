"""
TranStream Transcription Service (Phase 5 / 5.5).

Single source of truth for speech-to-text + language identification.

CRITICAL DESIGN RULE (learned the hard way in this project):
    Faster-Whisper is ALWAYS fed a NumPy float32 array, NEVER a file path.

    model.transcribe("<path>") internally calls
        faster_whisper.audio.decode_audio -> av.open()
    which (a) reintroduces the PyAV/Python-3.14 fragility that produced
    "open() got an unexpected keyword argument 'metadata_errors'", and
    (b) attempts to demux containers that may be truncated/incomplete.

    By decoding with soundfile ourselves and handing over a contiguous
    float32 ndarray, the `av` package is never touched on this path.
    `av` remains installed only because faster_whisper's module import
    graph references it; it is not exercised here.

Thread-safety:
    The model is a process-wide singleton guarded by a lock, because the
    live-preview task and the post-stop task can legitimately overlap on
    the first ever invocation (model download/load ~10-15 s cold).
"""

import logging
import threading
from pathlib import Path

import numpy as np
import soundfile as sf
from django.conf import settings

logger = logging.getLogger(__name__)

# ─── Process-wide model singleton ──────────────────────────────────────────

_model = None
_model_lock = threading.Lock()
_model_failed = False          # sticky: do not retry a broken install forever


def get_model():
    """
    Lazily construct the CTranslate2 Whisper model exactly once.

    Returns the model instance.
    Raises TranscriptionError (never ImportError) so callers on worker
    threads get a clean, catchable failure instead of a raw import leak.
    """
    global _model, _model_failed

    if _model is not None:
        return _model
    if _model_failed:
        raise TranscriptionError("Faster-Whisper model unavailable (previous load failed).")

    # Double-checked locking: only one thread pays the cold-load cost.
    with _model_lock:
        if _model is not None:
            return _model
        if _model_failed:
            raise TranscriptionError("Faster-Whisper model unavailable (previous load failed).")

        model_size = str(getattr(settings, "WHISPER_MODEL_SIZE", "tiny"))
        device = str(getattr(settings, "WHISPER_DEVICE", "cpu"))
        compute_type = str(getattr(settings, "WHISPER_COMPUTE_TYPE", "int8"))

        logger.info(
            "Loading Faster-Whisper model (size=%s, device=%s, compute=%s)...",
            model_size, device, compute_type,
        )
        try:
            from faster_whisper import WhisperModel
            _model = WhisperModel(model_size, device=device, compute_type=compute_type)
            logger.info("Faster-Whisper model loaded successfully")
        except ImportError as exc:
            _model_failed = True
            logger.error("faster-whisper not importable: %s", exc)
            raise TranscriptionError(
                "faster-whisper is not installed. Run: pip install faster-whisper"
            ) from exc
        except Exception as exc:
            _model_failed = True
            logger.exception("Faster-Whisper model load failed: %s", exc)
            raise TranscriptionError(f"Faster-Whisper model load failed: {exc}") from exc

    return _model


class TranscriptionError(Exception):
    """Raised when transcription fails. Always catchable, never a raw import error."""
    pass


# ─── Audio loading (soundfile -> ndarray; NO av) ───────────────────────────

def _load_pcm16k_mono(wav_path: Path) -> np.ndarray:
    """
    Read a WAV file into a contiguous float32 mono array at 16 kHz.

    Raises TranscriptionError on any format problem so the caller never
    has to distinguish IO errors from inference errors.
    """
    try:
        audio, sample_rate = sf.read(str(wav_path), dtype="float32")
    except Exception as exc:
        raise TranscriptionError(f"Failed to read audio file {wav_path.name}: {exc}") from exc

    if sample_rate != 16000:
        raise TranscriptionError(
            f"Expected 16000 Hz for {wav_path.name}, got {sample_rate} Hz. "
            f"Run FFmpeg normalization first (Phase 3)."
        )

    if audio.ndim > 1:          # defensive: stereo slips through if a caller forgets
        audio = audio[:, 0]

    audio = np.ascontiguousarray(audio, dtype=np.float32)

    if audio.size == 0:
        raise TranscriptionError(f"Audio file {wav_path.name} is empty.")

    return audio


# ─── Public API ────────────────────────────────────────────────────────────

def transcribe_audio(wav_path: Path, beam_size: int = 5, language: str | None = None,
                       initial_prompt: str | None = None) -> dict:
    """
    Transcribe a normalized 16 kHz mono PCM WAV.

    Args:
        wav_path:  path to a *complete* WAV produced by Phase 3 normalization.
        beam_size: 5 for the authoritative post-stop pass (best accuracy);
                   1 for the live rolling preview (best latency).
        language:  ISO-639-1 code to force, or None to take the WHISPER_LANGUAGE
                   setting (which itself defaults to "" = auto-detect). Callers
                   that pass nothing behave exactly as before. Pin it when the
                   audio language is known: detection is a guess, and on
                   code-switched speech it guesses wrong.
        initial_prompt:
                   fixed vocabulary to bias the decoder toward, or None to take
                   the WHISPER_INITIAL_PROMPT setting (default "" = the library's
                   own no-prompt behaviour). Same resolution order as language,
                   and callers that pass nothing behave exactly as before. Use it
                   for names and domain terms that a larger model still gets
                   wrong; keep it short, because it occupies part of the
                   decoder's context. It is NOT independent of language: with the
                   pin unset, a prompt in one language can bias detection toward
                   that language, so set WHISPER_LANGUAGE whenever you set this.

    Returns:
        {
          "language": str,                 # ISO-639-1, e.g. "en"
          "language_probability": float,
          "segments": [
             {"index","start_seconds","end_seconds",
              "start_ms","end_ms","duration_ms","text"}, ...
          ],
        }

    Raises:
        TranscriptionError on read/format/inference failure.
    """
    wav_path = Path(wav_path).resolve()
    if not wav_path.exists():
        raise TranscriptionError(f"Audio file not found: {wav_path}")

    audio = _load_pcm16k_mono(wav_path)      # <- ndarray, so av is never invoked
    model = get_model()

    # Explicit argument > deployment setting > auto-detect. The empty-string case
    # maps to None so that WHISPER_LANGUAGE= in a shell means "detect", not "the
    # language literally called empty". None from the library is its own
    # auto-detect default (faster-whisper 1.2.1 signature, read not recalled).
    lang = language if language is not None else (
        str(getattr(settings, "WHISPER_LANGUAGE", "")).strip() or None)
    # Two states, and the collapse is applied to the RESOLVED value rather than
    # to the setting alone:
    #   None or ""  -> no prompt, which is the library's own no-prompt behaviour
    #   text        -> use it
    # The setting's "" and any whitespace-only value collapse via .strip() or
    # None, and so does an explicit "" argument - on purpose. Reading
    # WhisperModel.transcribe could not settle how the library treats an empty
    # string: it contains no guard on the knob. It forwards the value to the
    # internal implementation, which was not read. Rather than guess, the code
    # avoids the unverified path entirely, so the question is moot rather than
    # merely unobserved. This is also where the knob differs from
    # WHISPER_LANGUAGE above, whose empty string would be
    # "the language literally called empty" and is collapsed on the setting
    # path only.
    prompt = (initial_prompt if initial_prompt is not None else
              str(getattr(settings, "WHISPER_INITIAL_PROMPT", ""))).strip() or None

    try:
        segments_iter, info = model.transcribe(
            audio,                            # NOTE: array, not path
            language=lang,                    # None -> the library auto-detect
            initial_prompt=prompt,            # None -> the library's no-prompt path
            beam_size=max(1, int(beam_size)),
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=500),
        )

        result_segments = []
        for idx, seg in enumerate(segments_iter):
            text = (seg.text or "").strip()
            if not text:
                continue
            result_segments.append({
                "index": idx,
                "start_seconds": round(float(seg.start), 3),
                "end_seconds": round(float(seg.end), 3),
                "start_ms": int(round(float(seg.start) * 1000)),
                "end_ms": int(round(float(seg.end) * 1000)),
                "duration_ms": int(round((float(seg.end) - float(seg.start)) * 1000)),
                "text": text,
            })
    except TranscriptionError:
        raise
    except Exception as exc:
        raise TranscriptionError(f"Whisper inference failed: {exc}") from exc

    logger.info(
        "Transcription complete: %s -> lang=%s (%.2f), %d segment(s), beam=%d",
        wav_path.name, info.language, info.language_probability,
        len(result_segments), beam_size,
    )

    return {
        "language": info.language,
        "language_probability": float(info.language_probability),
        "segments": result_segments,
    }


def join_segments(segments: list[dict]) -> str:
    """Flatten a segment list into a single readable line (used by live preview)."""
    return " ".join(s["text"].strip() for s in segments if s.get("text")).strip()
