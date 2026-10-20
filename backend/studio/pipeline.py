"""
TranStream synchronous pipeline core (Phase 6b).

process_take(take) runs the full normalize → VAD → transcribe → persist
chain for a single Take, using ONLY synchronous calls:

  * FFmpeg       → subprocess.run  (NOT asyncio.create_subprocess_exec)
  * Silero VAD   → extract_speech_segments  (already sync in vad_service.py)
  * Whisper      → transcribe_audio          (already sync, NumPy path)
  * Persistence  → studio.persistence.*     (already sync, best-effort)

This is the function the background worker calls. The WebSocket consumer
has its own async _run_post_processing which is proven green (commits
665c9a3 → 63fd646) and is NOT refactored here. The two paths coexist:

  ┌──────────────────────────────────────────────────────────────────┐
  │  Daphne process                                                  │
  │  AudioConsumer._run_post_processing  (async, in-process, hot)    │
  │  → handles real-time mic takes end-to-end                        │
  ├──────────────────────────────────────────────────────────────────┤
  │  Worker process (python manage.py runworker)                     │
  │  pipeline.process_take  (sync, background, cold)                 │
  │  → handles queued retries, future uploads, orphan adoption       │
  └──────────────────────────────────────────────────────────────────┘

They share the DB schema and persistence service but have ZERO code-level
coupling. Neither can break the other.

Design rules:
  * NEVER raises. All exceptions are caught, logged, and converted to
    Job FAILED state with exponential backoff via persistence.mark_job_failed.
    The caller (worker loop) checks the bool return to decide whether to
    release the lease or leave it for the recovery sweep.
  * Idempotent: segment writes are delete-then-insert per (take, source)
    inside persistence.*, so re-running after a crash produces the same
    final state. FFmpeg's -y flag overwrites the normalized WAV in place.
  * Deterministic paths: normalized output is derived from the raw path
    via audio_service.normalized_output_path(), so a retry targets the
    same file. No temp-file accumulation.
  * The pipeline does NOT acquire or release leases — that is the worker's
    responsibility. process_take only updates Job state/progress.
"""

import logging
import subprocess
from pathlib import Path

from django.conf import settings

from studio import persistence
from studio.audio_service import normalized_output_path
from studio.models import Job, JobState, Take
from studio.transcription_service import TranscriptionError, transcribe_audio
from studio.vad_service import VADError, extract_speech_segments

logger = logging.getLogger(__name__)


class PipelineError(Exception):
    """Internal pipeline stage failure. Never escapes process_take."""
    pass


# ─── FFmpeg (sync, subprocess.run) ──────────────────────────────────────

def _build_ffmpeg_cmd(input_path: Path, output_path: Path) -> list[str]:
    """
    Construct the FFmpeg argument list for 16 kHz mono 16-bit PCM WAV.

    Mirrors the arguments used by audio_service.normalize_audio (async)
    but returns a plain list for subprocess.run. Kept here to avoid
    modifying audio_service.py (schema-locked this turn) and to avoid
    importing an async function into a sync context.

    When we unify the two paths (Phase 6c or 7), this helper and
    audio_service's internal builder should be extracted into a shared
    _ffmpeg_args() function. For now, 15 lines of intentional duplication
    buys complete isolation from the green async path.
    """
    return [
        str(getattr(settings, "FFMPEG_BIN", "ffmpeg")),
        "-y",                        # overwrite output without asking
        "-hide_banner",              # suppress version/build noise on stderr
        "-loglevel", "error",        # only real errors on stderr
        "-i", str(input_path),       # input file
        "-ar", str(int(getattr(settings, "AUDIO_SAMPLE_RATE", 16000))),
        "-ac", str(int(getattr(settings, "AUDIO_CHANNELS", 1))),
        "-sample_fmt", "s16",        # 16-bit signed PCM
        "-f", "wav",                 # force WAV container (RIFF)
        str(output_path),
    ]


def _run_ffmpeg_sync(input_path: Path, output_path: Path) -> None:
    """
    Execute FFmpeg synchronously. Raises PipelineError on any failure.

    Timeout is read from settings.FFMPEG_TIMEOUT (default 300 s), matching
    the async path's timeout. capture_output=True so stderr is available
    for diagnostics without polluting the worker's own stderr.
    """
    cmd = _build_ffmpeg_cmd(input_path, output_path)
    timeout = int(getattr(settings, "FFMPEG_TIMEOUT", 300))

    logger.debug("FFmpeg sync: %s", " ".join(cmd))

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise PipelineError(
            f"FFmpeg timed out after {timeout}s for {input_path.name}"
        )
    except FileNotFoundError:
        raise PipelineError(
            f"FFmpeg binary not found: {cmd[0]}. "
            f"Install it or set FFMPEG_BIN in .env."
        )

    if proc.returncode != 0:
        stderr_tail = ""
        if proc.stderr:
            stderr_tail = proc.stderr.decode("utf-8", errors="replace")[-500:]
        raise PipelineError(
            f"FFmpeg exited {proc.returncode} for {input_path.name}: {stderr_tail}"
        )

    if not output_path.exists():
        raise PipelineError(
            f"FFmpeg reported success but output file missing: {output_path}"
        )


# ─── Main pipeline entry point ──────────────────────────────────────────

