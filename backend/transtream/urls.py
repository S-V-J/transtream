"""
TranStream project URL configuration.

Route map:
  /                    → root_redirect (auth-gated)
  /admin/              → Django admin
  /accounts/login/     → LoginView
  /accounts/logout/    → LogoutView
  /studio/             → Studio page (mic + upload UI)
  /studio/upload/      → POST multipart upload (Phase 7)
  /settings/           → Settings page
  /account/            → Account page (Phase 8: API key management)
  /about/              → About page
  /api/v1/…            → Public API (Phase 8, bearer-key authenticated)
  /media/…             → Media files (DEBUG only)
  /static/…            → Static files (DEBUG only)
"""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.contrib.staticfiles.urls import staticfiles_urlpatterns
from django.urls import include, path

from studio import views

urlpatterns = [
    # Root: redirect based on auth status
    path("", views.root_redirect, name="root"),

    # Django admin
    path("admin/", admin.site.urls),

    # Auth URLs
    path(
        "accounts/login/",
        auth_views.LoginView.as_view(
            template_name="login.html",
            redirect_authenticated_user=True,
        ),
        name="login",
    ),
    path(
        "accounts/logout/",
        auth_views.LogoutView.as_view(),
        name="logout",
    ),

    # Public API (Phase 8) — mounted BEFORE the app catch-all so
    # /api/v1/… is never swallowed by studio.urls.
    path("api/v1/", include("studio.api_urls")),

    # App pages (studio, settings, account, about, upload)
    path("", include("studio.urls")),
]

# Local development static/media serving.
# This is required because Daphne does not serve static/media automatically.
if settings.DEBUG:
    urlpatterns += staticfiles_urlpatterns()
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
