"""
TranStream upload ingest service (Phase 7).

Handles the multipart file upload path: browser (or API client) POSTs an
audio file; we validate, stream to disk, and enqueue a Job for the worker.

Design rules:
  * STREAMED: the file is written in 64 KB chunks via the UploadedFile's
    chunks() iterator. We NEVER call .read() on the whole body. A 100 MB
    upload must not allocate 100 MB of RAM.
  * VALIDATED BEFORE WRITE: extension whitelist + MIME sniff (magic bytes)
    + size cap. Reject early, reject cheaply. No partial files on disk
    after a rejection.
  * DETERMINISTIC PATH: files land in
      media/recordings/<user_id>/uploads/<take_id>/raw.<ext>
    This is the per-user, per-take layout promised in the README. The
    worker's process_take() reads raw_relpath from the Take row, so the
    path convention is the contract between upload_service and pipeline.
  * NON-FATAL PERSISTENCE: create_upload_take() is decorated _best_effort;
    if the DB write fails, the file is still on disk and we return the
    take_id as None. The caller (view) reports 500 with a clear message.
    We do NOT delete the file on DB failure — the recovery sweep or a
    manual admin action can adopt it later.
  * ONE ENTRY POINT: handle_upload() is the single function both the
    studio view (Phase 7) and the API view (Phase 8) call. This prevents
    the front-door drift class that Phase 6b-fix eliminated for FFmpeg.

Extension whitelist (matches the PRD's "any codec" requirement):
  .mp3  audio/mpeg
  .wav  audio/wav, audio/x-wav
  .m4a  audio/mp4
  .flac audio/flac
  .ogg  audio/ogg
  .aac  audio/aac
  .wma  audio/x-ms-wma
  .webm audio/webm  (browser MediaRecorder output, also valid for upload)
"""

import logging
import os
import re
import uuid
from pathlib import Path

from django.conf import settings
from django.core.files.uploadedfile import UploadedFile

from studio import persistence
from studio.models import Take, TakeKind

logger = logging.getLogger(__name__)

# ─── Constants ───────────────────────────────────────────────────────────

ALLOWED_EXTENSIONS = frozenset({
    ".mp3", ".wav", ".m4a", ".flac", ".ogg", ".aac", ".wma", ".webm",
})

# Magic-byte signatures for the most common containers. This is NOT a
# full MIME detector — it catches the "rename .exe to .mp3" class of
# abuse and the "browser sent the wrong Content-Type" class of accident.
# FFmpeg will reject truly invalid files at normalize time (Job → FAILED).
_MAGIC_SIGNATURES = {
    b"ID3": ".mp3",           # MP3 with ID3 tag
    b"\xff\xfb": ".mp3",      # MP3 frame sync (no ID3)
    b"\xff\xf3": ".mp3",      # MP3 frame sync (MPEG-2)
    b"\xff\xf2": ".mp3",      # MP3 frame sync (MPEG-2.5)
    b"RIFF": ".wav",          # WAV (RIFF container)
    b"fLaC": ".flac",         # FLAC
    b"OggS": ".ogg",          # OGG
    b"\x00\x00\x00\x18ftyp": ".m4a",  # MP4/M4A (iso base media)
    b"\x00\x00\x00\x20ftyp": ".m4a",  # MP4 variant
    b"\x1a\x45\xdf\xa3": ".webm",     # Matroska/WebM (EBML header)
    b"\xff\xf1": ".aac",      # ADTS AAC
    b"\xff\xf9": ".aac",      # ADTS AAC (explicit)
    b"\x30\x26\xb2\x75": ".wma",      # ASF/WMA
}

# Size cap: 100 MB. Read from settings with a safe default.
_DEFAULT_MAX_BYTES = 100 * 1024 * 1024

# Stream chunk size: 64 KB. Large enough for throughput, small enough
# to never spike memory on a 100 MB upload.
_CHUNK_SIZE = 64 * 1024


