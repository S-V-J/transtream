#!/usr/bin/env python3
"""
End-to-end first-take A/B: does TS_WARMUP_MODELS actually move cost off a user's
first take, and by how much.

WHY THIS EXISTS. The README states the warmup's COST from self-reports and its
SAVING only as an upper bound (4.22 s of model load + 1.19 s of CTranslate2 first
decode, both measured cold). The saving on a machine whose page cache is already
warm has never been measured. This harness measures it, and it also answers a
sharper question the samples exposed: warmup slices the first second of the newest
normalized WAV, and the 41.6 s reference take begins with 1.04 s of silence, so on
that file the slice is SILENT (VAD 0 segments, Whisper 0 segments, still ok=True).
Did the decoder warm anyway?

THE DESIGN THAT MAKES IT SELF-NORMALIZING. Each worker lifetime runs TWO takes in
the same process, and the metric is the WITHIN-LIFETIME difference:

    cold arm : take1 - take2  =  the full cold penalty a warmup should remove
    warm arm : take1 - take2  =  the residual penalty after warmup
    effectiveness             =  cold(take1-take2) - warm(take1-take2)

Each arm supplies its own baseline, so thermal state, MPS memory throttling, CPU
frequency and the size of the take cancel out instead of being argued about. A
single take per arm would compare 03:20's machine against 03:21's, which is the
mistake the bash attempt that preceded this made.

THE AUDIO PAGE CACHE DOES NOT CANCEL BY LUCK, IT CANCELs BY POSITION. Both arms'
take1 pays it and both arms' take2 does not, so it contributes equally to both
within-arm residuals and disappears from cold-warm. The warmup slices a DIFFERENT
file (the newest NRM under a take dir), so it cannot pre-warm the uploaded webm in
either arm - which is why the comparison is about model state and not about which
arm happened to read the container first.

IF THE WARM ARM'S RESIDUAL IS NEAR ZERO ON THE WORKER CLOCK (the residual is now
the worker clock, claim->release, so a warmed first take should sit inside the
second-take band and its take1-take2 residual should be small - on the order of
the band width, ~0.2 s), the silent slice warmed the decoder; if it is about the
size of the per-engine first-call penalty (~1-3 s of worker time), it did not, and
the slice offset moves past the leading silence. An earlier draft quoted 1.2 s
here, but that figure was a WALL expectation and wall residuals are quantised at
the poll interval, so it is not a valid threshold for this metric.

WHY IT USES scripts/verify_phase8_api.py's CLIENT RATHER THAN ITS OWN.
The first version of this harness carried ~120 lines of hand-written client and
failed on its first server call: generate -> 400 {'error': 'Key name is
required.'}. Cause, read from backend/studio/views.py:260 - the view parses
`json.loads(request.body)`, and the hand-written client posted
application/x-www-form-urlencoded. Reading further found a SECOND defect the 400
masked: revoke was aimed at /account/keys/revoke/ with {"id": kid} in the body,
while the route is /account/keys/<id>/revoke/ with an EMPTY body (views.py's own
docstring, and finish() in the certified module, agree). Two bugs, one of them
invisible until the other was fixed - which is the argument for the import far
stronger than taste: a second client re-derives certified behaviour by
inspection, and inspection is how this session has been wrong twenty-odd times.

THE IMPORT IS CONDITIONAL, AND THE CONDITION IS A HAZARD, NOT A STYLE NOTE.
verify_phase8_api runs django.setup() at module scope (lines 64-68), so importing
it configures Django in this process. That is fine and in fact convenient - see
queue_count() below. What is NOT fine is touching its reporting machinery:
  * check() appends to module-global RESULTS and prints PASS/FAIL
  * bail() sets BAILED and calls finish()
  * finish() writes /tmp/ts_phase8_report.txt - THE ARTIFACT green_gate.py
    CERTIFIES - revokes MINTED_KEYS, then calls sys.exit()
So a harness that imported this module and accidentally called any of those three
would overwrite the certification report with its own contents, and green_gate
would certify an A/B run as the API suite. That is contamination of THE GATE, not
of this harness's numbers. Therefore: import Client, hdr, multipart - and never
check, bail, finish, RESULTS, MINTED_KEYS, _CLEANUP or REPORT_PATH. The pre-flight
below asserts exactly that, and refuses if it is violated.

ONE MORE TRAP, LEARNED BY READING: urllib.parse is imported INSIDE that module's
`if __name__ == '__main__'` guard, so importing the module does NOT give you
urllib.parse. Anyone replicating main()'s login must import it themselves. This
file does. (Side note for Phase 9: that means main() only works when run as
__main__. Importing and calling it would NameError. Deliberately not fixed here -
editing a green file for a hypothetical is the api_views.py 22:15 failure.)

WHY IT IS PYTHON AND NOT BASH. README: "All the harnesses are stdlib-only Python,
never bash. A bash probe here once conflated an empty body with an HTML error page
(the json.loads error text is identical for both) and cost me an evening." The
attempt this replaces did exactly that, 28 times in a row: an empty $JOB produced
GET /api/v1/jobs/ (route mismatch -> Django's HTML 404) and json.load could not
tell that from an empty body. The certified client's json() already returns
{_non_json, _empty, _bytes, _preview, _csrf?} as explicit keys, so this harness
never has to infer anything from a parse failure - it reads the shape the client
hands it.

WHAT IT ASSERTS ABOUT ITS OWN PREMISE before reporting any number:
  * no worker alive at start, and exactly one alive per arm, killed between arms
  * the queue is empty at start, using studio.worker.runnable_jobs() - the ONE
    predicate. In-process here, because importing the certified module has already
    run django.setup(); scripts/guard_worker_boots.py keeps its subprocess for the
    OPPOSITE reason (it is a gate that must not import the worker's heavy deps, so
    it cannot fail for the same reason the thing it guards fails). One predicate,
    two callers, two import strategies, and the difference is written at both.
  * the warm arm's log slice contains a "warmup:" line and the cold arm's does
    NOT. If both do, TS_WARMUP_MODELS never reached the child and every number
    below is meaningless, so the run aborts. This is the experiment's negative
    control, and it is also why the log handler matters: readiness is detected by
    waiting for that exact line, which was impossible before the command emitted
    it (scripts/guard_worker_boots.py exists for the same reason).
  * the log is only ever READ, by offset, never truncated - and
    scripts/guard_log_append_only.py is the gate that keeps it that way. The three
    warmup samples this harness's README cites were destroyed once by a `: >` in a
    terminal; that is the failure mode the offset discipline exists to prevent.
  * after the run, ACTIVE api keys must be exactly what they were before. Eight
    keys are minted and eight must be revoked; a leak would show as active+1, and
    a leak is precisely what MINTED_KEYS/finish() exist to prevent in the certified
    suite. Checked here because this harness does NOT get to call finish().

ORDER AND ITS BIAS, stated rather than buried. Lifetimes alternate C, W, W, C by
default. The first position pays cold OS page cache for the audio file and any
first-process cost, and position 1 is a COLD rep, so that extra cost lands on
cold rep0's take1 ONLY. Under the within-lifetime metric it ADDS to the cold
arm's first residual and inflates cold-minus-warm: the bias runs FOR the effect,
not against it. The consequence is the opposite of what an earlier draft of this
paragraph claimed - a NULL result is the safe one (no effect beyond noise, if
anything an underestimate), while a POSITIVE result is contaminated by position-1
cost and must be read off the per-engine worker-clock decomposition, trusting the
clean rep (position 4, no page-cache hit) over the first. Note the two components
of position-1 cost behave differently: the AUDIO PAGE CACHE is symmetric (both
arms' take1 pay it on the uploaded webm) so it cancels in the residual, as the
paragraph above this one says; what does NOT cancel is the first-process and
interpreter warm cost and the Hub revision round trip, both of which land only on
the first lifetime. The Hub round trip is NOT a confound - it is signal, because
in the warm arm it lands inside the warmup (before the take) and in the cold arm
inside take1; but it is a rep0-and-position-1 phenomenon too, so the first cold
rep carries the page-cache cost AND, on a cold cache, the Hub trip, and its
residual is the least trustworthy of four.
"""

