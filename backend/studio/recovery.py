"""
TranStream recovery sweep (Phase 6b).

Called once at worker startup (and usable standalone / by a systemd timer).
Two independent sub-sweeps; the function NEVER raises (per-item try/except,
errors accumulated). A partial sweep beats no sweep.

  1. LEASE RECLAMATION (in-flight only)
     Jobs in NORMALIZING / SEGMENTING / TRANSCRIBING whose lease_expires_at
     is in the past => the holding worker crashed/was killed/hung. Per row we
     read attempts/max_attempts, set new_attempts = attempts+1, and choose:
         new_state = QUEUED   if new_attempts <  max_attempts   (retry it)
         new_state = FAILED   if new_attempts >= max_attempts   (park, terminal)
     We NEVER enqueue an exhausted row as QUEUED: the worker poll gates on
     attempts < max_attempts, so a QUEUED row at the budget ceiling would be
     invisible forever (silent park). Parking it as FAILED instead keeps it
     visible and admin-retryable.

     The UPDATE is guarded by lease_expires_at__lt=now. That guard is what
     makes the read-then-literal-write safe against a concurrent sweep: if
     another sweep already nulled the lease, our WHERE matches 0 rows, so we
     neither clobber its increment nor double-count. (Using literal ints
     rather than F() is fine precisely because of this guard.)

     We deliberately do NOT reclaim FAILED rows that merely have a stale
     lease column: that state only arises in a microsecond crash-window
     between mark_job_failed and lease release, and "reclaiming" it would
     reset a legitimately backed-off next_run_at to now, defeating the
     backoff. A stale lease on a terminal row is harmless cosmetic metadata.

  2. ORPHANED TEMP FILE DELETION
     .ts_part_* / .ts_snap_* / .ts_live_* in RECORDINGS_DIR with mtime older
     than RECOVERY_ORPHAN_MAX_AGE (default 3600 s). Younger files are LEFT
     ALONE: an active recording session (consumer) or an in-flight live
     preview (consumer) may still hold them open.
       .ts_part_*  = temp writer never finalized (consumer crashed pre-stop)
       .ts_snap_*  = live-preview snapshot leaked by a SIGKILL mid-preview
       .ts_live_*  = live-preview intermediate WAV leaked the same way
     None of these have a Take row (Take stores the FINAL renamed path), so
     they are pure disk leaks.

Returns dict: {"leases_reclaimed": int, "orphans_deleted": int, "errors": [str]}.
"""

import logging
import time
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from studio.models import Job, JobState

logger = logging.getLogger(__name__)

# Mid-processing states: a job here with an expired lease means its worker died.
_NON_TERMINAL_INFLIGHT = [
    JobState.NORMALIZING,
    JobState.SEGMENTING,
    JobState.TRANSCRIBING,
]

# Temp globs that must never outlive a session.
_TEMP_PATTERNS = (".ts_part_*", ".ts_snap_*", ".ts_live_*")


def _reclaim_expired_leases() -> tuple[int, list[str]]:
    """Reclaim in-flight jobs whose lease expired. Returns (count, errors)."""
    now = timezone.now()
    errors: list[str] = []
    reclaimed = 0

    qs = Job.objects.filter(
        state__in=_NON_TERMINAL_INFLIGHT,
        lease_expires_at__lt=now,
    )
    try:
        job_ids = list(qs.values_list("id", flat=True))
    except Exception as exc:
        logger.warning("recovery: lease query failed: %s", exc)
        return 0, [f"lease query: {exc}"]

    for jid in job_ids:
        try:
            cur = Job.objects.filter(id=jid).values("attempts", "max_attempts").first()
            if cur is None:
                continue  # vanished between query and read; nothing to do
            new_attempts = int(cur["attempts"]) + 1
            new_state = (
                JobState.QUEUED if new_attempts < int(cur["max_attempts"])
                else JobState.FAILED
            )
            # Guarded single update: matches 0 rows if a concurrent sweep
            # already cleared/nul led the lease, protecting the increment.
            updated = Job.objects.filter(
                id=jid,
                lease_expires_at__lt=now,
            ).update(
                state=new_state,
                attempts=new_attempts,
                lease_owner="",
                lease_expires_at=None,
                next_run_at=now,
                updated_at=now,
            )
            if updated:
                reclaimed += 1
                logger.info(
                    "recovery: reclaimed Job %s -> %s (attempt %d/%d)",
                    str(jid)[:8], new_state, new_attempts, cur["max_attempts"],
                )
        except Exception as exc:
            errors.append(f"reclaim Job {jid}: {exc}")
            logger.warning("recovery: failed to reclaim Job %s: %s", jid, exc)

    return reclaimed, errors


def _delete_orphaned_temps() -> tuple[int, list[str]]:
    """Delete session-temp files older than the age cutoff. (count, errors)."""
    max_age = int(getattr(settings, "RECOVERY_ORPHAN_MAX_AGE", 3600))
    cutoff = time.time() - max_age
    directory = Path(
        getattr(settings, "RECORDINGS_DIR", Path(settings.MEDIA_ROOT) / "recordings")
    )
    errors: list[str] = []
    deleted = 0

    if not directory.is_dir():
        return 0, []

    for pattern in _TEMP_PATTERNS:
        try:
            for p in directory.glob(pattern):
                try:
                    st = p.stat()
                    if st.st_mtime < cutoff:
                        p.unlink()
                        deleted += 1
                        logger.debug(
                            "recovery: deleted orphaned temp %s (age %.0f s)",
                            p.name, time.time() - st.st_mtime,
                        )
                except OSError as exc:
                    errors.append(f"unlink {p.name}: {exc}")
        except Exception as exc:
            errors.append(f"glob {pattern}: {exc}")

    if deleted:
        logger.info("recovery: deleted %d orphaned temp file(s) from %s", deleted, directory)
    return deleted, errors


def run_recovery_sweep() -> dict:
    """Execute both sub-sweeps. Returns a summary dict. Never raises."""
    logger.info("recovery: sweep starting...")
    leases_reclaimed, errs1 = _reclaim_expired_leases()
    orphans_deleted, errs2 = _delete_orphaned_temps()
    result = {
        "leases_reclaimed": leases_reclaimed,
        "orphans_deleted": orphans_deleted,
        "errors": errs1 + errs2,
    }
    logger.info(
        "recovery: sweep complete | leases_reclaimed=%d orphans_deleted=%d errors=%d",
        leases_reclaimed, orphans_deleted, len(result["errors"]),
    )
    return result
