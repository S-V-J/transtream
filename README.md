# TranStream

<p align="center">
  <strong>Local-first audio studio: live microphone transcription, file upload transcription, language detection, translation, and text-to-speech — entirely on your machine.</strong>
</p>

<p align="center">
  <a href="LICENSE"><img alt="License" src="https://img.shields.io/badge/license-TBD-informational"></a>
  <img alt="Python" src="https://img.shields.io/badge/python-3.12%20%7C%203.14-blue?logo=python&logoColor=white">
  <img alt="Django" src="https://img.shields.io/badge/django-6.1-092e20?logo=django&logoColor=white">
  <img alt="Channels" src="https://img.shields.io/badge/channels-websocket-2496ed">
  <img alt="Whisper" src="https://img.shields.io/badge/stt-Faster--Whisper%20(CTranslate2)-7c3aed">
  <a href="https://github.com/sponsors/S-V-J"><img alt="Sponsor" src="https://img.shields.io/badge/Sponsor-❤-ea4aaa?logo=github-sponsors&logoColor=white"></a>
</p>

---

## What is this?

TranStream is a self-hosted speech studio that runs **100% offline**. Audio never leaves your machine: microphone capture streams over a local WebSocket, is normalized with FFmpeg, segmented with Silero VAD, transcribed with Faster-Whisper, and (once translation/TTS land) translated with Argos Translate and voiced with Piper — all on CPU, no cloud API, no telemetry.

