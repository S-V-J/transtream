#!/usr/bin/env python3
"""
Prove the worker BOOTS, by running it. Not by parsing it.

WHY THIS EXISTS. Commit 28ffb2c added a persistent-log block to tsworker.py
whose last statement is logger.info("worker log -> ..."), and the file never
bound the name `logger`. The result: manage.py tsworker exited 1 on every
invocation, plain and warmed, so no upload could be processed at all. Every
static gate in this repository passed it - py_compile (the file is syntactically
perfect), manage.py check (it does not import management commands), and G1-G4
(AST over HTTP/serializer layers). NameError is a RUNTIME fact. The only
instrument that sees it is running the command, which is what the warmup A/B did
by accident, 0.5 s before its own purpose, and what this guard does on purpose.

WHY IT IS A SEPARATE FILE FROM guard_layers.py. guard_layers is static
introspection over resolve() and source text; it never spawns a process and has
no skip path. A boot proof must execute, must tolerate contention with a live
worker, and costs ~0.5 s rather than milliseconds. Merging them would give the
runtime guard the static file's missing skip branch.

THE SKIP BRANCH, AND WHY IT IS NOT OPTIONAL. `tsworker --once` runs the recovery
sweep AND one poll cycle. If a real worker is up and mid-take, this command can
claim the next job and spend ~8 s transcribing it - stealing work from the
production worker, from inside a test. So the guard reads the queue first and
reports SKIP (exit 2) rather than FAIL when non-terminal jobs exist. A guard that
does not know its own precondition is a hazard, and a hazard that runs on every
pre-push check is a hazard that fires often.

THE PRECONDITION IS THE WORKER'S OWN PREDICATE, NOT A PARALLEL ONE. check_queue()
calls studio.worker.runnable_jobs() rather than hand-listing states. The first
version counted "non-terminal" as NOT IN (done, failed, cancelled) while the poll
admits QUEUED *and* FAILED-with-retry-budget, so a job in backoff was invisible
here and claimable by --once: the guard could steal a live worker's retry. Worst
case was duplicated work rather than corruption (segment writes are
delete-then-insert per (take, source) and the lease claim is atomic), but a check
that under-reports its own precondition is not a check.

NOT PURE-READ, AND WHY THAT IS ACCEPTABLE. tsworker --once runs the recovery sweep
BEFORE it polls, and the sweep WRITES: it reclaims in-flight jobs whose lease
expired and increments their attempt counter. So this guard is not read-only. That
is safe because an expired lease means the holder is dead or already reclaimed -
LeaseHeartbeat stops renewing once its owner filter matches 0 rows - so reclaiming
is the correct action, not a stolen write. Recorded here because "a guard that only
reads" is a promise this file does not keep, and a future reader who assumed it
would be wrong.

WHAT IT ASSERTS, and why each line is a separate assertion rather than "exit 0":
  exit 0            the command completed
  banner            options actually reached WorkerLoop (a crash before line 114
                    would still exit 0 if the traceback were swallowed)
  sweep complete    the DB is reachable and recovery ran
  worker log ->     the RotatingFileHandler block executed - this is the
                    assertion that would have caught 28ffb2c, and it is about the
                    HANDLER, not about the logger name
  exited summary    handle() ran to its own end, not merely past the crash site
  file on disk      logs/worker.log exists and gained the lines, i.e. the handler
                    is attached to a logger whose level admits INFO. Without this,
                    "worker log ->" could print while nothing was being written.

--selftest feeds the assertion machinery a synthetic 28ffb2c-shaped failure and
requires it to FAIL, because a gate that has never been observed refusing has
never been tested. That is the rule that guard_layers.py --selftest encodes and
that this file would otherwise have violated in its first draft.
"""
import argparse
import os
import pathlib
import re
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
LOGFILE = ROOT / "logs" / "worker.log"
WORKER_ID = "boot-guard"

REQUIRED = [
    ("banner",            r"TranStream worker .* \| poll=.*lease=.*heartbeat=.*concurrent=.*batch="),
    ("recovery sweep",    r"recovery: sweep complete \| leases_reclaimed=\d+ orphans_deleted=\d+ errors=\d+"),
    ("log handler",       r"worker log -> .*worker\.log \(rotating, 5 MB x 3\)"),
    ("exit summary",      r"Worker .* exited \| processed=\d+ failed=\d+"),
]


