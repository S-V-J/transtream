from django.contrib import admin
from django.contrib.auth import views as auth_views
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

    # App pages (all login_required)
    path("", include("studio.urls")),
]
