"""
TranStream background job worker (Phase 6b).

WorkerLoop is a synchronous polling loop for local-first, single-machine
deployment. It is deliberately NOT Channels' `runworker` and NOT a
distributed queue (Celery/RQ): the Job ROW is the queue, so a job survives
Redis restarts and Daphne restarts. That durability is the whole point of a
local-first studio, and it is why we poll the DB instead of consuming a
Redis list. (Channels' own `runworker` is a pub/sub consumer with no
lease/retry/backoff/progress; adopting it would mean re-building all of
those by hand AND losing durability. Hence our own loop, under the
collision-free command name `tsworker`.)

Lifecycle:
  1. Startup: run recovery sweep (reclaim expired in-flight leases, delete
     orphaned temp files older than RECOVERY_ORPHAN_MAX_AGE).
  2. Loop:
     a. Poll SQLite for runnable jobs:
          (QUEUED) OR (FAILED with attempts < max_attempts),
          AND next_run_at <= now, AND no active lease.
        The attempts<max filter is applied to BOTH states uniformly; a
        QUEUED row always has attempts < max (recovery never enqueues an
        exhausted row as QUEUED — it parks exhausted rows as FAILED), so
        the filter is a no-op for QUEUED and the real gate for FAILED.
     b. Per job: acquire lease atomically -> start heartbeat thread ->
        pipeline.process_take() -> stop heartbeat -> release lease.
     c. No jobs -> sleep poll_interval (interruptibly).
  3. Shutdown: SIGTERM/SIGINT set an Event checked BETWEEN jobs; a running
     process_take() is never interrupted, so the lease is always released
     cleanly. (If it were interrupted, the recovery sweep would reclaim the
     expired lease — correct but wasteful; clean finish is preferred.)

Concurrency:
  * WHISPER_MAX_CONCURRENT (default 1) via threading.Semaphore -> strictly
    sequential by default, safest for CPU-bound Whisper on a laptop.
  * The semaphore is per-process. Multiple worker processes each have their
    own; the atomic lease acquisition (WHERE lease_owner empty) prevents two
    workers from ever grabbing the same job regardless.

Lease mechanics:
  * lease_owner = "worker-<pid>" (or --worker-id).
  * lease_expires_at = now + WORKER_LEASE_DURATION (default 120 s).
  * A daemon heartbeat thread renews lease_expires_at every
    WORKER_HEARTBEAT_INTERVAL (default 30 s) while process_take() runs, so a
    multi-minute Whisper on a long file is never stolen mid-flight.
  * The heartbeat's renewal is filtered on lease_owner == ours: if the
    recovery sweep already reclaimed the lease, the renewal matches 0 rows
    and the thread exits (no zombie lease extension).
  * On completion the lease is cleared (filtered on owner, so we never clear
    a lease another process already took).

Known limitation (documented, acceptable for single-machine):
  * If a >heartbeat_interval GC pause or DB stall lets the lease lapse while
    process_take() is still writing, the recovery sweep could re-enqueue the
    job and a second worker could process it concurrently. This is SAFE
    because every segment write is delete-then-insert per (take, source)
    inside persistence.*, so concurrent processing converges to a correct
    final state (last writer wins per source). The heartbeat makes the lapse
    window vanishingly small in practice.
"""

import logging
import os
import signal
import threading
from datetime import timedelta

from django.conf import settings
from django.db.models import F, Q
from django.utils import timezone

from studio.models import Job, JobState
from studio.pipeline import process_take
from studio.recovery import run_recovery_sweep

logger = logging.getLogger(__name__)

# FAILED jobs are runnable only if they still have retry budget; this is the
# state we additionally admit into the poll alongside QUEUED.
_RETRYABLE_FAILED = JobState.FAILED


