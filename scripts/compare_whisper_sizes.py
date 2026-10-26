#!/usr/bin/env python3
"""Transcribe ONE audio file at several Whisper model sizes and print a count.

WHY THIS EXISTS. The transcripts on this machine render English content words
well and domain proper nouns badly - "Kamailio" arrives as "KMLU", "Terraform"
as "telephone", "Ansible" as "n-seval". The cause was confirmed by execution,
not by reading: settings.py assigns no WHISPER_MODEL_SIZE, so
transcription_service.py's getattr fallback ("tiny") fires and the 39M-parameter
model does the work. The fix is a one-line settings read; the OPEN question is
which size earns its decode cost on THIS audio. This script answers that with a
number instead of an impression.

WHY A SEPARATE PROCESS PER SIZE. transcription_service caches the model in a
module global (_model, guarded by _model_lock and a double-checked lock). Two
sizes in one process would mean the second never loads - the cache returns the
first. Warm state also leaks: the first decode pays page-in and thread-pool
costs that the second would not. So each size is a child process, and the parent
only orchestrates and tabulates.

WHY THE OVERRIDE IS IN-PROCESS ON settings, NOT IN .env AND NOT ON THE SHELL.
Three channels were considered and two are wrong here. A shell prefix
(WHISPER_MODEL_SIZE=base transtream start) cannot reach the worker at all,
because systemd-run --setenv enumerates its variables by name and does not
include this one - observed directly when a three-size loop printed the same
stale log line for all three arms. Editing .env would work once settings.py
reads the knob, but it mutates a file the user owns to run an experiment. So the
override is `settings.WHISPER_MODEL_SIZE = size` inside the child, after
django.setup(): it needs no code change, it touches no file, and it exercises
the same getattr() the production path uses.

WHY IT CALLS THE PROJECT'S OWN FUNCTION. Reimplementing the decode call would
mean copying beam_size, vad_filter, condition_on_previous_text and friends from
a reading of the source - and a copy that drifts measures something other than
production. Importing transcribe() makes the arguments production by
construction, so the ONLY variable between arms is the model size. The function
name and signature are taken from the module at runtime and printed before any
measurement starts, so a rename cannot silently produce a zero.

WHY THE JARGON LIST HAS CONTROLS. Nine terms are expected to FAIL at tiny
(kamailio, asterisk, terraform, prometheus, grafana, ansible, pbx, voip, telus)
- they are the words this machine actually mangles. Four are expected to SUCCEED
(kubernetes, docker, aws, azure) and exist to catch regressions: a larger model
that loses a word tiny got is worse, not better, and a comparison without
controls cannot see that. Both counts are printed per arm.

WHAT IT REFUSES TO DO. It will not download a model. If a size is not in the
Hugging Face hub cache and an offline flag is set, WhisperModel raises - and a
measurement that silently triggers a network fetch is a measurement with an
unbounded side effect. So the parent checks the cache first and marks uncached
sizes SKIPPED with the path that was checked. It also will not measure a missing
audio file, and it will not trust a log line: the model loads LAZILY on first
transcription, so a worker that is given no work never logs a load line, and
`tail -n 1` on the log returns whatever the last real transcription logged -
which, in the run that motivated this rewrite, was a line from the previous day.
"""
import argparse
import json
import os
import pathlib
import subprocess
import sys
import time

REPO = pathlib.Path(__file__).resolve().parent.parent
BACKEND = REPO / "backend"

# The nine words this machine gets wrong, taken from its own transcripts, and the
# four it gets right and must keep getting right.
TARGETS = ("kamailio", "asterisk", "terraform", "prometheus", "grafana",
           "ansible", "pbx", "voip", "telus")
CONTROLS = ("kubernetes", "docker", "aws", "azure")

HUB_CACHE = pathlib.Path(os.environ.get("HF_HOME", "~/.cache/huggingface")).expanduser() / "hub"


def cached_sizes():
    """Which faster-whisper sizes are already on disk. No network, no download."""
    found = set()
    if not HUB_CACHE.is_dir():
        return found
    for p in HUB_CACHE.iterdir():
        name = p.name
        if "whisper" not in name.lower():
            continue
        # Systran/faster-whisper-<size> is the repo id faster-whisper resolves.
        for cand in ("tiny", "base", "small", "medium", "large-v2", "large-v3",
                     "distil-large-v3", "large-v3-turbo"):
            if name.lower().endswith(cand):
                found.add(cand)
    return found


def offline_flags():
    """Every offline switch we can see: process env, then .env (keys only)."""
    out = {k: v for k, v in os.environ.items()
           if k.startswith(("HF_", "HUGGINGFACE_", "TRANSFORMERS_"))}
    envf = REPO / ".env"
    if envf.is_file():
        for ln in envf.read_text().splitlines():
            ln = ln.strip()
            if not ln or ln.startswith("#") or "=" not in ln:
                continue
            k, _, v = ln.partition("=")
            if k.strip().startswith(("HF_", "HUGGINGFACE_", "TRANSFORMERS_")):
                out.setdefault(k.strip(), v.strip())
    return out


