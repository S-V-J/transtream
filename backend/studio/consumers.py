import json
import logging
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from asgiref.sync import sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.conf import settings

from studio.audio_service import (
    AudioNormalizationError,
    normalize_audio,
    normalized_output_path,
)
from studio.vad_service import VADError, extract_speech_segments
from studio.transcription_service import TranscriptionError, transcribe_audio

logger = logging.getLogger(__name__)

MIME_EXTENSIONS = {
    "audio/webm": ".webm",
    "audio/ogg": ".ogg",
    "audio/mp4": ".m4a",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/flac": ".flac",
    "audio/aac": ".aac",
}


def extension_for_mime(mime):
    mime = (mime or "").lower()
    for prefix, ext in MIME_EXTENSIONS.items():
        if mime.startswith(prefix):
            return ext
    return ".bin"


def unique_recording_path(directory, prefix, ext):
    directory = Path(directory)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    candidate = directory / f"{prefix}_{stamp}{ext}"
    counter = 1
    while candidate.exists():
        candidate = directory / f"{prefix}_{stamp}_{counter}{ext}"
        counter += 1
    return candidate


class SessionWriter:
    def __init__(self, directory, prefix, mime):
        self.directory = Path(directory)
        self.prefix = prefix
        self.mime = mime or ""
        self.ext = extension_for_mime(self.mime)
        self.temp_path = None
        self.final_path = None
        self.file_handle = None
        self.bytes_written = 0
        self.chunk_count = 0
        self.started_monotonic = time.monotonic()
        self.finished_monotonic = None
        self.closed = False

    @sync_to_async
    def open(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=".ts_part_",
            suffix=self.ext,
            dir=str(self.directory),
        )
        self.temp_path = Path(temp_name)
        self.file_handle = os.fdopen(fd, "wb")

    @sync_to_async
    def write_chunk(self, data):
        if self.closed or self.file_handle is None:
            return
        self.file_handle.write(data)
        self.bytes_written += len(data)
        self.chunk_count += 1

    @sync_to_async
    def finalize(self):
        if self.closed:
            return self.as_dict()
        if self.file_handle is not None:
            try:
                self.file_handle.flush()
                os.fsync(self.file_handle.fileno())
            finally:
                self.file_handle.close()
                self.file_handle = None
        self.closed = True
        self.finished_monotonic = time.monotonic()
        if self.bytes_written == 0:
            if self.temp_path is not None:
                self.temp_path.unlink(missing_ok=True)
            self.final_path = None
        else:
            self.final_path = unique_recording_path(self.directory, self.prefix, self.ext)
            if self.temp_path is not None:
                self.temp_path.replace(self.final_path)
        return self.as_dict()

    def current_duration_ms(self):
        end = self.finished_monotonic if self.finished_monotonic is not None else time.monotonic()
        return int((end - self.started_monotonic) * 1000)

    def as_dict(self):
        url = ""
        filename = ""
        abs_path = ""
        if self.final_path is not None:
            filename = self.final_path.name
            abs_path = str(self.final_path)
            try:
                relative = self.final_path.relative_to(Path(settings.MEDIA_ROOT))
            except ValueError:
                relative = Path("recordings") / self.final_path.name
            media_url = settings.MEDIA_URL
            if not media_url.startswith("/"):
                media_url = "/" + media_url
            url = f"{media_url}{relative.as_posix()}"
        return {
            "filename": filename,
            "path": abs_path,
            "url": url,
            "bytes": self.bytes_written,
            "chunks": self.chunk_count,
            "duration_ms": self.current_duration_ms(),
            "mime": self.mime,
        }


class AudioConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.session_id = self.scope["url_route"]["kwargs"]["session_id"]
        self.group_name = f"studio_{self.session_id}"
        self.writer = None
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()
        await self._safe_send({
            "type": "connected",
            "session_id": self.session_id,
            "message": "TranStream WebSocket consumer is ready.",
            "max_bytes": settings.STUDIO_MAX_TAKE_BYTES,
        })

    async def disconnect(self, close_code):
        if hasattr(self, "group_name"):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)
        if self.writer is not None and not self.writer.closed:
            info = await self.writer.finalize()
            if info["filename"]:
                logger.info("Saved partial recording on disconnect: %s", info["filename"])

    async def _safe_send(self, payload):
        try:
            await self.send(text_data=json.dumps(payload))
        except Exception:
            pass

    async def receive(self, text_data=None, bytes_data=None):
        if text_data is not None:
            await self.receive_control(text_data)
        elif bytes_data is not None:
            await self.receive_audio(bytes_data)

    async def receive_control(self, text_data):
        try:
            payload = json.loads(text_data)
        except json.JSONDecodeError:
            await self._safe_send({"type": "error", "message": "Invalid JSON control message."})
            return
        control_type = payload.get("type")
        if control_type == "start":
            await self.handle_start(payload)
        elif control_type == "stop":
            await self.handle_stop(payload)
        else:
            await self._safe_send({"type": "error", "message": f"Unsupported control type: {control_type}"})

    async def handle_start(self, payload):
        if self.writer is not None and not self.writer.closed:
            old_info = await self.writer.finalize()
            if old_info["filename"]:
                await self._safe_send({"type": "saved", **old_info})
        mime = payload.get("mime") or "audio/webm"
        self.writer = SessionWriter(directory=settings.RECORDINGS_DIR, prefix="TS_REC_RAW", mime=mime)
        await self.writer.open()
        await self._safe_send({
            "type": "started",
            "session_id": self.session_id,
            "mime": self.writer.mime,
            "extension": self.writer.ext,
            "max_bytes": settings.STUDIO_MAX_TAKE_BYTES,
        })

    async def handle_stop(self, payload):
        if self.writer is None or self.writer.closed:
            await self._safe_send({"type": "error", "message": "No active recording to stop."})
            return
        info = await self.writer.finalize()
        self.writer = None
        await self._safe_send({
            "type": "stats",
            "chunks": info["chunks"],
            "bytes": info["bytes"],
            "duration_ms": info["duration_ms"],
        })
        if info["filename"]:
            await self._safe_send({"type": "saved", **info})
            if info.get("path"):
                await self._run_post_processing(Path(info["path"]))
        else:
            await self._safe_send({"type": "error", "message": "Recording stopped, but no audio data was received."})

    async def _run_post_processing(self, raw_path: Path):
        # Stage 1: FFmpeg normalization
        output_path = normalized_output_path(raw_path)
        await self._safe_send({
            "type": "normalizing",
            "input_file": raw_path.name,
            "output_file": output_path.name,
            "message": f"Normalizing to {settings.AUDIO_SAMPLE_RATE} Hz, {settings.AUDIO_CHANNELS} ch, 16-bit PCM WAV…",
        })
        try:
            norm_result = await normalize_audio(raw_path, output_path)
            await self._safe_send({"type": "normalized", **norm_result})
        except AudioNormalizationError as exc:
            logger.warning("Normalization failed for %s: %s", raw_path.name, exc)
            await self._safe_send({"type": "error", "message": f"Normalization failed: {exc}", "stage": "normalization", "raw_saved": True, "raw_file": raw_path.name})
            return
        except Exception as exc:
            logger.exception("Unexpected error during normalization of %s", raw_path.name)
            await self._safe_send({"type": "error", "message": f"Normalization error: {exc}", "stage": "normalization", "raw_saved": True, "raw_file": raw_path.name})
            return

        # Stage 2: Silero VAD segmentation
        await self._trigger_vad(output_path)

        # Stage 3: Faster-Whisper Transcription
        await self._trigger_transcription(output_path)

    async def _trigger_vad(self, wav_path: Path):
        await self._safe_send({
            "type": "vad_started",
            "input_file": wav_path.name,
            "message": "Detecting speech segments with Silero VAD…",
        })
        try:
            segments = await sync_to_async(extract_speech_segments, thread_sensitive=True)(wav_path)
            total_speech_ms = sum(s["duration_ms"] for s in segments)
            await self._safe_send({
                "type": "vad_complete",
                "input_file": wav_path.name,
                "segment_count": len(segments),
                "total_speech_ms": total_speech_ms,
                "segments": segments,
            })
        except VADError as exc:
            logger.warning("VAD failed for %s: %s", wav_path.name, exc)
            await self._safe_send({"type": "error", "message": f"VAD segmentation failed: {exc}", "stage": "vad", "normalized_saved": True, "normalized_file": wav_path.name})
        except Exception as exc:
            logger.exception("Unexpected VAD error for %s", wav_path.name)
            await self._safe_send({"type": "error", "message": f"VAD error: {exc}", "stage": "vad", "normalized_saved": True, "normalized_file": wav_path.name})

    async def _trigger_transcription(self, wav_path: Path):
        await self._safe_send({
            "type": "transcribing_started",
            "input_file": wav_path.name,
            "message": "Transcribing with Faster-Whisper…",
        })
        try:
            result = await sync_to_async(transcribe_audio, thread_sensitive=True)(wav_path)
            await self._safe_send({
                "type": "transcribing_complete",
                "input_file": wav_path.name,
                "language": result["language"],
                "language_probability": result["language_probability"],
                "segments": result["segments"],
            })
        except TranscriptionError as exc:
            logger.warning("Transcription failed for %s: %s", wav_path.name, exc)
            await self._safe_send({"type": "error", "message": f"Transcription failed: {exc}", "stage": "transcription", "normalized_saved": True, "normalized_file": wav_path.name})
        except Exception as exc:
            logger.exception("Unexpected transcription error for %s", wav_path.name)
            await self._safe_send({"type": "error", "message": f"Transcription error: {exc}", "stage": "transcription", "normalized_saved": True, "normalized_file": wav_path.name})

    async def receive_audio(self, data):
        if self.writer is None or self.writer.closed:
            await self._safe_send({"type": "error", "message": "Audio received before start control message."})
            return
        incoming_size = len(data)
        projected_size = self.writer.bytes_written + incoming_size
        if projected_size > settings.STUDIO_MAX_TAKE_BYTES:
            info = await self.writer.finalize()
            self.writer = None
            await self._safe_send({"type": "error", "message": "Maximum recording size reached. Partial audio saved.", "saved": info})
            if info.get("path"):
                await self._run_post_processing(Path(info["path"]))
            return
        await self.writer.write_chunk(data)
        every = getattr(settings, "STUDIO_STATS_EVERY_CHUNKS", 20)
        if self.writer.chunk_count % every == 0:
            await self._safe_send({
                "type": "stats",
                "chunks": self.writer.chunk_count,
                "bytes": self.writer.bytes_written,
                "duration_ms": self.writer.current_duration_ms(),
            })