The project is **actively in development**. The live microphone pipeline (capture → normalize → VAD → transcribe → real-time preview) is working end-to-end. Persistence, file upload, translation, TTS, and the public API are in progress. See [Roadmap](#roadmap).

## Highlights (working today)

- **Live microphone ingestion** over WebSocket in 100 ms binary chunks, with incremental on-disk buffering and graceful partial-save on disconnect.
- **Real-time rolling transcript preview** while you speak — a frozen snapshot of the growing take is converted and re-transcribed on an isolated thread, so preview can never stall or drop the live connection.
- **FFmpeg normalization** of any browser/container format to canonical **16 kHz · mono · 16-bit PCM WAV**, the single input contract every downstream stage relies on.
- **Silero VAD (ONNX runtime)** speech segmentation with millisecond timestamps — ~30 MB footprint, no PyTorch/torch in the hot path.
- **Faster-Whisper (CTranslate2, int8, CPU)** transcription with **automatic language detection** and confidence scoring.
- **Dual timeline**: acoustic (VAD) segments and linguistic (Whisper) segments are both computed and surfaced, ready for word-level alignment.
- **Auth-gated studio** (Django sessions), dark Tailwind UI driven by Alpine.js, and a systemd-managed dev server (`transtream` CLI) with Redis channel layer.

## Architecture

```
┌─────────────┐   100 ms binary    ┌──────────────────────────────────────────────┐
│  Browser     │ ─────────────────▶ │  Django Channels  ·  AudioConsumer (ASGI)     │
│  MediaRecorder│  WebSocket frames │   • SessionWriter: incremental temp file      │
│  (Opus/WebM) │ ◀───────────────── │   • live preview: snapshot → ffmpeg → whisper │
└─────────────┘   JSON control/stats│   • post-stop pipeline (below)                │
                                    └──────────────────────────────────────────────┘
                                                        │
        raw .webm ──▶ ① FFmpeg normalize ──▶ ② Silero VAD ──▶ ③ Faster-Whisper ──▶ transcript
                     16k/mono/16-bit WAV     timed segments    text + language      + timestamps
```

| Stage | Component | Output |
|---|---|---|
| ① Normalize | FFmpeg (subprocess, async) | `*_NRM_*.wav` — 16 kHz mono 16-bit PCM |
| ② Segment | Silero VAD (ONNX) | `[{start_ms, end_ms, duration_ms}, …]` |
| ③ Transcribe | Faster-Whisper (CTranslate2) | `[{start_ms, end_ms, text}, …]` + `language`, `probability` |
| ④ Preview | snapshot + ① + ③ (beam=1, isolated thread) | rolling interim text during recording |

**Design invariant:** transcription is always fed a **NumPy `float32` array decoded by `soundfile`**, never a file path. Passing a path would route through PyAV (`av.open`) and re-introduce container-demux fragility on partially-written files. This invariant is enforced by an AST guard in CI-style verification.

## Tech stack

| Layer | Choice |
|---|---|
| Backend | Django 6.1 · Django Channels · Daphne (ASGI) · Redis channel layer |
| Audio | FFmpeg · soundfile/libsndfile · Silero VAD (ONNX Runtime) · Faster-Whisper (CTranslate2) |
| Translation *(planned)* | Argos Translate (offline NMT) |
| TTS *(planned)* | Piper (local neural voices) |
| Frontend | Alpine.js · Tailwind CSS · HTMX · PWA |
| Tooling | `transtream` CLI (systemd user units) · Python venv |

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

> The `libav*-dev` packages are required because `faster-whisper` imports PyAV at module load. On very new Python releases (3.14) prebuilt `av` wheels may not exist yet, so PyAV must compile from source against these headers. At *runtime* TranStream never calls PyAV (see the design invariant above) — it only needs to be importable.

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
# edit .env as needed — sensible defaults are baked into the code
```

### 4. Database + superuser

```bash
cd backend
python manage.py migrate
python manage.py createsuperuser
cd ..
```

### 5. Run

```bash
# Option A — managed dev server (systemd user unit + Redis + dynamic port)
transtream restart
transtream status
transtream logs
cat logs/transtream.port        # the bound port (e.g. 8003)

# Option B — plain ASGI run
cd backend
daphne -b 127.0.0.1 -p 8003 transtream.asgi:application
```

Open **`http://127.0.0.1:8003/`**, log in, go to **Studio**, click **🎤 Start**, speak, click **⏹️ Stop**.

> Microphone capture requires a **secure context**. `http://localhost` / `http://127.0.0.1` qualify; a LAN IP over plain HTTP will not (the browser blocks `getUserMedia`).

## Configuration

All values are read with safe defaults (`getattr(settings, KEY, default)`), so the app runs with an empty `.env`.

| Variable | Default | Purpose |
|---|---|---|
| `WHISPER_MODEL_SIZE` | `tiny` | Faster-Whisper model (`tiny`/`base`/`small`/`medium`/`large-v3`). `base`+ recommended for reliable language ID on short clips. |
| `WHISPER_DEVICE` | `cpu` | `cpu` or `cuda`. |
| `WHISPER_COMPUTE_TYPE` | `int8` | CTranslate2 precision (`int8`, `float16`, `int8_float16`…). |
| `LIVE_TRANSCRIBE_EVERY_CHUNKS` | `30` | Binary frames between live-preview runs. `≤0` disables preview with **no code change**. |
| `AUDIO_SAMPLE_RATE` | `16000` | Normalization target rate. |
| `AUDIO_CHANNELS` | `1` | Normalization target channel count. |
| `FFMPEG_BIN` | `ffmpeg` | FFmpeg binary path. |
| `FFMPEG_TIMEOUT` | `300` | Per-conversion subprocess timeout (seconds). |
| `STUDIO_MAX_TAKE_BYTES` | `512 MB` | Hard cap per take; force-saves and stops on exceed. |
| `STUDIO_STATS_EVERY_CHUNKS` | `20` | Cadence of live stats pushed to the browser. |

## Naming convention

On-disk capture artifacts (current implementation):

```text
TS_REC_RAW_<YYYYMMDD_HHMMSS>.<ext>     # verbatim browser/upload capture
TS_REC_NRM_<YYYYMMDD_HHMMSS>.wav       # normalized 16 kHz mono 16-bit PCM
```

Timestamps are **UTC**. Persisted takes (Phase 6) move to a per-user, per-take directory layout:

```text
media/recordings/<user_id>/<take_id>/{raw.<ext>, normalized.wav, transcript.{json,srt,vtt}}
```

## API *(in progress)*

A single fixed base URL serves all users; identity comes from a per-user bearer key minted on the **Account** page (shown once, stored hashed, revocable).

```text
POST   /api/v1/transcriptions                 multipart upload → 202 {job_id}
GET    /api/v1/jobs/{id}                       status + progress
GET    /api/v1/jobs/{id}/transcript?format=…   json | srt | vtt | txt
DELETE /api/v1/jobs/{id}                       cancel
```

```bash
curl -X POST http://127.0.0.1:8003/api/v1/transcriptions \
  -H "Authorization: Bearer ts_live_XXXXXXXXXXXXXXXXXXXX" \
  -F "file=@meeting.mp3" \
  -F "language=en"          # optional hint; omit for auto-detection
```

## Roadmap

| Area | Status |
|---|---|
| Live mic capture + WebSocket ingestion | ✅ Working |
| FFmpeg normalization | ✅ Working |
| Silero VAD segmentation | ✅ Working |
| Faster-Whisper transcription + language ID | ✅ Working |
| Real-time rolling preview | ✅ Working |
| Persistence (takes / segments / jobs) | 🚧 In progress |
| Retry / recovery / concurrency control | 🚧 In progress |
| File upload transcription | 🚧 In progress |
| Argos Translate (offline NMT) | ⏳ Planned |
| Piper TTS + voice avatars | ⏳ Planned |
| Public API + per-user keys | ⏳ Planned |
| Workspace file manager | ⏳ Planned |
| PWA install + offline shell | ⏳ Planned |

## Known behavior

- **VAD segment count ≠ transcript segment count.** Silero segments the *acoustic* signal; Whisper segments the *linguistic* stream over the whole take and merges across short silences. Both timelines are intentional and will be aligned for word-level timestamps.
- **Language ID can flip on very short live-preview clips** (< ~15 s) with the `tiny` model. The authoritative post-stop pass over the full take is stable; raise `WHISPER_MODEL_SIZE` to `base` or pass an explicit `language` to lock it.
- **First run downloads the model** from the Hugging Face Hub (~75 MB for `tiny`). Set `HF_TOKEN` in `.env` to avoid anonymous rate limits.

## Contributing

Issues and pull requests are welcome. For large changes, open an issue first to align on scope — the pipeline contract (16 kHz mono PCM in, timestamped segments out) is stable and should not be broken casually.

1. Fork and clone.
2. `pip install -r requirements.txt`.
3. Keep the NumPy-decode invariant: never pass a file path to `model.transcribe`.
4. Add/extend verification for any new stage.
5. Open a PR with a clear description of the pipeline impact.

## Sponsor

TranStream is built and maintained in the open. Sponsorship funds development time, model hosting experiments, and infrastructure.

<a href="https://github.com/sponsors/S-V-J">
  <img src="https://img.shields.io/badge/Sponsor-❤-ea4aaa?logo=github-sponsors&logoColor=white" alt="Sponsor S-V-J">
</a>

Sponsorships support the project at **[github.com/sponsors/S-V-J](https://github.com/sponsors/S-V-J)**. The repository-wide **Sponsor** button is enabled via [`.github/FUNDING.yml`](.github/FUNDING.yml).

## License

No `LICENSE` file is present in the repository as of this writing, which means **all rights are reserved by default** and the code is not formally open source yet. A permissive license (MIT or Apache-2.0) is planned. Until one is added, do not redistribute. Track this in the issue tracker.

## Repository

| | |
|---|---|
| HTTPS | `https://github.com/S-V-J/transtream.git` |
| SSH | `git@github.com:S-V-J/transtream.git` |
| Default branch | `main` |
