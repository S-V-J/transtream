"""
Layer guards (Phases 7/8). Four assertions, each precise enough that a
match means a real problem, and each with a gate that provably cannot be
silently bypassed.

WHY THIS FILE EXISTS: the ad-hoc greps used at 21:37 produced TWO false
positives — stale __pycache__ bytecode and prose inside my own docstrings
("no require_DELETE; DELETE is expressed as…"). A guard that cries wolf gets
ignored, and an ignored guard is worse than no guard. Every pattern below is
therefore anchored to real syntax.

WHY THE GATE IS A FUNCTION WITH ONE CALL SITE: an earlier revision of this
file had its exit gate as bare top-level statements,

    print()
    if fails:
        print(...)
        sys.exit(1)

and a patch that replaced the anchor text WITHOUT re-including it left

    print(f"RESULT: {len(fails)} GUARD FAILURE(S): {fails}")
    sys.exit(1)

sitting at 4-space indent directly after an `except` clause plus a blank line.
Python absorbed both into the handler body. The file compiled, printed
"G4 FAIL", populated `fails`, never reached sys.exit, and exited 0 with an
unconditional "ALL GREEN". Detection worked; the gate did not. A guard whose
failure path is unreachable is worse than no guard, because it manufactures
confidence. Hence: report() is a function, called exactly once, from column 0,
as the final statement in the file — structurally unable to be swallowed.
Run `python scripts/guard_layers.py --selftest` to prove the gate can fail.

  G1  No hallucinated HTTP decorators. Matches IMPORT statements only
      (^\\s*(from|import)…), Python files only. Docstring prose cannot trip
      it; stale bytecode is excluded by the *.py glob.
  G2  Upload/API/serializer layers must not import the audio engines
      (av / faster_whisper / silero_vad / numpy / soundfile). Engines live
      behind audio_service / vad_service / transcription_service and are
      reached only via pipeline.process_take.
  G3  Payload-shape locality, stated precisely. "is_terminal" belongs to
      exactly ONE resource representation (job_payload) and to nothing else
      in the app, so it may appear ONLY in serializers.py.

      DELIBERATELY EXCLUDED, WITH REASONS — do not re-widen this:
        * "progress_pct" also appears in the 409 "not ready" error bodies of
          both views. Error envelopes are view-specific; resource
          representations are not. Asserting otherwise would be wrong.
        * "segment_count" is ALSO emitted by consumers.py in the WebSocket
          "live_transcript_update" frame. That is a DIFFERENT protocol
          surface with a DIFFERENT meaning: "segments in this interim rolling
          preview" vs "authoritative segments in this transcript". Colliding
          key names across surfaces is normal and correct. Asserting global
          NAME ownership was my error and produced this guard's third false
          positive (the first two were stale __pycache__ and docstring prose).
          The invariant is SHAPE locality, not name ownership. Every false
          positive in this project's guards came from asserting a proxy
          property (a name, a substring) instead of the real property (a
          structure).
  G4  The bearer API must be CSRF-exempt AT THE RESOLVED CALLBACK.
      Deliberately RUNTIME INTROSPECTION, not a text search: a grep for the
      token "csrf_exempt" would match this very docstring, which is the exact
      proxy-property error behind G1-G3's three false positives. CsrfView-
      Middleware inspects the attribute on the callable the RESOLVER returns,
      so resolve() is the only faithful probe — it also catches csrf_exempt
      applied to the wrong function in the decorator chain, which no grep can
      see. WHY the exemption is correct rather than merely convenient: CSRF
      attacks exploit AMBIENT credentials (cookies a browser attaches
      automatically). An Authorization: Bearer header is not ambient, so no
      cross-site request can carry it and no attack surface exists. Without
      this, POST /api/v1/transcriptions and DELETE /api/v1/jobs/<id>/cancel
      return 403 to every non-browser client — i.e. the API is unusable by the
      platforms it was built for. Safe methods (GET/HEAD/OPTIONS/TRACE) are
      never CSRF-checked, which is why GET-only probes hid this bug for an
      entire phase.
"""

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
STUDIO = ROOT / "backend" / "studio"

GUARD_NAMES = "G1 decorators, G2 engines, G3 locality, G4 csrf"
fails = []


def report(fail_list):
    """The single exit gate. Returns the process exit code; never exits itself,
    so it is testable in-process by --selftest."""
    print()
    if fail_list:
        print(f"RESULT: {len(fail_list)} GUARD FAILURE(S): {fail_list}")
        return 1
    print(f"RESULT: ALL LAYER GUARDS GREEN ({GUARD_NAMES})")
    return 0