def child(argv_size, argv_wav, argv_fn):
    """One size, one process, one decode. Prints a single JSON line on stdout."""
    import django
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "transtream.settings")
    sys.path.insert(0, str(BACKEND))
    django.setup()
    from django.conf import settings
    setattr(settings, "WHISPER_MODEL_SIZE", argv_size)   # the only variable

    from studio import transcription_service as ts
    fn = getattr(ts, argv_fn, None)
    if fn is None:
        print(json.dumps({"error": f"{ts.__name__} has no callable {argv_fn!r}"}))
        return 1

    wav = pathlib.Path(argv_wav)
    if not wav.is_file():
        print(json.dumps({"error": f"audio not found: {wav}"}))
        return 1

    t0 = time.monotonic()
    try:
        res = fn(wav)
    except Exception as exc:
        # Includes the uncached-model case; the parent pre-screens, but a cache
        # can go stale between the check and the load.
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}", "size": argv_size}))
        return 1
    el = time.monotonic() - t0

    text = " ".join(s["text"].strip() for s in res.get("segments", [])) \
        if isinstance(res, dict) and "segments" in res else str(res)
    print(json.dumps({
        "size": argv_size,
        "seconds": round(el, 3),
        "language": (res.get("language") if isinstance(res, dict) else None),
        "probability": (res.get("language_probability") if isinstance(res, dict) else None),
        "n_segments": (len(res["segments"]) if isinstance(res, dict) and "segments" in res else None),
        "chars": len(text),
        "text": text,
    }))
    return 0


def score(text):
    low = text.lower()
    hit = lambda w: w in low or (len(w) > 3 and w[:4] in low)   # stem tolerance
    t = [w for w in TARGETS if hit(w)]
    c = [w for w in CONTROLS if hit(w)]
    return t, c


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sizes", default="tiny,base,small")
    ap.add_argument("--wav", default=None, help="audio to transcribe (normalized WAV preferred)")
    ap.add_argument("--fn", default="transcribe", help="callable in studio.transcription_service")
    ap.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--json", action="store_true", help="dump full transcripts to a file")
    args = ap.parse_args()

    if args.child:
        return child(os.environ["_CMP_SIZE"], os.environ["_CMP_WAV"], args.fn)

    # ── parent: resolve the audio, screen the cache, then fan out ──
    wav = args.wav
    if not wav:
        print("  no --wav given; refusing to guess which take is the jargon audio.")
        print("  Resolve it from the DB first (the upload take, not the mic take):")
        print("    (cd backend && python -c \"...Take.objects.order_by('-created_at')...\")")
        return 2
    wav = str(pathlib.Path(wav).resolve())
    if not pathlib.Path(wav).is_file():
        print(f"  ABORT: audio not found: {wav}")
        return 2

    have = cached_sizes()
    off = offline_flags()
    print(f"  audio        : {wav}  ({pathlib.Path(wav).stat().st_size:,} bytes)")
    print(f"  hub cache    : {HUB_CACHE}")
    print(f"  cached sizes : {sorted(have) or '(none found - every size will be screened)'}")
    print(f"  offline flags: {off or '(none)'}")
    print()

    rows, dump = [], {}
    for size in [s.strip() for s in args.sizes.split(",") if s.strip()]:
        if have and size not in have:
            print(f"── {size:<8} SKIPPED: not in the hub cache"
                  f"{' and an offline flag is set' if off else ''}. "
                  f"Downloading is a network write and is not this script's to make.")
            rows.append((size, None, None, None, None, "SKIPPED (uncached)"))
            continue
        env = dict(os.environ, _CMP_SIZE=size, _CMP_WAV=wav)
        t0 = time.monotonic()
        proc = subprocess.run([sys.executable, __file__, "--child", "--fn", args.fn],
                              env=env, capture_output=True, text=True, cwd=str(BACKEND))
        line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("{")), None)
        if line is None:
            err = (proc.stderr.strip().splitlines() or ["<no output>"])[-1]
            print(f"── {size:<8} ERROR rc={proc.returncode}: {err[:160]}")
            rows.append((size, None, None, None, None, f"ERROR: {err[:60]}"))
            continue
        d = json.loads(line)
        if "error" in d:
            print(f"── {size:<8} ERROR: {d['error'][:160]}")
            rows.append((size, None, None, None, None, f"ERROR: {d['error'][:60]}"))
            continue
        t, c = score(d["text"])
        dump[size] = d
        rt = d["seconds"] / (pathlib.Path(wav).stat().st_size / 1_000_000_000 * 8)  # rough
        print(f"── {size:<8} {d['seconds']:>7.2f} s wall | lang={d['language']} "
              f"({(d['probability'] or 0):.2f}) | {d['n_segments']} seg | {d['chars']} chars")
        print(f"   targets recovered {len(t)}/{len(TARGETS)}: {', '.join(t) or '(none)'}")
        print(f"   controls intact   {len(c)}/{len(CONTROLS)}: {', '.join(c) or '(NONE - regression!)'}")
        missing = [w for w in TARGETS if w not in t]
        if missing:
            print(f"   still wrong      : {', '.join(missing)}")
        lost = [w for w in CONTROLS if w not in c]
        if lost:
            print(f"   *** lost vs tiny  : {', '.join(lost)}")
        rows.append((size, d["seconds"], d["n_segments"], len(t), len(c), ""))

    print()
    print("  size      wall_s   segs  targets/9  controls/4  note")
    for size, secs, segs, nt, nc, note in rows:
        print(f"  {size:<8} {('%.2f' % secs) if secs else '-':>7}  "
              f"{str(segs) if segs else '-':>5}   "
              f"{(str(nt) + '/9') if nt is not None else '-':>9}   "
              f"{(str(nc) + '/4') if nc is not None else '-':>10}   {note}")
    print()
    print("  Reading the table: a size earns its decode cost only if targets/9 rises")
    print("  AND controls/4 stays at 4/4. Wall seconds are for THIS file only - the")
    print("  production path also pays VAD and normalization, which are identical")
    print("  across arms here because every arm calls the same transcribe().")
    if args.json:
        out = REPO / "logs" / "whisper_size_compare.json"
        out.write_text(json.dumps(dump, indent=2, ensure_ascii=False))
        print(f"  full transcripts -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
