from django.urls import path

from . import views

app_name = "studio"

urlpatterns = [
    path("studio/", views.studio, name="studio"),
    path("settings/", views.settings_page, name="settings"),
    path("account/", views.account, name="account"),
    path("about/", views.about, name="about"),
]
