"""
TranStream response serializers (Phase 7/8).

Single source of truth for the JSON/text shapes of Job status and Transcript
results. Consumed by BOTH surfaces:

  * studio.api_views   — bearer-key authenticated, /api/v1/...
  * studio.views       — session authenticated, /studio/jobs/...

Keeping one implementation is deliberate: the browser and any external
platform must see byte-identical payloads for the same resource. Two
hand-written copies would drift within a phase or two, and a client
debugging against the API would be chasing a phantom.

Pure functions, no request/response objects, no I/O beyond the caller's
queryset. Importable from anywhere without circular-import risk (this
module imports only models).
"""

from studio.models import Segment, SegmentSource


# ─── helpers ─────────────────────────────────────────────────────────────

def _iso(dt):
    """ISO-8601 string or None. Keeps every view out of null-checking."""
    return dt.isoformat() if dt else None


def whisper_rows(take):
    """
    The linguistic timeline, ordered. Materialised as a list so callers can
    use len() instead of issuing a second COUNT(*) query.
    """
    return list(
        Segment.objects
        .filter(take=take, source=SegmentSource.WHISPER)
        .order_by("index")
    )


# ─── Job ─────────────────────────────────────────────────────────────────

def job_payload(job):
    """
    The full state-machine view of one Job, plus the Take-level facts a
    client needs to render a result without a second round trip
    (language, duration, filename, kind).

    `is_terminal` is included so pollers can stop on a single boolean
    rather than re-encoding the terminal-state set client-side (a drift
    hazard the moment a state is added).
    """
    take = job.take
    return {
        "job_id": str(job.id),
        "take_id": str(take.id),
        "kind": take.kind,
        "state": job.state,
        "is_terminal": job.is_terminal,
        "progress_pct": job.progress_pct,
        "attempts": job.attempts,
        "max_attempts": job.max_attempts,
        "last_error": job.last_error or None,
        "language": take.language or None,
        "language_probability": take.language_probability,
        "duration_ms": take.duration_ms,
        "filename": take.source_filename,
        "created_at": _iso(job.created_at),
        "started_at": _iso(job.started_at),
        "finished_at": _iso(job.finished_at),
    }


# ─── Transcript ──────────────────────────────────────────────────────────

def transcript_payload(job, rows=None):
    """JSON transcript: metadata + the ordered whisper segment list."""
    take = job.take
    rows = whisper_rows(take) if rows is None else rows
    return {
        "job_id": str(job.id),
        "take_id": str(take.id),
        "language": take.language,
        "language_probability": take.language_probability,
        "duration_ms": take.duration_ms,
        "segment_count": len(rows),
        "segments": [
            {
                "index": s.index,
                "start_ms": s.start_ms,
                "end_ms": s.end_ms,
                "duration_ms": s.duration_ms,
                "text": s.text,
            }
            for s in rows
        ],
    }


def ms_to_hms(ms):
    """HH:MM:SS.mmm — the VTT convention."""
    ms = max(0, int(ms or 0))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, milli = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{milli:03d}"


def ms_to_srt(ms):
    """HH:MM:SS,mmm — SRT mandates the comma separator."""
    return ms_to_hms(ms).replace(".", ",")


def subtitles(rows, fmt):
    """
    Render segments as SubRip ('srt') or WebVTT ('vtt').

    Cue numbering is 1-based and sequential for SRT. VTT cues may be
    unnumbered; we emit none, which is valid and keeps the diff between
    the two formats minimal.
    """
    lines = []
    if fmt == "vtt":
        lines.append("WEBVTT")
        lines.append("")
    for i, s in enumerate(rows, start=1):
        if fmt == "srt":
            lines.append(str(i))
            lines.append(f"{ms_to_srt(s.start_ms)} --> {ms_to_srt(s.end_ms)}")
        else:
            lines.append(f"{ms_to_hms(s.start_ms)} --> {ms_to_hms(s.end_ms)}")
        lines.append(s.text or "")
        lines.append("")
    return "\n".join(lines)


def plain_text(rows):
    """Human-readable transcript, one segment per line with a time prefix."""
    return "\n".join(
        f"[{ms_to_hms(s.start_ms)} -> {ms_to_hms(s.end_ms)}] {s.text or ''}"
        for s in rows
    ) + "\n"


def transcript_body(job, fmt, rows=None):
    """
    Dispatch a transcript to (body_string, extension) for the non-JSON
    formats, or (None, None) for 'json' (whose caller returns JsonResponse).

    Centralising the dispatch means the API and the browser endpoints agree
    on which formats exist and what their filenames are.
    """
    rows = whisper_rows(job.take) if rows is None else rows
    if fmt == "srt":
        return subtitles(rows, "srt"), "srt"
    if fmt == "vtt":
        return subtitles(rows, "vtt"), "vtt"
    if fmt == "txt":
        return plain_text(rows), "txt"
    return None, None
