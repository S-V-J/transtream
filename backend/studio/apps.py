"""
TranStream app config.

ready() exists for exactly one reason: to move model load off the first take's
critical path, in the one process that will actually run inference.

WHY THE SWITCH IS AN ENVIRONMENT VARIABLE AND NOT A settings.py VALUE.
AppConfig.ready() runs in EVERY process that calls django.setup(): the Daphne
ASGI server, the tsworker command, manage.py check, migrate, createsuperuser,
and every harness script under scripts/. Warming unconditionally would load
~75 MB of Whisper weights plus the Silero ONNX graph into the WEB process, which
never transcribes anything - the request saves the file, writes Take +
Job(QUEUED) and returns 202, and a separate worker does the inference. It would
also load them into one-off commands that should finish in milliseconds, and
twice on a normal dev machine.

A settings.py flag cannot express "only in the worker", because every process
reads the same .env into the same settings module. The process environment CAN:
it is per-process and it is set on the launch line. Hence

    TS_WARMUP_MODELS=1 python manage.py tsworker --verbose

and nothing else. Deliberately NO argv sniffing - deciding "am I the worker?" by
reading sys.argv is a label standing in for a property, which is the error class
this project has been burned by nine times. The launch line states the intent
directly, and if it is wrong the worst case is a cold first take, not a silent
misconfiguration.

WHY WARMUP RUNS AN INFERENCE RATHER THAN CONSTRUCTING MODELS. Measured, same
process, 122 s reference take: first job 8.195 s, second job 2.596 s. Of that
~5.6 s gap, 4.22 s is model loading and 1.19 s is CTranslate2's first-decode
warm-up, which a constructor never pays. See studio.warmup's module docstring.

WHY ready() CANNOT RAISE. A warmup that cannot run - no cache, no network,
corrupt weights, unsupported platform - must not stop the process from starting.
The services load lazily on first use regardless, so the worst case is today's
behaviour (a slow first take) plus one warning line that tells the operator why
before a user has to report it.
"""

import logging
import os

from django.apps import AppConfig

logger = logging.getLogger(__name__)

_TRUTHY = {"1", "true", "yes", "on"}


class StudioConfig(AppConfig):
    name = 'studio'

    def ready(self):
        """Warm the audio models if, and only if, THIS process asked for it."""
        if os.environ.get("TS_WARMUP_MODELS", "").strip().lower() not in _TRUTHY:
            return
        try:
            from studio.warmup import warmup_models
            result = warmup_models("AppConfig.ready")
            if not result.get("ok"):
                logger.warning(
                    "warmup: incomplete (%s); the first take will still pay the "
                    "cold-start cost", result.get("reason") or "unknown")
        except Exception as exc:  # noqa: BLE001 - startup must never die here
            logger.warning("warmup: unexpected %s: %s", type(exc).__name__, exc)