# ─── Validation ──────────────────────────────────────────────────────────

class UploadValidationError(Exception):
    """Raised when an upload is rejected before any bytes hit disk."""
    def __init__(self, message: str, code: str = "invalid_upload"):
        super().__init__(message)
        self.code = code


def _validate_extension(filename: str) -> str:
    """Return the lowercased extension, or raise."""
    ext = Path(filename or "").suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise UploadValidationError(
            f"Extension '{ext or '(none)'}' not allowed. "
            f"Accepted: {', '.join(sorted(ALLOWED_EXTENSIONS))}",
            code="bad_extension",
        )
    return ext


def _validate_magic(first_bytes: bytes, declared_ext: str) -> None:
    """
    Check the first 12 bytes against known audio container signatures.
    If the magic matches a KNOWN format, it must match the declared
    extension (catches rename-abuse). If the magic is UNKNOWN, we allow
    it (FFmpeg will sort it out at normalize time; some rare codecs have
    no simple magic).
    """
    for sig, expected_ext in _MAGIC_SIGNATURES.items():
        if first_bytes[:len(sig)] == sig:
            if expected_ext != declared_ext:
                raise UploadValidationError(
                    f"File content looks like '{expected_ext}' but "
                    f"extension is '{declared_ext}'. Rename or re-export.",
                    code="magic_mismatch",
                )
            return  # known format, matches extension → OK
    # Unknown magic → permissive. FFmpeg validates at normalize.
    logger.debug("Upload magic unknown (%s…), allowing through", first_bytes[:4].hex())


def _validate_size(uploaded: UploadedFile) -> int:
    """Return the file size, or raise if over cap or zero."""
    max_bytes = int(getattr(settings, "UPLOAD_MAX_BYTES", _DEFAULT_MAX_BYTES))
    size = uploaded.size
    if size is None:
        # Some chunked uploads don't report size upfront; we'll check
        # during streaming. Allow through with a sentinel.
        return -1
    if size == 0:
        raise UploadValidationError("File is empty (0 bytes).", code="empty_file")
    if size > max_bytes:
        raise UploadValidationError(
            f"File size {size:,} bytes exceeds the {max_bytes:,}-byte cap.",
            code="too_large",
        )
    return size


# ─── Path construction ──────────────────────────────────────────────────

def _upload_dir(user_id: int, take_id: uuid.UUID) -> Path:
    """
    Deterministic per-user, per-take upload directory:
      media/recordings/<user_id>/uploads/<take_id>/
    Created with mkdir(parents=True, exist_ok=True) at write time.
    """
    return Path(settings.MEDIA_ROOT) / "recordings" / str(user_id) / "uploads" / str(take_id)


def _raw_filename(ext: str) -> str:
    """Stable filename inside the take directory."""
    return f"raw{ext}"


# ─── Streaming write ─────────────────────────────────────────────────────

def _stream_to_disk(uploaded: UploadedFile, dest: Path, max_bytes: int) -> int:
    """
    Write the uploaded file to dest in _CHUNK_SIZE increments.
    Returns total bytes written. Raises UploadValidationError if the
    cumulative size exceeds max_bytes mid-stream (guards against
    lying Content-Length headers).

    The dest file is created atomically: write to dest.tmp, then rename.
    If the stream is interrupted (client disconnect, server crash), the
    .tmp file is left behind and the recovery sweep's orphan cleanup
    will remove it (it matches no Take row).
    """
    tmp_path = dest.with_suffix(dest.suffix + ".tmp")
    total = 0
    try:
        with open(tmp_path, "wb") as fh:
            for chunk in uploaded.chunks(chunk_size=_CHUNK_SIZE):
                total += len(chunk)
                if total > max_bytes:
                    raise UploadValidationError(
                        f"Stream exceeded {max_bytes:,}-byte cap at {total:,} bytes.",
                        code="too_large_stream",
                    )
                fh.write(chunk)
        # Atomic rename: the file is either fully written or not present.
        tmp_path.rename(dest)
    except BaseException:
        # Clean up partial .tmp on ANY failure (including KeyboardInterrupt).
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return total


