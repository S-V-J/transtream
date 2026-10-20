"""
TranStream Studio views.

Page views (unchanged since Phase 1):
  root_redirect, studio, settings_page, account, about

Upload ingest (Phase 7):
  upload_view            POST /studio/upload/            -> 202 {job_id, take_id}

Job polling, session-authenticated (Phase 7):
  upload_job_status      GET  /studio/jobs/<id>/         -> 200 job_payload
  upload_job_transcript  GET  /studio/jobs/<id>/transcript/?format=json|srt|vtt|txt

These two exist so the browser never has to hold an API key to watch its own
uploads. /api/v1/... stays strictly bearer-authenticated; the payload shapes
are shared via studio.serializers, so the two surfaces cannot diverge.

API key management (Phase 8, account page):
  api_key_list           GET  /account/keys/
  api_key_generate       POST /account/keys/generate/
  api_key_revoke         POST /account/keys/<id>/revoke/

The upload view does NOT transcribe. It streams the file to disk, creates
Take(kind=upload) + Job(QUEUED), and returns. The background worker
(python manage.py tsworker) picks it up on its next poll.

*** OPERATIONAL REQUIREMENT ***
Uploads are useless without a running worker. The microphone path does not
need one (the consumer transcribes inline, in-process). If you upload and the
status stays "queued" forever, start the worker — nothing is broken.
"""

import json
import logging
import uuid

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from studio import serializers
from studio.models import ApiKey, Job, JobState
from studio.upload_service import UploadValidationError, handle_upload

logger = logging.getLogger(__name__)

KNOWN_FORMATS = ("json", "srt", "vtt", "txt")


# ─── Page views (UNCHANGED since Phase 1) ────────────────────────────────

def root_redirect(request):
    """/ -> /studio/ when authenticated, else /accounts/login/."""
    if request.user.is_authenticated:
        return redirect("studio:studio")
    return redirect("login")


@login_required
def studio(request):
    return render(request, "studio.html", {"page": "studio"})


@login_required
def settings_page(request):
    return render(request, "settings.html", {"page": "settings"})


@login_required
def account(request):
    return render(request, "account.html", {"page": "account"})


@login_required
def about(request):
    return render(request, "about.html", {"page": "about"})


# ─── Shared resolver ─────────────────────────────────────────────────────