def run_once(timeout: int = 90):
    """Execute `tsworker --once` with a distinctive worker id; return (rc, text)."""
    env = dict(os.environ)
    env.pop("TS_WARMUP_MODELS", None)          # boot proof must not cost ~4 s
    proc = subprocess.run(
        [sys.executable, "manage.py", "tsworker", "--once", "--worker-id", WORKER_ID],
        cwd=str(BACKEND), env=env, timeout=timeout,
        capture_output=True, text=True,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def check_queue():
    """
    How many jobs a `tsworker --once` would actually try to claim, or None if the
    DB could not be read.

    Delegates to studio.worker.runnable_jobs() so the predicate has ONE
    implementation. See the module docstring for why a hand-written state list was
    wrong: it disagreed with the poll about FAILED jobs, in the direction that made
    this guard under-report.

    COST, stated as a cost and measured by the probe in the commit that adds this
    (not estimated here, and not labelled "measured" before the number exists):
    importing studio.worker pulls studio.pipeline and the audio services into this
    subprocess at MODULE level. The models themselves load lazily on first call, so
    no weights are read - but the package imports are not free, and this subprocess
    is a second one alongside the tsworker --once the guard spawns anyway. If the
    delta proves material, the predicate belongs in studio/models.py as a Job
    manager, which imports nothing heavy; that move is deferred rather than guessed
    because models.py was not read when this was written.
    """
    code = (
        "import os,sys; sys.path.insert(0,'.');"
        "os.environ.setdefault('DJANGO_SETTINGS_MODULE','transtream.settings');"
        "import django; django.setup();"
        "from studio.worker import runnable_jobs;"
        "print(runnable_jobs().count())"
    )
    try:
        out = subprocess.run([sys.executable, "-c", code], cwd=str(BACKEND),
                             capture_output=True, text=True, timeout=60)
        return int(out.stdout.strip().splitlines()[-1])
    except Exception:
        return None


def evaluate(rc: int, text: str) -> list:
    """Return a list of failure strings; empty means the boot is proven."""
    fails = []
    if rc != 0:
        fails.append(f"exit={rc} (want 0)")
    for label, pat in REQUIRED:
        if not re.search(pat, text):
            fails.append(f"{label} line absent: /{pat[:48]}/")
    if "Traceback" in text:
        fails.append("traceback present in output")
    if "NameError" in text:
        fails.append("NameError present in output")
    return fails


def selftest() -> int:
    """Prove the assertion machinery can refuse. Feed it 28ffb2c's own failure."""
    synthetic_rc = 1
    synthetic_text = (
        "Traceback (most recent call last):\n"
        '  File ".../tsworker.py", line 112, in handle\n'
        '    logger.info("worker log -> %s (rotating, 5 MB x 3)", log_path)\n'
        "NameError: name 'logger' is not defined\n"
    )
    fails = evaluate(synthetic_rc, synthetic_text)
    print(f"  synthetic 28ffb2c crash -> {len(fails)} failure(s) detected")
    for f in fails:
        print("    ", f)
    if not fails:
        print("SELFTEST FAIL - the gate cannot see the exact bug it was written for.")
        return 1

    good_rc, good_text = 0, (
        "TranStream worker boot-guard | poll=2.0s lease=120s heartbeat=30s "
        "concurrent=1 batch=5 once=True\n"
        "recovery: sweep complete | leases_reclaimed=0 orphans_deleted=0 errors=0\n"
        "worker log -> /home/ML/transtream/logs/worker.log (rotating, 5 MB x 3)\n"
        "Worker boot-guard exited | processed=0 failed=0\n"
    )
    good_fails = evaluate(good_rc, good_text)
    print(f"  synthetic healthy boot -> {len(good_fails)} failure(s) (want 0)")
    if good_fails:
        for f in good_fails:
            print("    ", f)
        print("SELFTEST FAIL - the gate rejects a good boot (false positive).")
        return 1

    print("SELFTEST PASS - refuses the real bug, accepts a clean boot.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true", help="prove the gate can fail")
    ap.add_argument("--force", action="store_true",
                    help="run even if the queue is non-empty (will contend with a live worker)")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    if not (BACKEND / "manage.py").exists():
        print(f"ABORT: no manage.py under {BACKEND}")
        return 1

    live = check_queue()
    if live is None:
        print("ABORT: could not read the job queue; refusing to run blind.")
        return 1
    if live > 0 and not args.force:
        print(f"SKIP: {live} non-terminal job(s) in the queue. `tsworker --once` would")
        print("      poll and could steal one from a live worker. Stop the worker, or")
        print("      drain the queue, or pass --force if you accept the contention.")
        return 2

    before = LOGFILE.stat().st_size if LOGFILE.exists() else 0
    t0 = time.perf_counter()
    try:
        rc, text = run_once()
    except subprocess.TimeoutExpired:
        print(f"FAIL: tsworker --once did not return within 90s")
        return 1
    dt = time.perf_counter() - t0

    fails = evaluate(rc, text)

    # The handler assertion is not satisfied by a printed line: prove bytes landed.
    after = LOGFILE.stat().st_size if LOGFILE.exists() else 0
    if not LOGFILE.exists():
        fails.append("logs/worker.log does not exist - handler never attached")
    elif after <= before:
        fails.append(f"logs/worker.log did not grow ({before} -> {after} bytes) "
                     f"- handler attached to a logger whose level excludes INFO")

    if fails:
        print(f"WORKER BOOT GUARD FAIL ({dt:.2f}s, exit={rc}):")
        for f in fails:
            print("   -", f)
        print("── captured output ──")
        print(text[-2000:] if text else "(empty)")
        return 1

    print(f"WORKER BOOT GUARD OK ({dt:.2f}s): exit 0, all 4 markers present, "
          f"logs/worker.log grew {before} -> {after} bytes.")
    print("The command executes. That is a fact no static gate can establish.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
