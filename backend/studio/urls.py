"""
TranStream Studio URL configuration (app-level, session-authenticated).

Pages:
  /studio/                              mic recording + upload UI
  /settings/  /account/  /about/
Upload + job polling (Phase 7):
  /studio/upload/                       POST multipart
  /studio/jobs/<uuid>/                  GET status
  /studio/jobs/<uuid>/transcript/       GET json|srt|vtt|txt
API key management (Phase 8, account page):
  /account/keys/                        GET list
  /account/keys/generate/               POST mint
  /account/keys/<uuid>/revoke/          POST revoke

The bearer-authenticated public API lives in studio.api_urls and is mounted
at /api/v1/ by transtream/urls.py — deliberately NOT here, so the two trust
models stay in separate files and neither can accidentally inherit the other's
authentication.
"""

from django.urls import path

from . import views

app_name = "studio"

urlpatterns = [
    # Pages
    path("studio/", views.studio, name="studio"),
    path("settings/", views.settings_page, name="settings"),
    path("account/", views.account, name="account"),
    path("about/", views.about, name="about"),

    # Upload ingest + job polling (session auth)
    path("studio/upload/", views.upload_view, name="upload"),
    path("studio/jobs/<uuid:job_id>/", views.upload_job_status, name="job_status"),
    path(
        "studio/jobs/<uuid:job_id>/transcript/",
        views.upload_job_transcript,
        name="job_transcript",
    ),

    # API key management (session auth, account page)
    path("account/keys/", views.api_key_list, name="api_key_list"),
    path("account/keys/generate/", views.api_key_generate, name="api_key_generate"),
    path("account/keys/<uuid:key_id>/revoke/", views.api_key_revoke, name="api_key_revoke"),
]
