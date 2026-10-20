import json

from channels.generic.websocket import AsyncWebsocketConsumer


class AudioConsumer(AsyncWebsocketConsumer):
    """
    Phase 1 placeholder WebSocket consumer.

    Later phases will:
    - receive 100 ms binary audio chunks from browser MediaRecorder
    - buffer audio per session
    - normalize audio using FFmpeg
    - run VAD
    - transcribe using Faster-Whisper
    - broadcast timestamped transcript segments to UI
    """

    async def connect(self):
        self.session_id = self.scope["url_route"]["kwargs"]["session_id"]
        self.group_name = f"studio_{self.session_id}"

        await self.channel_layer.group_add(
            self.group_name,
            self.channel_name,
        )

        await self.accept()

        await self.send(
            text_data=json.dumps(
                {
                    "type": "connected",
                    "session_id": self.session_id,
                    "message": "TranStream WebSocket consumer is ready.",
                }
            )
        )

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(
            self.group_name,
            self.channel_name,
        )

    async def receive(self, text_data=None, bytes_data=None):
        await self.send(
            text_data=json.dumps(
                {
                    "type": "ack",
                    "session_id": self.session_id,
                    "text_len": len(text_data) if text_data else 0,
                    "binary_len": len(bytes_data) if bytes_data else 0,
                }
            )
        )
