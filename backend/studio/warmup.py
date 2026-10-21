"""
Model warmup for the process that will actually run inference.

WHY THIS EXISTS. Measured on the 122 s reference take, first job vs second job
in the SAME worker process (worker-1181943, 2026-10-21 21:18):

    first job    8.195 s   VAD load 2.310 + Whisper load 1.908
                           + first Whisper inference 2.795
    second job   2.596 s   no "Loading ..." line at all
                           Whisper inference 1.605

So about 5.60 s of a user's very first take is cost that has nothing to do with
their audio. Only 4.22 s of that is model LOADING; the remaining 1.19 s is
CTranslate2's first-decode warm-up (thread pool and kernel dispatch), which
constructing a model object does not pay. That number is why this module runs
REAL AUDIO THROUGH BOTH STAGES instead of instantiating the models and calling
it done: a bare constructor would leave 1.19 s on the table and would not prove
the weights can actually decode.

SECOND BENEFIT, which is arguably the larger one. If the model cache is missing
or corrupt, this fails at WORKER STARTUP with a log line, rather than at the
user's first take with a spinner. Fail-early is the whole point of a warmup.

WHY IT IS CALLED FROM OUTSIDE, BEHIND AN ENV SWITCH: see studio/apps.py.
ready() runs in Daphne, in manage.py check, in migrate and in every harness
script, and none of those transcribe anything.

INPUT CONTRACT. Everything fed here is 16 kHz mono 16-bit PCM WAV, built either
by slicing the first second off an existing NORMALIZED take (real speech, so the
decoder genuinely runs) or, when no take exists yet on a fresh install, by
synthesizing low-level noise. Normalization is deliberately NOT invoked: stage 1
belongs to audio_service and the pipeline, and re-implementing it in a warmup is
exactly the "two code paths doing the same job" drift the project decided
against. Sample rate and channel count are READ FROM SETTINGS, never hardcoded,
for the same reason.

VAD AND WHISPER ARE INDEPENDENTLY GUARDED. Our stage-2 VAD returning zero
segments does not stop Whisper from warming, because the authoritative pass runs
Whisper over the whole file with its own internal vad_filter - the two engines
do different jobs (see README decision 10). A failure in one stage is recorded
and the other still runs.

NEVER RAISES. The caller always receives a dict.
"""

import logging
import os
import random
import struct
import tempfile
import time
import wave
from pathlib import Path

logger = logging.getLogger(__name__)

# How much audio to push through the models. One second is enough to force a
# full encoder pass and at least one decoder step, which is what the 1.19 s of
# first-decode cost actually consists of. Longer buys nothing measurable and
# makes worker startup feel arbitrary.
WARM_SECONDS = 1.0

# Amplitude of the synthesized fallback, as a fraction of full scale. Low enough
# to be obviously not speech, high enough that the encoder has real signal to
# chew on. Only used when no normalized take exists yet.
_NOISE_AMPLITUDE = 1600          # ~0.05 of int16 full scale

_state = {"done": False}         # idempotence: a second call is a no-op


def _target_params():
    """(sample_rate, channels, sampwidth) from settings - one input contract."""
    rate = int(getattr(__import__("django").conf.settings, "AUDIO_SAMPLE_RATE", 16000))
    chans = int(getattr(__import__("django").conf.settings, "AUDIO_CHANNELS", 1))
    return rate, chans, 2


def _candidate_sources():
    """
    Existing NORMALIZED wavs, newest first.

    Both layouts are covered deliberately, because the mic path still writes a
    flat TS_REC_NRM_*.wav while uploads write NRM_raw.wav under a take dir
    (README: "the microphone and upload paths use different on-disk layouts
    today"). A warmup that only knew one layout would silently degrade to
    synthesized noise on a mic-only install, and the degradation would be
    invisible.
    """
    from django.conf import settings
    root = Path(settings.RECORDINGS_DIR)
    if not root.exists():
        return []
    pats = ("**/NRM_*.wav", "**/TS_REC_NRM_*.wav")
    seen, out = set(), []
    for pat in pats:
        for p in root.glob(pat):
            if p.is_file() and p not in seen:
                seen.add(p)
                out.append(p)
    out.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return out


def _slice_first_second(src: Path, dest_dir: str, rate: int, chans: int):
    """
    Copy the first WARM_SECONDS of a contract-conforming wav to a temp file.

    REFUSES a non-conforming input rather than fixing it. If the file is stereo,
    a different sample rate or not 16-bit, we return None and let the caller
    synthesize - because silently normalizing here would mean the warmup grew a
    second copy of stage 1, which is the drift this project has already been
    bitten by once.
    """
    frames_needed = int(rate * WARM_SECONDS)
    try:
        with wave.open(str(src), "rb") as rin:
            if (rin.getnchannels() != chans
                    or rin.getsampwidth() != 2
                    or rin.getframerate() != rate):
                logger.debug("warmup: skipping %s (channels=%s width=%s rate=%s)",
                             src.name, rin.getnchannels(), rin.getsampwidth(),
                             rin.getframerate())
                return None, None
            n = min(frames_needed, rin.getnframes())
            data = rin.readframes(n)
            got_ms = int(1000 * n / rate)
        fd, tmp = tempfile.mkstemp(prefix=".ts_warm_", suffix=".wav", dir=dest_dir)
        os.close(fd)
        with wave.open(tmp, "wb") as wout:
            wout.setnchannels(chans)
            wout.setsampwidth(2)
            wout.setframerate(rate)
            wout.writeframes(data)
        return tmp, f"slice:{src.name}({got_ms}ms)"
    except Exception as exc:
        logger.debug("warmup: could not slice %s: %s", src.name, exc)
        return None, None


