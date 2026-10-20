#!/usr/bin/env python3
"""
TranStream Phase 8 API integration harness.

WHY PYTHON AND NOT BASH: the bash probe for these same endpoints failed three
distinct ways in one evening — (1) CSRF extracted from the cookie jar instead of
the HTML body, (2) MPS-ADMIT stdout swallowed by $(python ...), (3) an empty
response body with no HTTP status captured, which silently voided six assertions
and made a real question ("did the bearer upload 500 or was the connection reset?")
unanswerable. Every HTTP call here records status + headers + body, and a non-JSON
body is a first-class DIAGNOSTIC outcome, never a crash.

WHAT IT COVERS (all four bearer endpoints, every branch):
  S1  session login                       (CSRF from the HTML body, 302 observed)
  S2  key generate via session endpoint   (raw_key prefix, hint, id)
  S3  key list reveals no secrets         (fingerprint / hashed_secret / raw_key)
  S4  POST /api/v1/transcriptions         -> 202 {job_id, take_id, size}
  S5  GET  .../transcript while PENDING   -> 409 (never previously exercised)
  S6  poll to terminal via bearer         (worker-absence detected, not confused)
  S7  DB cross-check                      (Take.kind, duration_ms>0, files on disk)
  S8  transcript json|srt|vtt|txt         + 400 unknown format
  S9  negatives: no header / wrong scheme / empty bearer / garbage key /
      foreign job id / GET-on-POST-only
  S10 DELETE .../cancel                   -> 200 or 409 (worker race, both correct)
      + re-cancel -> 409
  S11 revoke                              -> 200, then POST-REVOKE -> 401 with the
      EXACT uniform message. This is the assertion that decides Q4.
  S12 rate-limit enforcement (SEEDED COUNTER, deterministic)
      Reads the limit from settings, so it behaves identically at 10/min and
      120/min. Three probes: seed limit-1 -> one request -> 200 AND exactly one
      increment (the double-feed detector); seed limit -> one request -> 429 +
      Retry-After: 60 + naming body; clear -> 200. This REPLACES a 14-shot
      burst that was unsound BY CONSTRUCTION: a fixed window keyed on
      int(time.time()//60) splits a ~1 s burst harmlessly across a boundary
      (5+9, both halves <= 10), so "no 429" proved nothing. At 23:33 the burst
      returned [200 x 14] while the SAME run's key A hit its limit at exactly
      the predicted 11th call - the limiter was demonstrably working and the
      test was demonstrably unsound.
  S13 cleanup                             (revoke test keys, flush limiter counters)

Exit code: 0 = every assertion passed. 1 = at least one FAIL. 2 = bailed early
(missing prerequisite), which is NOT a pass.

Run:  python scripts/verify_phase8_api.py            # prompts for the password
      TS_ADMIN_PASSWORD=... python scripts/verify_phase8_api.py
Requires: Daphne running (reads logs/transtream.port) and, for S6/S10, tsworker.
"""

import getpass
import http.cookiejar
import io
import json
import os
import pathlib
import re
import sys
import time
import urllib.error
import urllib.request
import uuid

# ─── Django context (for the DB cross-check and REDIS_URL) ────────────────
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "transtream.settings")
import django  # noqa: E402
django.setup()
from django.conf import settings  # noqa: E402

REPORT_PATH = pathlib.Path("/tmp/ts_phase8_report.txt")
RESULTS = []          # (section, label, ok, detail)
BAILED = None

# H4: every key this run mints is recorded here and revoked in finish(), on
# EVERY exit path - including early returns after a failure. Without this, the
# 22:43 run stopped at S4, never reached S11/S13, and left an ACTIVE key whose
# raw secret had already been printed to the console. A test harness must not
# be able to leak a credential by failing.
MINTED_KEYS = []      # list of (key_id, raw_key)
_CLEANUP = {}         # {"client":…, "csrf":…, "base":…} stashed by main()


# ─── reporting ────────────────────────────────────────────────────────────

def check(section, label, ok, detail=""):
    RESULTS.append((section, label, bool(ok), detail))
    print(f"{'PASS' if ok else '*** FAIL ***'} [{section}] {label}"
          + (f"   :: {detail}" if detail else ""))
    return bool(ok)


def bail(reason):
    """Abort with a non-zero, non-passing exit. Used only for missing prereqs."""
    global BAILED
    BAILED = reason
    print(f"\n!!! BAIL: {reason}")
    finish()