def runnable_jobs(now=None):
    """
    The ONE definition of "a job some worker may claim right now".

    Extracted from WorkerLoop._poll so that scripts/guard_worker_boots.py counts
    THIS instead of maintaining a parallel state list. The guard previously
    defined "non-terminal" as NOT IN (done, failed, cancelled) while the poll
    admits QUEUED *and* FAILED-with-retry-budget: a job sitting in backoff was
    invisible to the guard yet claimable by `tsworker --once`, so the guard could
    steal a live worker's retry. Two definitions of one concept disagreed in
    exactly the direction that makes a check under-report. Sharing the
    implementation is the only fix that cannot drift again by editing one file.

    Returns an UNFILTERED-FURTHER queryset: no slice, no select_related. The
    poll_batch slice and the select_related are per-caller concerns (the guard
    wants a COUNT, which must not carry an ORDER BY or a join); the predicate is
    the shared part.

    CHAINED .filter() CALLS, NEVER A SINGLE MIXED CALL. The first version of this
    query used filter(id=..., Q(...)) and raised SyntaxError: keyword-then-
    positional. Every positional Q lives in its own .filter(), and that discipline
    must survive any future edit here - it is the reason this function reads as
    four .filter() lines rather than one.

    EVENTUAL HOME IS studio/models.py as a Job manager, not this module. This
    function needs only Job, JobState, Q, F and timezone, so living in worker.py
    drags studio.pipeline and the audio services into every importer that wants
    to ask "is the queue empty" - including the boot guard's subprocess. Moving it
    is deferred rather than guessed because models.py was not read when this was
    written, and relocating code into a file you have not read is how this
    project has been burned repeatedly.
    """
    if now is None:
        now = timezone.now()
    return (
        Job.objects
        .filter(Q(state=JobState.QUEUED) | Q(state=_RETRYABLE_FAILED))
        .filter(next_run_at__lte=now)
        .filter(Q(lease_owner="") | Q(lease_owner__isnull=True))
        .filter(attempts__lt=F("max_attempts"))
        .order_by("next_run_at", "created_at")
    )


class LeaseHeartbeat:
    """
    Daemon thread that renews a job's lease_expires_at every `interval`
    seconds while the job is being processed.

    Daemon=True so it never blocks interpreter exit. The renewal UPDATE is
    filtered on lease_owner == our worker_id: if the recovery sweep reclaimed
    the lease (set owner ""), the renewal matches 0 rows and the loop returns,
    preventing a dead worker's heartbeat from extending a live lease.
    """

    def __init__(self, job_id, worker_id: str, lease_duration: int, interval: int):
        self.job_id = job_id
        self.worker_id = worker_id
        self.lease_duration = lease_duration
        self.interval = interval
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(
            target=self._loop, name=f"hb-{self.job_id.hex[:6]}", daemon=True,
        )
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            # Bounded join: never hang the worker on a wedged heartbeat.
            self._thread.join(timeout=self.interval + 5)

    def _loop(self):
        # wait(interval) returns True if stop was set during the wait -> exit.
        while not self._stop.wait(self.interval):
            try:
                now = timezone.now()
                updated = Job.objects.filter(
                    id=self.job_id,
                    lease_owner=self.worker_id,        # all-kwargs filter: legal
                ).update(
                    lease_expires_at=now + timedelta(seconds=self.lease_duration),
                    updated_at=now,
                )
                if updated == 0:
                    logger.debug(
                        "heartbeat %s: lease no longer ours (reclaimed/released) -> stop",
                        self.job_id.hex[:6],
                    )
                    return
            except Exception as exc:
                # A renewal failure must NOT kill the worker. Log and let the
                # next tick retry; if the lease truly expires first, the
                # recovery sweep reclaims it, which is correct for a dead worker.
                logger.warning("heartbeat %s: renewal failed: %s", self.job_id.hex[:6], exc)


