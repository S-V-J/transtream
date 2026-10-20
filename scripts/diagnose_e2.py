"""
Phase 6b E2 diagnostic v2 — rewritten for the unified pipeline (def9e34+).

WHY v2 EXISTS:
  v1 imported studio.pipeline._run_ffmpeg_sync, a PRIVATE helper that the
  6b-fix deleted when stage-1 normalization was unified through
  audio_service.normalize_audio via the asyncio.run bridge. Re-running v1
  after the rewrite raised ImportError. Lesson encoded here: diagnostics
  must target the architecture that exists; when an implementation is
  replaced, its tools are updated in the same change.

ARCHITECTURE AFTER 6b-fix — THERE IS ONE FRONT DOOR:
  The worker delegates stage 1 to the SAME async normalizer the WebSocket
  consumer uses. Consequently:
    * old T1 (worker-ffmpeg determinism) and old T3 (worker-vs-browser
      parity) COLLAPSE into T1': run the shared bridge TWICE on one webm;
      hashes must be equal. Equality now proves both ffmpeg determinism
      and the impossibility of front-door drift (nothing left to drift).
    * old T4 (browser-WAV transcription) is deleted: same bytes as T1'.
    * T2 (unpinned ML run-variance) is unchanged: the standing H3 monitor.
    * T2p (NEW): T2 re-run inside a SUBPROCESS with OMP_NUM_THREADS=1 set
      before the interpreter starts. In-process pinning is impossible once
      ctranslate2 is imported (it reads the env at library init), so the
      subprocess is the only repeatable proof of the WHISPER_DETERMINISTIC
      knob shipped in tsworker.py.

VERDICT POLICY (matches the system contract, do not misread):
  The product promises STRUCTURAL convergence on re-run (E-v2) and PIN
  EFFECTIVENESS (T2p). It does NOT promise unpinned bit-reproducibility.
  Therefore the exit code gates on:
      H1_bridge_determinism  must be green   (RED => ffmpeg/IO anomaly)
      H3p_pin_effective      must be green   (NOT True => knob broken/inconclusive)
  and treats:
      H3_ml_run_variance     REPORTED, non-fatal (RED = documented CPU variance;
                                                  structural E-v2 covers it)
  A shift between T2's count and T2p's count is INFORMATIONAL and EXPECTED:
  thread count changes float reduction order, which can flip a segment
  boundary. That shift IS the README "Known behavior" mechanism, observed.

Safety: inputs read-only; all outputs under /tmp/ts_diag; never touches
media/recordings/, never creates Take/Job rows, never opens a socket.

Run:   cd /home/ML/transtream && python scripts/diagnose_e2.py
Exit:  0 green / 1 a gated red or inconclusive pin / 2 prereq missing.
Cost:  ~30-45 s (two cold model loads: parent T2 + child T2p).
"""
import hashlib
import os
import subprocess
import sys
from pathlib import Path

# --- self-locating Django bootstrap (works from any cwd) -------------------
REPO = Path(__file__).resolve().parent.parent
BACKEND = REPO / "backend"
sys.path.insert(0, str(BACKEND))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "transtream.settings")

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402
from studio.pipeline import (  # noqa: E402
    PipelineError,
    _normalize_via_audio_service,
)
from studio.transcription_service import transcribe_audio  # noqa: E402