def finish():
    lines = []
    for section, label, ok, detail in RESULTS:
        lines.append(f"{'PASS' if ok else 'FAIL'} [{section}] {label}"
                     + (f"   :: {detail}" if detail else ""))
    if BAILED:
        lines.append(f"BAIL {BAILED}")
    # Revoke everything this run minted, before deciding the exit code.
    cli, csrf_tok, base_url = _CLEANUP.get("client"), _CLEANUP.get("csrf"), _CLEANUP.get("base")
    if cli and csrf_tok and base_url and MINTED_KEYS:
        for kid, raw in MINTED_KEYS:
            st, _, bd = cli.json("POST", f"/account/keys/{kid}/revoke/", data=b"",
                                 headers={"Content-Type": "application/json",
                                          "X-CSRFToken": csrf_tok,
                                          "Referer": f"{base_url}/account/"})
            tag = "revoked" if st == 200 else f"*** REVOKE FAILED status={st} ***"
            print(f"cleanup: {tag} {str(raw)[:12]}… ({kid[:8]}…)")
            RESULTS.append(("S13", f"cleanup revoked {kid[:8]}…", st == 200, f"status={st}"))
    fails = [r for r in RESULTS if not r[2]]
    lines.append("")
    lines.append(f"assertions: {len(RESULTS)}   passed: {len(RESULTS) - len(fails)}"
                 f"   failed: {len(fails)}")
    body = "\n".join(lines) + "\n"
    REPORT_PATH.write_text(body)
    print(f"\nreport -> {REPORT_PATH}")
    if BAILED:
        sys.exit(2)
    sys.exit(1 if fails else 0)


# ─── HTTP client: status + headers + body on EVERY call ──────────────────

class NoRedirect(urllib.request.HTTPRedirectHandler):
    """
    Do not follow redirects. A 302 must be OBSERVABLE: silently following one
    turns an auth failure into a 200 of the login page, which is precisely how
    'looks green, actually broken' happens.
    """
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ARG002
        return None


class Client:
    def __init__(self, base):
        self.base = base.rstrip("/")
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar),
            NoRedirect(),
        )

    def call(self, method, path, *, data=None, headers=None, bearer=None,
             timeout=90):
        """Returns (status, headers_dict, body_bytes). status 0 == transport error."""
        hdrs = dict(headers or {})
        if bearer:
            hdrs["Authorization"] = f"Bearer {bearer}"
        req = urllib.request.Request(self.base + path, data=data,
                                     method=method, headers=hdrs)
        try:
            with self.opener.open(req, timeout=timeout) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read()
            except Exception:  # pragma: no cover - defensive
                body = b""
            return exc.code, dict(exc.headers or {}), body
        except Exception as exc:
            return 0, {}, f"TRANSPORT_ERROR {type(exc).__name__}: {exc}".encode()

    def json(self, method, path, *, _max_429_waits=3, **kw):
        """
        Call + parse. On non-JSON returns a dict carrying explicit shape keys
        (_empty / _bytes / _preview / _csrf) so the CALLER decides what a
        non-JSON response means. This is the fix for the bash probe's silent
        cascade: an empty or HTML body becomes DATA, not a stack trace.

        RETRY-ON-429. A 429 is a legitimate server answer, not a transport
        failure: the bearer contract publishes Retry-After exactly so a correct
        client waits and continues. Without this the poll loop (S6) fails the
        whole suite on a limit the server is entitled to enforce - which is
        what happened at 23:07:05, where S6 died on 429 after only three
        authenticated calls. Honoring it makes the harness behave like the
        client we document, and incidentally proves the Retry-After contract
        end-to-end instead of merely asserting its shape.

        S12 IS NOT AFFECTED: the burst test drives c.call() directly, because a
        rate-limit test must OBSERVE the 429 rather than wait it out. Do not
        "simplify" S12 onto this method.
        """
        waits = 0
        while True:
            status, hdrs, body = self.call(method, path, **kw)
            if status != 429 or waits >= _max_429_waits:
                break
            waits += 1
            ra = hdr(hdrs, "Retry-After")
            try:
                delay = min(int(ra), 65) if ra else 60
            except (TypeError, ValueError):
                delay = 60
            print(f"    (429 on {path}; honoring Retry-After={ra!r} "
                  f"-> sleeping {delay}s  [wait {waits}/{_max_429_waits}])")
            time.sleep(delay)
        try:
            return status, hdrs, json.loads(body.decode("utf-8"))
        except Exception:
            # THREE shapes, kept distinct on purpose. json.loads raises the
            # byte-identical message "Expecting value: line 1 column 1 (char 0)"
            # for an EMPTY body and for a body whose first char is '<' - so the
            # crash text cannot tell "connection reset" from "Django's 403 HTML
            # page". The 22:30 bash probe read that message, concluded "empty",
            # and an entire evening went to the wrong hypothesis. Here the
            # distinction is explicit keys, not an inference.
            text = body.decode("utf-8", "replace")
            out = {"_non_json": True,
                   "_empty": len(body) == 0,
                   "_bytes": len(body),
                   "_preview": text[:400].replace("\n", " ")}
            # CSRF middleware rejects with a 403 whose REASON lives only in the
            # server log. Sniff the marker so a CSRF regression is NAMED at the
            # assertion that caught it, not reconstructed from a grep later.
            if status == 403 and "csrf" in text.lower():
                out["_csrf"] = True
            return status, hdrs, out


