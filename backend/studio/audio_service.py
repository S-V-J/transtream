"""
TranStream Audio Normalization Service (Phase 3).

Converts raw browser recordings (WebM/Opus, OGG, M4A, etc.)
into 16 kHz, 16-bit, mono PCM WAV files suitable for
Silero VAD (Phase 4) and Faster-Whisper (Phase 5).

Uses FFmpeg via asyncio subprocess so the Daphne event loop
is never blocked.
"""

import asyncio
import logging
import shutil
from pathlib import Path

from django.conf import settings

logger = logging.getLogger(__name__)


class AudioNormalizationError(Exception):
    """Raised when FFmpeg normalization fails."""
    pass


def normalized_output_path(input_path: Path) -> Path:
    """
    Derive the normalized output path from a raw recording path.

    TS_REC_RAW_20261020_104238.webm  →  TS_REC_NRM_20261020_104238.wav
    TS_REC_20261020_104238.ogg       →  NRM_TS_REC_20261020_104238.wav
    """
    stem = input_path.stem

    if "RAW" in stem:
        stem = stem.replace("RAW", "NRM")
    else:
        stem = f"NRM_{stem}"

    return input_path.parent / f"{stem}.wav"


def _media_url(path: Path) -> str:
    """Convert an absolute filesystem path to a MEDIA_URL-relative URL."""
    media_root = Path(settings.MEDIA_ROOT)
    media_url = settings.MEDIA_URL

    if not media_url.startswith("/"):
        media_url = "/" + media_url

    try:
        relative = path.relative_to(media_root)
    except ValueError:
        relative = Path("recordings") / path.name

    return f"{media_url}{relative.as_posix()}"


def _estimate_duration_ms(file_size: int, sample_rate: int, channels: int) -> int:
    """
    Estimate audio duration from file size.

    WAV header is 44 bytes. PCM 16-bit = 2 bytes per sample.
    bytes_per_second = sample_rate * channels * 2
    """
    pcm_bytes = max(0, file_size - 44)
    bytes_per_sec = sample_rate * channels * 2

    if bytes_per_sec == 0:
        return 0

    return int((pcm_bytes / bytes_per_sec) * 1000)


async def normalize_audio(
    input_path: Path,
    output_path: Path,
) -> dict:
    """
    Normalize a raw audio file to 16 kHz, 16-bit, mono PCM WAV.

    Args:
        input_path:  Absolute path to the raw recording.
        output_path: Absolute path for the normalized WAV output.

    Returns:
        dict with keys:
            success       bool
            input_file    str   (filename only)
            output_file   str   (filename only)
            output_url    str   (MEDIA_URL path, starts with /)
            size_bytes    int
            duration_ms   int   (estimated)
            sample_rate   int
            channels      int
            bit_depth     int
            error         None

    Raises:
        AudioNormalizationError on any failure.
    """
    input_path = Path(input_path)
    output_path = Path(output_path)

    if not input_path.exists():
        raise AudioNormalizationError(
            f"Input file does not exist: {input_path}"
        )

    ffmpeg_bin = getattr(settings, "FFMPEG_BIN", "ffmpeg")

    if shutil.which(ffmpeg_bin) is None:
        raise AudioNormalizationError(
            f"FFmpeg binary not found: {ffmpeg_bin}. "
            f"Install with: sudo apt-get install -y ffmpeg"
        )

    sample_rate = getattr(settings, "AUDIO_SAMPLE_RATE", 16000)
    channels = getattr(settings, "AUDIO_CHANNELS", 1)
    timeout = getattr(settings, "FFMPEG_TIMEOUT", 300)

    output_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel", "warning",
        "-y",
        "-i", str(input_path),
        "-ar", str(sample_rate),
        "-ac", str(channels),
        "-sample_fmt", "s16",
        "-f", "wav",
        str(output_path),
    ]

    logger.info("FFmpeg normalize: %s → %s", input_path.name, output_path.name)

    proc = None

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=timeout,
        )

        if proc.returncode != 0:
            error_detail = stderr.decode("utf-8", errors="replace").strip()

            if len(error_detail) > 2000:
                error_detail = "..." + error_detail[-2000:]

            raise AudioNormalizationError(
                f"FFmpeg exited with code {proc.returncode}: {error_detail}"
            )

        if not output_path.exists():
            raise AudioNormalizationError(
                "FFmpeg completed successfully but output file was not created."
            )

        file_size = output_path.stat().st_size

        if file_size <= 44:
            raise AudioNormalizationError(
                f"Output file is too small ({file_size} bytes). "
                f"The input may contain no decodable audio."
            )

        duration_ms = _estimate_duration_ms(file_size, sample_rate, channels)

        logger.info(
            "FFmpeg normalize complete: %s (%d bytes, ~%d ms)",
            output_path.name,
            file_size,
            duration_ms,
        )

        return {
            "success": True,
            "input_file": input_path.name,
            "output_file": output_path.name,
            "output_url": _media_url(output_path),
            "size_bytes": file_size,
            "duration_ms": duration_ms,
            "sample_rate": sample_rate,
            "channels": channels,
            "bit_depth": 16,
            "error": None,
        }

    except asyncio.TimeoutError:
        if proc is not None:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass

        if output_path.exists():
            try:
                output_path.unlink()
            except Exception:
                pass

        raise AudioNormalizationError(
            f"FFmpeg timed out after {timeout} seconds."
        )

    except FileNotFoundError:
        raise AudioNormalizationError(
            f"FFmpeg binary not found: {ffmpeg_bin}"
        )

    except AudioNormalizationError:
        if output_path.exists():
            try:
                output_path.unlink()
            except Exception:
                pass
        raise

    except Exception as exc:
        if output_path.exists():
            try:
                output_path.unlink()
            except Exception:
                pass

        raise AudioNormalizationError(
            f"Unexpected FFmpeg error: {exc}"
        ) from exc