# ── G1 ───────────────────────────────────────────────────────────────────
BAD_DECORATOR = re.compile(
    r"^\s*(?:from\s+[\w.]+\s+import|import)\b.*\brequire_(DELETE|PUT|PATCH|OPTIONS|HEAD)\b"
)
hits = []
for f in sorted(ROOT.glob("backend/**/*.py")):
    for i, line in enumerate(f.read_text().splitlines(), 1):
        if BAD_DECORATOR.search(line):
            hits.append(f"{f.relative_to(ROOT)}:{i}: {line.strip()}")
if hits:
    fails.append("G1 hallucinated http-method decorator")
    print("G1 FAIL - import statements referencing nonexistent decorators:")
    for h in hits:
        print("   ", h)
else:
    print("G1 OK   - only real decorators (require_GET / require_POST / "
          "require_http_methods / require_safe)")

# ── G2 ───────────────────────────────────────────────────────────────────
ENGINE = re.compile(r"^\s*(?:import|from)\s+(av|faster_whisper|silero_vad|numpy|soundfile)\b")
G2_FILES = ["upload_service.py", "views.py", "api_views.py",
            "serializers.py", "api_auth.py", "api_urls.py", "urls.py"]
hits = []
for name in G2_FILES:
    f = STUDIO / name
    if not f.exists():
        continue
    for i, line in enumerate(f.read_text().splitlines(), 1):
        if ENGINE.search(line):
            hits.append(f"{name}:{i}: {line.strip()}")
if hits:
    fails.append("G2 engine import leaked into HTTP layer")
    print("G2 FAIL - audio engines imported by the HTTP/serialization layer:")
    for h in hits:
        print("   ", h)
else:
    print("G2 OK   - upload/api/serializer layer is pure HTTP + ORM")

# ── G3 ───────────────────────────────────────────────────────────────────
KEYS = ('"is_terminal"',)
hits = []
for f in sorted(STUDIO.glob("*.py")):
    if f.name == "serializers.py":
        continue
    body = f.read_text()
    for k in KEYS:
        if k in body:
            hits.append(f"{f.name} contains {k}")
if hits:
    fails.append("G3 payload shape duplicated outside serializers")
    print("G3 FAIL - success-response keys built outside serializers.py:")
    for h in hits:
        print("   ", h)
else:
    print("G3 OK   - job/transcript response shapes live only in serializers.py")

# ── G4 ───────────────────────────────────────────────────────────────────
# Self-contained: the try/except wraps ONLY the introspection, and the except
# handler holds ONLY an append + a print. Nothing shared lives inside either
# block, so no future edit can relocate the gate into this handler.
if "--selftest" not in sys.argv:
    ZERO = "00000000-0000-0000-0000-000000000000"
    G4_PATHS = [
        "/api/v1/transcriptions",              # POST   - unsafe, must be exempt
        f"/api/v1/jobs/{ZERO}",                # GET    - safe, exempt for uniformity
        f"/api/v1/jobs/{ZERO}/transcript",     # GET    - safe
        f"/api/v1/jobs/{ZERO}/cancel",         # DELETE - unsafe, must be exempt
    ]
    try:
        import os as _os
        _os.environ.setdefault("DJANGO_SETTINGS_MODULE", "transtream.settings")
        sys.path.insert(0, str(ROOT / "backend"))
        import django
        django.setup()
        from django.urls import resolve

        hits = []
        for path in G4_PATHS:
            try:
                cb = resolve(path).func
            except Exception as exc:
                hits.append(f"{path} did not resolve: {type(exc).__name__}: {exc}")
                continue
            if not getattr(cb, "csrf_exempt", False):
                hits.append(f"{path} -> {cb.__module__}.{cb.__name__} lacks csrf_exempt")
        if hits:
            fails.append("G4 bearer API is not CSRF-exempt at the resolved callback")
            print("G4 FAIL - POST/DELETE to /api/v1/* will 403 for every non-browser client:")
            for h in hits:
                print("   ", h)
        else:
            print("G4 OK   - all 4 bearer routes carry csrf_exempt on the resolved callback")
    except Exception as exc:
        fails.append(f"G4 could not run ({type(exc).__name__}: {exc})")
        print(f"G4 FAIL - introspection unavailable: {type(exc).__name__}: {exc}")

# ── self-test: prove the gate can fail, without needing a real violation ──
if "--selftest" in sys.argv:
    print("SELFTEST - exercising report() in-process (no guards run):")
    rc_fail = report(["synthetic failure for gate proof"])
    rc_ok = report([])
    ok = (rc_fail == 1 and rc_ok == 0)
    print(f"  report(['x']) -> {rc_fail} (want 1)   "
          f"report([]) -> {rc_ok} (want 0)")
    print("SELFTEST " + ("PASS - the gate can and does fail." if ok
                         else "*** FAIL - the gate is inert. DO NOT TRUST THIS FILE. ***"))
    sys.exit(0 if ok else 1)

# The one and only exit site. Column 0. Final statement in the file.
sys.exit(report(fails))
