"""
TranStream TTS Service (Phase 9/10).

Single source of truth for text-to-speech using Piper.

Architecture:
  * get_tts_engine() -> TTS engine instance (lazy import, cached)
  * synthesize_text(text, language, voice) -> bytes (PCM 16 kHz mono)
  * synthesize_segments(segments, language, voice) -> generator of audio chunks
    Streams segment-by-segment for real-time playback.

Thread-safety:
  The engine cache is a process-wide dict guarded by a lock.

Voice models:
  Piper voice models (.onnx + .json config) must be placed in TTS_VOICE_DIR
  (default: media/tts_voices/). The project ships with NONE by default.
  Download from https://github.com/rhasspy/piper/releases/tag/v1.2.0

Language codes:
  Uses ISO-639-1 codes. Voice mapping is configurable via TTS_VOICE_MAP.
"""

import logging
import threading
from pathlib import Path
from typing import Optional, Generator

from django.conf import settings

logger = logging.getLogger(__name__)

# ─── Process-wide engine cache ────────────────────────────────────────────────

_engines: dict[str, object] = {}  # voice_name -> PiperVoice
_engines_lock = threading.Lock()
_piper_available = None  # None=unknown, True/False=checked


class TTSError(Exception):
    """Raised when TTS synthesis fails. Always catchable, never a raw import error."""
    pass


def _load_piper() -> bool:
    """
    Import piper lazily.
    Returns True if available, False if not installed.
    """
    global _piper_available
    if _piper_available is not None:
        return _piper_available
    try:
        from piper import PiperVoice
        logger.info("Piper TTS loaded (deferred import)")
        _piper_available = True
    except ImportError as exc:
        logger.warning("Piper TTS not installed: %s", exc)
        _piper_available = False
    return _piper_available


def get_tts_engine(voice: str):
    """
    Get or create a PiperVoice instance for the given voice name.
    
    Args:
        voice: Voice identifier (e.g., "en_US-lessac-medium", "hi_IN-aryan-medium")
    
    Returns:
        PiperVoice instance with .synthesize(text) -> (audio_bytes, sample_rate)
    
    Raises:
        TTSError if Piper is not installed or voice not found.
    """
    if not _load_piper():
        raise TTSError(
            "Piper TTS not installed. Run: pip install piper-tts && "
            "download voice models to TTS_VOICE_DIR"
        )
    
    from piper import PiperVoice
    
    voice_key = voice.strip().lower()
    with _engines_lock:
        if voice_key in _engines:
            return _engines[voice_key]
    
    # Resolve voice path
    voice_dir = Path(getattr(settings, "TTS_VOICE_DIR", 
        Path(settings.MEDIA_ROOT) / "tts_voices"))
    voice_path = voice_dir / f"{voice}.onnx"
    config_path = voice_dir / f"{voice}.onnx.json"
    
    if not voice_path.exists():
        # Try voice map from settings
        voice_map = getattr(settings, "TTS_VOICE_MAP", {})
        mapped = voice_map.get(voice)
        if mapped:
            voice_path = voice_dir / f"{mapped}.onnx"
            config_path = voice_dir / f"{mapped}.onnx.json"
    
    if not voice_path.exists():
        raise TTSError(
            f"Voice model not found: {voice_path}. "
            f"Download from https://github.com/rhasspy/piper/releases/tag/v1.2.0"
        )
    
    # Load voice
    try:
        engine = PiperVoice.load(str(voice_path), config_path=str(config_path) if config_path.exists() else None)
        logger.info("Loaded Piper voice: %s", voice)
    except Exception as exc:
        raise TTSError(f"Failed to load Piper voice {voice}: {exc}") from exc
    
    with _engines_lock:
        _engines[voice_key] = engine
    return engine


