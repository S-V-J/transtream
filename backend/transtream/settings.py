import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BASE_DIR.parent

load_dotenv(REPO_ROOT / ".env")

SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "django-insecure-dev-only-change-me")

DEBUG = os.getenv("DJANGO_DEBUG", "1").lower() in {"1", "true", "yes", "on"}

ALLOWED_HOSTS = [
    host.strip()
    for host in os.getenv(
        "DJANGO_ALLOWED_HOSTS",
        "localhost,127.0.0.1,[::1]",
    ).split(",")
    if host.strip()
]

# Default local development CSRF origins.
# The transtream CLI also injects the exact selected port dynamically.
_DEV_PORTS = tuple(range(8000, 8011)) + (8080,)

_DEFAULT_CSRF_ORIGINS = ",".join(
    f"http://{host}:{port}"
    for port in _DEV_PORTS
    for host in ("localhost", "127.0.0.1", "[::1]")
)

CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.getenv(
        "DJANGO_CSRF_TRUSTED_ORIGINS",
        _DEFAULT_CSRF_ORIGINS,
    ).split(",")
    if origin.strip()
]

INSTALLED_APPS = [
    "daphne",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "channels",
    "studio",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "transtream.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [REPO_ROOT / "frontend" / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "transtream.wsgi.application"
ASGI_APPLICATION = "transtream.asgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = REPO_ROOT / "staticfiles"
STATICFILES_DIRS = [REPO_ROOT / "frontend" / "static"]

MEDIA_URL = "/media/"
MEDIA_ROOT = REPO_ROOT / "media"

# ─── TranStream Studio Storage ─────────────────────────────────────────────
RECORDINGS_DIR = MEDIA_ROOT / "recordings"

STUDIO_MAX_TAKE_BYTES = 512 * 1024 * 1024
STUDIO_STATS_EVERY_CHUNKS = 20

# ─── FFmpeg / Audio Normalization (Phase 3) ────────────────────────────────
FFMPEG_BIN = os.getenv("FFMPEG_BIN", "ffmpeg")
FFMPEG_TIMEOUT = int(os.getenv("FFMPEG_TIMEOUT", "300"))
AUDIO_SAMPLE_RATE = int(os.getenv("AUDIO_SAMPLE_RATE", "16000"))
AUDIO_CHANNELS = int(os.getenv("AUDIO_CHANNELS", "1"))

# ─── Silero VAD Speech Segmentation (Phase 4) ─────────────────────────────
# Probability threshold: higher = stricter (less silence classified as speech).
VAD_THRESHOLD = float(os.getenv("VAD_THRESHOLD", "0.5"))

# Minimum silence duration (ms) to split two speech regions into
# separate segments.  Larger values merge nearby speech.
VAD_MIN_SILENCE_MS = int(os.getenv("VAD_MIN_SILENCE_MS", "500"))

# Padding (ms) added before/after each detected speech region
# to avoid clipping the first/last phoneme.
VAD_SPEECH_PAD_MS = int(os.getenv("VAD_SPEECH_PAD_MS", "30"))

# ─── Upload ingest + public API (Phases 7/8) ─────────────────────────────────
# Both names were advertised in .env.example from Phase 7 but never read here,
# so upload_service and api_auth fell through to their in-code fallbacks and
# editing .env changed nothing. The phase.7+8 commit message recorded this as a
# KNOWN GAP; reading them here closes it.
#
# UPLOAD_MAX_BYTES - a NO-OP by design. The default below is byte-identical to
# upload_service._DEFAULT_MAX_BYTES, so a deployment without this line behaves
# exactly as it did before. It exists so the documented knob is real.
UPLOAD_MAX_BYTES = int(os.getenv("UPLOAD_MAX_BYTES", str(100 * 1024 * 1024)))

# UPLOAD_RATE_LIMIT_PER_MINUTE - a DELIBERATE RAISE, not a no-op. The in-code
# fallback in api_auth is 10, and 10/min is incompatible with this API's own
# documented contract ("poll GET /api/v1/jobs/{id} until state is terminal"):
# a 2 s poller needs 30/min and studio.js polls at 1.5 s (40/min), so the old
# default throttled legitimate traffic within ~15 s of polling. 120/min = one
# request per 2 s, which admits every documented poller and still refuses
# credential stuffing (hundreds per second). The number is derived from the
# pollers' own intervals rather than chosen. api_auth's fallback stays 10 as a
# last resort if this attribute is ever removed.
UPLOAD_RATE_LIMIT_PER_MINUTE = int(os.getenv("UPLOAD_RATE_LIMIT_PER_MINUTE", "120"))

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ─── Authentication ────────────────────────────────────────────────────────
LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/studio/"
LOGOUT_REDIRECT_URL = "/accounts/login/"

# ─── Local development cookie security ─────────────────────────────────────
if DEBUG:
    CSRF_COOKIE_SECURE = False
    SESSION_COOKIE_SECURE = False
    SECURE_SSL_REDIRECT = False

# ─── Channels / Redis ──────────────────────────────────────────────────────
REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")

CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {
            "hosts": [REDIS_URL],
        },
    },
}
