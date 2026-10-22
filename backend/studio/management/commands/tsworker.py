"""
Django management command:  python manage.py tsworker

Runs the TranStream background job worker (poll -> lease -> process ->
repeat). Named `tsworker`, NOT `runworker`, because Django Channels (in
INSTALLED_APPS ahead of `studio`) already ships a `runworker` command that
would shadow ours. `tsworker` is collision-free across Django core + Channels.

This worker is independent of Channels' runworker by design: the Job ROW is
the queue (durable across Redis/Daphne restarts), with leases, exponential
backoff, progress, and a startup recovery sweep — none of which Channels'
pub/sub runworker provides.

Production: run as a separate systemd user service beside Daphne, e.g.
    [Service]
    ExecStart=/home/ML/transtream/venv/bin/python /home/ML/transtream/backend/manage.py tsworker
    Restart=on-failure
    RestartSec=5
Development: second terminal ->  cd backend && python manage.py tsworker

Flags:
    --once                  recovery + one poll cycle, then exit (test/cron)
    --worker-id NAME        override auto "worker-<pid>" identity
    --poll-interval S       idle poll period (default settings/2.0)
    --lease-duration S      lease TTL (default settings/120)
    --heartbeat-interval S  lease renewal period (default settings/30)
    --max-concurrent N      semaphore bound (default settings/1)
    --poll-batch N          max jobs per poll (default settings/5)
    --verbose               DEBUG level for studio loggers
"""

import logging
import logging.handlers
import os
import sys
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

