#!/usr/bin/env python3
"""
TranStream rate-limiter diagnostic.

WHY THIS EXISTS. The 23:07:05 Phase 8 harness run returned 429 on what should
have been its THIRD authenticated request, against a documented limit of
10/minute. Three and ten cannot both be true. Every explanation available from
the evidence so far is a guess, and this project has been burned five times
this evening by confident extrapolation (require_DELETE, G3's segment_count,
the dash-arithmetic, the empty-vs-HTML JSONDecodeError conflation, and the
GET-only probes that hid a CSRF defect for a whole phase). So this script
measures instead of reasoning.

IT SEPARATES TWO QUANTITIES THAT WERE BEING CONFLATED:
  P1  the limit the DECORATOR actually computes at runtime, and whether the
      setting exists at all. NOTE: the phase.7+8 commit message asserts that
      settings.py reads neither UPLOAD_* knob. That claim was derived from an
      ELIDED FRAGMENT of settings.py (the paste contains literal "..." where
      these lines would sit), i.e. a negative inferred from incomplete
      evidence. It is re-verified here rather than inherited.
  P2  the increment contributed by ONE authenticated call, measured twice:
        P2a  _check_rate_limit() called DIRECTLY in-process - no HTTP, no
             decorator, no lookup_by_raw, no verify, no view. If this crosses
             at i=3, the limit is 2 and the cause is configuration.
        P2b  ONE request through the REAL middleware stack, with the counter
             flushed first so the delta is attributable to that one request.
             If P2a crosses at 11 but P2b moves the counter by 2, the
             decorator is double-counting.
  P3  the DECORATION CHAIN, walked via __wrapped__ and identified by
      __code__.co_filename. Double-decoration is the mechanism behind P2b==2
      and is visible structurally: the api_auth wrapper appears twice.

      CORRECTION, recorded because the lesson is in the method: this probe's
      first version identified layers by __module__/__qualname__ and printed
      four identical names, then declared "0 api_auth wrappers: DOUBLE
      DECORATION". That was a FALSE ALARM caused by the probe, not the code.
      functools.wraps COPIES __module__/__name__/__qualname__/__doc__ from the
      wrapped function onto the wrapper - that is its entire purpose - so every
      layer reports the real view's identity and the wrappers are
      indistinguishable by label. __code__ is NOT copied, so co_filename is the
      file a layer was actually defined in: the real property, not a forwarded
      label. Sixth proxy-property failure in this project (after require_DELETE,
      G3's segment_count, the dash-arithmetic, the empty-vs-HTML conflation, the
      GET-only probes, and the elided-settings claim), caught only because P2b
      measured the same question independently and DISAGREED.
      RULE: when a structural inference and a direct measurement conflict, the
      measurement wins by default and the inference is the suspect.

      SECOND CORRECTION, same file, ten minutes later: the patch that was meant
      to install this fix carried a SyntaxError in its own post-condition list.
      Python parses all of stdin before executing any statement, so the anchor
      counts, the replaces and the write NEVER ran - and py_compile on the
      target file reported green, because the target had not been touched. The
      correct chain output that followed came from a separate inline probe, not
      from this file. A crash in the patch script is not a compile failure in
      the target. Hence: verify a patch by READING THE FILE BACK (grep for the
      new content, assert the old content is gone), never by compiling it.

Cleanup is unconditional (try/finally): the minted key is revoked and every
counter it touched is deleted, on every exit path - the lesson of the 22:43
run, which left an ACTIVE key whose secret had already been printed.
"""

import os
import pathlib
import sys
import time
import urllib.error
import urllib.request
import uuid

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "transtream.settings")
import django  # noqa: E402
django.setup()

from django.conf import settings  # noqa: E402
from django.contrib.auth import get_user_model  # noqa: E402
from django.urls import resolve  # noqa: E402

from studio.api_auth import _check_rate_limit, _get_redis  # noqa: E402
from studio.models import ApiKey  # noqa: E402

ZERO = "00000000-0000-0000-0000-000000000000"
REPORT = pathlib.Path("/tmp/ts_ratelimit.txt")
LINES = []


def say(s=""):
    print(s)
    LINES.append(str(s))


def counter_key(fp):
    """Reproduces api_auth's key composition EXACTLY, so a divergence here is
    itself a finding (hypothesis 3: the counter is not really per-key)."""
    return f"ratelimit:{fp}:{int(time.time() // 60)}"


def read_counter(r, fp):
    k = counter_key(fp)
    raw = r.get(k)
    ttl = r.ttl(k)
    return (int(raw) if raw is not None else 0), ttl, k


