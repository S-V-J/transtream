"""
TranStream persistence service (Phase 6a).

Best-effort, NON-FATAL database writers for the Take/Job/Segment layer.

Contract with callers (the WebSocket consumer, and later the upload view +
worker):
  * Every public function NEVER raises. On any error it logs a warning and
    returns a safe value (None / 0 / False). A database problem must degrade
    to "not persisted", never to "recording failed" or "socket closed".
    The browser still receives its transcript and the files still exist on
    disk even if every function here no-ops.
  * Functions are PLAIN SYNC (Django ORM is sync). Callers on the ASGI path
    MUST wrap them:  await sync_to_async(fn, thread_sensitive=False)(*args)
    i.e. OFF asgiref's single sensitive thread, so a slow DB write can never
    starve binary-frame ingestion (the exact failure mode that caused the
    historical WS 1011).
  * Multi-row writes are idempotent: segments are delete-then-inserted per
    (take, source) inside a transaction, so re-running a stage (retry)
    overwrites cleanly instead of duplicating.

This module intentionally imports NOTHING audio-related. It stores paths,
durations, languages, and text only.
"""

import functools
import logging
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from studio.models import Job, JobState, Segment, SegmentSource, Take, TakeKind

logger = logging.getLogger(__name__)


def _best_effort(fallback):
    """
    Decorator: run an ORM writer; on ANY exception log + return `fallback`.
    `fallback` may be a constant or a callable producing the default.
    """
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - deliberate total containment
                logger.warning(
                    "persistence.%s degraded to no-op: %s", fn.__name__, exc,
                    exc_info=True,
                )
                return fallback() if callable(fallback) else fallback
        return wrapper
    return deco


def relpath_for(abs_path):
    """
    Convert an absolute filesystem path to a MEDIA_ROOT-relative string.
    Falls back to storing the absolute path verbatim if it lives outside
    MEDIA_ROOT (defensive; shouldn't happen for pipeline outputs).
    """
    p = Path(abs_path)
    try:
        return str(p.resolve().relative_to(Path(settings.MEDIA_ROOT).resolve()))
    except (ValueError, OSError):
        return str(p)


# ─── Take / Job creation ─────────────────────────────────────────────────

@_best_effort(None)
def create_mic_take(user, source_filename, raw_abs_path, duration_ms, mime=""):
    """
    Persist a microphone take the moment its raw bytes are finalized on disk.
    Creates Take + an initial Job already in NORMALIZING (the consumer calls
    this right before running FFmpeg). Returns the Take instance or None.
    """
    take = Take.objects.create(
        user=user,
        kind=TakeKind.MICROPHONE,
        source_filename=source_filename or "",
        raw_relpath=relpath_for(raw_abs_path),
        duration_ms=int(duration_ms or 0),
    )
    Job.objects.create(
        take=take,
        state=JobState.NORMALIZING,
        progress_pct=10,
        next_run_at=timezone.now(),
        started_at=timezone.now(),
    )
    logger.info("persisted mic Take %s (job normalizing)", take.id.hex[:8])
    return take


@_best_effort(None)
def create_upload_take(user, source_filename, raw_abs_path, duration_ms=None):
    """
    Persist an uploaded-file take (Phase 7). Same shape as mic but kind=upload.
    duration_ms may be None until ffprobe/normalization measures it.
    """
    take = Take.objects.create(
        user=user,
        kind=TakeKind.UPLOAD,
        source_filename=source_filename or "",
        raw_relpath=relpath_for(raw_abs_path),
        duration_ms=int(duration_ms) if duration_ms is not None else 0,
    )
    Job.objects.create(
        take=take,
        state=JobState.QUEUED,
        progress_pct=0,
        next_run_at=timezone.now(),
    )
    return take


# ─── Stage transitions (progress + state) ────────────────────────────────

@_best_effort(False)
def set_job_stage(take, state, progress_pct=None):
    """
    Advance the take's current job to a new pipeline stage. Purely a state
    marker for UI/API progress and for the recovery sweep; never fatal.
    """
    job = take.current_job
    if job is None:
        return False
    job.state = state
    if progress_pct is not None:
        job.progress_pct = max(0, min(100, int(progress_pct)))
    job.save(update_fields=["state", "progress_pct", "updated_at"])
    return True