import argparse
import datetime as dt
import json
import os
import pathlib
import re
import signal
import subprocess
import sys
import time
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
PORT_FILE = ROOT / "logs" / "transtream.port"
LOG_FILE = ROOT / "logs" / "worker.log"
RECORDINGS = ROOT / "media" / "recordings"

# ─── the certified client, under the conditions stated in the docstring ──────
sys.path.insert(0, str(ROOT / "scripts"))
from verify_phase8_api import Client, hdr, multipart     # noqa: E402

POLL_S = 1.5                 # same cadence as studio.js, so the poller is realistic
TAKE_TIMEOUT_S = 300.0
WORKER_READY_S = 120.0
MIN_TAKE_BYTES = 100 * 1024  # ignore truncated leftovers when picking the file
TS_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),(\d{3})")


class HarnessError(Exception):
    """Anything that makes the measurement invalid rather than merely slow."""


# ─── session bootstrap: mirrors main()'s S1, including its assertions ────────

def login(c: Client, base: str, user: str, password: str) -> str:
    """
    Log in through the certified client and return the csrftoken.

    Mirrors verify_phase8_api.main() S1 exactly, including the two assertions that
    make a 302 meaningful: the redirect must be a 302/303 and NOT a re-rendered
    200 (a 200 on a login POST means the credentials were rejected and Django
    redisplayed the form), and the csrftoken cookie must be 32 chars. The
    32-length check is not pedantry: it is the difference between "a cookie was
    set" and "the cookie the CSRF middleware will accept", and the certified
    suite asserts it for a reason.

    Form-encoded, deliberately. /accounts/login/ is a Django FORM endpoint, unlike
    /account/keys/generate/ which is a JSON endpoint (views.py:260). Getting these
    two mixed up is exactly the bug this function replaces.
    """
    st, _, body = c.call("GET", "/accounts/login/")
    if st != 200:
        raise HarnessError(f"login page -> {st}; cannot scrape CSRF token")
    m = re.search(rb'name="csrfmiddlewaretoken" value="([^"]+)"', body)
    if not m:
        raise HarnessError("no csrfmiddlewaretoken in the login HTML")
    tok = m.group(1).decode()
    form = ("username=" + urllib.parse.quote(user)
            + "&password=" + urllib.parse.quote(password)
            + "&csrfmiddlewaretoken=" + urllib.parse.quote(tok)).encode()
    st, h, b = c.call("POST", "/accounts/login/", data=form, headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "Referer": f"{base}/accounts/login/",
    })
    if st not in (302, 303):
        pathlib.Path("/tmp/ts_ab_login_body.html").write_bytes(b)
        raise HarnessError(
            f"login POST -> {st} (wanted 302; a 200 means rejected credentials); "
            f"location={hdr(h, 'Location')} body captured to "
            f"/tmp/ts_ab_login_body.html")
    names = {ck.name for ck in c.jar}
    if "sessionid" not in names:
        raise HarnessError(f"login 302 but no sessionid cookie (have {sorted(names)})")
    csrf = next((ck.value for ck in c.jar if ck.name == "csrftoken"), None)
    if not csrf:
        raise HarnessError(f"login 302 but no csrftoken cookie (have {sorted(names)})")
    if len(csrf) != 32:
        raise HarnessError(f"csrftoken len={len(csrf)}, want 32")
    return csrf