def _synthesize(rate: int, chans: int, dest_dir: str):
    """Fresh-install fallback: low-level noise, stdlib only, no engine imports."""
    n = int(rate * WARM_SECONDS)
    lo, hi = -_NOISE_AMPLITUDE, _NOISE_AMPLITUDE
    data = b"".join(struct.pack("<h", random.randint(lo, hi)) for _ in range(n * chans))
    fd, tmp = tempfile.mkstemp(prefix=".ts_warm_", suffix=".wav", dir=dest_dir)
    os.close(fd)
    with wave.open(tmp, "wb") as w:
        w.setnchannels(chans)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(data)
    return tmp, "synthesized"


def warmup_models(reason: str = "unspecified") -> dict:
    """
    Push one second of audio through VAD and Whisper, timings included.

    Returns a dict with ok/vad_ms/whisper_ms/total_ms/vad_segments/
    whisper_segments/source/reason. Never raises: every stage is guarded, and
    the temp file is removed in finally.
    """
    if _state["done"]:
        return {"ok": True, "skipped": "already warmed in this process"}

    from django.conf import settings
    rate, chans, _ = _target_params()
    out = {"ok": False, "vad_ms": None, "whisper_ms": None, "total_ms": None,
           "vad_segments": None, "whisper_segments": None,
           "source": None, "reason": None}

    t_all = time.perf_counter()
    tmp_path, source = None, None
    try:
        # Temp file lives under RECORDINGS_DIR so it is on the same filesystem
        # as the real takes (mkstemp across filesystems would be a copy, and a
        # cross-device rename later would fail - cheap insurance, zero cost).
        dest_dir = str(settings.RECORDINGS_DIR)
        os.makedirs(dest_dir, exist_ok=True)

        for cand in _candidate_sources():
            tmp_path, source = _slice_first_second(cand, dest_dir, rate, chans)
            if tmp_path:
                break
        if tmp_path is None:
            tmp_path, source = _synthesize(rate, chans, dest_dir)
        out["source"] = source

        # ── stage A: our Silero VAD (acoustic timeline engine) ─────────────
        try:
            from studio.vad_service import extract_speech_segments
            t0 = time.perf_counter()
            segs = extract_speech_segments(Path(tmp_path))
            out["vad_ms"] = round(1000 * (time.perf_counter() - t0))
            out["vad_segments"] = len(segs)
        except Exception as exc:
            # Recorded, not fatal: Whisper below is a different engine.
            out["vad_ms"] = round(1000 * (time.perf_counter() - t0))
            out["reason"] = f"vad: {type(exc).__name__}: {exc}"
            logger.warning("warmup: VAD stage failed (%s); Whisper will still warm",
                           out["reason"])

        # ── stage B: Faster-Whisper (linguistic engine + CTranslate2 decode) ─
        try:
            from studio.transcription_service import transcribe_audio
            t0 = time.perf_counter()
            res = transcribe_audio(Path(tmp_path), 1)   # beam=1: cheapest real pass
            out["whisper_ms"] = round(1000 * (time.perf_counter() - t0))
            out["whisper_segments"] = len(res.get("segments", []))
        except Exception as exc:
            out["whisper_ms"] = round(1000 * (time.perf_counter() - t0))
            out["reason"] = (out["reason"] or "") + f" whisper: {type(exc).__name__}: {exc}"
            logger.warning("warmup: Whisper stage failed: %s", exc)

        out["ok"] = out["vad_ms"] is not None and out["whisper_ms"] is not None \
            and "whisper:" not in (out["reason"] or "")
        out["total_ms"] = round(1000 * (time.perf_counter() - t_all))
        _state["done"] = out["ok"]

        logger.info(
            "warmup: %s | source=%s | vad=%sms(%s seg) whisper=%sms(%s seg) "
            "total=%sms | ok=%s%s",
            reason, source,
            out["vad_ms"], out["vad_segments"],
            out["whisper_ms"], out["whisper_segments"],
            out["total_ms"], out["ok"],
            "" if out["ok"] else f" | issue={out['reason']}",
        )
        return out

    except Exception as exc:
        out["reason"] = f"{type(exc).__name__}: {exc}"
        out["total_ms"] = round(1000 * (time.perf_counter() - t_all))
        logger.warning("warmup: aborted (%s); first take will pay the cold cost",
                       out["reason"])
        return out
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