def main():
    # ── P1: what the decorator computes, and whether the name exists ──────
    say("═══ P1  CONFIG TRUTH (as api_auth computes it) ═══")
    for name in ("UPLOAD_RATE_LIMIT_PER_MINUTE", "UPLOAD_MAX_BYTES"):
        present = hasattr(settings, name)
        val = getattr(settings, name, None)
        env = os.environ.get(name)
        say(f"  settings.{name:32s} exists={present!s:5s} "
            f"value={val!r} type={type(val).__name__}")
        say(f"  {'':35s} os.environ={env!r}")
    # The exact expression from api_auth.py line 64. Computed here rather than
    # quoted, so the two cannot drift without this script noticing.
    limit = int(getattr(settings, "UPLOAD_RATE_LIMIT_PER_MINUTE", 10))
    say(f"  -> api_auth's resolved limit = {limit}")
    say(f"  -> 429 would first fire when the counter exceeds {limit}"
        + ("  (MATCHES the observed 3rd call)" if limit < 3 else
           "  (DOES NOT match the observed 3rd call - so the limit is not the "
           "whole story, or the counter is not per-key)"))

    r = _get_redis()
    if r is None:
        say("  !! Redis unavailable via _get_redis(); P2/P3 need it. Bailing.")
        return 2
    say(f"  redis ping: {r.ping()}")

    # ── mint a throwaway key ──────────────────────────────────────────────
    User = get_user_model()
    admin = User.objects.filter(username="admin").first()
    if admin is None:
        say("  !! no 'admin' user; cannot mint a key")
        return 2
    api_key, raw_key = ApiKey.create_for_user(admin, "ratelimit-diagnostic")
    fp = api_key.fingerprint
    say(f"\n  minted {api_key.id.hex[:8]}… fingerprint={fp[:16]}… len={len(fp)}")
    say(f"  fingerprint is {'64-hex (SHA-256, per-key unique)' if len(fp) == 64 else '*** UNEXPECTED LENGTH ***'}")

    try:
        # ── P2a: direct in-process loop, no HTTP at all ───────────────────
        say("\n═══ P2a  DIRECT _check_rate_limit() LOOP (no HTTP/decorator/view) ═══")
        r.delete(counter_key(fp))
        crossing = None
        say(f"  {'i':>3} {'allowed':8s} {'counter':8s} {'ttl':5s}")
        for i in range(1, 15):
            allowed = _check_rate_limit(fp)
            n, ttl, _ = read_counter(r, fp)
            say(f"  {i:>3} {str(allowed):8s} {n:<8d} {ttl:<5d}")
            if not allowed and crossing is None:
                crossing = i
        say(f"  -> first refusal at i={crossing}")
        if crossing is not None:
            say(f"  -> implies limit={crossing - 1}"
                + ("  == resolved limit above: CONFIGURATION is the cause"
                   if crossing - 1 == limit else
                   "  != resolved limit above: the counter is being fed by "
                   "something other than this call"))

        # ── P2b: ONE request through the real stack, counter flushed first ─
        say("\n═══ P2b  ONE HTTP REQUEST THROUGH THE REAL MIDDLEWARE STACK ═══")
        r.delete(counter_key(fp))
        before, _, key = read_counter(r, fp)
        port = (ROOT / "logs" / "transtream.port").read_text().strip()
        url = f"http://127.0.0.1:{port}/api/v1/jobs/{ZERO}"
        req = urllib.request.Request(url, method="GET",
                                     headers={"Authorization": f"Bearer {raw_key}"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                status = resp.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        except Exception as exc:
            status = f"TRANSPORT {type(exc).__name__}"
        after, ttl, _ = read_counter(r, fp)
        delta = after - before
        say(f"  GET {url}")
        say(f"  -> status={status} (404 expected: foreign job id, no side effects)")
        say(f"  -> counter {before} -> {after}   delta={delta}  ttl={ttl}")
        if delta == 1:
            say("  PASS - exactly one increment per authenticated request. "
                "The decorator does NOT double-count.")
        else:
            say(f"  *** FINDING - {delta} increments for ONE request. "
                "Double-counting is real; see P3 for the mechanism.")

        # ── P3: decoration chain, walked structurally ─────────────────────
        say("\n═══ P3  DECORATION CHAIN (walk __wrapped__; no text search) ═══")
        for path in ("/api/v1/transcriptions", f"/api/v1/jobs/{ZERO}/cancel"):
            cb = resolve(path).func
            chain, depth = [], 0
            node = cb
            while node is not None and depth < 12:
                # co_filename, NOT __module__: functools.wraps forwards the
                # module/qualname of the wrapped function onto every wrapper,
                # so labels cannot distinguish layers. See the module docstring.
                code = getattr(node, "__code__", None)
                fname = pathlib.Path(code.co_filename).name if code is not None else "?"
                chain.append(f"{fname} ({getattr(node, '__qualname__', '?')})")
                node = getattr(node, "__wrapped__", None)
                depth += 1
            auth_wrappers = sum(1 for c in chain if c.startswith("api_auth.py"))
            say(f"  {path}")
            for c in chain:
                say(f"      <- {c}")
            verdict = ("OK - single api_auth wrapper" if auth_wrappers == 1
                       else f"*** {auth_wrappers} api_auth wrappers: DOUBLE DECORATION ***")
            say(f"      csrf_exempt={getattr(cb, 'csrf_exempt', False)}  {verdict}")

        say("\n═══ ALL ratelimit:* / key_lastused:* KEYS RIGHT NOW ═══")
        for pattern in ("ratelimit:*", "key_lastused:*"):
            for k in r.scan_iter(match=pattern, count=500):
                say(f"  {k}  = {r.get(k)!r}  ttl={r.ttl(k)}")
    finally:
        # Unconditional cleanup: revoke the minted key and delete every counter
        # it touched, whatever happened above.
        api_key.revoke()
        wiped = 0
        for k in list(r.scan_iter(match=f"ratelimit:{fp}:*", count=500)) + \
                 list(r.scan_iter(match=f"key_lastused:{fp}", count=500)):
            r.delete(k)
            wiped += 1
        say(f"\ncleanup: key {api_key.id.hex[:8]}… revoked, {wiped} counter(s) deleted")
        say("(the diagnostic's own key is NOT left active - 22:43 lesson)")

    return 0


if __name__ == "__main__":
    rc = 1
    try:
        rc = main()
    except Exception as exc:
        import traceback
        traceback.print_exc()
        say(f"!! unhandled {type(exc).__name__}: {exc}")
        rc = 1
    finally:
        REPORT.write_text("\n".join(LINES) + "\n")
        print(f"\nreport -> {REPORT}")
    sys.exit(rc)