def _account_headers(csrf: str, base: str) -> dict:
    """The session-route CSRF contract: X-CSRFToken header + Referer, JSON body.

    NOT a form field. /account/* keeps full CSRF enforcement (README decision 13);
    only the four bearer routes are csrf_exempt. finish() in the certified module
    uses this exact shape, and so does S2/S11 - three independent confirmations.
    """
    return {"Content-Type": "application/json",
            "X-CSRFToken": csrf,
            "Referer": f"{base}/account/"}


def mint_key(c: Client, base: str, csrf: str, name: str):
    """POST /account/keys/generate/ with a JSON body. Returns (key_id, raw_key)."""
    st, _, d = c.json("POST", "/account/keys/generate/",
                      data=json.dumps({"name": name}).encode(),
                      headers=_account_headers(csrf, base))
    if st != 200 or not isinstance(d, dict) or d.get("_non_json"):
        raise HarnessError(f"generate -> {st}; body={str(d)[:300]}")
    kid, raw = d.get("id"), d.get("raw_key")
    if not kid or not raw:
        raise HarnessError(f"generate 200 but no id/raw_key: {list(d)}")
    return kid, raw


def revoke_key(c: Client, base: str, csrf: str, kid: str) -> int:
    """POST /account/keys/<id>/revoke/ with an EMPTY body. Returns the status.

    The id is in the URL, not the body. The hand-written client this replaces put
    {"id": kid} into a POST to /account/keys/revoke/ - a route that does not exist.
    It never surfaced, because the 400 on generate aborted the run first: one bug
    hiding another, which is the argument for not re-deriving certified behaviour.
    """
    st, _, d = c.json("POST", f"/account/keys/{kid}/revoke/", data=b"",
                      headers=_account_headers(csrf, base))
    if st != 200:
        print(f"  !! revoke {kid[:8]} -> {st}: {str(d)[:200]}")
    return st


def upload(c: Client, key: str, path: pathlib.Path):
    """Multipart upload through the certified encoder. Returns (job_id, take_id)."""
    blob = path.read_bytes()
    mp, boundary = multipart({}, "file", path.name, blob, "audio/webm")
    st, h, d = c.json("POST", "/api/v1/transcriptions", data=mp,
                      headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                      bearer=key)
    if st != 202 or not isinstance(d, dict) or d.get("_non_json"):
        raise HarnessError(
            f"upload -> {st} (wanted 202); content-type={hdr(h, 'Content-Type')}; "
            f"body={str(d)[:300]}; if _empty/_non_json the response was not JSON, "
            f"which for a well-formed route means a server-side failure")
    job, take = d.get("job_id"), d.get("take_id")
    if not job or not take:
        raise HarnessError(f"202 but no job_id/take_id: {list(d)}")
    return job, take


