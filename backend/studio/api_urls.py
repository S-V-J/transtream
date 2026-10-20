"""
TranStream API URL configuration (Phase 8).

Mounted at /api/v1/ by the project urls.py.
All routes are versioned under v1. Future breaking changes → v2.
"""

from django.urls import path

from studio import api_views

app_name = "api_v1"

urlpatterns = [
    path("transcriptions", api_views.transcribe_upload, name="transcribe_upload"),
    path("jobs/<uuid:job_id>", api_views.job_status, name="job_status"),
    path("jobs/<uuid:job_id>/transcript", api_views.job_transcript, name="job_transcript"),
    path("jobs/<uuid:job_id>/cancel", api_views.job_cancel, name="job_cancel"),
]
