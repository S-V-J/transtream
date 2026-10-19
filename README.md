# TranStream

TranStream is a local-first audio studio for real-time microphone transcription, uploaded audio transcription, language detection, translation, and text-to-speech playback.

## Planned Capabilities

- Live microphone ingestion over WebSocket using 100 ms audio chunks
- Automatic language detection
- English transcription by default with native-script toggle
- Segment-level and word-level timestamps
- Long audio / infinite stream processing using VAD-based chunking
- FFmpeg normalization to 16 kHz, 16-bit, mono PCM
- Faster-Whisper transcription engine
- Argos Translate for offline translation
- Piper TTS for local voice avatar playback
- Upload support for common audio formats:
  - `.mp3`
  - `.wav`
  - `.m4a`
  - `.flac`
  - `.ogg`
  - `.aac`
  - `.wma`
- Workspace file manager with folders and transcription files
- Settings page with storage usage and file administration
- PWA installation support for desktop and mobile

## Tech Stack

- Backend: Django 5.1 + Django Channels
- ASGI Server: Daphne
- Realtime Broker: Redis
- Speech-to-Text: Faster-Whisper
- Voice Activity Detection: Silero VAD
- Audio Processing: FFmpeg + Pydub
- Translation: Argos Translate
- Text-to-Speech: Piper
- Frontend: HTMX + Alpine.js + Tailwind CSS
- Delivery: Progressive Web App

## Repository

GitHub: https://github.com/S-V-J/transtream

## Development Status

Phase 0: repository initialized.

Claude Code will implement the application according to `prd.md`.

## License

This project is licensed under the MIT License. See the LICENSE file for details.