def poll_to_terminal(c: Client, key: str, job_id: str):
    """
    Returns (wall_s, polls, payload).

    409 is the DOCUMENTED 'not ready' answer (README decision 6), so it is normal
    flow. 429 is handled INSIDE c.json (retry up to 3 times, honoring Retry-After),
    which is the behaviour we publish for clients - and which is why this harness
    uses json() rather than call(). S12 in the certified suite deliberately uses
    call() because a rate-limit test must OBSERVE the 429; a timing harness must
    not, and the asymmetry is intentional in both directions.

    A non-JSON body is read as the SHAPE the client returns (_empty / _bytes /
    _preview), never inferred from a parse error. That is the whole lesson the
    bash probe paid for.
    """
    t0 = time.monotonic()
    n = 0
    last = None
    while True:
        n += 1
        st, _, d = c.json("GET", f"/api/v1/jobs/{job_id}", bearer=key)
        if isinstance(d, dict) and d.get("_non_json"):
            raise HarnessError(
                f"poll {job_id} -> {st} NON-JSON (empty={d.get('_empty')} "
                f"bytes={d.get('_bytes')} csrf={bool(d.get('_csrf'))}); "
                f"preview={d.get('_preview', '')[:200]!r}")
        if st == 409:
            last = d
        elif st == 200 and isinstance(d, dict):
            if d.get("is_terminal"):
                return time.monotonic() - t0, n, d
            last = d
        else:
            raise HarnessError(f"poll {job_id} -> {st}; body={str(d)[:300]}")
        if time.monotonic() - t0 > TAKE_TIMEOUT_S:
            raise HarnessError(f"poll {job_id} timed out after {TAKE_TIMEOUT_S}s "
                               f"({n} polls); last={last}")
        time.sleep(POLL_S)


# ─── worker lifecycle ───────────────────────────────────────────────────────

def queue_count() -> int:
    """In-process, because importing the certified module already ran django.setup()."""
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "transtream.settings")
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))
    from studio.worker import runnable_jobs
    return runnable_jobs().count()


def spawn_worker(warm: bool):
    env = dict(os.environ)
    env.pop("TS_WARMUP_MODELS", None)
    if warm:
        env["TS_WARMUP_MODELS"] = "1"
    return subprocess.Popen(
        [sys.executable, "manage.py", "tsworker"],
        cwd=str(BACKEND), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def log_slice(offset: int) -> str:
    """
    Read only what this lifetime appended, so markers from earlier reps cannot be
    mistaken for this one's. NEVER truncates: the three warmup samples the README
    cites were destroyed once by a `: > logs/worker.log` in a bash probe, and
    scripts/guard_log_append_only.py now exists so that cannot happen from a
    tracked script. Offsets are the discipline; the gate is the backstop.
    """
    if not LOG_FILE.exists():
        return ""
    size = LOG_FILE.stat().st_size
    if size <= offset:
        return ""
    with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as fh:
        fh.seek(offset)
        return fh.read()


def wait_ready(proc, warm: bool, offset: int, label: str) -> str:
    """
    Block until the worker has finished whatever it does before taking work.

    Warm arm: wait for the `warmup:` line, because the warmup is the thing being
    measured and a take enqueued during it would overlap and corrupt the number.
    Cold arm: wait for the recovery sweep line, the equivalent readiness marker.
    """
    marker = (re.compile(r"warmup: AppConfig\.ready") if warm
              else re.compile(r"recovery: sweep complete \| leases_reclaimed=\d+"))
    t0 = time.monotonic()
    slice_text = ""
    while time.monotonic() - t0 < WORKER_READY_S:
        if proc.poll() is not None:
            raise HarnessError(f"{label}: worker exited early (rc={proc.returncode}); "
                               f"see {LOG_FILE}")
        slice_text = log_slice(offset)
        if marker.search(slice_text):
            return slice_text
        time.sleep(0.25)
    raise HarnessError(f"{label}: worker never became ready within {WORKER_READY_S}s; "
                       f"looked for {marker.pattern!r}; tail={slice_text[-600:]!r}")


def stop_worker(proc, label: str):
    """SIGTERM, which worker.py handles as 'finish the current job then stop'.
    Nothing should be in flight here, so this should be quick."""
    if proc.poll() is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=60)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=15)
        raise HarnessError(f"{label}: worker ignored SIGTERM and had to be killed")


# ─── log-derived stage decomposition ───────────────────────────────────────

def _ts(line: str):
    m = TS_RE.match(line)
    if not m:
        return None
    return dt.datetime.strptime(m.group(1) + "." + m.group(2), "%Y-%m-%d %H:%M:%S.%f")


