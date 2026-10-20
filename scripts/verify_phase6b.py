"""
Phase 6b verification harness (persisted, 6b-fix edition).

Scenarios (order keeps exactly one eligible job per worker poll):
  A  happy path        : scratch Take + Job(QUEUED) -> worker -> DONE,
                         dual segments, normalized file on disk.
  E  re-run convergence: re-run the SAME take -> STRUCTURAL invariants hold:
                         counts > 0, indices contiguous 0..n-1, zero duplicate
                         (source,index) rows, all whisper text non-empty, job
                         DONE. Exact count EQUALITY across re-runs is NOT
                         asserted: CPU int8 / internal-VAD-ONNX re-runs are
                         not bit-reproducible (README "Known behavior").
                         Counts are printed for information.
  B  retry path        : FAILED(attempts=1<max) -> picked up -> DONE,
                         attempts not inflated on success.
  C  lease reclaim     : in-flight + expired lease -> sweep -> QUEUED,
                         attempts+1, lease cleared; exhausted in-flight ->
                         FAILED (never a silent QUEUED park).
  D  orphan sweep      : old .ts_part_* deleted; young one protected.

Safety: operates on a COPY of the newest real webm (scratch
TS_REC_RAW_HARNESS_<hex>.webm); copy + derived NRM + fakes + test user are
removed in finally, even on exception. Real captures are never mutated.

Run:  python scripts/verify_phase6b.py        (from anywhere; self-bootstraps)
Exit: 0 all pass / 1 failure / 2 prerequisite missing.
"""
import os
import shutil
import sys
import time
import uuid
from datetime import timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "backend"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "transtream.settings")

import django  # noqa: E402

django.setup()

from django.conf import settings              # noqa: E402
from django.contrib.auth import get_user_model  # noqa: E402
from django.db.models import Count            # noqa: E402
from django.utils import timezone             # noqa: E402

from studio import persistence                # noqa: E402
from studio.audio_service import normalized_output_path  # noqa: E402
from studio.models import Job, JobState, Segment, SegmentSource, Take  # noqa: E402
from studio.recovery import run_recovery_sweep  # noqa: E402
from studio.worker import WorkerLoop            # noqa: E402

User = get_user_model()
fails = []


def check(name, cond):
    print(("PASS" if cond else "FAIL"), "-", name)
    if not cond:
        fails.append(name)


rec_dir = Path(settings.RECORDINGS_DIR)
webms = sorted(
    rec_dir.glob("TS_REC_RAW_*.webm"),
    key=lambda p: p.stat().st_mtime,
    reverse=True,
)
if not webms:
    print("No TS_REC_RAW_*.webm found. Record one browser take first.")
    sys.exit(2)

src_webm = webms[0]
hexid = uuid.uuid4().hex[:12]
scratch_raw = rec_dir / f"TS_REC_RAW_HARNESS_{hexid}.webm"
scratch_nrm = normalized_output_path(scratch_raw)
fake_old = rec_dir / f".ts_part_harness_old_{hexid}.webm"
fake_new = rec_dir / f".ts_part_harness_new_{hexid}.webm"

print(f"source webm : {src_webm.name} ({src_webm.stat().st_size} bytes)")
print(f"scratch raw : {scratch_raw.name}")

user, _ = User.objects.get_or_create(username="ts_worker_test")


def new_worker(wid):
    return WorkerLoop(
        worker_id=wid, poll_interval=0.1, lease_duration=300,
        heartbeat_interval=10, max_concurrent=1, poll_batch=5,
    )


def contiguous(rows):
    idx = sorted(r["index"] for r in rows)
    return idx == list(range(len(idx)))


