"""
TranStream Transcription Service (Phase 5).
Uses Faster-Whisper (CTranslate2) for lightweight, local,
high-performance speech-to-text and automatic language detection.
Bypasses the 'av' package by using soundfile + numpy, ensuring 
compatibility with Python 3.14.
"""
import logging
import numpy as np
import soundfile as sf
from django.conf import settings

logger = logging.getLogger(__name__)

_model = None

def get_model():
    global _model
    if _model is None:
        model_size = getattr(settings, "WHISPER_MODEL_SIZE", "tiny")
        device = getattr(settings, "WHISPER_DEVICE", "cpu")
        compute_type = getattr(settings, "WHISPER_COMPUTE_TYPE", "int8")
        logger.info(f"Loading Faster-Whisper model ({model_size}, {compute_type}, {device})...")
        try:
            from faster_whisper import WhisperModel
            _model = WhisperModel(model_size, device=device, compute_type=compute_type)
            logger.info("Faster-Whisper model loaded successfully")
        except ImportError:
            logger.error("faster-whisper not installed. Run: pip install faster-whisper")
            raise
    return _model

class TranscriptionError(Exception):
    pass

def transcribe_audio_progressive(wav_path, result_queue):
    """
    Runs transcription in a background thread and puts progressive 
    results into the provided thread-safe queue for real-time UI updates.
    """
    try:
        audio, sample_rate = sf.read(str(wav_path), dtype="float32")
    except Exception as exc:
        result_queue.put({"error": f"Failed to read WAV file: {exc}"})
        return

    if sample_rate != 16000:
        result_queue.put({"error": f"Expected 16000 Hz, got {sample_rate} Hz."})
        return

    if audio.ndim > 1:
        audio = audio[:, 0]
    audio = np.ascontiguousarray(audio, dtype=np.float32)

    model = get_model()

    try:
        # Pass numpy array directly, bypassing 'av' entirely
        segments, info = model.transcribe(
            audio,
            beam_size=5,
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=500),
        )
        
        # Yield segments progressively as they are generated
        for seg in segments:
            result_queue.put({
                "type": "segment",
                "start_ms": int(round(seg.start * 1000)),
                "end_ms": int(round(seg.end * 1000)),
                "duration_ms": int(round((seg.end - seg.start) * 1000)),
                "text": seg.text.strip(),
            })
            
        # Signal completion with language info
        result_queue.put({
            "type": "complete",
            "language": info.language,
            "language_probability": info.language_probability,
        })
        
    except Exception as exc:
        result_queue.put({"error": f"Whisper transcription failed: {exc}"})