def stages_for(log_text: str, job_id: str, take_id: str) -> dict:
    """
    Pull this take's per-stage timestamps out of the worker's own log.

    Stage lines are keyed by TAKE id (`process_take [xxxx] stage 1 OK`), claim and
    completion by JOB id. Using the wrong one silently returns nothing, which is
    why every marker is required below rather than trusted.
    """
    j8, t8 = job_id[:8], take_id[:8]
    marks = {}
    for line in log_text.splitlines():
        ts = _ts(line)
        if ts is None:
            continue
        if f"processing Job {j8}" in line:
            marks.setdefault("claim", ts)
        elif f"process_take [{t8}] stage 1 OK" in line:
            marks.setdefault("normalize", ts)
        elif f"process_take [{t8}] stage 2 OK" in line:
            marks.setdefault("vad", ts)
        elif f"process_take [{t8}] stage 3 OK" in line:
            marks.setdefault("whisper", ts)
        elif f"Job {j8} COMPLETED" in line:
            marks.setdefault("release", ts)
    need = ("claim", "normalize", "vad", "whisper", "release")
    missing = [k for k in need if k not in marks]
    if missing:
        raise HarnessError(f"stage markers missing for job {j8}/take {t8}: {missing}; "
                           f"the log does not describe this take")
    spans = {}
    for a, b in zip(need, need[1:]):
        spans[f"{a}->{b}"] = round((marks[b] - marks[a]).total_seconds(), 3)
    spans["total"] = round((marks["release"] - marks["claim"]).total_seconds(), 3)
    return spans


# ─── one lifetime ───────────────────────────────────────────────────────────

def run_lifetime(c: Client, base: str, csrf: str, take_file: pathlib.Path,
                 warm: bool, tag: str) -> dict:
    """
    One worker process, two takes.

    A fresh key per TAKE, not per lifetime: 2 takes x ~30 polls inside ~20 s could
    brush the 120/min limiter, and the limiter is a known-sensitive boundary (S12).
    Removing it as a variable costs nothing. Revoked in a finally, because this
    harness does not get to rely on the certified module's finish()/MINTED_KEYS.
    """
    label = f"{tag}({'warm' if warm else 'cold'})"
    offset = LOG_FILE.stat().st_size if LOG_FILE.exists() else 0
    proc = spawn_worker(warm)
    results = {}
    try:
        ready_slice = wait_ready(proc, warm, offset, label)

        # NEGATIVE CONTROL, before any number is reported.
        saw_warmup = bool(re.search(r"warmup: AppConfig\.ready", ready_slice))
        if warm and not saw_warmup:
            raise HarnessError(f"{label}: warm arm produced NO warmup line; "
                               f"TS_WARMUP_MODELS did not reach the child. "
                               f"tail={ready_slice[-400:]!r}")
        if not warm and saw_warmup:
            raise HarnessError(f"{label}: COLD arm produced a warmup line; the two "
                               f"arms are not actually different and every number "
                               f"below is meaningless")

        for which in ("take1", "take2"):
            kid, key = mint_key(c, base, csrf, f"ab-{tag}-{which}")
            try:
                t_send = time.monotonic()
                job_id, take_id = upload(c, key, take_file)
                t_accept = time.monotonic()      # 202 received: the clock the user feels
                wall, polls, payload = poll_to_terminal(c, key, job_id)
                spans = stages_for(log_slice(offset), job_id, take_id)
                results[which] = {
                    "wall_s": round(wall, 3),
                    "accept_lag_s": round(t_accept - t_send, 3),
                    "polls": polls,
                    # The worker's own claim->release total, from its own log
                    # lines read by offset. wall_s is measured send ->
                    # terminal-observed and therefore lands on the POLL_S grid;
                    # this does not, which is why every cross-arm comparison
                    # below uses it and the wall figures are stratified.
                    "worker_s": spans["total"],
                    "state": payload.get("state"),
                    "duration_ms": payload.get("duration_ms"),
                    "spans": spans,
                }
                print(f"  {label} {which}: wall={wall:6.3f}s polls={polls:2d} "
                      f"worker={spans['total']:6.3f}s "
                      f"accept={t_accept - t_send:.3f}s "
                      f"state={payload.get('state')} "
                      f"normalize={spans['claim->normalize']:.3f} "
                      f"vad={spans['normalize->vad']:.3f} "
                      f"whisper={spans['vad->whisper']:.3f} "
                      f"release={spans['whisper->release']:.3f}")
            finally:
                revoke_key(c, base, csrf, kid)

        # On the worker clock, not wall_s: wall_s lands on the POLL_S grid, so a
        # within-lifetime take1-take2 difference across unequal poll counts is
        # measuring phase (04:50 cold t1 7 polls vs t2 3; 05:46 warm t2 2 vs cold
        # t2 3 - and r14 cold t2 had the LOWEST worker clock of all 16 takes yet
        # took 3 polls against r01 cold t2's 2). worker_s is claim->release from
        # the worker's own log lines, so it is immune to how often we polled.
        # This also makes the worker_s comment above ("every cross-arm comparison
        # below uses it") true for the first time: it removes a prose/code
        # divergence, it does not annotate one.
        results["residual_s"] = round(results["take1"]["worker_s"]
                                      - results["take2"]["worker_s"], 3)
        results["warm"] = warm
        return results
    finally:
        stop_worker(proc, label)
        time.sleep(1.0)   # let the SQLite write settle before the next lifetime


