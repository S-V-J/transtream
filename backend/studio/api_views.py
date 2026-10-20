"""
TranStream public API views (Phase 8).

Endpoints (mounted at /api/v1/ by api_urls.py):
  POST   /api/v1/transcriptions                 -> 202 {job_id, take_id}
  GET    /api/v1/jobs/<job_id>                  -> 200 {state, progress_pct, ...}
  GET    /api/v1/jobs/<job_id>/transcript       -> 200 json|srt|vtt|txt
  DELETE /api/v1/jobs/<job_id>/cancel           -> 200 {cancelled: true}

Authentication: @api_key_required (Authorization: Bearer ts_live_...).
Every query is scoped to the key owner's takes. Cross-user access returns
404, never 403, so a probe cannot distinguish "not yours" from "does not
exist".

Response shapes come from studio.serializers — the same module the
session-authenticated browser endpoints use. One contract, two front doors.

NOTE ON HTTP METHOD DECORATORS: django.views.decorators.http provides
require_http_methods, require_safe, require_GET and require_POST. There is
no require_DELETE; DELETE is expressed as require_http_methods(["DELETE"]).
(A prior revision of this file imported a nonexistent require_DELETE and
failed at URLConf import time — caught by `manage.py check`, not by
py_compile. Both gates are mandatory.)
"""

import logging
import uuid

from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.http import (
    require_GET,
    require_http_methods,
    require_POST,
)

from studio import serializers
from studio.api_auth import api_key_required
from studio.models import Job, JobState
from studio.upload_service import UploadValidationError, handle_upload

logger = logging.getLogger(__name__)

KNOWN_FORMATS = ("json", "srt", "vtt", "txt")


def _owned_job(user, job_id):
    """
    Resolve a Job by UUID string, restricted to this user's takes.
    Returns None for a malformed id, a missing row, or another user's row —
    all indistinguishable to the caller, hence a single 404.
    """
    try:
        jid = uuid.UUID(str(job_id))
    except (ValueError, AttributeError, TypeError):
        return None
    return (
        Job.objects
        .select_related("take")
        .filter(id=jid, take__user=user)
        .first()
    )


# ─── POST /api/v1/transcriptions ─────────────────────────────────────────

@api_key_required
@require_POST
def transcribe_upload(request):
    """
    Accept a multipart audio upload and enqueue it for transcription.

    Form fields:
      file     : (required) the audio binary
      language : (optional) ISO-639-1 hint, e.g. "en"

    Returns 202 {job_id, take_id, filename, size_bytes, status:"queued"}.
    Poll GET /api/v1/jobs/{job_id} until state is terminal.
    """
    try:
        result = handle_upload(
            user=request.api_user,
            uploaded_file=request.FILES.get("file"),
            language_hint=(request.POST.get("language") or "").strip() or None,
        )
    except UploadValidationError as exc:
        return JsonResponse({"error": str(exc), "code": exc.code}, status=400)
    except Exception as exc:
        logger.exception("API upload catastrophic failure: %s", exc)
        return JsonResponse({"error": "Internal upload failure."}, status=500)

    if result["take_id"] is None:
        return JsonResponse(
            {"error": "File saved but could not be enqueued (database unavailable)."},
            status=500,
        )

    return JsonResponse(
        {
            "job_id": result["job_id"],
            "take_id": result["take_id"],
            "filename": result["filename"],
            "size_bytes": result["size_bytes"],
            "status": "queued",
        },
        status=202,
    )


# ─── GET /api/v1/jobs/<job_id> ───────────────────────────────────────────

@api_key_required
@require_GET
def job_status(request, job_id):
    """Current state + progress of one job owned by this key's user."""
    job = _owned_job(request.api_user, job_id)
    if job is None:
        return JsonResponse({"error": "Job not found."}, status=404)
    return JsonResponse(serializers.job_payload(job))


# ─── GET /api/v1/jobs/<job_id>/transcript ────────────────────────────────

@api_key_required
@require_GET
def job_transcript(request, job_id):
    """
    Transcript for a completed job.

    Query param ?format=json|srt|vtt|txt (default json).
    409 while the job is not DONE — the client is told exactly which
    endpoint to poll instead of being handed an empty segment list.
    """
    job = _owned_job(request.api_user, job_id)
    if job is None:
        return JsonResponse({"error": "Job not found."}, status=404)

    if job.state != JobState.DONE:
        return JsonResponse(
            {
                "error": f"Transcript not ready. Job state: {job.state}.",
                "state": job.state,
                "progress_pct": job.progress_pct,
                "poll": request.build_absolute_uri(f"/api/v1/jobs/{job_id}"),
            },
            status=409,
        )

    fmt = (request.GET.get("format") or "json").lower()
    if fmt not in KNOWN_FORMATS:
        return JsonResponse(
            {"error": f"Unknown format: {fmt}. Use one of: {', '.join(KNOWN_FORMATS)}."},
            status=400,
        )

    rows = serializers.whisper_rows(job.take)

    if fmt == "json":
        return JsonResponse(serializers.transcript_payload(job, rows))

    body, ext = serializers.transcript_body(job, fmt, rows)
    filename = f"transcript_{job.take.id.hex[:8]}.{ext}"
    return HttpResponse(
        body,
        content_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ─── DELETE /api/v1/jobs/<job_id>/cancel ─────────────────────────────────────────
@api_key_required
@require_http_methods(["DELETE"])
def job_cancel(request, job_id):
    """
    Cancel a job by moving it to the terminal CANCELLED state.

    Semantics (documented, not accidental):
      * Queued / backoff-waiting job -> never picked up again. True cancel.
      * Job currently leased and mid-process_take() -> the worker finishes
        the in-flight inference and its next persistence write moves the row
        to DONE, overwriting CANCELLED. The transcript endpoint will then
        serve it. This is safe (no corruption, no duplicate rows) but it is
        cooperative, not preemptive. Preemptive cancel requires a
        cancellation token polled inside process_take() — deferred, because
        interrupting Whisper mid-inference buys nothing a local-first studio
        needs and risks leaving half-written segments.
    """
    job = _owned_job(request.api_user, job_id)
    if job is None:
        return JsonResponse({"error": "Job not found."}, status=404)

    if job.is_terminal:
        return JsonResponse(
            {
                "error": f"Job already in terminal state: {job.state}. Cannot cancel.",
                "state": job.state,
            },
            status=409,
        )

    job.state = JobState.CANCELLED
    job.finished_at = timezone.now()
    job.save(update_fields=["state", "finished_at", "updated_at"])

    logger.info(
        "API: Job %s cancelled by user %s",
        job.id.hex[:8], request.api_user.username,
    )
    return JsonResponse({"job_id": str(job.id), "cancelled": True, "state": job.state})