# ─── Main entry point ────────────────────────────────────────────────────

def handle_upload(user, uploaded_file: UploadedFile, language_hint: str | None = None) -> dict:
    """
    Validate, stream, and enqueue one uploaded audio file.

    Called by:
      * studio.views.upload_view  (Phase 7, browser 📤 button)
      * studio.api_views.transcribe_upload  (Phase 8, POST /api/v1/transcriptions)

    Args:
        user:          Django User instance (must be authenticated).
        uploaded_file: The UploadedFile from request.FILES["file"].
        language_hint: Optional ISO-639-1 code (e.g. "en"). Stored on the
                       Take row; the worker's transcribe stage will use it
                       to skip LID when present. Phase 8 API accepts this
                       as a form field; Phase 7 browser omits it (auto-detect).

    Returns:
        dict with keys:
          take_id     : str (UUID hex) or None if DB write failed
          job_id      : str (UUID hex) or None
          filename    : str (original upload name)
          size_bytes  : int
          raw_path    : str (absolute path to the written file)
          extension   : str (validated lowercase ext)

    Raises:
        UploadValidationError: on any validation failure (no file written).
        Exception: only from catastrophic I/O (disk full, permissions).
                   The file may or may not exist on disk in this case;
                   the caller should report 500 and let recovery sweep
                   clean up.
    """
    if uploaded_file is None:
        raise UploadValidationError("No file provided.", code="no_file")

    # ── Validate ──
    filename = uploaded_file.name or "upload"
    ext = _validate_extension(filename)
    size = _validate_size(uploaded_file)
    max_bytes = int(getattr(settings, "UPLOAD_MAX_BYTES", _DEFAULT_MAX_BYTES))

    # Read first 12 bytes for magic check, then seek back.
    # UploadedFile from Django's multipart parser supports .read() + .seek().
    header = uploaded_file.read(12)
    uploaded_file.seek(0)
    _validate_magic(header, ext)

    # ── Generate take_id BEFORE writing (deterministic path) ──
    take_id = uuid.uuid4()
    dest_dir = _upload_dir(user.id, take_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    raw_path = dest_dir / _raw_filename(ext)

    # ── Stream to disk ──
    written = _stream_to_disk(uploaded_file, raw_path, max_bytes)
    logger.info(
        "upload: %s (%d bytes) streamed to %s for user %s",
        filename, written, raw_path, user.username,
    )

    # ── Persist Take + Job(QUEUED) ──
    # create_upload_take is _best_effort: returns None on DB failure.
    take = persistence.create_upload_take(
        user=user,
        source_filename=filename,
        raw_abs_path=str(raw_path),
        duration_ms=None,  # worker's normalize stage will measure it
    )

    if take is None:
        # DB write failed. File is on disk. Log loudly; the caller
        # returns 500. Recovery sweep or admin can adopt later.
        logger.error(
            "upload: create_upload_take returned None (DB failure?). "
            "File preserved at %s for manual adoption.", raw_path,
        )
        return {
            "take_id": None,
            "job_id": None,
            "filename": filename,
            "size_bytes": written,
            "raw_path": str(raw_path),
            "extension": ext,
        }

    # Store language hint on the Take if provided (Phase 8 API feature).
    if language_hint:
        take.language = language_hint.strip().lower()[:16]
        take.save(update_fields=["language", "updated_at"])

    job = take.current_job
    logger.info(
        "upload: Take %s + Job %s enqueued (QUEUED) for user %s",
        take.id.hex[:8], job.id.hex[:8] if job else "?", user.username,
    )

    return {
        "take_id": str(take.id),
        "job_id": str(job.id) if job else None,
        "filename": filename,
        "size_bytes": written,
        "raw_path": str(raw_path),
        "extension": ext,
    }