def median(vals):
    s = sorted(vals)
    n = len(s)
    return round(s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2, 3)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--lifetimes", type=int, default=2,
                    help="worker lifetimes PER ARM (default 2 -> C,W,W,C)")
    ap.add_argument("--file", type=pathlib.Path, default=None,
                    help="take to upload (default: newest raw at the recordings root)")
    args = ap.parse_args()

    # The pre-flight lives INSIDE main(), not beside it in a heredoc. On
    # 04:49:33 the heredoc version exited at its retired-shape check, so its
    # final section - the syntactic log check and the synthetic-refusal proof -
    # never ran, and this harness then wrote 23 KB to logs/worker.log ungated.
    # A control that lives beside the thing it guards can be skipped; one that
    # lives inside it cannot. That is the same reason _acquire_lease carries its
    # DO-NOT-collapse note at the site rather than in the README.
    from preflight_ab_warmup import run_preflight
    rc = run_preflight()
    if rc:
        print("ABORT: pre-flight refused; every number below would be uncalled for.")
        return rc

    if not (BACKEND / "manage.py").exists():
        print(f"ABORT: no manage.py under {BACKEND}")
        return 1
    port = PORT_FILE.read_text().strip()
    base = f"http://127.0.0.1:{port}"
    password = os.environ.get("TS_ADMIN_PASSWORD")
    if not password:
        print("ABORT: TS_ADMIN_PASSWORD unset. Source $HOME/.transtream_dev_env, and run")
        print("       this harness alone - never pasted together with the creds command.")
        return 1

    take_file = args.file
    if take_file is None:
        cands = [p for p in sorted(RECORDINGS.glob("TS_REC_RAW_*.webm"),
                                   key=lambda p: p.stat().st_mtime, reverse=True)
                 if p.stat().st_size > MIN_TAKE_BYTES]
        if not cands:
            print(f"ABORT: no raw take over {MIN_TAKE_BYTES} bytes under {RECORDINGS}")
            return 1
        take_file = cands[0]
    if not take_file.exists():
        print(f"ABORT: --file {take_file} does not exist")
        return 1
    print(f"take     : {take_file.name} ({take_file.stat().st_size:,} bytes)")
    print(f"base url : {base}")
    print(f"log      : {LOG_FILE} (read by offset, never truncated)")
    print(f"lifetimes: {args.lifetimes} per arm, order C,W,W,C\n")

    alive = subprocess.run(["pgrep", "-af", "manage.py tsworker"],
                           capture_output=True, text=True)
    if alive.stdout.strip():
        print("ABORT: a tsworker is already running:\n" + alive.stdout.strip())
        print("       Two workers on one SQLite queue would make every number wrong.")
        return 1
    n = queue_count()
    if n:
        print(f"ABORT: {n} runnable job(s) already in the queue; drain them first, or")
        print("       the first take of the first lifetime races pre-existing work.")
        return 1

    c = Client(base)
    csrf = login(c, base, "admin", password)
    print("login    : ok (302 + sessionid + csrftoken len=32)\n")

    from studio.models import ApiKey
    active_before = ApiKey.objects.filter(is_active=True).count()
    print(f"precondition: active api keys = {active_before} (must equal the "
          f"post-run count)\n")

    arms = {"cold": [], "warm": []}
    order = []
    for i in range(args.lifetimes):
        order += [("cold", i), ("warm", i)] if i % 2 == 0 else [("warm", i), ("cold", i)]

    try:
        for idx, (kind, rep) in enumerate(order, start=1):
            tag = f"r{rep}{idx}"
            print(f"── lifetime {idx}/{len(order)}: {kind} rep{rep} [{tag}] ──")
            res = run_lifetime(c, base, csrf, take_file, warm=(kind == "warm"), tag=tag)
            arms[kind].append(res)
            print(f"   residual (take1-take2) = {res['residual_s']}s\n")
    except HarnessError as exc:
        print(f"\nHARNESS ABORT (measurement invalid, not slow): {exc}")
        return 1
    finally:
        subprocess.run(["pkill", "-f", "manage.py tsworker"], capture_output=True)

    if not (arms["cold"] and arms["warm"]):
        print("ABORT: one arm has no completed lifetime; no comparison is possible.")
        return 1

    active_after = ApiKey.objects.filter(is_active=True).count()
    leaked = active_after - active_before
    cold_res = [r["residual_s"] for r in arms["cold"]]
    warm_res = [r["residual_s"] for r in arms["warm"]]
    mc, mw = median(cold_res), median(warm_res)
    eff = round(mc - mw, 3)

    # The residual-of-residual above is thermally clean but it is NOT the
    # user-facing quantity. What a caller experiences is take1's wall clock, and
    # the 04:50 run says the interesting thing about it is the SPREAD: cold
    # take1 11.193/5.654 (5.54 s wide, one HF-Hub round trip landing in rep0
    # only) against warm take1 3.938/3.954 (0.016 s wide). Warm take1 also lands
    # within noise of cold take2 (3.959/3.955) - i.e. the warmup makes the first
    # take cost what the second take costs. Print that beside the derived figure
    # so the README cannot quote 2.61 s without the distribution next to it.
    cold_t1 = [r["take1"]["wall_s"] for r in arms["cold"]]
    warm_t1 = [r["take1"]["wall_s"] for r in arms["warm"]]
    print("=" * 72)
    print(f"USER-FACING first take (wall s, EXCLUDES accept lag): "
          f"cold {cold_t1} median {median(cold_t1)}"
          f" | warm {warm_t1} median {median(warm_t1)}"
          f" | saving {round(median(cold_t1) - median(warm_t1), 3)} s"
          f" | POLL-UNMATCHED cold t1 {[r['take1']['polls'] for r in arms['cold']]}"
          f" vs warm t1 {[r['take1']['polls'] for r in arms['warm']]}")
    # The outlier note is conditional on an OBSERVED >2 s cold-t1 spread - the
    # 04:50 signature - so it is a live detector rather than a stale assertion about
    # a run that is no longer here. Built as a plain variable, not as a conditional
    # inside an f-string replacement field: nested quotes inside braces are the
    # escape-and-brace class that has killed eight patches, and a name costs nothing.
    # n comes from len(cold_t1), which this print already uses, rather than
    # len(cold_res), whose definition point is not established above this line.
    _outlier = (" plus one cold-t1 outlier over 2 s (a Hub round trip)"
                if max(cold_t1) - min(cold_t1) > 2.0 else "")
    print(f"  spread: cold {round(max(cold_t1) - min(cold_t1), 3)} s vs "
          f"warm {round(max(warm_t1) - min(warm_t1), 3)} s - the variance collapse "
          f"is the durable claim; the median saving rests on n={len(cold_t1)}"
          f"{_outlier}")
    # ── the wall comparison, stratified by poll count ───────────────────────
    # This line used to compare median(warm take1) against median(cold take2)
    # and label the result 'first take costs what the second take costs'. That claim holds only when both
    # medians come from EQUAL poll counts, because wall_s lands on the POLL_S
    # grid. At 04:50 they matched (3,3 vs 3,3) and it held at -0.011 s. At 05:46
    # cold take2 used 2 and 3 polls against warm take1's 3 and 3, and the same
    # line printed a 0.95 s gap that was entirely phase - while still carrying
    # the sentence that claimed the opposite. Execution found what reading had
    # not. Thin cells are labelled, because a median of one sample is not a
    # median.
    def _walls(kind, which, npolls):
        return sorted(r[which]["wall_s"] for r in arms[kind]
                      if r[which]["polls"] == npolls)

    strata = sorted({r["take1"]["polls"] for r in arms["warm"]}
                    & {r["take2"]["polls"] for r in arms["cold"]})
    print("POLL-STRATIFIED wall comparison (the only sound wall claim):")
    if not strata:
        print("  no poll count shared by warm take1 and cold take2 this run; "
              "no wall comparison is available - read the WORKER CLOCK below")
    for npolls in strata:
        wt, ct = _walls("warm", "take1", npolls), _walls("cold", "take2", npolls)
        thin = ("" if min(len(wt), len(ct)) >= 2
                else "  [n=1 on one side: an observation, not a median]")
        print(f"  @{npolls} polls: warm t1 {wt} | cold t2 {ct} | "
              f"delta {round(median(wt) - median(ct), 3)} s{thin}")
    print("=" * 72)
    # ── the metric that needs no stratum ────────────────────────────────────
    # The worker's own claim->release clock, from its own log lines, so it is
    # immune to poll phase, to MPS throttle state (36-41% across the two runs,
    # 14-15% while this patch was written) and to a cold page cache at once.
    def _col(kind, which, key):
        return [r[which]["spans"][key] for r in arms[kind]]

    cold_w1, warm_w1 = _col("cold", "take1", "total"), _col("warm", "take1", "total")
    cold_w2, warm_w2 = _col("cold", "take2", "total"), _col("warm", "take2", "total")
    # The band is the SECOND-TAKE population ONLY. warm_w1 is deliberately NOT
    # a member: including it made the containment test tautological, because a
    # set always contains its own subset, and the check could then only ever
    # print "inside" however badly the warmup had failed.
    steady = sorted(cold_w2 + warm_w2)
    lo, hi = steady[0], steady[-1]
    width = round(hi - lo, 3)
    # arms[] is appended in lifetime order and the order list alternates by rep,
    # so index i of each arm is rep i. If --lifetimes or the order scheme ever
    # changes, re-derive the pairing rather than trusting zip.
    def _pen(key):
        return [round(a - b, 3) for a, b in
                zip(_col("cold", "take1", key), _col("warm", "take1", key))]

    inside = sum(1 for v in warm_w1 if lo <= v <= hi)
    over = [round(v - hi, 3) for v in warm_w1 if v > hi]
    under = [round(lo - v, 3) for v in warm_w1 if v < lo]
    print("WORKER CLOCK claim->release (s), immune to poll phase:")
    print(f"  cold t1 {cold_w1} | warm t1 {warm_w1} | cold t2 {cold_w2} | warm t2 {warm_w2}")
    print(f"  second-take band (all take2, n={len(steady)}): "
          f"{lo:.3f}-{hi:.3f} s (width {width} s); cold t1 exceeds its top by "
          f"{[round(v - hi, 3) for v in cold_w1]} s")
    print(f"  per-engine cold penalty (cold t1 - warm t1): "
          f"vad {_pen('normalize->vad')} whisper {_pen('vad->whisper')} "
          f"normalize {_pen('claim->normalize')}")
    print(f"  poll counts: cold t1 {[r['take1']['polls'] for r in arms['cold']]} "
          f"warm t1 {[r['take1']['polls'] for r in arms['warm']]} "
          f"cold t2 {[r['take2']['polls'] for r in arms['cold']]} "
          f"warm t2 {[r['take2']['polls'] for r in arms['warm']]} "
          f"- compare only equal counts")
    print(f"  silent 1 s slice vs the second-take band: {inside}/{len(warm_w1)} warm "
          f"take1 inside; over={over} under={under} (band width {width} s, so an "
          f"exceedance below it is sub-noise, not a contradiction)")
    print("=" * 72)
    print(f"cold-arm residuals (take1-take2, seconds): {cold_res}  median {mc}")
    print(f"warm-arm residuals (take1-take2, seconds): {warm_res}  median {mw}")
    print(f"warmup effectiveness = cold - warm = {eff} s")
    print(f"active api keys: before {active_before} -> after {active_after} "
          f"({'no leak' if leaked == 0 else f'*** LEAKED {leaked} ***'})")
    print("=" * 72)
    print("Reading it (NOTE: eff is now derived from the worker clock "
          "(claim->release, immune to the "
          f"{POLL_S} s poll grid); the wall figures above are stratified by poll "
          "count and the USER-FACING saving is labelled POLL-UNMATCHED. The "
          "per-engine stage figures remain the cleanest read):")
    if eff <= 0.25:
        print("  ~0 s : the warmup moved nothing measurable. Either the silent slice")
        print("         did not warm the decoder, or the cold penalty is smaller than")
        print("         the 5.41 s bound. Compare warm-arm whisper vs cold-arm.")
    elif eff >= mc * 0.8:
        print("  ~full: the warmup removed essentially the whole cold penalty, which")
        print("         means the 1 s slice DID warm the decoder despite 0 segments.")
    else:
        print(f"  partial ({eff} of {mc} s): the warmup took most but not all of the")
        print("         penalty. The residual is the part a 1 s slice cannot pay.")
    print("\nCaveats, stated not buried:")
    print(f"  * n={len(cold_res)} per arm. Indicative, not definitive.")
    print("  * order C,W,W,C puts the cold-page-cache position on a COLD rep, so the")
    print("    bias runs FOR the effect: a null result is the safe one, a positive one")
    print("    is inflated by position-1 cost - read the per-engine worker clock, and")
    print("    trust the clean rep (position 4) over the first.")
    print("  * MPS memory throttling varies run to run; read the [MPS-ADMIT] lines in")
    print("    your terminal for THIS run's state. The")
    print("    within-lifetime design cancels it, but only if both takes of a lifetime")
    print("    fall inside the same throttle state (~10 s apart, so mostly yes).")
    print("  * on the reference take the slice is silent (first speech at 1.040 s there), so a")
    print("    near-zero effectiveness is about the SLICE, not about warmup in general.")
    return 1 if leaked else 0


if __name__ == "__main__":
    sys.exit(main())