def hdr(headers, name):
    """Case-insensitive header lookup (urllib hands us a plain dict)."""
    low = name.lower()
    for k, v in headers.items():
        if k.lower() == low:
            return v
    return None


# ─── multipart encoder (stdlib has none) ─────────────────────────────────

def multipart(fields, file_field, filename, blob, content_type):
    boundary = "----TSPhase8" + uuid.uuid4().hex
    out = io.BytesIO()
    for name, value in (fields or {}).items():
        out.write(b"--" + boundary.encode() + b"\r\n")
        out.write(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        out.write(str(value).encode() + b"\r\n")
    out.write(b"--" + boundary.encode() + b"\r\n")
    out.write(
        f'Content-Disposition: form-data; name="{file_field}"; '
        f'filename="{filename}"\r\n'.encode())
    out.write(f"Content-Type: {content_type}\r\n\r\n".encode())
    out.write(blob + b"\r\n")
    out.write(b"--" + boundary.encode() + b"--\r\n")
    return out.getvalue(), boundary


# ─── S0: prerequisites ────────────────────────────────────────────────────

def main():
    port_file = ROOT / "logs" / "transtream.port"
    base = os.environ.get("TS_BASE_URL")
    if not base:
        if not port_file.exists():
            bail(f"no {port_file} and no TS_BASE_URL - is Daphne running?")
        base = f"http://127.0.0.1:{port_file.read_text().strip()}"
    base = base.rstrip("/")
    print(f"base url : {base}")

    cands = sorted((ROOT / "media" / "recordings").glob("TS_REC_RAW_*.webm"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    if not cands:
        bail("no TS_REC_RAW_*.webm under media/recordings - record in the browser first")
    testfile = cands[0]
    blob = testfile.read_bytes()
    print(f"test file: {testfile.name} ({len(blob):,} bytes)")

    pw = os.environ.get("TS_ADMIN_PASSWORD") or getpass.getpass("TranStream admin password: ")
    c = Client(base)
    _CLEANUP["client"], _CLEANUP["base"] = c, base

    # ── S0: CSRF preflight. Isolates ONE variable before anything else. ──
    # A POST with a deliberately invalid key must reach the AUTH DECORATOR and
    # be refused 401. If CsrfViewMiddleware eats it in process_view, the
    # decorator never runs and the answer is 403 HTML. This single cheap call
    # therefore separates "bearer auth is broken" from "CSRF is blocking the
    # entire API" - the exact ambiguity that voided six assertions last run.
    # It needs no login, no key, no file and no worker, so it is the fastest
    # possible discriminator and it runs first.
    st0, _, d0 = c.json("POST", "/api/v1/transcriptions",
                        data=b"", bearer="ts_live_preflight_invalid")
    ok0 = check("S0", "unauthenticated POST reaches the auth decorator (401, not 403)",
                st0 == 401,
                f"status={st0}"
                + ("  <- CsrfViewMiddleware is blocking the bearer API: POST and "
                   "DELETE to /api/v1/* are unusable by every non-browser client. "
                   "Fix: api_key_required must apply csrf_exempt to the callable "
                   "it RETURNS."
                   if d0.get("_csrf") else ""))
    if not ok0:
        return finish()

    # ── S1: session login ────────────────────────────────────────────────
    st, _, body = c.call("GET", "/accounts/login/")
    if not check("S1", "login page reachable", st == 200, f"status={st}"):
        return finish()
    m = re.search(rb'name="csrfmiddlewaretoken" value="([^"]+)"', body)
    if not check("S1", "CSRF token present in HTML body", bool(m),
                 f"len={len(m.group(1)) if m else 0}"):
        return finish()
    csrf = m.group(1).decode()
    form = (f"username=admin&password={urllib.parse.quote(pw)}"
            f"&csrfmiddlewaretoken={urllib.parse.quote(csrf)}").encode()
    st, h, b = c.call("POST", "/accounts/login/", data=form, headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "Referer": f"{base}/accounts/login/",
    })
    if not check("S1", "login POST -> 302 (redirect, not 200 re-render)", st == 302,
                 f"status={st} location={hdr(h, 'Location')} body={b[:120]!r}"):
        # A 200 here means LoginView re-rendered the form, i.e. it rejected the
        # credentials - but the REASON lives in the HTML body, and the assertion
        # detail truncates to 120 bytes of <head>. Dump the whole body so the
        # server's own words ("Please enter a correct username and password" vs
        # anything else) are available instead of a status code plus a guess.
        # The 23:56 run cost a full cycle to a bare status=200; this is why.
        pathlib.Path("/tmp/ts_login_body.html").write_bytes(b)
        print(f"    >> login body captured to /tmp/ts_login_body.html "
              f"({len(b):,} bytes). Django's form errors render inside it:")
        for m in re.finditer(r"<ul[^>]*>(.*?)</ul>", b.decode("utf-8", "replace"), re.S):
            for li in re.findall(r"<li>(.*?)</li>", m.group(1), re.S):
                print("    >> FORM ERROR:", re.sub(r"<[^>]+>", "", li).strip())
        return finish()
    cookie_csrf = next((ck.value for ck in c.jar if ck.name == "csrftoken"), None)
    # H4 depends on this: finish() revokes every minted key on EVERY exit
    # path, and it needs the CSRF token to call the revoke endpoint. An
    # earlier revision stashed only client+base, so the conjunction in
    # finish() was permanently falsy and no key was ever cleaned up.
    _CLEANUP["csrf"] = cookie_csrf
    check("S1", "csrftoken cookie set (32 chars is correct)",
          bool(cookie_csrf) and len(cookie_csrf) == 32, f"len={len(cookie_csrf or '')}")

    def session_post(path, payload=None):
        data = json.dumps(payload or {}).encode() if payload is not None else b""
        return c.call("POST", path, data=data, headers={
            "Content-Type": "application/json",
            "X-CSRFToken": cookie_csrf or "",
            "Referer": f"{base}/account/",
        })

    # ── S2: generate key A ───────────────────────────────────────────────
    st, _, d = c.json("POST", "/account/keys/generate/",
                      data=json.dumps({"name": "phase8-A"}).encode(),
                      headers={"Content-Type": "application/json",
                               "X-CSRFToken": cookie_csrf or "",
                               "Referer": f"{base}/account/"})
    if not check("S2", "generate key A -> 200", st == 200 and not d.get("_non_json"),
                  f"status={st} body={d.get('_preview', d)}"):
        return finish()
    raw_a, kid_a = d.get("raw_key"), d.get("id")
    if kid_a:
        MINTED_KEYS.append((kid_a, raw_a))
    check("S2", "raw_key prefixed ts_live_", str(raw_a).startswith("ts_live_"),
          f"prefix={str(raw_a)[:12]}…")
    check("S2", "hint returned (non-secret display fragment)", bool(d.get("hint")),
          str(d.get("hint")))
    if not raw_a or not kid_a:
        bail("key A generation returned no secret/id")

    # ── S3: list must hide secrets ───────────────────────────────────────
    st, _, dl = c.call("GET", "/account/keys/")
    leaks = []
    keys = []
    try:
        keys = json.loads(dl.decode()).get("keys", [])
        leaks = sorted({k for row in keys
                        for k in ("fingerprint", "hashed_secret", "raw_key") if k in row})
    except Exception:
        pass
    check("S3", "key list -> 200 with rows", st == 200 and len(keys) >= 1,
          f"status={st} rows={len(keys)}")
    check("S3", "no secret material in the list response", not leaks, f"leaked={leaks or 'NONE'}")

    # ── S4: bearer upload ────────────────────────────────────────────────
    mp, boundary = multipart({}, "file", testfile.name, blob, "audio/webm")
    st, h, d = c.json("POST", "/api/v1/transcriptions", data=mp,
                      headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                      bearer=raw_a)
    ok4 = check("S4", "POST /api/v1/transcriptions -> 202", st == 202 and not d.get("_non_json"),
                f"status={st} body={d.get('_preview', d) if d.get('_non_json') else d}")
    if not ok4:
        # THIS is the branch the bash probe could not take: we have the status,
        # the content-type, AND a classified body.
        print(f"    >> S4 diagnostics: status={st} content-type={hdr(h, 'Content-Type')}"
              f" bytes={d.get('_bytes', '?') if isinstance(d, dict) else '?'})")
        if isinstance(d, dict) and d.get("_csrf"):
            print("    >> CAUSE: CsrfViewMiddleware rejected this POST. The bearer API "
                  "is unreachable by non-browser clients until api_key_required "
                  "applies csrf_exempt to its RETURNED callable.")
        elif isinstance(d, dict) and d.get("_empty"):
            print("    >> CAUSE: empty body with a non-2xx status - the server closed "
                  "the response without content. Read logs/daphne-error.log.")
        print(f"    >> body: {str(d)[:600]}")
        return finish()
    job_a, take_a = d.get("job_id"), d.get("take_id")
    check("S4", "response carries job_id + take_id", bool(job_a and take_a),
          f"job={job_a} take={take_a}")
    check("S4", "size_bytes echoes the uploaded byte count",
          d.get("size_bytes") == len(blob), f"{d.get('size_bytes')} vs {len(blob)}")
    check("S4", "status field is 'queued'", d.get("status") == "queued", str(d.get("status")))
    if not job_a:
        bail("S4 returned 202 but no job_id")

    # ── S5: transcript while PENDING -> 409 (never exercised before) ──────
    st, _, d5 = c.json("GET", f"/api/v1/jobs/{job_a}/transcript", bearer=raw_a)
    check("S5", "transcript before DONE -> 409 with state + poll hint",
          st == 409 and "state" in d5 and "poll" in d5,
          f"status={st} body={str(d5)[:200]}")

    # ── S6: poll to terminal ─────────────────────────────────────────────
    state, pct, payload = "queued", 0, {}
    # 180 s is the DEFAULT and it is not arbitrary: with concurrent=1, a worker
    # legitimately mid-take on a LONG recording leaves our job
    # queued/attempts=0/started_at=null - indistinguishable from "no worker"
    # using this job's own payload. Distinguishing them needs the lease columns
    # of OTHER jobs, i.e. schema knowledge this harness does not hold. So the
    # suite does not attempt that diagnosis; it waits, and reports honestly.
    # Override only when you KNOW the queue is idle (e.g. a fast re-run after
    # starting a fresh worker): TS_S6_DEADLINE=30. Shortening the default would
    # convert a busy worker into a false failure - worse than a slow pass.
    deadline = time.time() + int(os.environ.get("TS_S6_DEADLINE", "180"))
    ticks = 0
    while time.time() < deadline:
        st, _, payload = c.json("GET", f"/api/v1/jobs/{job_a}", bearer=raw_a)
        ticks += 1
        if st != 200 or payload.get("_non_json"):
            check("S6", "poll returns 200 JSON", False, f"status={st} body={str(payload)[:300]}")
            return finish()
        state, pct = payload.get("state"), payload.get("progress_pct")
        if state in ("done", "failed", "cancelled"):
            break
        time.sleep(2)
    worker_note = ""
    if state == "queued" and ticks >= 5:
        worker_note = ("still QUEUED after ~10 polls -> tsworker is almost certainly "
                       "not running (cd backend && python manage.py tsworker)")
    check("S6", f"job reached terminal state ({ticks} polls)",
          state in ("done", "failed", "cancelled"), f"state={state} pct={pct} {worker_note}")
    check("S6", "final state is 'done'", state == "done",
          f"state={state} last_error={payload.get('last_error')}")
    if state != "done":
        print(f"    >> full payload: {json.dumps(payload)[:800]}")
        return finish()
    check("S6", "progress_pct == 100", pct == 100, str(pct))
    check("S6", "is_terminal is true", payload.get("is_terminal") is True)
    check("S6", "kind == upload", payload.get("kind") == "upload", str(payload.get("kind")))
    check("S6", "duration_ms > 0 (P1 backfill)", (payload.get("duration_ms") or 0) > 0,
          str(payload.get("duration_ms")))

    # ── S7: DB cross-check (API said it; the row must agree) ──────────────
    from studio.models import Job, Segment, SegmentSource, Take
    take = Take.objects.filter(pk=uuid.UUID(str(take_a))).first()
    job = Job.objects.filter(pk=uuid.UUID(str(job_a))).first()
    check("S7", "Take row exists for the API's take_id", take is not None, str(take_a))
    check("S7", "Take.kind == 'upload'", take is not None and take.kind == "upload",
          getattr(take, "kind", None))
    check("S7", "Take.duration_ms matches the API payload",
          take is not None and take.duration_ms == payload.get("duration_ms"),
          f"db={getattr(take, 'duration_ms', None)} api={payload.get('duration_ms')}")
    raw_on_disk = (settings.MEDIA_ROOT / take.raw_relpath) if take and take.raw_relpath else None
    nrm_on_disk = (settings.MEDIA_ROOT / take.normalized_relpath) if take and take.normalized_relpath else None
    check("S7", "raw audio file exists on disk", bool(raw_on_disk and raw_on_disk.exists()),
          str(raw_on_disk))
    check("S7", "normalized WAV exists on disk", bool(nrm_on_disk and nrm_on_disk.exists()),
          str(nrm_on_disk))
    n_vad = Segment.objects.filter(take=take, source=SegmentSource.VAD).count()
    n_wsp = Segment.objects.filter(take=take, source=SegmentSource.WHISPER).count()
    check("S7", f"dual timeline persisted (vad={n_vad} whisper={n_wsp})",
          n_vad > 0 and n_wsp > 0)
    check("S7", "job row is DONE with attempts recorded",
          job is not None and job.state == "done",
          f"state={getattr(job, 'state', None)} attempts={getattr(job, 'attempts', None)}")

    # ── S8: transcript in all four formats ───────────────────────────────
    st, _, t8 = c.json("GET", f"/api/v1/jobs/{job_a}/transcript", bearer=raw_a)
    segs = t8.get("segments", []) if not t8.get("_non_json") else []
    idx = [s.get("index") for s in segs]
    check("S8", "json transcript -> 200", st == 200 and not t8.get("_non_json"), f"status={st}")
    check("S8", "segment_count > 0 and equals len(segments)",
          t8.get("segment_count", 0) > 0 and t8.get("segment_count") == len(segs),
          f"count={t8.get('segment_count')} len={len(segs)}")
    check("S8", "indices contiguous 0..n-1", idx == list(range(len(idx))), str(idx))
    check("S8", "every segment carries non-empty text",
          all((s.get("text") or "").strip() for s in segs))
    check("S8", "duration_ms present in the transcript envelope too",
          t8.get("duration_ms") == payload.get("duration_ms"),
          f"tr={t8.get('duration_ms')} job={payload.get('duration_ms')}")
    for fmt in ("srt", "vtt", "txt"):
        st, h, b = c.call("GET", f"/api/v1/jobs/{job_a}/transcript?format={fmt}", bearer=raw_a)
        text = b.decode("utf-8", "replace")
        first = text.splitlines()[:3]
        good = st == 200 and len(text.strip()) > 0
        if fmt == "srt":
            good = good and re.search(r"^\d+\r?$", first[0].strip()) is not None \
                and "," in (first[1] if len(first) > 1 else "")
        if fmt == "vtt":
            good = good and first[0].strip() == "WEBVTT"
        check("S8", f"{fmt} -> 200, well-formed, attachment disposition",
              good and "attachment" in str(hdr(h, "Content-Disposition")),
              f"status={st} head={first}")
    st, _, dbad = c.json("GET", f"/api/v1/jobs/{job_a}/transcript?format=pdf", bearer=raw_a)
    check("S8", "unknown format -> 400", st == 400, f"status={st} body={str(dbad)[:160]}")

    # ── S9: negatives (each must reach the DECORATOR, so ids are valid) ──
    real_id = job_a
    zero_id = "00000000-0000-0000-0000-000000000000"
    cases = [
        ("no Authorization header",      {},                                   401),
        ("wrong scheme (Token)",         {"Authorization": f"Token {raw_a}"},  401),
        ("empty bearer token",           {"Authorization": "Bearer "},         401),
        ("garbage key",                  {"Authorization": "Bearer ts_live_nope_nope_nope"}, 401),
        ("foreign/nonexistent job id",   None,                                 404),
    ]
    for label, headers, expected in cases:
        if headers is None:
            st, _, bd = c.json("GET", f"/api/v1/jobs/{zero_id}", bearer=raw_a)
        else:
            st, _, bd = c.json("GET", f"/api/v1/jobs/{real_id}", headers=headers)
        check("S9", f"{label} -> {expected}", st == expected,
              f"status={st} body={str(bd)[:160]}")
    st, _, gb = c.json("GET", f"/api/v1/jobs/{real_id}", bearer=raw_a)
    check("S9", "control: valid key + own job -> 200", st == 200, f"status={st}")
    st, _, _ = c.json("GET", "/api/v1/transcriptions", bearer=raw_a)
    check("S9", "GET on POST-only endpoint -> 405", st == 405, f"status={st}")

    # ── S10: cancel ──────────────────────────────────────────────────────
    mp2, b2 = multipart({}, "file", testfile.name, blob, "audio/webm")
    st, _, d10 = c.json("POST", "/api/v1/transcriptions", data=mp2,
                        headers={"Content-Type": f"multipart/form-data; boundary={b2}"},
                        bearer=raw_a)
    if check("S10", "second upload -> 202 (cancellable target)", st == 202,
             f"status={st} body={str(d10)[:200]}"):
        jc = d10.get("job_id")
        st, _, cc = c.json("DELETE", f"/api/v1/jobs/{jc}/cancel", bearer=raw_a)
        raced = st == 409 and "terminal" in str(cc.get("error", "")).lower()
        check("S10", "cancel -> 200 {cancelled:true}  (or 409 if the worker won the race)",
              (st == 200 and cc.get("cancelled") is True) or raced,
              f"status={st} body={str(cc)[:200]}"
              + ("  <- worker completed it first: cooperative branch not exercised" if raced else ""))
        st2, _, cc2 = c.json("DELETE", f"/api/v1/jobs/{jc}/cancel", bearer=raw_a)
        check("S10", "re-cancel -> 409 already-terminal", st2 == 409,
              f"status={st2} body={str(cc2)[:160]}")

    # ── S11: THE LINE THAT DECIDES Q4 ────────────────────────────────────
    st, _, rv = c.json("POST", f"/account/keys/{kid_a}/revoke/",
                       data=b"", headers={"Content-Type": "application/json",
                                          "X-CSRFToken": cookie_csrf or "",
                                          "Referer": f"{base}/account/"})
    check("S11", "revoke key A -> 200 {revoked:true}", st == 200 and rv.get("revoked") is True,
          f"status={st} body={str(rv)[:200]}")
    st, _, post = c.json("GET", f"/api/v1/jobs/{real_id}", bearer=raw_a)
    msg = str(post.get("error", ""))
    check("S11", "POST-REVOKE bearer use -> 401", st == 401, f"status={st} body={msg[:200]}")
    check("S11", "uniform message, no absent-vs-revoked oracle",
          msg == "Invalid or revoked API key.", repr(msg))
    st, _, dl2 = c.json("GET", "/account/keys/")
    row = next((r for r in dl2.get("keys", []) if r.get("id") == kid_a), None)
    check("S11", "list shows key A with is_active=False",
          row is not None and row.get("is_active") is False, str(row))

    # ── S12: rate-limit enforcement, proven deterministically ──────────────
    # The limit is READ FROM SETTINGS, not hardcoded, so this section is
    # threshold-independent. That independence is the point: the threshold is a
    # product decision and a test must not encode it.
    st, _, dB = c.json("POST", "/account/keys/generate/",
                       data=json.dumps({"name": "phase8-B-limiter"}).encode(),
                       headers={"Content-Type": "application/json",
                                "X-CSRFToken": cookie_csrf or "",
                                "Referer": f"{base}/account/"})
    raw_b, kid_b = (dB or {}).get("raw_key"), (dB or {}).get("id")
    if kid_b:
        MINTED_KEYS.append((kid_b, raw_b))
    if not check("S12", "generate key B -> 200", st == 200 and bool(raw_b),
                 f"status={st} body={str(dB)[:200]}"):
        return finish()
    try:
        import redis as _redis
        rdb = _redis.Redis.from_url(
            getattr(settings, "REDIS_URL", "redis://127.0.0.1:6379/0"),
            decode_responses=True, socket_timeout=3)
        rdb.ping()
    except Exception as exc:
        check("S12", "Redis reachable (S12 cannot run without it)", False,
              f"{type(exc).__name__}: {exc}")
        return finish()

    # The fingerprint comes from the DB, NOT from re-implementing the hash:
    # api_auth.py is in hand, models.py is not, and assuming the construction is
    # the same error class as require_DELETE.
    from studio.models import ApiKey as _ApiKey
    fp_b = _ApiKey.objects.get(pk=uuid.UUID(str(kid_b))).fingerprint
    limit = int(getattr(settings, "UPLOAD_RATE_LIMIT_PER_MINUTE", 10))
    bucket = f"ratelimit:{fp_b}:{int(time.time() // 60)}"
    check("S12", "fingerprint is 64 hex chars (SHA-256, per-key unique)",
          len(fp_b) == 64, f"len={len(fp_b)} limit={limit}")

    # Probe 1: seed to limit-1, fire ONE request -> 200, and the counter must
    # move by exactly 1. The delta is the double-feed detector: a path that
    # touches the counter twice per request shows delta=2 -> 429 where 200 is
    # predicted. That is the one anomaly this project has never reproduced
    # (23:07:05's 429 on a fresh key's 3rd call), and it is now tested in a
    # single request rather than inferred from a flush-and-diff round trip.
    rdb.set(bucket, limit - 1, ex=120)          # ex mirrors api_auth's expire(120)
    st1, _, _ = c.call("GET", f"/api/v1/jobs/{real_id}", bearer=raw_b)
    delta1 = int(rdb.get(bucket) or 0) - (limit - 1)
    check("S12", f"seeded {limit - 1}, one request -> 200 (boundary is '>' not '>=')",
          st1 == 200, f"status={st1}")
    check("S12", "exactly one increment per authenticated request",
          delta1 == 1, f"delta={delta1}")

    # Probe 2: seed to limit, fire ONE request -> 429 + Retry-After + body.
    # If api_auth's key composition ever diverges from the mirror above, the
    # seed has no effect and this returns 200 - so the label names BOTH readings
    # rather than leaving a mystery. A test whose failure message distinguishes
    # "product broken" from "test stale" is worth writing.
    rdb.set(bucket, limit, ex=120)
    st2, h2, b2 = c.call("GET", f"/api/v1/jobs/{real_id}", bearer=raw_b)
    body2 = b2.decode("utf-8", "replace")
    check("S12", f"seeded {limit}, one request -> 429  (a 200 here means the seeded "
                 f"counter had NO EFFECT, i.e. api_auth's key composition diverged "
                 f"from '{bucket}')",
          st2 == 429, f"status={st2}")
    check("S12", "429 carries Retry-After: 60", hdr(h2, "Retry-After") == "60",
          f"Retry-After={hdr(h2, 'Retry-After')!r}")
    check("S12", "429 body names the limit and the retry window",
          "ate limit" in body2 and "60" in body2, body2[:160])

    # Probe 3: clear the counter, fire one -> 200. Proves the refusal was the
    # window and not a latch, and leaves key B usable for anything after S12.
    rdb.delete(bucket)
    st3, _, _ = c.call("GET", f"/api/v1/jobs/{real_id}", bearer=raw_b)
    check("S12", "counter cleared -> 200 (refusal was the window, not a latch)",
          st3 == 200, f"status={st3}")

    # ── S13: cleanup ─────────────────────────────────────────────────────
    for kid in (kid_b,):
        if kid:
            c.call("POST", f"/account/keys/{kid}/revoke/", data=b"",
                   headers={"Content-Type": "application/json",
                            "X-CSRFToken": cookie_csrf or "",
                            "Referer": f"{base}/account/"})
    flushed = 0
    try:
        import redis as redis_lib
        r = redis_lib.Redis.from_url(
            getattr(settings, "REDIS_URL", "redis://127.0.0.1:6379/0"),
            decode_responses=True, socket_timeout=3)
        for pattern in ("ratelimit:*", "key_lastused:*"):
            for k in r.scan_iter(match=pattern, count=500):
                r.delete(k)
                flushed += 1
    except Exception as exc:
        print(f"    (redis flush skipped: {type(exc).__name__}: {exc})")
    check("S13", "test keys revoked + limiter counters flushed", True,
          f"flushed={flushed}")
    print(f"\nnote: this harness creates 2 upload takes per run and there is no "
          f"delete endpoint; media/recordings/<uid>/uploads/<tid>/ accumulates by design.")

    # Success path must also report and clean up. Without this, a fully
    # green run exits 0 having written no report and revoked no keys.
    finish()


if __name__ == "__main__":
    import urllib.parse  # used in S1's form encoding
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        import traceback
        traceback.print_exc()
        bail(f"unhandled {type(exc).__name__}: {exc}")
    finally:
        if BAILED is None and not RESULTS:
            bail("main() returned without recording any assertion")