class WorkerLoop:
    """The main polling loop. Construct with config, call .run()."""

    def __init__(
        self,
        worker_id: str | None = None,
        poll_interval: float | None = None,
        lease_duration: int | None = None,
        heartbeat_interval: int | None = None,
        max_concurrent: int | None = None,
        poll_batch: int | None = None,
    ):
        self.worker_id = worker_id or f"worker-{os.getpid()}"
        self.poll_interval = float(
            poll_interval if poll_interval is not None
            else getattr(settings, "WORKER_POLL_INTERVAL", 2.0)
        )
        self.lease_duration = int(
            lease_duration if lease_duration is not None
            else getattr(settings, "WORKER_LEASE_DURATION", 120)
        )
        self.heartbeat_interval = int(
            heartbeat_interval if heartbeat_interval is not None
            else getattr(settings, "WORKER_HEARTBEAT_INTERVAL", 30)
        )
        self.max_concurrent = int(
            max_concurrent if max_concurrent is not None
            else getattr(settings, "WHISPER_MAX_CONCURRENT", 1)
        )
        self.poll_batch = int(
            poll_batch if poll_batch is not None
            else getattr(settings, "WORKER_POLL_BATCH", 5)
        )

        self._semaphore = threading.Semaphore(self.max_concurrent)
        self._stop_event = threading.Event()
        self._jobs_processed = 0
        self._jobs_failed = 0

    # ── signal handling ────────────────────────────────────────────────

    def install_signal_handlers(self):
        """
        SIGTERM/SIGINT set the stop Event. The loop checks it between jobs and
        never interrupts a running process_take(), guaranteeing clean lease
        release. Idempotent: safe to call repeatedly (the harness does).
        """
        def _handler(signum, frame):
            logger.info(
                "Worker %s received %s -> finishing current job then stopping.",
                self.worker_id, signal.Signals(signum).name,
            )
            self._stop_event.set()

        signal.signal(signal.SIGTERM, _handler)
        signal.signal(signal.SIGINT, _handler)

    # ── main loop ──────────────────────────────────────────────────────

    def run(self, once: bool = False):
        """
        Start the worker. once=True -> recovery + a single poll cycle + exit
        (testing / cron-style invocation).
        """
        self.install_signal_handlers()

        logger.info(
            "Worker %s starting | poll=%.1fs lease=%ds heartbeat=%ds "
            "concurrent=%d batch=%d once=%s",
            self.worker_id, self.poll_interval, self.lease_duration,
            self.heartbeat_interval, self.max_concurrent, self.poll_batch, once,
        )

        sweep = run_recovery_sweep()
        logger.info(
            "Worker %s recovery sweep | leases_reclaimed=%d orphans_deleted=%d errors=%d",
            self.worker_id, sweep["leases_reclaimed"],
            sweep["orphans_deleted"], len(sweep["errors"]),
        )
        for err in sweep["errors"]:
            logger.warning("Worker %s recovery error: %s", self.worker_id, err)

        while not self._stop_event.is_set():
            jobs = self._poll()

            if not jobs:
                if once:
                    break
                self._stop_event.wait(self.poll_interval)   # interruptible sleep
                continue

            for idx, job in enumerate(jobs):
                if self._stop_event.is_set():
                    logger.info(
                        "Worker %s stopping mid-batch; %d job(s) skipped.",
                        self.worker_id, len(jobs) - idx,
                    )
                    break

                if not self._acquire_lease(job):
                    logger.debug(
                        "Worker %s: job %s lease won by another worker -> skip",
                        self.worker_id, job.id.hex[:8],
                    )
                    continue

                self._semaphore.acquire()
                try:
                    self._process_job(job)
                finally:
                    self._semaphore.release()

            if once:
                break

        logger.info(
            "Worker %s stopped | processed=%d failed=%d",
            self.worker_id, self._jobs_processed, self._jobs_failed,
        )

    # ── poll ───────────────────────────────────────────────────────────

    def _poll(self) -> list[Job]:
        """
        Fetch runnable jobs. Chained .filter() calls (never mixing kwarg +
        positional Q) so the query is legal and readable. The attempts<max
        gate uses F() to compare two columns and is harmless to QUEUED rows
        (attempts 0 < max 3) while being the real gate for FAILED rows.
        """
        now = timezone.now()
        try:
            qs = runnable_jobs(now)[: self.poll_batch]
            return list(qs.select_related("take"))
        except Exception as exc:
            logger.warning("Worker %s: poll failed: %s", self.worker_id, exc)
            return []

    # ── lease acquisition (FIX 1) ──────────────────────────────────────

    def _acquire_lease(self, job: Job) -> bool:
        """
        Atomically claim a job. CHAINED filters (the previous single
        filter(id=..., Q(...)) was the SyntaxError: keyword-then-positional).

        The lease_owner-empty predicate lives in its own .filter(), so the
        UPDATE's WHERE includes "still unleased". If another worker (or a
        race inside our own batch) claimed it first, our UPDATE matches 0
        rows and we return False -> skip. SQLite serializes writes, so this
        is correct for a single machine; multi-machine would add a Redis lock
        or Postgres SELECT ... FOR UPDATE SKIP LOCKED.

        WHY THE LEASE-EMPTY PREDICATE IS REPEATED HERE AND NOT IMPORTED FROM
        runnable_jobs(). It looks like duplication, and a future reader may be
        tempted to collapse it. DO NOT. This filter must live in the UPDATE's own
        WHERE clause - that placement is the entire mechanism making the claim
        atomic. runnable_jobs() additionally constrains state and next_run_at, and
        a job can move between the poll and this UPDATE (another worker claimed it,
        or the sweep reclaimed it), so reusing the broader predicate would either
        over-constrain (silently failing to claim a job we should take) or, worse,
        be read as permission to drop the lease condition from the UPDATE - which
        is the one line keeping two workers from ever grabbing the same job.
        Deliberate near-duplication, same category as the two upload caps in
        settings.py.
        """
        now = timezone.now()
        try:
            updated = (
                Job.objects
                .filter(id=job.id)
                .filter(Q(lease_owner="") | Q(lease_owner__isnull=True))
                .update(
                    lease_owner=self.worker_id,
                    lease_expires_at=now + timedelta(seconds=self.lease_duration),
                    updated_at=now,
                )
            )
            return updated == 1
        except Exception as exc:
            logger.warning(
                "Worker %s: lease acquisition failed for job %s: %s",
                self.worker_id, job.id.hex[:8], exc,
            )
            return False

    # ── job processing ─────────────────────────────────────────────────

    def _process_job(self, job: Job):
        """
        Run process_take() under a heartbeat, then release the lease in a
        finally. process_take() owns Job state (DONE/FAILED); we own only the
        lease columns. The belt-and-suspenders except guards against a future
        refactor that lets process_take() raise.
        """
        take = job.take
        take_id = take.id.hex[:8]
        job_id = job.id.hex[:8]

        logger.info(
            "Worker %s: processing Job %s (Take %s, state=%s, attempt %d/%d)",
            self.worker_id, job_id, take_id, job.state, job.attempts, job.max_attempts,
        )

        hb = LeaseHeartbeat(
            job_id=job.id,
            worker_id=self.worker_id,
            lease_duration=self.lease_duration,
            interval=self.heartbeat_interval,
        )
        hb.start()

        try:
            success = process_take(take, worker_id=self.worker_id)
            if success:
                self._jobs_processed += 1
                logger.info("Worker %s: Job %s COMPLETED", self.worker_id, job_id)
            else:
                self._jobs_failed += 1
                logger.warning("Worker %s: Job %s FAILED (see pipeline logs above)",
                               self.worker_id, job_id)
        except Exception as exc:
            self._jobs_failed += 1
            logger.exception("Worker %s: Job %s raised unexpectedly: %s",
                             self.worker_id, job_id, exc)
            from studio import persistence
            persistence.mark_job_failed(take, "worker-exception", f"Unhandled: {exc}")
        finally:
            hb.stop()
            # Release lease, filtered on owner so we never clear a lease the
            # recovery sweep already handed to someone else.
            try:
                Job.objects.filter(
                    id=job.id,
                    lease_owner=self.worker_id,        # all-kwargs: legal
                ).update(lease_owner="", lease_expires_at=None)
            except Exception as exc:
                logger.warning("Worker %s: lease release failed for Job %s: %s",
                               self.worker_id, job_id, exc)
