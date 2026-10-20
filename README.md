# TranStream

TranStream is a local-first audio studio for real-time microphone transcription, uploaded audio transcription, language detection, translation, and text-to-speech playback.

## Core Vision

TranStream is designed to run entirely on the user's machine.

The application should support:

- Live microphone ingestion over WebSocket
- 100 ms binary audio chunk streaming
- Automatic language detection
- English transcription by default
- Native-script toggle for detected language
- Segment-level timestamps
- Word-level timestamps where supported
- Long audio / infinite stream processing
- VAD-based speech segmentation
- FFmpeg normalization to 16 kHz, 16-bit, mono PCM
- Faster-Whisper transcription
- Argos Translate offline translation
- Piper TTS local voice playback
- Upload support for common audio formats
- Workspace file manager
- Settings page with storage administration
- PWA installation support

## Supported Audio Inputs

Planned supported upload formats:

- `.mp3`
- `.wav`
- `.m4a`
- `.flac`
- `.ogg`
- `.aac`
- `.wma`

## Naming Convention

Recording filename format:

```text
TS_REC_[ISO_LANG]_[YYYYMMDD_HHMMSS].[ext]
```

Example:

```text
TS_REC_SPA_20261020_154522.wav
```

## Tech Stack

Backend:

- Django
- Django Channels
- Daphne ASGI server
- Redis channel layer

Audio / AI:

- Faster-Whisper
- Silero VAD
- FFmpeg
- Pydub

Translation:

- Argos Translate

Text-to-Speech:

- Piper

Frontend:

- HTMX
- Alpine.js
- Tailwind CSS
- Progressive Web App

## Repository

GitHub:

```text
https://github.com/S-V-J/transtream
```

SSH:

```text
git@github.com:S-V-J/transtream.git
```

## Development Model

This project is being developed manually through guided full-stack engineering steps.