try:
    shutil.copy2(src_webm, scratch_raw)

    # ── A: happy path ──────────────────────────────────────────────────
    print("\n-- Scenario A: happy path --")
    Take.objects.filter(user=user).delete()
    take_a = persistence.create_mic_take(
        user=user, source_filename=scratch_raw.name,
        raw_abs_path=str(scratch_raw), duration_ms=0, mime="audio/webm",
    )
    check("A1 Take created", isinstance(take_a, Take))
    job_a = take_a.current_job
    Job.objects.filter(id=job_a.id).update(
        state=JobState.QUEUED, progress_pct=0, next_run_at=timezone.now(),
    )
    take_a.refresh_from_db()
    check("A2 forced to QUEUED", take_a.current_job.state == JobState.QUEUED)

    new_worker("test-worker-A").run(once=True)

    take_a.refresh_from_db()
    job_a = take_a.current_job
    check("A3 job DONE", job_a is not None and job_a.state == JobState.DONE)
    check("A4 progress 100", job_a is not None and job_a.progress_pct == 100)
    check("A5 lease released", job_a is not None and job_a.lease_owner == "")
    check("A6 normalized_relpath set", bool(take_a.normalized_relpath))
    check("A7 language detected", bool(take_a.language))
    check("A8 VAD segments > 0", take_a.vad_segments.count() > 0)
    check("A9 Whisper segments > 0", take_a.whisper_segments.count() > 0)
    check("A10 Whisper text non-empty",
          all((s.text or "").strip() for s in take_a.whisper_segments))
    nrm_abs = (Path(settings.MEDIA_ROOT) / take_a.normalized_relpath
               if take_a.normalized_relpath else None)
    check("A11 normalized file exists", nrm_abs is not None and nrm_abs.exists())
    print(f"   lang={take_a.language} ({(take_a.language_probability or 0):.2f}) "
          f"vad={take_a.vad_segments.count()} wsp={take_a.whisper_segments.count()}")
    for s in take_a.whisper_segments.order_by("index")[:3]:
        print(f"     [{s.index}] {s.start_ms}->{s.end_ms}ms  {(s.text or '')[:60]}")

    # ── E: re-run convergence (STRUCTURAL, 6b-fix) ─────────────────────
    print("\n-- Scenario E: re-run convergence (structural invariant) --")
    vad_before = take_a.vad_segments.count()
    wsp_before = take_a.whisper_segments.count()
    Job.objects.filter(id=job_a.id).update(
        state=JobState.QUEUED, progress_pct=0, next_run_at=timezone.now(),
    )
    new_worker("test-worker-E").run(once=True)
    take_a.refresh_from_db()

    nv = take_a.vad_segments.count()
    nw = take_a.whisper_segments.count()
    check("E1 VAD rows > 0 after re-run", nv > 0)
    check("E2 Whisper rows > 0 after re-run", nw > 0)
    check("E3 VAD indices contiguous 0..n-1 (no dupes/gaps)",
          contiguous(take_a.vad_segments.values("index")))
    check("E4 Whisper indices contiguous 0..n-1 (no dupes/gaps)",
          contiguous(take_a.whisper_segments.values("index")))
    dupes = (
        Segment.objects.filter(take=take_a)
        .values("source", "index")
        .annotate(c=Count("id"))
        .filter(c__gt=1)
        .count()
    )
    check("E5 zero duplicate (source,index) rows", dupes == 0)
    check("E6 all Whisper rows carry text after re-run",
          all((s.text or "").strip() for s in take_a.whisper_segments))
    check("E7 job DONE after re-run", take_a.current_job.state == JobState.DONE)
    print(f"   counts (informational, variance allowed): "
          f"vad {vad_before}->{nv}, whisper {wsp_before}->{nw}")

    # ── B: retry path ──────────────────────────────────────────────────
    print("\n-- Scenario B: retry path --")
    take_b = persistence.create_mic_take(
        user=user, source_filename=scratch_raw.name,
        raw_abs_path=str(scratch_raw), duration_ms=0, mime="audio/webm",
    )
    job_b = take_b.current_job
    Job.objects.filter(id=job_b.id).update(
        state=JobState.FAILED, attempts=1, max_attempts=3,
        last_error="[simulated] previous crash", next_run_at=timezone.now(),
    )
    take_b.refresh_from_db()
    check("B1 FAILED attempts=1<max=3",
          take_b.current_job.state == JobState.FAILED and take_b.current_job.attempts == 1)
    new_worker("test-worker-B").run(once=True)
    take_b.refresh_from_db()
    job_b = take_b.current_job
    check("B2 retry -> DONE", job_b is not None and job_b.state == JobState.DONE)
    check("B3 attempts stays 1 on success", job_b is not None and job_b.attempts == 1)
    check("B4 Whisper segments present", take_b.whisper_segments.count() > 0)

    # ── C: lease reclaim (direct sweep; NO worker run after) ───────────
    print("\n-- Scenario C: lease reclaim --")
    take_c = persistence.create_mic_take(
        user=user, source_filename=scratch_raw.name,
        raw_abs_path=str(scratch_raw), duration_ms=0, mime="audio/webm",
    )
    job_c = take_c.current_job
    past = timezone.now() - timedelta(seconds=60)
    Job.objects.filter(id=job_c.id).update(
        state=JobState.NORMALIZING, attempts=0, max_attempts=3,
        lease_owner="dead-worker-999", lease_expires_at=past,
    )
    take_c.refresh_from_db()
    check("C0 pre-sweep: NORMALIZING w/ expired lease",
          take_c.current_job.state == JobState.NORMALIZING
          and take_c.current_job.lease_owner == "dead-worker-999")
    sweep = run_recovery_sweep()
    check("C1 sweep reclaimed >=1 lease", sweep["leases_reclaimed"] >= 1)
    take_c.refresh_from_db()
    job_c = take_c.current_job
    check("C2 reclaimed -> QUEUED", job_c.state == JobState.QUEUED)
    check("C3 attempts incremented to 1", job_c.attempts == 1)
    check("C4 lease cleared", job_c.lease_owner == "" and job_c.lease_expires_at is None)
    Job.objects.filter(id=job_c.id).update(
        state=JobState.TRANSCRIBING, attempts=2, max_attempts=3,
        lease_owner="dead-worker-999", lease_expires_at=past,
    )
    run_recovery_sweep()
    take_c.refresh_from_db()
    job_c = take_c.current_job
    check("C5 exhausted in-flight -> FAILED (not silent QUEUED park)",
          job_c.state == JobState.FAILED and job_c.attempts == 3)

    # ── D: orphan sweep (direct sweep; NO worker run after) ────────────
    print("\n-- Scenario D: orphan temp sweep --")
    fake_old.write_bytes(b"x")
    fake_new.write_bytes(b"y")
    old_t = time.time() - 7200
    os.utime(fake_old, (old_t, old_t))   # 2h old -> past 1h cutoff -> deleted
    sweep_d = run_recovery_sweep()       # fake_new keeps fresh mtime -> protected
    check("D1 old orphan deleted", not fake_old.exists())
    check("D2 young temp KEPT (active-session protection)", fake_new.exists())
    check("D3 sweep reported >=1 orphan deleted", sweep_d["orphans_deleted"] >= 1)

finally:
    for p in (scratch_raw, scratch_nrm, fake_old, fake_new):
        try:
            if p.exists():
                p.unlink()
        except OSError:
            pass
    try:
        user.delete()   # cascades Take -> Job/Segment, ApiKey
    except Exception:
        Take.objects.filter(user=user).delete()
    print("\ncleanup: scratch + fake temps removed, test user deleted")

print()
print("=" * 64)
if fails:
    print(f"RESULT: {len(fails)} FAILURE(S): {fails}")
    sys.exit(1)
print("RESULT: PHASE 6b ALL SCENARIOS PASSED "
      "(A happy, E structural-convergence, B retry, C reclaim, D orphan)")
sys.exit(0)