def _owned_job(user, job_id):
    """
    Resolve a Job by UUID string restricted to this user's takes.
    Malformed id / missing row / another user's row all return None, so the
    caller emits one undifferentiated 404 and leaks no existence signal.
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


# ─── Upload ingest (Phase 7) ─────────────────────────────────────────────

@login_required
@require_POST
def upload_view(request):
    """
    POST /studio/upload/  (multipart/form-data, field "file")

    202 {"take_id","job_id","filename","size_bytes","status":"queued"}
    400 {"error","code"}   bad_extension | magic_mismatch | too_large |
                           too_large_stream | empty_file | no_file
    500 {"error"}          I/O or database catastrophe

    CSRF is enforced by CsrfViewMiddleware; the browser sends
    X-CSRFToken. Concurrent POSTs are fine: each creates an independent
    Take+Job, and the worker's semaphore bounds heavy execution to
    WHISPER_MAX_CONCURRENT (default 1). The view itself only streams bytes
    to disk, so it returns in well under a second for a 100 MB file on SSD.
    """
    try:
        result = handle_upload(
            user=request.user,
            uploaded_file=request.FILES.get("file"),
            language_hint=(request.POST.get("language") or "").strip() or None,
        )
    except UploadValidationError as exc:
        logger.warning("upload rejected from %s: [%s] %s",
                       request.user.username, exc.code, exc)
        return JsonResponse({"error": str(exc), "code": exc.code}, status=400)
    except Exception as exc:
        logger.exception("upload catastrophic failure for %s: %s",
                         request.user.username, exc)
        return JsonResponse(
            {"error": "Internal upload failure. The file may be on disk; "
                      "check /admin/studio/take/ before retrying."},
            status=500,
        )

    if result["take_id"] is None:
        return JsonResponse(
            {"error": "File saved to disk but could not be enqueued "
                      "(database unavailable). Retry shortly.",
             "raw_path": result["raw_path"]},
            status=500,
        )

    return JsonResponse(
        {
            "take_id": result["take_id"],
            "job_id": result["job_id"],
            "filename": result["filename"],
            "size_bytes": result["size_bytes"],
            "status": "queued",
            "message": "Upload accepted. The background worker will "
                       "transcribe it shortly.",
        },
        status=202,
    )


# ─── Job polling, session-auth (Phase 7) ─────────────────────────────────

@login_required
@require_GET
def upload_job_status(request, job_id):
    """GET /studio/jobs/<id>/ -> same payload shape as /api/v1/jobs/<id>."""
    job = _owned_job(request.user, job_id)
    if job is None:
        return JsonResponse({"error": "Job not found."}, status=404)
    return JsonResponse(serializers.job_payload(job))


@login_required
@require_GET
def upload_job_transcript(request, job_id):
    """
    GET /studio/jobs/<id>/transcript/?format=json|srt|vtt|txt

    409 while not DONE (with state + progress so the UI can keep its
    own polling loop honest).
    """
    job = _owned_job(request.user, job_id)
    if job is None:
        return JsonResponse({"error": "Job not found."}, status=404)

    if job.state != JobState.DONE:
        return JsonResponse(
            {
                "error": f"Transcript not ready. Job state: {job.state}.",
                "state": job.state,
                "progress_pct": job.progress_pct,
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


# ─── API key management (Phase 8, account page) ──────────────────────────

@login_required
@require_GET
def api_key_list(request):
    """
    GET /account/keys/ -> the user's keys, metadata only.

    fingerprint and hashed_secret are never serialised. `hint` is the
    non-secret display fragment (ts_live_ab12...9xyz) that lets the user
    tell keys apart without exposing one.
    """
    keys = ApiKey.objects.filter(user=request.user).order_by("-created_at")
    return JsonResponse({
        "keys": [
            {
                "id": str(k.id),
                "name": k.name,
                "hint": k.hint,
                "is_active": k.is_active,
                "scopes": k.scopes,
                "created_at": k.created_at.strftime("%Y-%m-%d %H:%M"),
                "last_used_at": (k.last_used_at.strftime("%Y-%m-%d %H:%M")
                                 if k.last_used_at else None),
                "revoked_at": (k.revoked_at.strftime("%Y-%m-%d %H:%M")
                               if k.revoked_at else None),
            }
            for k in keys
        ],
    })


@login_required
@require_POST
def api_key_generate(request):
    """
    POST /account/keys/generate/  {"name": "..."} -> 200 {raw_key, id, hint}

    The raw key is returned exactly once. It is stored only as a SHA-256
    fingerprint (for O(1) lookup) plus a salted PBKDF2 hash (for constant-time
    verification). Neither is reversible; a lost key means generating a new one.
    """
    try:
        body = json.loads(request.body) if request.body else {}
    except (json.JSONDecodeError, ValueError):
        body = {}

    name = (body.get("name") or "").strip()[:128]
    if not name:
        return JsonResponse({"error": "Key name is required."}, status=400)

    api_key, raw_key = ApiKey.create_for_user(request.user, name)
    logger.info("API key %s (%s) generated for %s",
                api_key.id.hex[:8], name, request.user.username)
    return JsonResponse({
        "id": str(api_key.id),
        "name": api_key.name,
        "hint": api_key.hint,
        "scopes": api_key.scopes,
        "raw_key": raw_key,
    })


@login_required
@require_POST
def api_key_revoke(request, key_id):
    """POST /account/keys/<id>/revoke/ -> 200 {revoked: true}."""
    try:
        key = ApiKey.objects.get(id=uuid.UUID(str(key_id)), user=request.user)
    except (ApiKey.DoesNotExist, ValueError, AttributeError, TypeError):
        return JsonResponse({"error": "Key not found."}, status=404)

    key.revoke()
    logger.info("API key %s revoked for %s", key.id.hex[:8], request.user.username)
    return JsonResponse({"revoked": True, "id": str(key.id), "hint": key.hint})
