"""
TranStream synchronous pipeline core (Phase 6b / 6b-fix).

process_take(take) runs the full normalize -> VAD -> transcribe -> persist
chain for a single Take, from a SYNCHRONOUS caller (the background worker).

Stage implementations:
  * Normalize  -> DELEGATED to studio.audio_service.normalize_audio (the same
                  proven async implementation the WebSocket consumer uses),
                  bridged into sync via asyncio.run(). The worker is a
                  standalone process with NO running event loop, so
                  asyncio.run() is the canonical, safe bridge: it creates a
                  loop, executes the coroutine, and tears the loop down.
                  This REPLACES the earlier duplicated _build_ffmpeg_cmd /
                  _run_ffmpeg_sync, which harness Scenario E and the
                  browser=8-vs-worker=9 discrepancy proved to be a drift
                  hazard. One implementation, two front doors — byte-identical
                  normalized WAVs by construction.
  * Segment    -> studio.vad_service.extract_speech_segments (already sync).
  * Transcribe -> studio.transcription_service.transcribe_audio (already sync,
                  NumPy/soundfile decode; the av-free invariant is preserved
                  because we never pass a path to model.transcribe).
  * Persist    -> studio.persistence.* (sync, best-effort, idempotent).

Architecture (unchanged, deliberate):
  ┌──────────────────────────────────────────────────────────────────┐
  │  Daphne process                                                  │
  │  AudioConsumer._run_post_processing  (async, in-process, hot)    │
  │  -> real-time mic takes, end-to-end, proven green since 63fd646  │
  ├──────────────────────────────────────────────────────────────────┤
  │  Worker process (python manage.py tsworker)                      │
  │  pipeline.process_take  (sync, background, cold)                 │
  │  -> queued retries, recovery-reclaimed jobs, future uploads      │
  └──────────────────────────────────────────────────────────────────┘
  Both call the SAME normalize/VAD/transcribe/persist implementations.
  Neither can break the other: zero shared mutable state, and the
  consumer file is not touched by this module.

Design rules (unchanged):
  * NEVER raises. Every exception path converts to Job FAILED with
    exponential backoff via persistence.mark_job_failed and returns False.
  * Idempotent: segment writes are delete-then-insert per (take, source);
    normalization overwrites the deterministic *_NRM_* path. Re-runs
    converge to a structurally identical result. (The transcript TEXT is
    not guaranteed bit-identical across re-runs on CPU int8 — see README
    "Known behavior"; the DB invariant is contiguity + no duplicates.)
  * Leases are the worker's concern; process_take only moves Job state.
"""

import asyncio
import logging
from pathlib import Path

from django.conf import settings  # noqa: F401  (kept: stage code may read knobs)

from studio import persistence
from studio.audio_service import (
    AudioNormalizationError,
    normalize_audio,
    normalized_output_path,
)
from studio.models import Job, JobState, Take
from studio.transcription_service import TranscriptionError, transcribe_audio
from studio.vad_service import VADError, extract_speech_segments

logger = logging.getLogger(__name__)


class PipelineError(Exception):
    """Internal pipeline stage failure. Never escapes process_take."""


# ─── Stage 1 bridge: async normalizer, sync caller ───────────────────────

def _normalize_via_audio_service(input_path: Path, output_path: Path) -> dict:
    """
    Bridge audio_service.normalize_audio (async) into this sync context.

    Safe HERE and only here: the worker process has no running event loop,
    so asyncio.run owns the loop lifetime cleanly. (Calling asyncio.run from
    inside Daphne's loop, or from a thread that already has one, would raise
    "asyncio.run() cannot be called from a running event loop" — that is why
    this bridge lives in the worker-side module and nowhere else.)

    Converts AudioNormalizationError -> PipelineError so process_take's
    single stage-1 except-clause covers both. Returns the metadata dict
    normalize_audio produces (size/duration/sample_rate/…), currently used
    for logging only; Phase 7 will lift duration_ms from it for uploads.
    """
    try:
        return asyncio.run(normalize_audio(input_path, output_path))
    except AudioNormalizationError as exc:
        raise PipelineError(f"normalization failed: {exc}") from exc
    except RuntimeError as exc:
        # Defensive: surfaces the "called from a running loop" misuse loudly
        # instead of letting it masquerade as a media error.
        raise PipelineError(f"event-loop bridge misuse (must run sync): {exc}") from exc


# ─── Main pipeline entry point ───────────────────────────────────────────

def process_take(take: Take, worker_id: str = "pipeline") -> bool:
    """
    Run the full synchronous pipeline for one Take.

    Stages (each converted to Job FAILED + backoff on error, never raised):
      0 Validate   : raw file exists and is non-empty.
      1 Normalize  : _normalize_via_audio_service -> 16 kHz mono 16-bit PCM WAV;
                     persistence.attach_normalized stores the path and advances
                     the Job to SEGMENTING/40%.
      2 Segment    : extract_speech_segments (Silero VAD, sync);
                     persistence.save_vad_segments writes VAD rows and advances
                     the Job to TRANSCRIBING/60%.
      3 Transcribe : transcribe_audio(beam=5) (Faster-Whisper, NumPy decode);
                     persistence.save_whisper_transcript writes Whisper rows,
                     sets Take.language, marks Job DONE/100%.

    Returns True on success (Job DONE), False on any stage failure.
    """
    take_id = take.id.hex[:8]
    job = take.current_job
    if job is None:
        logger.error("process_take [%s]: no Job row found - cannot proceed", take_id)
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
        "process_take [%s] START - worker=%s - raw=%s (%d bytes) - dur=%dms",
        take_id, worker_id, raw_path.name, file_size, take.duration_ms,
    )

    # ── Stage 1: Normalize (delegated to the proven async implementation) ─
    persistence.set_job_stage(take, JobState.NORMALIZING, 10)
    norm_path = normalized_output_path(raw_path)

    try:
        norm_meta = _normalize_via_audio_service(raw_path, norm_path)
        persistence.attach_normalized(take, norm_path)
        logger.info(
            "process_take [%s] stage 1 OK - normalized -> %s (%s)",
            take_id, norm_path.name,
            {k: norm_meta.get(k) for k in ("size_bytes", "duration_ms", "sample_rate")}
            if isinstance(norm_meta, dict) else norm_meta,
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
    try:
        vad_segments = extract_speech_segments(norm_path)
        n_vad = persistence.save_vad_segments(take, vad_segments)
        total_speech_ms = sum(s.get("duration_ms", 0) for s in vad_segments)
        logger.info(
            "process_take [%s] stage 2 OK - VAD -> %d segment(s), %.1f s speech",
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
    try:
        result = transcribe_audio(norm_path, beam_size=5)
        n_wsp = persistence.save_whisper_transcript(
            take,
            result["language"],
            result["language_probability"],
            result["segments"],
        )
        logger.info(
            "process_take [%s] stage 3 OK - Whisper -> %d segment(s), "
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

    # ── Success: clear our lease (self-contained for non-WorkerLoop callers) ─
    Job.objects.filter(id=job.id, lease_owner=worker_id).update(
        lease_owner="", lease_expires_at=None,
    )

    logger.info("process_take [%s] COMPLETED SUCCESSFULLY", take_id)
    return True