# Module-level logger. THIS LINE WAS MISSING and it took the worker down on
# every start - plain and warmed alike, exit 1 - from commit 28ffb2c until now.
# The persistent-log block in handle() ends with logger.info("worker log -> ...")
# and this file never bound the name: it imports logging, logging.handlers, Path
# and settings, and calls logging.getLogger(noisy) and logging.getLogger() (root),
# but nothing assigns `logger`. NameError is a RUNTIME fact, so py_compile passed,
# manage.py check passed, and G1-G4 passed. The only gate that sees it is running
# the command, which is why scripts/guard_worker_boots.py now exists.
#
# Named per the convention every other module in studio/ already follows
# (consumers.py, warmup.py, worker.py, persistence.py all bind
# getLogger(__name__)), so the worker's lines carry the command's dotted path
# instead of arriving unnamed on the root logger.
logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Run the TranStream durable job worker (poll -> lease -> process -> repeat)."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true",
                            help="Run recovery + one poll cycle then exit.")
        parser.add_argument("--worker-id", type=str, default=None,
                            help="Override worker identity string.")
        parser.add_argument("--poll-interval", type=float, default=None,
                            help="Seconds between idle DB polls.")
        parser.add_argument("--lease-duration", type=int, default=None,
                            help="Lease TTL in seconds.")
        parser.add_argument("--heartbeat-interval", type=int, default=None,
                            help="Lease renewal period in seconds.")
        parser.add_argument("--max-concurrent", type=int, default=None,
                            help="Max simultaneous job executions (semaphore bound).")
        parser.add_argument("--poll-batch", type=int, default=None,
                            help="Max jobs fetched per poll cycle.")
        parser.add_argument("--verbose", action="store_true",
                            help="Set studio logger level to DEBUG.")

    def handle(self, *args, **options):
        # Optional reproducibility pin: must run BEFORE ctranslate2 is imported
        # (i.e. before `from studio.worker import WorkerLoop` below).
        if os.getenv("WHISPER_DETERMINISTIC", "0").lower() in {"1", "true", "yes", "on"}:
            os.environ["OMP_NUM_THREADS"] = "1"
            os.environ.setdefault("CT2_VERBOSE", "0")
        # The worker configures ITS OWN logging (settings.py has no LOGGING
        # dict, so Django's implicit default is WARNING+ to stderr). This does
        # NOT touch Daphne's process. INFO+ so pipeline/recovery progress shows.
        level = logging.DEBUG if options["verbose"] else logging.INFO
        logging.basicConfig(
            level=level,
            format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
            stream=sys.stderr,
        )
        for noisy in ("django.db.backends", "urllib3", "asyncio", "hpack", "h2"):
            logging.getLogger(noisy).setLevel(logging.WARNING)

        # ─── Persistent worker log ──────────────────────────────────────────
        # WHY THIS EXISTS AND WHY IT IS HERE RATHER THAN IN settings.py.
        # The evidence for the durable path - "persisted 11 VAD segment(s)",
        # "stage 1 backfilled duration_ms=122041", the recovery sweep counts,
        # the warmup timings - lived only in a terminal that closes. Losing it
        # meant losing the only record that Q2/Q3 ever worked.
        #
        # A LOGGING DICT IN settings.py WAS THE OBVIOUS FIX AND IT IS WRONG:
        # this command configures logging with basicConfig(), which does
        # NOTHING if the root logger already has handlers. Django applies
        # settings.LOGGING at setup() time, i.e. before handle() runs, so a
        # dict that installed a root handler would silently disable the
        # --verbose level and the format string above. The fix would have
        # broken the switch that motivated it, invisibly. Attaching the handler
        # here, AFTER basicConfig, has no such interaction, and it keeps the
        # worker's log configuration in the one process that owns it.
        #
        # logs/ is gitignored by the same rule that keeps daphne.log out of the
        # repository, so nothing recorded here can be committed by accident.
        # Rotation bounds the disk cost: 5 MB x 3 backups = 15 MB ceiling,
        # which is generous for text and small next to one 122 s WAV.
        log_path = Path(settings.REPO_ROOT) / "logs" / "worker.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8",
        )
        fh.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        ))
        root = logging.getLogger()
        root.addHandler(fh)
        logger.info("worker log -> %s (rotating, 5 MB x 3)", log_path)

        # ─── Warmup result, emitted HERE rather than in ready() ──────────────
        # This is the line 28ffb2c's own message promised ("the warmup timings")
        # and did not deliver: ready() has already run by the time the handler
        # exists, so warmup.py's record had nowhere to go and was dropped by
        # lastResort (WARNING+). Emitting it here, after the handler is attached,
        # is the smallest change that makes the number land in logs/worker.log.
        # format_result() is warmup.py's single implementation, so the two call
        # sites cannot drift.
        #
        # Flag OFF is the normal case and must stay quiet at INFO: Daphne never
        # warms, and a "not requested" line on every worker start would be noise
        # that trains the reader to skip the section. DEBUG keeps it available
        # to --verbose without making it a fixture.
        from studio.apps import get_warmup_result
        from studio.warmup import format_result
        wr = get_warmup_result()
        if wr is None:
            logger.debug("warmup: not requested in this process (TS_WARMUP_MODELS unset)")
        else:
            logger.info(format_result("AppConfig.ready", wr))

        from studio.worker import WorkerLoop

        loop = WorkerLoop(
            worker_id=options["worker_id"],
            poll_interval=options["poll_interval"],
            lease_duration=options["lease_duration"],
            heartbeat_interval=options["heartbeat_interval"],
            max_concurrent=options["max_concurrent"],
            poll_batch=options["poll_batch"],
        )

        self.stdout.write(
            f"TranStream worker {loop.worker_id} | "
            f"poll={loop.poll_interval}s lease={loop.lease_duration}s "
            f"heartbeat={loop.heartbeat_interval}s concurrent={loop.max_concurrent} "
            f"batch={loop.poll_batch} once={options['once']}"
        )

        try:
            loop.run(once=options["once"])
        except KeyboardInterrupt:
            # SIGINT is normally caught by the worker's own handler; this
            # guards the race where it arrives before handlers are installed.
            self.stdout.write(self.style.WARNING("Interrupted during startup."))

        self.stdout.write(self.style.SUCCESS(
            f"Worker {loop.worker_id} exited | "
            f"processed={loop._jobs_processed} failed={loop._jobs_failed}"
        ))