# Child script for the pinned re-run. Built with .replace("{BACKEND}", ...)
# and %-formatting internally: NO f-strings, NO brace tokens other than the
# single {BACKEND} placeholder. This is deliberate — nested heredoc/quoting
# accidents have bitten this project twice; a plain template cannot.
CHILD_SRC = '''"""Pinned re-run child, spawned by diagnose_e2 v2. Fresh interpreter
so OMP_NUM_THREADS is honored before ctranslate2 initializes."""
import os
import sys

os.environ["OMP_NUM_THREADS"] = "1"
os.environ.setdefault("CT2_VERBOSE", "0")
sys.path.insert(0, "{BACKEND}")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "transtream.settings")

import django

django.setup()

from pathlib import Path

from studio.transcription_service import transcribe_audio

wav = Path(sys.argv[1])
a = transcribe_audio(wav, beam_size=5)
b = transcribe_audio(wav, beam_size=5)
ta = " | ".join(s["text"] for s in a["segments"])
tb = " | ".join(s["text"] for s in b["segments"])
same = (len(a["segments"]) == len(b["segments"])) and (ta == tb)
print("PINNED_RESULT %d %d %s" % (len(a["segments"]), len(b["segments"]), same))
'''


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main() -> int:
    rec = Path(settings.RECORDINGS_DIR)
    webms = sorted(
        rec.glob("TS_REC_RAW_*.webm"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not webms:
        print("PREREQ MISSING: no TS_REC_RAW_*.webm - record one browser take first.")
        return 2

    W = webms[0]
    print(f"input webm : {W.name} ({W.stat().st_size} bytes) sha256={sha256(W)[:16]}...")

    tmp = Path("/tmp/ts_diag")
    tmp.mkdir(exist_ok=True)
    p1, p2 = tmp / "bridge_1.wav", tmp / "bridge_2.wav"
    for p in (p1, p2):
        p.unlink(missing_ok=True)

    flags: dict[str, bool | None] = {}

    # -- T1': shared-bridge determinism (covers old T1 + old T3) -------------
    print("\n[T1'] unified bridge, same webm, twice -> hashes must match")
    try:
        _normalize_via_audio_service(W, p1)
        _normalize_via_audio_service(W, p2)
        h1, h2 = sha256(p1), sha256(p2)
        flags["H1_bridge_determinism"] = (h1 != h2)  # True == variance == RED
        print(f"     run1={h1[:16]}... ({p1.stat().st_size} B)")
        print(f"     run2={h2[:16]}... ({p2.stat().st_size} B)")
        print(f"     identical={h1 == h2}"
              f"   (single front door: equality proves determinism AND no drift)")
    except PipelineError as exc:
        print(f"     ERROR (bridge): {exc}")
        return 1
    except Exception as exc:
        print(f"     ERROR (unexpected): {exc}")
        return 1

    # -- T2: unpinned ML run-variance (standing H3 monitor, non-fatal) -------
    print("\n[T2] transcribe_audio on the bridge WAV, twice (cold+warm model)")
    ra = transcribe_audio(p1, beam_size=5)
    rb = transcribe_audio(p1, beam_size=5)
    ta = " | ".join(s["text"] for s in ra["segments"])
    tb = " | ".join(s["text"] for s in rb["segments"])
    unpinned_same = (len(ra["segments"]) == len(rb["segments"])) and (ta == tb)
    flags["H3_ml_run_variance"] = (not unpinned_same)
    print(f"     run1: {len(ra['segments'])} seg  lang={ra['language']} "
          f"({ra['language_probability']:.2f})")
    print(f"     run2: {len(rb['segments'])} seg  lang={rb['language']} "
          f"({rb['language_probability']:.2f})")
    print(f"     identical={unpinned_same}   (RED here = documented variance, "
          f"non-fatal by policy)")

    # -- T2p: pinned subprocess proof (GATED: the knob must work) ------------
    print("\n[T2p] pinned child subprocess (OMP_NUM_THREADS=1 before import)")
    child = tmp / "child_pinned.py"
    child.write_text(CHILD_SRC.replace("{BACKEND}", str(BACKEND)))
    child_env = {**os.environ, "OMP_NUM_THREADS": "1"}
    try:
        proc = subprocess.run(
            [sys.executable, str(child), str(p1)],
            capture_output=True, text=True, timeout=600, env=child_env,
        )
    except subprocess.TimeoutExpired:
        flags["H3p_pin_effective"] = None
        print("     INCONCLUSIVE: child timed out after 600 s (resource governor?)")
        print("     Fallback evidence: the 2026-10-20 21:13 in-process pin proof "
              "(identical=True) stands on record.")
        proc = None

    pinned_counts: tuple[int, int] | None = None
    pinned_same: bool | None = None
    if proc is not None:
        if proc.returncode != 0:
            flags["H3p_pin_effective"] = None
            tail = (proc.stderr or "").strip().splitlines()[-8:]
            print("     INCONCLUSIVE: child exited "
                  f"{proc.returncode}; stderr tail:")
            for line in tail:
                print(f"       {line}")
        else:
            for line in reversed(proc.stdout.splitlines()):
                if line.startswith("PINNED_RESULT"):
                    _, c1, c2, same_s = line.split()
                    pinned_counts = (int(c1), int(c2))
                    pinned_same = (same_s == "True")
                    break
            if pinned_same is None:
                flags["H3p_pin_effective"] = None
                print("     INCONCLUSIVE: no PINNED_RESULT line in child stdout")
            else:
                flags["H3p_pin_effective"] = (not pinned_same)  # True == broken
                print(f"     pinned run1={pinned_counts[0]} seg  "
                      f"run2={pinned_counts[1]} seg  identical={pinned_same}")
                shift = (pinned_counts[0] != len(ra["segments"])
                         or pinned_counts[1] != len(rb["segments"]))
                if shift:
                    print(f"     count shift vs unpinned "
                          f"({len(ra['segments'])} -> {pinned_counts[0]}): "
                          f"EXPECTED and INFORMATIVE - thread count changes float "
                          f"reduction order, flipping boundaries. This is the "
                          f"README mechanism, observed live.")

    # -- verdict: independent flags, gated policy ----------------------------
    print("\n" + "=" * 66)
    print("VERDICT (gated: H1 + H3p; H3 reported, non-fatal):")
    h1_red = flags.get("H1_bridge_determinism") is True
    h3_red = flags.get("H3_ml_run_variance") is True
    h3p = flags.get("H3p_pin_effective")
    print(f"  H1  bridge determinism      : {'RED' if h1_red else 'green'}")
    print(f"  H3  unpinned ML variance    : {'RED (documented, non-fatal)' if h3_red else 'green'}")
    print(f"  H3p pin effective           : "
          f"{'green' if h3p is False else ('RED (broken)' if h3p is True else 'INCONCLUSIVE')}")
    print()
    if h1_red:
        print("  -> H1 RED: the shared bridge is non-deterministic on this input.")
        print("     Escalate: ffmpeg version, disk/IO health, audio_service args.")
    if h3p is True:
        print("  -> H3p RED: WHISPER_DETERMINISTIC does NOT collapse variance.")
        print("     The knob is a lie; remove the README sentence + tsworker pin")
        print("     or debug env propagation (OMP must precede ctranslate2 init).")
    if h3p is None:
        print("  -> H3p INCONCLUSIVE: rerun when memory is calm, or accept the")
        print("     21:13 in-process proof as the recorded evidence.")
    if not h1_red and h3p is False:
        print("  -> ALL GATED CHECKS GREEN. Diagnostic v2 matches the architecture.")

    gated_fail = h1_red or (h3p is not False)
    return 1 if gated_fail else 0


if __name__ == "__main__":
    sys.exit(main())