def process_take(take: Take, worker_id: str = "pipeline") -> bool:
    """
    Run the full synchronous pipeline for one Take.

    Stages (each wrapped in try/except → mark_job_failed on error):
      1. Validate: raw file must exist on disk.
      2. Normalize: FFmpeg subprocess → 16 kHz mono 16-bit PCM WAV.
                   persistence.attach_normalized records the path + advances
                   Job to SEGMENTING/40%.
      3. Segment:  Silero VAD → acoustic speech regions.
                   persistence.save_vad_segments writes VAD rows + advances
                   Job to TRANSCRIBING/60%.
      4. Transcribe: Faster-Whisper (NumPy path, beam=5) → linguistic segments
                     + language ID.
                     persistence.save_whisper_transcript writes Whisper rows,
                     sets Take.language, and marks Job DONE/100%.

    Args:
        take:      A Take instance with raw_relpath pointing at an existing file.
        worker_id: Diagnostic label for log messages (the worker's PID-based id).

    Returns:
        True  if the pipeline completed successfully (Job is now DONE).
        False if any stage failed (Job is now FAILED with backoff scheduled).

    NEVER raises. Every exception path calls persistence.mark_job_failed and
    returns False. The caller (WorkerLoop._process_job) uses the return value
    to decide whether to release the lease immediately (True) or leave it
    for the recovery sweep (False — though mark_job_failed already cleared it).
    """
    take_id = take.id.hex[:8]
    job = take.current_job
    if job is None:
        logger.error("process_take [%s]: no Job row found — cannot proceed", take_id)
        return False

    # ── Stage 0: Validate ──────────────────────────────────────────────
    raw_path = take.raw_file
    if raw_path is None or not raw_path.exists():
        msg = f"Raw file missing on disk: raw_relpath={take.raw_relpath!r}"
        logger.error("process_take [%s]: %s", take_id, msg)
        persistence.mark_job_failed(take, "validate", msg)
        return False

    file_size = raw_path.stat().st_size
    if file_size == 0:
        msg = f"Raw file is empty (0 bytes): {raw_path.name}"
        logger.error("process_take [%s]: %s", take_id, msg)
        persistence.mark_job_failed(take, "validate", msg)
        return False

    logger.info(
        "process_take [%s] START · worker=%s · raw=%s (%d bytes) · dur=%dms",
        take_id, worker_id, raw_path.name, file_size, take.duration_ms,
    )

    # ── Stage 1: Normalize (FFmpeg sync) ───────────────────────────────
    persistence.set_job_stage(take, JobState.NORMALIZING, 10)
    norm_path = normalized_output_path(raw_path)

    try:
        _run_ffmpeg_sync(raw_path, norm_path)
        # attach_normalized sets normalized_relpath on Take AND advances
        # Job to SEGMENTING/40% internally (see persistence.py).
        persistence.attach_normalized(take, norm_path)
        logger.info(
            "process_take [%s] stage 1 OK · normalized → %s (%d bytes)",
            take_id, norm_path.name, norm_path.stat().st_size,
        )
    except PipelineError as exc:
        logger.warning("process_take [%s] stage 1 FAILED (normalization): %s", take_id, exc)
        persistence.mark_job_failed(take, "normalization", str(exc))
        return False
    except Exception as exc:
        logger.exception("process_take [%s] stage 1 UNEXPECTED (normalization): %s", take_id, exc)
        persistence.mark_job_failed(take, "normalization", f"Unexpected: {exc}")
        return False

    # ── Stage 2: Segment (Silero VAD) ──────────────────────────────────
    # Job state is already SEGMENTING/40% (set by attach_normalized).
    try:
        vad_segments = extract_speech_segments(norm_path)
        # save_vad_segments writes VAD rows AND advances Job to
        # TRANSCRIBING/60% internally (see persistence.py).
        n_vad = persistence.save_vad_segments(take, vad_segments)
        total_speech_ms = sum(s.get("duration_ms", 0) for s in vad_segments)
        logger.info(
            "process_take [%s] stage 2 OK · VAD → %d segment(s), %.1f s speech",
            take_id, n_vad, total_speech_ms / 1000.0,
        )
    except VADError as exc:
        logger.warning("process_take [%s] stage 2 FAILED (VAD): %s", take_id, exc)
        persistence.mark_job_failed(take, "vad", str(exc))
        return False
    except Exception as exc:
        logger.exception("process_take [%s] stage 2 UNEXPECTED (VAD): %s", take_id, exc)
        persistence.mark_job_failed(take, "vad", f"Unexpected: {exc}")
        return False

    # ── Stage 3: Transcribe (Faster-Whisper, NumPy path, beam=5) ───────
    # Job state is already TRANSCRIBING/60% (set by save_vad_segments).
    try:
        result = transcribe_audio(norm_path, beam_size=5)
        # save_whisper_transcript writes Whisper rows, sets Take.language,
        # and marks Job DONE/100% internally (see persistence.py).
        n_wsp = persistence.save_whisper_transcript(
            take,
            result["language"],
            result["language_probability"],
            result["segments"],
        )
        logger.info(
            "process_take [%s] stage 3 OK · Whisper → %d segment(s), "
            "lang=%s (%.2f)",
            take_id, n_wsp, result["language"], result["language_probability"],
        )
    except TranscriptionError as exc:
        logger.warning("process_take [%s] stage 3 FAILED (transcription): %s", take_id, exc)
        persistence.mark_job_failed(take, "transcription", str(exc))
        return False
    except Exception as exc:
        logger.exception(
            "process_take [%s] stage 3 UNEXPECTED (transcription): %s", take_id, exc,
        )
        persistence.mark_job_failed(take, "transcription", f"Unexpected: {exc}")
        return False

    # ── Success ────────────────────────────────────────────────────────
    # Clear the lease so the worker's finally-block doesn't double-clear.
    # (The worker also clears it, but doing it here makes process_take
    #  self-contained for any future caller that isn't the WorkerLoop.)
    Job.objects.filter(id=job.id, lease_owner=worker_id).update(
        lease_owner="", lease_expires_at=None,
    )

    logger.info("process_take [%s] COMPLETED SUCCESSFULLY", take_id)
    return True