@_best_effort(False)
def attach_normalized(take, normalized_abs_path):
    """Record the normalized WAV path + bump progress after FFmpeg succeeds."""
    take.normalized_relpath = relpath_for(normalized_abs_path)
    take.save(update_fields=["normalized_relpath", "updated_at"])
    return set_job_stage(take, JobState.SEGMENTING, 40)


# ─── Segment writers (idempotent per source) ─────────────────────────────

@_best_effort(0)
def save_vad_segments(take, segments):
    """
    Store the Silero acoustic timeline. `segments` is the list of dicts the
    vad_service returns: [{"index","start_ms","end_ms","duration_ms"}, ...].
    Delete-then-insert for source="vad" so retries don't duplicate.
    Returns rows written (0 on no-op/failure).
    """
    rows = [
        Segment(
            take=take,
            source=SegmentSource.VAD,
            index=int(s.get("index", i)),
            start_ms=int(s.get("start_ms", 0)),
            end_ms=int(s.get("end_ms", 0)),
            duration_ms=int(s.get("duration_ms", 0)),
            text="",
        )
        for i, s in enumerate(segments or [])
    ]
    with transaction.atomic():
        Segment.objects.filter(take=take, source=SegmentSource.VAD).delete()
        if rows:
            Segment.objects.bulk_create(rows)
    set_job_stage(take, JobState.TRANSCRIBING, 60)
    logger.info("persisted %d VAD segment(s) for Take %s", len(rows), take.id.hex[:8])
    return len(rows)


@_best_effort(0)
def save_whisper_transcript(take, language, language_probability, segments):
    """
    Store the Whisper linguistic timeline + the identified language on the
    Take, then mark the job DONE at 100%. `segments` is the list of dicts
    transcription_service returns:
      [{"index","start_ms","end_ms","duration_ms","text","confidence"?}, ...]
    Delete-then-insert for source="whisper". Returns rows written.
    """
    rows = [
        Segment(
            take=take,
            source=SegmentSource.WHISPER,
            index=int(s.get("index", i)),
            start_ms=int(s.get("start_ms", 0)),
            end_ms=int(s.get("end_ms", 0)),
            duration_ms=int(s.get("duration_ms", 0)),
            text=(s.get("text") or "").strip(),
            confidence=s.get("confidence"),
            word_json=s.get("word_json"),
        )
        for i, s in enumerate(segments or [])
    ]
    with transaction.atomic():
        Segment.objects.filter(take=take, source=SegmentSource.WHISPER).delete()
        if rows:
            Segment.objects.bulk_create(rows)
        take.language = (language or "").lower()
        take.language_probability = (
            float(language_probability) if language_probability is not None else None
        )
        take.save(update_fields=["language", "language_probability", "updated_at"])
        job = take.current_job
        if job is not None:
            job.state = JobState.DONE
            job.progress_pct = 100
            job.finished_at = timezone.now()
            job.last_error = ""
            job.save(update_fields=["state", "progress_pct", "finished_at", "last_error", "updated_at"])
    logger.info(
        "persisted %d Whisper segment(s) + lang=%s for Take %s (job done)",
        len(rows), take.language, take.id.hex[:8],
    )
    return len(rows)


# ─── Failure marking (retry substrate) ───────────────────────────────────

@_best_effort(False)
def mark_job_failed(take, stage, error):
    """
    Record a stage failure on the current job: increment attempts, store the
    error, set state=FAILED, and schedule a backoff retry via next_run_at if
    attempts remain. The actual RE-RUN is driven by the worker (Phase 6b);
    this only persists the recoverable failure state. Non-fatal.
    """
    job = take.current_job
    if job is None:
        return False
    job.attempts = (job.attempts or 0) + 1
    job.state = JobState.FAILED
    job.last_error = f"[{stage}] {error}"[:4000]
    if job.attempts < job.max_attempts:
        # Exponential backoff: 5s, 20s, 80s, ... capped at 10 min.
        delay = min(5 * (4 ** (job.attempts - 1)), 600)
        job.next_run_at = timezone.now() + timezone.timedelta(seconds=delay)
    else:
        job.next_run_at = None  # exhausted; awaits manual/admin retry
    job.finished_at = timezone.now()
    job.save(update_fields=[
        "attempts", "state", "last_error", "next_run_at", "finished_at", "updated_at",
    ])
    logger.warning(
        "Take %s job FAILED at %s (attempt %d/%d): %s",
        take.id.hex[:8], stage, job.attempts, job.max_attempts, error,
    )
    return True
