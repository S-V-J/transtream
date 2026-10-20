"""
Phase 6b E2 root-cause diagnostic.

Partitions the "re-run yields a different Whisper segment count" finding into
three INDEPENDENT hypotheses (a failure of one does not excuse the others):

  H1  FFmpeg run-variance   : T1  worker-normalize the same webm TWICE, hash both WAVs.
                              Unequal => ffmpeg itself is non-deterministic (anomalous).
  H2  Front-door arg drift  : T3  hash worker-WAV vs browser-WAV (audio_service) for the
                              SAME webm. Unequal => pipeline.py's reconstructed FFmpeg
                              args drifted from the proven async path => the two front
                              doors transcribe different audio. (Explains the observed
                              browser=8 vs worker=9 on take 172818.)
  H3  ML run-variance       : T2  transcribe ONE identical WAV TWICE via transcribe_audio.
                              Unequal counts/text => CTranslate2-int8 / internal
                              Silero-VAD-ONNX are not bit-reproducible on this CPU
                              (OpenMP reduction order). E2's worker-vs-worker failure
                              reduces to H1 or H3; H2 cannot explain E2 but must be
                              fixed regardless because it breaks pipeline parity.

  T4  (informational) transcribe the browser-WAV once; its count vs the worker-WAV's
      count quantifies H2's practical impact on transcripts.

Inputs are read-only; all outputs go to /tmp/ts_diag. Never touches
media/recordings/, never creates Take/Job rows, never opens a socket.

Run:  cd /home/ML/transtream && python scripts/diagnose_e2.py
"""
import hashlib
import os
import sys
from pathlib import Path

# --- self-locating Django bootstrap (works from any cwd) -------------------
REPO = Path(__file__).resolve().parent.parent
BACKEND = REPO / "backend"
sys.path.insert(0, str(BACKEND))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "transtream.settings")

import asyncio  # noqa: E402  (import after path setup, before django.apps)
import django   # noqa: E402

django.setup()

from django.conf import settings             # noqa: E402
from studio.audio_service import (           # noqa: E402
    AudioNormalizationError,
    normalize_audio,
)
from studio.pipeline import PipelineError, _run_ffmpeg_sync  # noqa: E402
from studio.transcription_service import transcribe_audio    # noqa: E402


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
        print("PREREQ MISSING: no TS_REC_RAW_*.webm — record one browser take first.")
        return 2

    W = webms[0]
    print(f"input webm : {W.name} ({W.stat().st_size} bytes) sha256={sha256(W)[:16]}…")

    tmp = Path("/tmp/ts_diag")
    tmp.mkdir(exist_ok=True)
    w1, w2, wb = tmp / "worker_1.wav", tmp / "worker_2.wav", tmp / "browser.wav"
    for p in (w1, w2, wb):
        p.unlink(missing_ok=True)

    flags = {}

    # ── T1: FFmpeg run-variance (H1) ──────────────────────────────────────
    print("\n[T1] worker FFmpeg determinism (same webm, twice)")
    try:
        _run_ffmpeg_sync(W, w1)
        _run_ffmpeg_sync(W, w2)
        h1, h2 = sha256(w1), sha256(w2)
        flags["H1_ffmpeg_run_variance"] = (h1 != h2)
        print(f"     run1={h1[:16]}…  run2={h2[:16]}…  identical={h1 == h2}")
    except (PipelineError, Exception) as exc:
        flags["H1_ffmpeg_run_variance"] = None
        print(f"     ERROR: {exc}")
        return 1

    # ── T3: front-door parity (H2) ────────────────────────────────────────
    print("\n[T3] browser path (audio_service.normalize_audio) vs worker path, same webm")
    try:
        asyncio.run(normalize_audio(W, wb))
        hb = sha256(wb)
        flags["H2_front_door_arg_drift"] = (hb != h1)
        print(f"     browser={hb[:16]}… ({wb.stat().st_size} B)")
        print(f"     worker ={h1[:16]}… ({w1.stat().st_size} B)")
        print(f"     identical={hb == h1}   <-- False means the two front doors"
              f" transcribe DIFFERENT audio")
    except (AudioNormalizationError, Exception) as exc:
        flags["H2_front_door_arg_drift"] = None
        print(f"     ERROR: {exc}")
        return 1

    # ── T2: ML run-variance (H3) ──────────────────────────────────────────
    print("\n[T2] transcribe_audio on the SAME worker WAV, twice (cold+warm model)")
    ra = transcribe_audio(w1, beam_size=5)
    rb = transcribe_audio(w1, beam_size=5)
    ta = " | ".join(s["text"] for s in ra["segments"])
    tb = " | ".join(s["text"] for s in rb["segments"])
    same = (len(ra["segments"]) == len(rb["segments"])) and (ta == tb)
    flags["H3_ml_run_variance"] = (not same)
    print(f"     run1: {len(ra['segments'])} seg  lang={ra['language']} "
          f"({ra['language_probability']:.2f})")
    print(f"     run2: {len(rb['segments'])} seg  lang={rb['language']} "
          f"({rb['language_probability']:.2f})")
    print(f"     identical={same}")
    if not same:
        print(f"     text1: {ta[:120]}")
        print(f"     text2: {tb[:120]}")

    # ── T4: H2 practical impact ───────────────────────────────────────────
    print("\n[T4] whisper on the BROWSER WAV (parity impact, informational)")
    rc = transcribe_audio(wb, beam_size=5)
    print(f"     browser-WAV -> {len(rc['segments'])} seg "
          f"(worker-WAV was {len(ra['segments'])})")

    # ── verdict matrix: INDEPENDENT flags, not an elif chain ──────────────
    print("\n" + "=" * 66)
    print("VERDICT (each hypothesis evaluated independently):")
    v = flags
    print(f"  H1 ffmpeg run-variance     : {'RED' if v['H1_ffmpeg_run_variance'] else 'green'}")
    print(f"  H2 front-door arg drift    : {'RED' if v['H2_front_door_arg_drift'] else 'green'}")
    print(f"  H3 ML run-variance         : {'RED' if v['H3_ml_run_variance'] else 'green'}")
    print()
    if v["H2_front_door_arg_drift"]:
        print("  -> H2 RED: apply the pipeline.py asyncio.run unification (Step 2).")
        print("     It makes browser/worker byte-identical BY CONSTRUCTION and needs")
        print("     NO edit of audio_service.py (schema lock never triggers).")
    else:
        print("  -> H2 green: args already match; the unification is still applied")
        print("     as permanent hygiene against future drift.")
    if v["H3_ml_run_variance"]:
        print("  -> H3 RED: CPU int8/ONNX re-runs are not bit-reproducible. Adopt the")
        print("     structural E-v2 assertion (Step 4) + README known-behavior bullet")
        print("     (Step 5). Optional: WHISPER_DETERMINISTIC=1, then RE-RUN T2 under")
        print("     the pin to prove it collapses to identical=True.")
    else:
        print("  -> H3 green: ML reproduced here; E2's failure was H1/H2-driven or")
        print("     environmental. Re-run the harness; if E-v2 still flakes, dig deeper.")
    if v["H1_ffmpeg_run_variance"]:
        print("  -> H1 RED (unexpected): ffmpeg itself varies. Report ffmpeg version")
        print("     and pin -y/resampler flags; escalate before any other step.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