def get_voice_for_language(language: str) -> str:
    """
    Map a target language code to a Piper voice name.
    Falls back to settings.TTS_VOICE_MAP or default voice.
    """
    voice_map = getattr(settings, "TTS_VOICE_MAP", {})
    if language in voice_map:
        return voice_map[language]
    # Default multilingual voice if available
    default = voice_map.get("default", "en_US-lessac-medium")
    return default


def synthesize_text(text: str, language: str = "en", voice: Optional[str] = None) -> bytes:
    """
    Synthesize text to PCM audio (16 kHz, 16-bit, mono).
    
    Args:
        text: Text to synthesize. Empty/whitespace returns empty bytes.
        language: ISO-639-1 target language code.
        voice: Optional explicit voice name. If None, uses get_voice_for_language.
    
    Returns:
        Raw PCM bytes (16 kHz, 16-bit, mono, little-endian).
    
    Raises:
        TTSError on synthesis failure.
    """
    text = (text or "").strip()
    if not text:
        return b""

    voice_name = voice or get_voice_for_language(language)
    engine = get_tts_engine(voice_name)

    try:
        # Piper's synthesize returns a generator of AudioChunk objects.
        # Each AudioChunk has audio_int16_bytes (already bytes) and sample_rate.
        audio_chunks = []
        sample_rate = None
        for chunk in engine.synthesize(text):
            audio_chunks.append(chunk.audio_int16_bytes)
            sample_rate = chunk.sample_rate
        audio_bytes = b"".join(audio_chunks)
        logger.debug("Synthesized %d chars -> %d bytes (%d Hz)", len(text), len(audio_bytes), sample_rate or 0)
        return audio_bytes
    except Exception as exc:
        raise TTSError(f"TTS synthesis failed: {exc}") from exc


def synthesize_segments(
    segments: list[dict],
    language: str = "en",
    voice: Optional[str] = None,
    silence_ms: int = 100
) -> Generator[bytes, None, None]:
    """
    Synthesize multiple segments sequentially, yielding audio chunks.
    
    For real-time streaming: each segment is synthesized and yielded
    as it completes, allowing playback to start before all segments finish.
    
    Args:
        segments: List of {"text": str, ...} segment dicts (from Whisper).
        language: Target language code.
        voice: Optional explicit voice name.
        silence_ms: Silence padding between segments (milliseconds).
    
    Yields:
        PCM audio bytes for each segment (+ optional silence).
    """
    voice_name = voice or get_voice_for_language(language)
    engine = get_tts_engine(voice_name)
    
    # Pre-calculate silence bytes (16 kHz, 16-bit mono)
    silence_samples = int(16000 * silence_ms / 1000)
    silence_bytes = b"\x00\x00" * silence_samples  # 16-bit zero samples
    
    for i, seg in enumerate(segments):
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        
        try:
            import numpy as np
            # Piper's synthesize returns a generator of AudioChunk objects.
            audio_chunks = []
            sample_rate = None
            for chunk in engine.synthesize(text):
                audio_chunks.append(chunk.audio_int16_bytes)
                sample_rate = chunk.sample_rate
            audio_bytes = b"".join(audio_chunks)
            yield audio_bytes
            
            # Inter-segment silence (except after last)
            if i < len(segments) - 1 and silence_ms > 0:
                yield silence_bytes
                
        except Exception as exc:
            logger.warning("TTS segment %d synthesis failed: %s", i, exc)
            # Yield silence instead of failing the whole stream
            yield silence_bytes


def tts_status() -> dict:
    """Return TTS service status for diagnostics."""
    voice_dir = Path(getattr(settings, "TTS_VOICE_DIR",
        Path(settings.MEDIA_ROOT) / "tts_voices"))
    voices = []
    if voice_dir.exists():
        for onnx_file in voice_dir.glob("*.onnx"):
            voices.append(onnx_file.stem)
    
    return {
        "piper_available": _load_piper(),
        "voice_dir": str(voice_dir),
        "available_voices": voices,
        "cached_engines": list(_engines.keys()),
    }
