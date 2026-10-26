# TranStream

A local-first audio studio. Talk into your microphone or upload a file, and get a transcript back without the audio ever leaving your machine. Language detection works today; translation and text-to-speech are next on the list.

[![License: MIT](https://img.shields.io/badge/license-MIT-0b7285?logo=opensourceinitiative&logoColor=white)](LICENSE)
![Python](https://img.shields.io/badge/python-3.14-blue?logo=python&logoColor=white)
![Django](https://img.shields.io/badge/django-6.1-092e20?logo=django&logoColor=white)
![Channels](https://img.shields.io/badge/channels-websocket-2496ed)
![STT](https://img.shields.io/badge/stt-Faster--Whisper%20(CTranslate2%20int8)-7c3aed)
![API suite](https://img.shields.io/badge/api%20suite-62%2F62%20passing-4caf50)
[![Sponsor](https://img.shields.io/badge/Sponsor-%E2%9D%A4-ea4aaa?logo=github-sponsors&logoColor=white)](https://github.com/sponsors/S-V-J)

---

## What is this?

TranStream is a self-hosted speech studio that runs completely offline. Microphone audio streams in over a local WebSocket, and uploaded files arrive over HTTP or the public API. Everything is normalized with FFmpeg, split into speech regions with Silero VAD, and transcribed with Faster-Whisper. Once translation and TTS land, text will be translated with Argos Translate and spoken with Piper.

There are three ways in, and they all feed one pipeline. That's the main rule of the project. The moment two code paths do the same job, they start to drift, and they drift quietly because both of them appear to work.

| Way in | Transport | Route | Status |
|---|---|---|---|
| Live microphone | WebSocket, 100 ms binary frames | `AudioConsumer`, then the pipeline inline | Working |
| Browser file upload | `POST /studio/jobs` (session + CSRF) | `upload_service`, then `Job(QUEUED)`, then the worker | Working |
| Public API | `POST /api/v1/transcriptions` (bearer key) | The same `upload_service`, the same queue | Working |

## Highlights

- **Live microphone ingestion** over WebSocket in 100 ms binary chunks, with incremental on-disk buffering and a graceful partial save if the connection drops.
- **Rolling preview while you speak.** A frozen snapshot of the growing take is normalized and re-transcribed on a separate thread at `beam=1`, so the preview can never stall or drop the live connection.
- **File-upload transcription** with streamed multipart ingest, extension and magic-byte validation, a size cap enforced twice, atomic `.tmp` plus rename writes, and a per-user, per-UUID directory for every take.
- **A public API with per-user keys.** One fixed base URL, keys minted and revoked on the Account page, four endpoints, rate limiting, and a 62-assertion integration suite that exercises it end to end.
- **FFmpeg normalization** of any browser or container format to 16 kHz, mono, 16-bit PCM WAV. That's the single input contract every later stage relies on.
- **Silero VAD (ONNX Runtime)** for acoustic segmentation with millisecond timestamps, and **Faster-Whisper (CTranslate2, int8, CPU)** for transcription with automatic language detection and a confidence score.
- **Two timelines.** Acoustic (VAD) and linguistic (Whisper) segments are both computed and both stored, ready for word-level alignment later.
- **A durable job queue** with leases, heartbeats, a startup recovery sweep, bounded retries, cooperative cancellation and a concurrency semaphore.
- **About 22x faster than realtime** on CPU for a 122-second take (see [Measured performance](#measured-performance)).
- **Auth-gated studio** using Django sessions, a dark Tailwind UI driven by Alpine.js, and a systemd-managed dev server (the `transtream` CLI) with a Redis channel layer.

## How it fits together

The diagram below shows the whole system. The microphone path (left) runs the pipeline inline and never touches the queue. Uploads (right) go through the queue and a separate worker. Both arrive at the same pipeline.

```text
┌───────────────────────────────────┐    ┌───────────────────────────────────┐
│         Browser microphone        │    │        Browser upload form        │
│    MediaRecorder, 100 ms slices   │    │   or API client with bearer key   │
└───────────────────────────────────┘    └───────────────────────────────────┘
                  │ WebSocket frames                       │ multipart POST
                  ▼                                        ▼
┌───────────────────────────────────┐    ┌───────────────────────────────────┐
│ AudioConsumer (Channels)          │    │ upload_service.handle_upload      │
│ SessionWriter, append-only        │    │ 64 KB chunks, sniff file type     │
│ live preview on its own thread    │    │ size cap checked twice            │
│ pipeline runs inline after stop   │    │ atomic rename, Take + Job(QUEUED) │
└───────────────────────────────────┘    └───────────────────────────────────┘
                  │                                        │ Job row in DB
                  │                                        ▼
                  │                      ┌───────────────────────────────────┐
                  │ skips the queue      │ tsworker, separate process        │
                  │ (runs inline)        │ 120 s lease, 30 s heartbeat       │
                  │                      │ 3 attempts, one job at a time     │
                  │                      └───────────────────────────────────┘
                  │                                        │
                  ▼                                        ▼
┌────────────────────────────────────────────────────────────────────────────┐
│ ONE PIPELINE, ONE ENTRY POINT:  pipeline.process_take                      │
├────────────────────────────────────────────────────────────────────────────┤
│ 1  normalize    FFmpeg, 16 kHz mono 16-bit PCM WAV                         │
│ 2  segment      Silero VAD (ONNX), acoustic timeline                       │
│ 3  transcribe   Faster-Whisper on a float32 array, never a path            │
│ 4  persist      both timelines saved, source = VAD or WHISPER              │
└────────────────────────────────────────────────────────────────────────────┘
                                       │
                                       ▼
┌────────────────────────────────────────────────────────────────────────────┐
│ Disk   media/recordings/<user_id>/uploads/<take_id>/  raw + NRM wav        │
│ DB     Take, Segment, Job, ApiKey  (relative paths only)                   │
│ Redis  channel layer, rate-limit counters, last_used_at throttle           │
└────────────────────────────────────────────────────────────────────────────┘
                                       │
                                       ▼
┌────────────────────────────────────────────────────────────────────────────┐
│ EGRESS:  serializers.py owns every response shape                          │
├────────────────────────────────────────────────────────────────────────────┤
│ Browser  WebSocket control and stats frames, rendered transcript           │
│ API      202 / 200 / 409 + poll URL, json | srt | vtt | txt, 4xx, 429      │
└────────────────────────────────────────────────────────────────────────────┘
```

| Stage | Component | Output |
|---|---|---|
| 1. Normalize | FFmpeg (async subprocess) | `NRM_raw.wav` or `*_NRM_*.wav`, 16 kHz mono 16-bit PCM |
| 2. Segment | Silero VAD (ONNX) | `[{start_ms, end_ms, duration_ms}, ...]` |
| 3. Transcribe | Faster-Whisper (CTranslate2, int8, CPU) | `[{start_ms, end_ms, text}, ...]` plus `language` and `probability` |
| 4. Preview | Snapshot, then stage 1 and stage 3 at `beam=1`, on an isolated thread | Rolling interim text while recording |

### Job lifecycle and lease recovery

A job moves through five states. Retries loop back to `QUEUED`; `DONE`, `FAILED` and `CANCELLED` are terminal.

```text
                       retry while attempts < 3
                ┌───────────────────────────────┐
                │                               │
                ▼                               │
           ┌─────────┐    claim + lease  ┌─────────────┐    success    ┌───────┐
upload────▶│  QUEUED │──────────────────▶│  PROCESSING │──────────────▶│  DONE │
           └─────────┘                   └─────────────┘               └───────┘
                │                               │                      terminal
                │ cancel                        │ 3 attempts, or fatal error
                │                               │
                ▼                               ▼
          ┌───────────┐                  ┌─────────────┐
          │ CANCELLED │                  │    FAILED   │
          └───────────┘                  └─────────────┘
          terminal                       last_error kept, readable via API
```

Here's what happens when a worker is killed mid-job. Nobody has to be told anything.

```text
t = 0 s      worker claims the job, lease_until = t + 120 s
t = 30 s     heartbeat refreshes the lease
t = 60 s     heartbeat
t = 90 s     heartbeat
t = 97 s     worker process is killed
t = 120 s    the lease expires on its own
t = 120 s+   next sweep reclaims it: leases_reclaimed=1,
             the job is QUEUED again, attempt 1 of 3
```

## Design decisions

These are the choices that shaped the codebase, what each one bought me, and what each one cost.

### 1. One input contract, enforced at the front door

Every piece of audio, from any source, becomes 16 kHz mono 16-bit PCM WAV before anything intelligent happens:

```bash
ffmpeg -i in -ar 16000 -ac 1 -sample_fmt s16 -f wav out
```

This collapses "N container formats times 3 front doors" into a single shape. Neither the VAD model nor Whisper ever sees a WebM, an Opus stream or a half-written file, so every later stage has exactly one case to handle.

The cost is one FFmpeg subprocess per take (about 0.3 to 0.5 s measured) and a normalized copy on disk, roughly twice the raw size for a two-minute take.

### 2. One entry point per job, never a copy

`upload_service.handle_upload` is the only code that accepts an upload. The browser view and the bearer API both call it. Stage 1 normalization works the same way: it goes through `audio_service` from both the microphone and upload paths, across an `asyncio.run` bridge that a guard pins to exactly one call site.

I'm strict about this because the project has already been bitten. Two FFmpeg paths disagreed about the audio, and I only noticed because the same file produced a different transcript depending on how it arrived. Two code paths that do the same job both look correct, until they don't.

The price is a bit more indirection at the edges. The HTTP layer goes through a service instead of shelling out inline.

### 3. Never pass a file path to Whisper

Transcription is always fed a NumPy `float32` array decoded by `soundfile`, never a path.

If you call `model.transcribe("<path>")`, it internally goes through `faster_whisper.audio.decode_audio` and then `av.open()`. That brings back PyAV's container-demux fragility on truncated or partly written files, and on Python 3.14 it fails outright. This isn't hypothetical. It's the actual error from this project's log, before the fix:

```text
2026-10-20 13:27:11 WARNING Transcription failed for TS_REC_NRM_20261020_132711.wav:
    Whisper transcription failed: open() got an unexpected keyword argument 'metadata_errors'
```

The cost is that PyAV still has to be importable, because `faster_whisper`'s import graph references it. That's why the quickstart installs the `libav*-dev` build dependencies. At runtime TranStream never calls it. An AST guard enforces that, rather than convention, because a rule that only lives in someone's memory isn't really a rule.

### 4. The live preview runs on its own thread, with its own files

While you speak, a snapshot of the growing take is copied to `.ts_snap_<rand>.webm`, normalized to `.ts_live_<rand>.wav`, and transcribed at `beam=1` on an isolated thread. The preview cadence is counted in chunks.

Re-transcribing the live buffer inside the consumer's event loop would stall the socket. And reading a file that's still being appended to is the same demux problem as decision 3 in different clothes. Snapshot-then-decode means the preview always reads a complete, closed file.

The cost is extra disk churn (dot-prefixed temporaries, deleted after use) and a second model invocation per preview. In practice preview clips come out at 3 to 4 segments at `beam=1`, while the authoritative pass over the full take after you stop uses `beam=5`.

### 5. Uploads stream; they are never buffered

Files go straight to disk in 64 KB chunks. The size cap is enforced twice: once before streaming, from the declared length, and once during streaming, against the bytes actually counted. Data is written to a `.tmp` name and only renamed on success, with cleanup on any `BaseException`. Each take gets its own per-user, per-UUID directory.

A declared `Content-Length` can lie. A server that trusts it can be filled up by a client that announces 1 KB and sends 1 GB. A half-written file is also indistinguishable from a complete one to everything downstream, so an atomic rename is the difference between "no file" and "a file that lies". Per-UUID directories make name collisions structurally impossible, so there's no lock because there's nothing to fight over.

It's more code at the ingest boundary than a naive `f.read()`. That's deliberate: this is the one place where complexity is cheaper than a corrupt take. The cleanup holds. Checked rather than assumed: `find media \( -name '*.tmp' -o -name '*.part' \)` returns 0, and `.ts_*` at the recordings root returns 0, as does Django's own view via `settings.MEDIA_ROOT`. The twelve `.ts_part_*` files seen earlier in development were all pre-queue history (13:41-17:47 on 2026-10-20) and none has reappeared since the consumer was rebuilt around the durable pipeline. "As far as I can tell" was the wrong register for something a one-line command answers.

### 6. The request never transcribes

Upload handlers save the file, write one `Take` and one `Job(QUEUED)`, and return 202 with a `job_id`. A separate `tsworker` process does the inference.

A request that takes several seconds gets retried. Returning 202 makes the client's job obvious (poll), and it keeps the ASGI worker free.

The cost is that clients have to poll or subscribe. The 409-while-pending response exists to make "not ready yet" a normal answer instead of something to guess at. It returns the current state, the progress, and an absolute poll URL a client can follow blindly.

### 7. Durability by lease, not by notification

A worker claims a job with a 120 s lease and refreshes it with a 30 s heartbeat. A startup recovery sweep reclaims expired leases and also deletes orphaned temporary files. Retries are capped at 3 attempts, and the last error is saved on the row.

Processes die. A design that needs someone to be told a worker died has a second way to fail. Here nothing has to be notified, because the lease simply runs out.

I didn't plan the evidence for this, but I got it anyway. During development, jobs sat in `QUEUED` for about 2, 7, 19 and 25 minutes with no worker process at all, and every one was picked up within about 2 s of a worker starting. That's nine independent observations. A queue that survives its producer giving up is the property that matters, and here it was observed rather than just claimed.

The cost is clock-skew and lease tuning. The sweep reports its counts (`leases_reclaimed`, `orphans_deleted`, `errors`) so the mechanism stays visible.

### 8. Cancellation is cooperative, and that is a choice

A `QUEUED` job, or one waiting out a retry backoff, is truly cancelled and never runs. A job that's already inside `process_take()` finishes its in-flight inference, and its next persistence write moves the row to `DONE`, overwriting `CANCELLED`.

Interrupting Whisper mid-inference buys a local-first studio nothing, and it risks leaving half-written segments behind. The transcript endpoint just serves the finished result.

The downside is that cancelling a running job can still produce a transcript. A preemptive cancel would need a cancellation token polled inside the pipeline, which is a change to make on purpose, not as a side effect. I wrote this into the view's own docstring so it reads as a decision and not an oversight.

### 9. Two segment timelines are stored, not reconciled

VAD segments and Whisper segments are both saved, told apart by a `source` column.

They answer different questions. VAD segments the acoustic signal and cuts on silence. Whisper segments the linguistic stream over the whole take and merges across short pauses, because a sentence shouldn't be cut in half. On one 122 s take I measured 11 VAD segments and 7 Whisper segments. On a 39 s take it was 10 and 2. Both are right. Averaging them, or picking a winner, would throw away exactly the information word-level alignment needs later.

The cost is that readers have to understand `source`, and the API exposes both timelines instead of hiding one.

### 10. There are two VAD passes, and they are different engines

Stage 2 runs our own Silero VAD over the normalized WAV and stores the result as the acoustic timeline. Stage 3 also passes `vad_filter=True, vad_parameters=dict(min_silence_duration_ms=500)` to Faster-Whisper, which runs its own internal Silero as a silence-trimming pass before decoding.

They do different jobs. Ours is an observable artifact: timestamps we store and serve. Whisper's is an inference optimization that improves segment quality by not decoding pure silence.

The cost is two silence numbers for one file, which is confusing until you know why. On the 122 s take, our stage 2 VAD reported 11 segments and 52.5 s of speech, while Whisper's internal filter logged `removed 01:02.520 of audio`. Neither is wrong. They measure different things at different points in the pipeline.

### 11. Segments, not a text blob

Transcripts are stored as rows of `(start_ms, end_ms, text, source, index)` and rendered on demand. One set of rows serves `json`, `srt`, `vtt` and `txt`, and it means the live preview and the final pass are the same data at different moments, not two competing documents. The only cost is a little rendering work per request, which is trivial at these sizes.

### 12. One serializer module owns every response shape

`serializers.py` is the single implementation of each resource representation, used by both `/api/v1` (bearer) and `/studio/jobs` (session). Error envelopes stay local to each view.

This way the browser and the API can't disagree about what a job looks like, because there's only one description of a job. It closed a real class of bug: a field added to one surface and forgotten on the other. I checked it in practice. `duration_ms` reads `122041` in the job envelope, in the transcript envelope and in the database row, and all three agree.

Error messages are deliberately not unified, since a 409 "not ready" body is a view-specific concern. The locality guard checks who owns a response shape, not who owns a name. Checking names would be wrong, because `segment_count` legitimately appears on the WebSocket frame too, with a different meaning.

### 13. Bearer routes are CSRF-exempt by design; session routes are not

`api_key_required` returns `csrf_exempt(wrapper)`. `/studio/*` and `/account/*` keep full CSRF enforcement.

A CSRF attack works by making a victim's browser attach ambient credentials, meaning cookies, to a cross-site request. An `Authorization: Bearer ...` header isn't ambient. No browser attaches it on its own, so a cross-site request can't carry it. There's no CSRF surface on a bearer endpoint. Leaving the middleware in the path added no security at all, and it made `POST /api/v1/transcriptions` and `DELETE /api/v1/jobs/{id}/cancel` return 403 to every non-browser client. In other words it switched off the public API for the people it exists to serve.

**How I missed it, and what it taught me.** `CsrfViewMiddleware.process_view` runs before the view, so the authentication decorator never even executed on those two methods. The browser worked the whole time, because a browser has the cookie. And every probe I'd run against the API was a `GET`, and `GET`, `HEAD`, `OPTIONS` and `TRACE` are never CSRF-checked. A bug that broke the feature for its entire intended audience was invisible through a whole phase of green checks, and only showed up the moment a `POST` joined the probe set.

**The guard.** `scripts/guard_layers.py` check **G4** asserts `csrf_exempt` on the callable that `django.urls.resolve()` returns. That's runtime introspection, not a text search, because a grep for the token would match the guard's own docstring. There's also a negative control that I re-run after every restart: `POST /account/keys/generate/` with no CSRF token must still return 403. Without that reading, "the API returns 401" is equally consistent with CSRF having been turned off globally, which would be a vulnerability dressed up as a fix.

### 14. Keys: shown once, fingerprinted, refused explicitly

The key flow is generate, copy once, list by hint and scopes, revoke. The raw key is never stored and never shown again. What's stored is a SHA-256 fingerprint in a unique indexed column, plus a salted PBKDF2 hash that's verified in constant time. Revocation is refused by an explicit `is_active` check in the decorator, and every authentication failure returns the same message.

The fingerprint gives an O(1) lookup with no scanning, and the hash protects against a fingerprint collision or a tampered row. The explicit activity check is there because a security property should never be inherited from a collaborator's implicit filter. If someone later widens a lookup to power an audit view, an implicit `is_active=True` disappears and revoked keys start working again. The uniform message removes the "absent versus revoked" oracle, so responses can't be used to enumerate valid keys.

PBKDF2 verification costs around 100 ms, which is fine for an API that processes multi-second audio. It's noted in the module docstring so nobody "optimizes" it away without reading why it's there.

### 15. The rate limiter is a fixed window, and it fails open

Per key, per minute, there's one Redis key, `ratelimit:{fingerprint}:{unix_minute}`, with `INCR` and `EXPIRE 120`. A request is refused when `count > limit`. The default is 120 per minute, with `Retry-After: 60`. If Redis is unavailable the request is allowed and a warning is logged. `last_used_at` is written at most once per key per minute, gated by a Redis `SETNX`.

**Why a fixed window.** A sliding window needs bookkeeping that a local-first tool shouldn't carry. One key and one expiry is honest about its cost.

**Why 120 and not 10.** A limit of 10 per minute contradicts the API's own documented contract, which says to poll `GET /api/v1/jobs/{id}` until the state is terminal. A poller hitting every 2 s needs 30 per minute, and `studio.js` polls every 1.5 s (40 per minute). The old default throttled legitimate traffic within about 15 s of polling, and it failed selectively on long recordings: a 2-minute take needs 3 polls and squeaks through, while a 20-minute take needs 15 to 30 and stalls even with a healthy worker. I measured this instead of arguing it. At a limit of 10, a 180 s poll loop produced 27 requests and 2 stalls. At 120 the same loop produced 79 requests and 0 stalls. And 120 per minute still stops credential stuffing, which needs hundreds of requests per second.

**Why fail open.** A local-first tool shouldn't become unusable because Redis restarted. The availability of the user's own work matters more than a limit that only matters against strangers.

**The cost.** A fixed window can be gamed at a boundary, with two half-bursts either side of a minute. I accept that, and I'm saying so here.

**The proof.** Test `S12` seeds the counter directly and reads the limit from settings, so it doesn't depend on the threshold. Seed `limit-1`, send one request, and you get 200 with exactly one increment (which catches double-feeding). Seed `limit` and you get 429, `Retry-After: 60` and a naming body. Clear it and you get 200 again. Together the two probes pin the comparison operator exactly: `>` at `count == limit`, refuse at `limit + 1`.

## Tech stack

| Layer | Choice (versions pinned in `requirements.txt`) |
|---|---|
| Backend | Django 6.1.2, Channels 4.3.2, channels_redis 4.3.0, Daphne 4.2.3, python-dotenv 1.2.4 |
| Audio | FFmpeg 8.x, soundfile 0.14.0, numpy 2.5.4, Silero VAD 6.2.3 (onnxruntime 1.31.0), Faster-Whisper (CTranslate2, int8) |
| Queue | Custom durable worker (`manage.py tsworker`) with leases, heartbeats, a recovery sweep, bounded retries and a semaphore |
| Store | SQLite (`backend/db.sqlite3`), the filesystem under `media/`, Redis 8.1.0 (channel layer, limiter and throttle) |
| Frontend | Alpine.js, Tailwind CSS, HTMX, PWA *(planned)* |
| Runtime | Python 3.14, WSL2 / Ubuntu, systemd user units via the `transtream` CLI |
| Translation *(planned)* | Argos Translate (offline NMT) |
| TTS *(planned)* | Piper (local neural voices) |

## Quickstart

### 1. System dependencies

```bash
# Ubuntu / Debian / WSL2
sudo apt-get update
sudo apt-get install -y \
  ffmpeg \
  libsndfile1 \
  redis-server \
  pkg-config \
  build-essential \
  python3-dev \
  libavformat-dev libavcodec-dev libavdevice-dev \
  libavutil-dev libswscale-dev libswresample-dev libavfilter-dev
```

> The `libav*-dev` packages are needed because `faster-whisper` imports PyAV when the module loads. On Python 3.14 there may not be prebuilt `av` wheels yet, so PyAV compiles from source against these headers. At runtime TranStream never calls PyAV (see [decision 3](#3-never-pass-a-file-path-to-whisper)). It only has to be importable.

> **WSL2 note.** Redis may warn `Memory overcommit must be enabled!`. Fix it with `sudo sysctl vm.overcommit_memory=1` and persist that in `/etc/sysctl.conf`. Without it, background saves can fail under memory pressure, and this project's channel layer runs on that same Redis.

### 2. Clone and install

```bash
git clone https://github.com/S-V-J/transtream.git
cd transtream

python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### 3. Configure

```bash
cp .env.example .env
# Edit .env if you want to. Every value has a safe default baked into the code,
# so the app runs fine with an empty .env.
```

### 4. Database and superuser

```bash
cd backend
python manage.py migrate
python manage.py createsuperuser
cd ..
```

### 5. Run (two terminals)

```bash
# Terminal 1: managed dev server (systemd user unit + Redis + dynamic port)
transtream restart
transtream status
transtream logs
cat logs/transtream.port        # the bound port, e.g. 8003

# Terminal 2: the worker. Without it, uploads stay QUEUED forever
# (and get picked up the moment it starts; see decision 7).
cd backend

# Plain: first take pays ~5.6 s of cold-start cost.
python manage.py tsworker --verbose

# Warmed: models loaded at startup instead, off the user's first take.
TS_WARMUP_MODELS=1 python manage.py tsworker --verbose
```

Open `http://127.0.0.1:8003/` and you'll land on the login page. Sign in and you're redirected to `/studio/`. Click **Start**, speak, then click **Stop**.

> Microphone capture needs a secure context. `http://localhost` and `http://127.0.0.1` qualify. A LAN IP over plain HTTP doesn't, because the browser blocks `getUserMedia`.
>
> The `transtream` CLI never kills a process it didn't start, and it always picks a free port dynamically (it prefers 8000 and scans upward, then writes the chosen port to `logs/transtream.port`). Nothing runs in the background unless you explicitly run `transtream enable`.

## Configuration

Every value is read with a safe default (`getattr(settings, KEY, default)`), so the app runs with an empty `.env`.

| Variable | Default | Purpose |
|---|---|---|
| `AUDIO_SAMPLE_RATE` | `16000` | Normalization target sample rate. |
| `AUDIO_CHANNELS` | `1` | Normalization target channel count. |
| `FFMPEG_BIN` | `ffmpeg` | Path to the FFmpeg binary. |
| `FFMPEG_TIMEOUT` | `300` | Timeout per conversion subprocess, in seconds. |
| `VAD_THRESHOLD` | `0.5` | Silero probability threshold. Higher is stricter. |
| `VAD_MIN_SILENCE_MS` | `500` | Silence needed to split two speech regions. |
| `VAD_SPEECH_PAD_MS` | `30` | Padding before and after each region, so phonemes aren't clipped. |
| `WHISPER_MODEL_SIZE` | `tiny` | The accuracy lever, and not a free one: `tiny` is 39M params, `base` roughly 2x the decode cost, `small` roughly 6x. Only `tiny` is in the Hub cache by default, so a larger value is a download, not an edit. `scripts/compare_whisper_sizes.py` measures what a size buys on your audio. |
| `WHISPER_DEVICE` | `cpu` | CTranslate2 device. `cuda` needs a GPU build of ctranslate2; this project's measured numbers are all `cpu`. |
| `WHISPER_COMPUTE_TYPE` | `int8` | Quantisation. `int8` is the CPU default and the reason a 122 s take finishes in seconds. |
| `WHISPER_LANGUAGE` | unset (= auto-detect) | ISO-639-1 code to pin, e.g. `hi`. Empty reproduces auto-detect exactly, so shipping the knob changed nothing. Detection is a guess, and it guessed `id` for a Hindi utterance on this machine and returned romanized noise; pin it when you know the language. |
| `LIVE_TRANSCRIBE_EVERY_CHUNKS` | `30` | Frames between rolling-preview runs. `<= 0` is the documented kill switch, and this is the one read wrapped in try/except, so a typo degrades to the default instead of killing the consumer. |
| `WHISPER_MAX_CONCURRENT` | `1` | The concurrency semaphore: one job in flight per worker process. |
| `WORKER_POLL_INTERVAL` | `2.0` | Seconds between queue polls. A **float** - the worker wraps it in `float()`, which is why `2` and `2.0` are not the same claim. |
| `WORKER_LEASE_DURATION` | `120` | Lease length in seconds ([decision 7](#7-durability-by-lease-not-by-notification)). |
| `WORKER_HEARTBEAT_INTERVAL` | `30` | Heartbeat period. Must stay well under the lease or the sweep reclaims live work. |
| `WORKER_POLL_BATCH` | `5` | Rows fetched per poll. |
| `RECOVERY_ORPHAN_MAX_AGE` | `3600` | Age in seconds before the startup sweep treats a dot-prefixed temporary as an orphan. |
| `WHISPER_DETERMINISTIC` | unset | **Not a `settings.py` read, on purpose.** `tsworker.py` takes it from `os.environ` directly because it must pin `OMP_NUM_THREADS=1` before CTranslate2 spawns its pool. Setting it in `.env` still works, because `load_dotenv` runs first. |
| `STUDIO_MAX_TAKE_BYTES` | `512 MB` | Hard cap per microphone take. It force-saves and stops when exceeded. |
| `STUDIO_STATS_EVERY_CHUNKS` | `20` | How often live stats are pushed to the browser. |
| `UPLOAD_MAX_BYTES` | `100 MB` | Hard cap per upload, enforced before and during streaming. |
| `UPLOAD_RATE_LIMIT_PER_MINUTE` | `120` | Bearer rate limit per key per minute ([decision 15](#15-the-rate-limiter-is-a-fixed-window-and-it-fails-open)). |
| `REDIS_URL` | `redis://127.0.0.1:6379/0` | Channel layer, rate limiter and `last_used_at` throttle. |
| `HF_OFFLINE` | unset | Sets `HF_HUB_OFFLINE=1` before any Hub import, dropping the 0.5-1.1 s revision round trip from every cold Whisper load. **Leave unset until one successful run has downloaded the model** - with it set, a fresh clone fails instead of fetching the ~75 MB `tiny` weights. |
| `TS_WARMUP_MODELS` | unset | Runs one second of audio through VAD and Whisper at worker startup. **Costs 2.7-3.2 s** (three self-reports: 3080, 2704 and 3234 ms, all `source=slice`, all `ok=True`) and moves the cold-load plus first-decode cost off the user's first take. Read from the **process environment**, not from `.env`, because `AppConfig.ready()` also runs in Daphne and in every one-off command, and a `.env` flag cannot tell them apart. Set it on the launch line only. |

> **What this table does not list.** The four `DJANGO_*` bootstrap variables (secret, debug flag, two host lists) are in `.env.example` but not here, because they are deployment identity rather than tuning; and `STUDIO_MAX_TAKE_BYTES` is here but not in the template, because it is a hard constant and not a knob. The template is the full surface; this table is the part worth reading before changing.

> **Two caps, on purpose.** `STUDIO_MAX_TAKE_BYTES` (512 MB) covers the microphone path and `UPLOAD_MAX_BYTES` (100 MB) covers uploads. They differ deliberately: a live take is bounded by a human speaking, while an upload is bounded by a stranger. Please don't merge them.

> **Whisper and live-preview knobs: these are wired now.** `WHISPER_MODEL_SIZE`, `WHISPER_DEVICE`, `WHISPER_COMPUTE_TYPE`, `WHISPER_LANGUAGE`, `LIVE_TRANSCRIBE_EVERY_CHUNKS`, `STUDIO_STATS_EVERY_CHUNKS`, `WHISPER_MAX_CONCURRENT`, the six `WORKER_*`/`RECOVERY_*` knobs are all assigned in `settings.py` as of commit `806b11d`, so editing them in `.env` changes behaviour. Defaults were copied verbatim from the services' own `getattr` fallbacks, so wiring the read changed nothing for anyone who sets nothing - the same shape as the `UPLOAD_MAX_BYTES` gap closed in `0926812`. Two exceptions, both deliberate. `WHISPER_DETERMINISTIC` is still read straight from `os.environ` by `tsworker.py`, because it has to pin `OMP_NUM_THREADS=1` before CTranslate2 is imported; moving it onto settings would be a behaviour change dressed as a tidy-up. And the `vad_parameters=dict(min_silence_duration_ms=500)` inside the Whisper call is still a hardcode, because whether it should track `VAD_MIN_SILENCE_MS` is an open design question ([decision 10](#10-there-are-two-vad-passes-and-they-are-different-engines)), not a knob to steal. One consequence worth knowing: these reads are unguarded `int()`/`float()`, so a malformed value raises at import and stops every process, `manage.py check` included - the same crash-on-typo shape `VAD_MIN_SILENCE_MS` has had since Phase 4. Silent reversion to a default was rejected, because nobody would ever learn their `.env` was broken. The discriminator in [Verification](#verification) still tells you which knobs are wired in a single command.

## Storage layout

Files live on disk, facts live in SQLite, and the database stores relative paths only, so the media root can move without rewriting rows.

```text
# Upload path (current, target layout)
media/recordings/<user_id>/uploads/<take_id>/
    raw.webm          # verbatim capture, kept so re-normalization is possible
    NRM_raw.wav       # 16 kHz mono 16-bit PCM

# Microphone path (older flat layout; unifying it with the above is open work)
media/recordings/TS_REC_RAW_<YYYYMMDD_HHMMSS>.<webm|ogg|m4a|...>
media/recordings/TS_REC_NRM_<YYYYMMDD_HHMMSS>.wav

# Transient, dot-prefixed, deleted on success
.ts_part_<rand>.<ext>     # SessionWriter incremental append
.ts_snap_<rand>.<ext>     # live-preview snapshot of the growing take
.ts_live_<rand>.wav       # snapshot normalized for preview transcription
```

Timestamps are UTC, and a colliding name gets a numeric suffix (`_1`, `_2`) instead of being overwritten. The extension comes from the negotiated MIME type: `audio/webm;codecs=opus` becomes `.webm`, `audio/mp4` becomes `.m4a`, `audio/flac` becomes `.flac`, and anything unknown becomes `.bin`.

| Table | Holds |
|---|---|
| `Take` | `kind` (`mic` or `upload`), `duration_ms`, `raw_relpath`, `normalized_relpath` |
| `Segment` | `start_ms`, `end_ms`, `text`, `source` (`VAD` or `WHISPER`), `index` |
| `Job` | `state`, `attempts`, `max_attempts`, `last_error`, lease and heartbeat columns, timestamps |
| `ApiKey` | `fingerprint` (unique, indexed), `hashed_secret`, `is_active`, `hint`, `scopes`, `created_at`, `last_used_at`, `revoked_at` |

`duration_ms` is backfilled from the normalizer's measured metadata, and only when it's currently unset, so the microphone path is provably unaffected by it.

> Generated audio (`*.wav`, `*.mp3`, `*.m4a`, `*.flac`, `*.ogg`, `*.aac`) and `media/` are git-ignored, so nothing you record ends up in the repository.

## API

**Status: working.** It's covered end to end by a 62-assertion integration suite.

One fixed base URL serves every user, and identity comes from a per-user bearer key minted on the Account page (`/account/`). Keys are shown once, stored hashed, and can be revoked. The key-list response carries no secret material at all: no `fingerprint`, no `hashed_secret`, no `raw_key` (test `S3` asserts this).

```text
POST    /api/v1/transcriptions              multipart upload -> 202 {job_id, take_id, size_bytes, status}
GET     /api/v1/jobs/{id}                   status + progress + is_terminal
GET     /api/v1/jobs/{id}/transcript        json (default) | ?format=srt|vtt|txt
DELETE  /api/v1/jobs/{id}/cancel            cooperative cancel -> 200 {cancelled: true}
```

```bash
curl -X POST http://127.0.0.1:8003/api/v1/transcriptions \
  -H "Authorization: Bearer ts_live_XXXXXXXXXXXXXXXXXXXX" \
  -F "file=@meeting.mp3"
```

### Responses

| Situation | Code | Body |
|---|---|---|
| Accepted | 202 | `{job_id, take_id, filename, size_bytes, status: "queued"}` |
| Poll | 200 | Full job representation: `state`, `progress_pct`, `is_terminal`, `kind`, `duration_ms`, `attempts`, `max_attempts`, `last_error`, `language`, timestamps |
| Transcript not ready | 409 | `{error: "Transcript not ready. Job state: queued.", state, progress_pct, poll: <absolute URL>}` |
| Transcript | 200 | `{segments[], segment_count, language, language_probability, duration_ms}`, or `srt`/`vtt`/`txt` as an attachment download |
| Unknown format | 400 | `{error: "Unknown format: pdf. Use one of: json, srt, vtt, txt."}` |
| Any auth failure | 401 | Missing header, wrong scheme, empty token, unknown key or revoked key all return one uniform message |
| Foreign or unknown job | 404 | `{error: "Job not found."}` |
| Wrong method | 405 | (none) |
| Already terminal | 409 | `{error: "Job already in terminal state: cancelled. Cannot cancel.", state}` |
| Rate limited | 429 | `{error: "Rate limit exceeded. Try again in 60 seconds.", retry_after: 60}` plus a `Retry-After: 60` header |

### CSRF posture (please read before "fixing" this)

Bearer routes under `/api/v1/` are CSRF-exempt by design. The exemption is applied inside `api_key_required`, so every authenticated endpoint inherits it. It's safe because CSRF exploits ambient credentials (cookies that a browser attaches without being asked), and no browser attaches an `Authorization` header on its own. Session-authenticated surfaces (`/studio/*` and `/account/*`) keep full CSRF enforcement.

**Don't re-enable CSRF checking on `/api/v1/`.** Doing so returns 403 to every non-browser client and silently disables the public API for the platforms it exists to serve. Guard `G4` in `guard_layers.py` fails the build if the exemption is lost, and the negative control (a session `POST` with no CSRF token must still return 403) is part of every verification run.

## Verification

I treat "it works" as a claim that has to be mechanically refutable. All the harnesses are stdlib-only Python, never bash. A bash probe here once conflated an empty body with an HTML error page (the `json.loads` error text is identical for both) and cost me an evening.

| Script | What it asserts |
|---|---|
| `scripts/guard_layers.py` | **G1** no hallucinated HTTP decorators (import statements only). **G2** no audio-engine imports in the HTTP/serializer layer. **G3** response-shape locality (`is_terminal` lives only in `serializers.py`). **G4** `csrf_exempt` on the callable `resolve()` returns, by runtime introspection, not grep. |
| `scripts/guard_layers.py --selftest` | Proves the exit gate can actually fail (`report(['x']) -> 1`, `report([]) -> 0`). A guard whose failure path is unreachable is worse than no guard, because it manufactures confidence. |
| `scripts/guard_pipeline_invariants.py` | Exactly one `asyncio.run` bridge call site, which kills front-door FFmpeg drift. |
| `scripts/verify_phase6b.py` | Pipeline harness covering recovery, retry, lease, concurrency and determinism (sections A/E/B/C/D). |
| `scripts/diagnose_e2.py` | Stage 1 unification diagnostics (T1' bridge determinism, T2 unpinned monitor, T2p subprocess pin-proof). |
| `scripts/verify_phase8_api.py` | The 62-assertion API integration suite, run over real HTTP: every bearer endpoint, every branch, a DB cross-check, the full auth-negative matrix and a seeded rate-limit proof. It mints keys and revokes everything it created on every exit path, because a harness must not be able to leak a credential by failing. |
| `scripts/diagnose_ratelimit.py` | Separates limiter arithmetic (in-process) from HTTP-path behaviour (real stack), and walks the decoration chain by `__code__.co_filename` instead of by label. |
| `scripts/green_gate.py` | Refuses to certify "green" unless the report shows 0 failures and reached the terminal section. It deliberately does not assert the assertion count, because a count is a proxy for completeness and the two can come apart. |
| `scripts/guard_worker_boots.py` | **Runs** `tsworker --once` and asserts four output markers plus real byte growth in `logs/worker.log`. Exists because commit `28ffb2c` shipped a `NameError` that `py_compile`, `manage.py check` and G1-G4 all passed: the worker could not start at all, and no static gate in the repository could see it. A boot is a runtime fact, so the gate executes. It SKIPs (exit 2) when the queue is non-empty rather than risking stealing a job from a live worker, and `--selftest` feeds it that same `NameError` to prove it can refuse. |

```bash
cd /path/to/transtream && source scripts/_venv_guard.sh

python scripts/guard_layers.py --selftest && python scripts/guard_layers.py
python scripts/guard_pipeline_invariants.py
python scripts/verify_phase6b.py
python scripts/diagnose_e2.py

# The API suite needs a running server, a running worker, and the admin password.
python scripts/verify_phase8_api.py && python scripts/green_gate.py

# Which of the documented knobs are actually wired?
python - <<'PY'
import os, sys
sys.path.insert(0, "backend")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "transtream.settings")
import django; django.setup()
from django.conf import settings
for n in ("WHISPER_MODEL_SIZE","WHISPER_DEVICE","WHISPER_COMPUTE_TYPE",
          "LIVE_TRANSCRIBE_EVERY_CHUNKS","WHISPER_DETERMINISTIC",
          "UPLOAD_MAX_BYTES","UPLOAD_RATE_LIMIT_PER_MINUTE"):
    print(f"{n:34s} in settings={hasattr(settings,n)!s:5s} value={getattr(settings,n,None)!r}")
PY
```

Each of these method rules came from a real failure:

- **Ask the thing itself.** Ask the URL resolver, not the source. Ask the counter, not the clock. Check the file after a write, not the compiler. Ask the DB and the API, and require them to agree.
- **Never let one probe answer a question alone.** When a structural inference and a direct measurement disagree, the measurement wins by default and the inference is the suspect. (This caught a false "double-decoration" alarm, caused by `functools.wraps` forwarding `__module__` and `__qualname__` to every wrapper in the chain.)
- **A check that can't stop you is a report, not a guard.**
- **Correct history forward, and never amend a pushed claim.** Commit `e8297e6` retracts the incorrect "green 61/61" claim from `9e80209` in a new commit instead of rewriting the old one. The retraction is part of the record on purpose.

## Measured performance

Reference machine: WSL2 / Ubuntu, CPU only, `tiny` model, `int8`. The test take is 122 seconds long (1.98 MB WebM, which becomes a 3.9 MB normalized WAV), with 11 VAD segments, 7 Whisper segments and language `en`.

| Measurement | Value |
|---|---|
| FFmpeg normalize | about 0.3 to 0.5 s |
| Silero VAD inference | about 0.6 s |
| Whisper inference (7 segments, `beam=5`) | about 2.7 s |
| **End to end, warm model** | **about 2.6 to 5.5 s, or 22x to 45x faster than realtime** |
| End to end, cold (first job after worker start) | about 5.4 to 8.2 s |
| Silero VAD ONNX cold load | 1.2 to 2.3 s |
| Faster-Whisper cold load | 1.5 to 1.9 s (0.5 to 1.1 s of that is the Hugging Face revision round trip) |
| Second job in the same process | 0.000 s model load, with no `Loading ...` line at all |
| Worker claim latency | within about 2 s of a job appearing (`poll=2.0s`) |

The cold-load numbers are why startup warmup is on the roadmap, and one measured detail changes what the fix has to be. Cold versus warm in the same process: first job 8.195 s, second job 2.596 s, a gap of about 5.6 s. Only 4.22 s of that is model loading (VAD ONNX 2.310 + Faster-Whisper 1.908). The remaining 1.19 s is CTranslate2's first-decode cost - Whisper inference took 2.795 s cold and 1.605 s warm on identical audio. So a warmup that merely constructs the model objects would leave a fifth of the penalty in place. It has to push real audio through both stages, which is what `TS_WARMUP_MODELS=1` does. Load times also vary run to run (VAD 1.2-2.3 s, Whisper 1.5-1.9 s), which is why the roadmap item is quoted as a range rather than a number.

**What the warmup costs and what it saves are two different numbers, and only the cost is measured.** Three self-reports put the startup cost at 2.7-3.2 s (3080, 2704, 3234 ms). The 5.41 s figure above is an *upper bound* on what it removes from a first take, and it was itself measured cold, so the saving on a machine whose page cache is already warm has not been measured directly. The warmup also does not guarantee it warms on speech: it slices the first second of the newest normalized WAV, and the 41.6 s reference take begins with 1.04 s of silence (its first transcript cue is `00:00:01,040` and Whisper's own VAD filter keeps nothing before `00:01.040`). On that file the slice is silent, VAD reports 0 segments and Whisper returns 0 segments - and the warmup still reports `ok=True`, because the flag means "both engines were invoked", not "both warmed on speech". The whisper cost was 1503, 1653 and 1738 ms across the three runs, and the 0-segment run sits between the two 1-segment runs, so the dominant cost is clearly not segment-proportional - but that cannot distinguish "encoder ran, decoder skipped" from "both ran". `scripts/ab_warmup_first_take.py` measures it directly: each worker lifetime runs two takes and the comparison is the within-lifetime difference, so thermal state and page cache cancel instead of being argued about.

## Roadmap

| Area | Status |
|---|---|
| Live mic capture and WebSocket ingestion | Working |
| FFmpeg normalization | Working |
| Silero VAD segmentation | Working |
| Faster-Whisper transcription and language ID | Working |
| Real-time rolling preview | Working |
| Persistence (takes, segments, jobs) | Working |
| Retry, recovery and concurrency control | Working |
| File upload transcription | Working |
| Public API and per-user keys | Working |
| Model warmup on startup (`TS_WARMUP_MODELS`) and the `HF_OFFLINE` bridge | **Working, both opt-in.** Warmup costs 2.7-3.2 s at startup and moves the cold-load plus first-decode cost off the first take; the end-to-end first-take A/B is the one number still outstanding ([measured](#measured-performance)). The `HF_OFFLINE` bridge saves the 0.5-1.1 s Hub revision round trip and must stay unset until one run has populated the model cache. |
| `LOGGING` config (worker output currently lives only in a terminal) | **Working, and the row was stale.** `logs/worker.log` with a 5 MB x 3 rotating handler is attached in `tsworker.py` after `basicConfig` (commit `28ffb2c`), and the warmup timings land in it too - which was the one line that commit promised and missed, because `ready()` runs before any handler exists and `logging.lastResort` emits WARNING and above. Fixed by emitting the result from the command via `warmup.format_result()`. Deliberately NOT a `LOGGING` dict in `settings.py`: Django applies that at `setup()`, before `handle()`, and `basicConfig` would have been shadowed by it. |
| Orphan adoption on mic disconnect, into the durable queue | Next. **Must be in `disconnect`, not in the recovery sweep**: the mic layout is flat (`media/recordings/TS_REC_RAW_*`) with no user id in the path, so a sweep cannot attribute an orphan to an owner, while `disconnect` has `scope["user"]`. The `NOTE (6b)` comment in `consumers.py` says the sweep will adopt orphans; under the current layout it cannot. Landing this also moves the mic path onto the queue, which is why it rides with "Unified storage layout" below rather than ahead of it. |
| Unified storage layout (mic path moves to per-user dirs) | Next |
| Parallel upload stress test | Next |
| Wire the Whisper and live-preview knobs into `settings.py` | **Done** (commit `806b11d`). Twelve reads now assigned, defaults copied verbatim from the services' fallbacks so nothing changed for anyone who sets nothing. `WHISPER_DETERMINISTIC` deliberately left on its `os.environ` route, and the `vad_parameters` hardcode left as an open design question rather than quietly turned into a knob. |
| Delete / purge endpoint for takes | Planned |
| Argos Translate (offline NMT) | Planned |
| Piper TTS and voice avatars | Planned |
| Workspace file manager | Planned |
| PWA install and offline shell | Planned |
| Word-level alignment (VAD and Whisper timelines) | Planned |

## Known behavior

- **VAD segment count and transcript segment count differ.** That's by design; see [decision 9](#9-two-segment-timelines-are-stored-not-reconciled). Both timelines are stored and neither is authoritative over the other.
- **There are two VAD passes per take**: ours, and Faster-Whisper's internal `vad_filter`. They report different numbers for the same file. See [decision 10](#10-there-are-two-vad-passes-and-they-are-different-engines).
- **Language-ID confidence rises with take length.** On the `tiny` model I measured 0.68 and 0.79 on 26 to 27 s preview clips, 0.80 on 36 to 39 s clips, and 0.95 on the full 122 s take. The authoritative pass over the whole take after you stop is stable, and a larger model (`base` or above) or an explicit `language` locks it down further.
- **Re-runs may give a slightly different segment count.** Faster-Whisper (CTranslate2 int8) and its internal Silero VAD (ONNX) aren't bit-reproducible across runs on CPU. Thread scheduling changes the order of float reductions, which can flip a segment boundary. A retry converges to a structurally identical transcript (contiguous indices, no duplicates, same language, job `DONE`), but not necessarily a byte-identical one.
- **Cancelling a running job can still produce a transcript.** Cancellation is cooperative by design; see [decision 8](#8-cancellation-is-cooperative-and-that-is-a-choice).
- **The rate limiter fails open** if Redis is unavailable, and it uses a fixed window that can be gamed at a minute boundary. See [decision 15](#15-the-rate-limiter-is-a-fixed-window-and-it-fails-open).
- **The first run downloads the model** from the Hugging Face Hub (about 75 MB for `tiny`), and every cold model load makes a revision round trip (0.5 to 1.1 s measured). Set `HF_TOKEN` in `.env` to avoid anonymous rate limits.
- **Pressing Start twice without Stop drops the first take's persistence.** `handle_start` finalizes the open writer and tells the browser `saved`, but it does not create a `Take` or run the pipeline, and it clears `self._take`. The stop path and the size-cap path both do. So the audio is on disk, the UI believes it saved, and there is no transcript and no job row. Four finalization sites, two of them persist. Fixing it is a two-line change mirroring the cap branch.
- **Uploaded takes pile up on disk**, because there's no delete endpoint yet.
- **Revoked API keys stay listed** on the account page (there's no purge), so a heavily tested instance collects rows.
- **The microphone and upload paths use different on-disk layouts** today, and the microphone path doesn't go through the durable queue yet. Both are open work on the roadmap.
- **A malformed knob value stops every process, by design.** The Whisper, live-preview, worker and recovery reads in `settings.py` are unguarded `int()`/`float()` conversions, so `WORKER_POLL_INTERVAL=two` raises `ValueError` at settings import and kills Daphne, the worker and `manage.py check` alike. This matches `VAD_MIN_SILENCE_MS`, which has behaved that way since Phase 4. The alternative - falling back to the default - was rejected because a silently-ignored typo is the worse failure: nobody would ever learn their `.env` was broken. `LIVE_TRANSCRIBE_EVERY_CHUNKS` is the one exception; the service wraps that read in try/except and degrades to 30. The superseded note here said the five knobs might be ignored by `.env`; as of commit `806b11d` they are read, which is why the note had to change.

## Contributing

Issues and pull requests are welcome. For anything big, please open an issue first so we can agree on scope. The pipeline contract (16 kHz mono PCM in, timestamped segments out) is stable and shouldn't be broken casually.

1. Fork and clone the repo.
2. Run `pip install -r requirements.txt`.
3. Keep the NumPy-decode invariant: never pass a file path to `model.transcribe`.
4. Keep the CSRF posture: don't re-enable CSRF checking on `/api/v1/`.
5. Keep one entry point per job. If you catch yourself copying a block, extract it instead.
6. Run the verification suite above, and make sure `green_gate.py` passes before you claim a suite is green.
7. Add or extend verification for any new stage.
8. Open a PR with a clear description of how it affects the pipeline.

## Sponsor

TranStream is built and maintained in the open. Sponsorship pays for development time, model-hosting experiments and infrastructure.

[![Sponsor S-V-J](https://img.shields.io/badge/Sponsor-%E2%9D%A4-ea4aaa?logo=github-sponsors&logoColor=white)](https://github.com/sponsors/S-V-J)

You can sponsor the project at [github.com/sponsors/S-V-J](https://github.com/sponsors/S-V-J). The repository-wide **Sponsor** button is enabled through [`.github/FUNDING.yml`](.github/FUNDING.yml).

## License

Distributed under the MIT License. See [`LICENSE`](LICENSE) for details.

## Repository

| | |
|---|---|
| HTTPS | `https://github.com/S-V-J/transtream.git` |
| SSH | `git@github.com:S-V-J/transtream.git` |
| Default branch | `main` |
| Author | Siddhant Kumar, [github.com/S-V-J](https://github.com/S-V-J) |

## About the author

I'm Siddhant Kumar, a full-stack developer who also does VoIP and telephony engineering, AI work and technical support. I'm based in Bihar, India, and I'm looking for remote roles anywhere in the world (full-time, up to 40 hours a week).

| | |
|---|---|
| Email | [stjl093@gmail.com](mailto:stjl093@gmail.com) |
| GitHub | [github.com/S-V-J](https://github.com/S-V-J) |
| LinkedIn | [linkedin.com/in/sid-093](https://linkedin.com/in/sid-093) (employment, education and certificates can be verified there) |
| Phone | +91 8095875948 |

> I've listed my phone number because this README goes along with a job application. If you'd rather not have it publicly indexed, delete that row. Nothing else in the file depends on it.

TranStream isn't the first time I've pushed audio over a wire, or the first time I've wired up Whisper. It comes out of about four years of telephony work:

- **TELUS Digital (Jan 2022 to Aug 2025, remote), Network Associate.** I programmed, tested and troubleshot GTD 5 and DMS 100 telephony switches from the command line, and diagnosed SIP and ISUP call-setup and trunking failures from `.pcap` captures using IRIS, CGIS and Wireshark. That job taught me to treat audio as a stream with a strict contract at its boundary. It also taught me that the interesting bugs are almost always in negotiation and framing, not in the payload.
- **Basal Analytics (Dec 2025), VoIP AI Integration Engineer.** I configured Asterisk with the AudioSocket module and wrote a Python WebSocket client that bridged live calls into a voice-AI engine, so answered outbound calls were handed to the AI on pickup. That is the same shape as TranStream's microphone path (a live audio stream crossing a socket into a model), just on a different wire.
- **Raiva, Germany (Oct 2025 to Jan 2026), AI Backend Developer.** I built document indexing and search that could be queried by text or by live voice, using Whisper for speech-to-text and conversational AI for grounded answers, deployed on Hetzner. TranStream is roughly what that work turned into once I decided I didn't want the audio to leave the machine.
- **Lancelot Technology (Aug to Sep 2025) and IWALINK SA, Switzerland (May to Jun 2025), VoIP / PBX Engineer.** Asterisk and Kamailio builds, SIP trunking, and multilingual IVR with voice and language detection. So automatic language identification isn't new to me, and I know how fragile it gets on short utterances. The [Known behavior](#known-behavior) section measures that fragility instead of hiding it.

**Education and research.** B.E. in Electronics and Communication Engineering, VTU Belagavi (2023). I co-authored "Intelligent Line Follower Robot using MSP430G2ET for Industrial Applications" in *Helix* Vol. 10(2), pp. 232-237, April 2020 ([DOI 10.29042/2020-10-2-232-237](https://doi.org/10.29042/2020-10-2-232-237)), which was presented at the AICTE-sponsored ISCCS 2019. I hold certifications in generative AI and LLM application development (Udemy, 2024), cybersecurity (Cisco, 2024) and computer networking (Google, 2020). My English is C1+ professional, including work with Canadian B2B and B2C clients, and Hindi is my first language.

**Why local-first.** After years of moving voice across SIP trunks and RTP streams, I like knowing exactly where the audio is at every moment. In TranStream it's on your disk, in your SQLite file, and in a model running on your CPU. There's no cloud API, no telemetry and no key to manage.

Other projects: [OminiVoice](https://github.com/S-V-J) (WebRTC voice workbench on Pipecat/FastRTC), [PBX-Platform](https://github.com/S-V-J) (12-microservice Asterisk/Kamailio core), [Sasyashri](https://github.com/S-V-J) (multi-tenant marketplace with NVIDIA Riva voice agents), [MyCode](https://github.com/S-V-J) (agentic coding CLI using Tree-sitter and ChromaDB), [AetherAgent](https://github.com/S-V-J) (fully local, LoRA-tuned decoder with SSE streaming), [FuelRoute Pro](https://github.com/S-V-J), [Spotter ML](https://github.com/S-V-J), [MyClaude](https://github.com/S-V-J) (LiteLLM failover cluster) and [Zero to Hero](https://github.com/S-V-J) (open-source SIP/WebRTC and OWASP curriculum).

---
