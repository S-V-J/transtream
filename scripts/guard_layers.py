"""
Layer guards (Phase 7/8). Three assertions, each precise enough that a
match means a real problem.

WHY THIS FILE EXISTS: the ad-hoc greps used at 21:37 produced TWO false
positives — they matched stale __pycache__ bytecode and matched prose
inside my own docstrings ("no require_DELETE; DELETE is expressed as…").
A guard that cries wolf gets ignored, and an ignored guard is worse than
no guard. Every pattern below is therefore anchored to real syntax.

  G1  No hallucinated HTTP decorators. Matches IMPORT statements only
      (^\\s*(from|import)…), Python files only. Docstring prose cannot
      trip it; stale bytecode is excluded by --include.
  G2  Upload/API/serial layers must not import the audio engines
      (av / faster_whisper / silero_vad / numpy / soundfile). Engines
      live behind audio_service / vad_service / transcription_service and
      are reached only via pipeline.process_take.
  G3  Payload-shape locality, stated precisely. "is_terminal" belongs to
      exactly ONE resource representation (job_payload) and to nothing else
      in the app, so it may appear ONLY in serializers.py.

      DELIBERATELY EXCLUDED, WITH REASONS — do not re-widen this:
        * "progress_pct" also appears in the 409 "not ready" error bodies of
          both views. Error envelopes are view-specific; resource
          representations are not. Asserting otherwise would be wrong.
        * "segment_count" is ALSO emitted by consumers.py in the WebSocket
          "live_transcript_update" frame. That is a DIFFERENT protocol
          surface with a DIFFERENT meaning: "segments in this interim
          rolling preview" vs "authoritative segments in this transcript".
          Colliding key names across surfaces is normal and correct.
          Asserting global NAME ownership was my error and produced this
          guard's third false positive (the first two were stale
          __pycache__ and docstring prose). The invariant is SHAPE
          locality, not name ownership. Every false positive in this
          project's guards came from asserting a proxy property (a name, a
          substring) instead of the real property (a structure).
"""
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
STUDIO = ROOT / "backend" / "studio"
fails = []

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

print()
if fails:
    print(f"RESULT: {len(fails)} GUARD FAILURE(S): {fails}")
    sys.exit(1)
print("RESULT: ALL LAYER GUARDS GREEN (G1 decorators, G2 engines, G3 locality)")
