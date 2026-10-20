from django.urls import re_path

from studio import consumers

websocket_urlpatterns = [
    re_path(
        r"^ws/studio/(?P<session_id>[0-9a-zA-Z_-]+)/$",
        consumers.AudioConsumer.as_asgi(),
    ),
]
