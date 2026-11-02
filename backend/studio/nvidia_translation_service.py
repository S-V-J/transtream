"""
TranStream NVIDIA Nemotron Translation Service.

Single source of truth for NVIDIA Nemotron-3 Ultra text translation.
Integrates with existing translation pipeline as primary provider with
argostranslate fallback.

Architecture:
  * get_nvidia_client() -> requests.Session (lazy, cached)
  * translate_text(text, source_lang, target_lang) -> str
  * translate_take(take, target_lang) -> dict
  * Rate limiting: asyncio.Semaphore(14) + token bucket (30 RPM)
  * Queue: asyncio.Queue for excess requests
  * Falls back to argostranslate on failure

Thread-safety:
  * Client cache is process-wide dict guarded by lock
  * Rate limiter uses asyncio primitives (async context)
  * Package installation is best-effort: if unavailable, falls back

Language codes:
  Uses ISO-639-1 (two-letter) codes matching Whisper's output:
  "en", "hi", "es", "fr", "de", "zh", "ja", "ko", "ar", "pt", "ru", etc.

Import cost:
  requests import = negligible. Rate limiter uses asyncio.

Configuration:
  NVIDIA_API_KEY: required (stored in .env)
  NVIDIA_API_BASE: default "https://integrate.api.nvidia.com/v1/chat/completions"
  NVIDIA_MODEL: default "nvidia/nemotron-3-ultra-550b-a55b"
  NVIDIA_MAX_PARALLEL: default 14 (semaphore)
  NVIDIA_RPM: default 30 (requests per minute)
"""

import asyncio
import logging
import threading
import time
from typing import Optional

import requests
from django.conf import settings
from django.utils import timezone

from studio.models import Take, Translation

logger = logging.getLogger(__name__)

# ─── Process-wide client cache ───────────────────────────────────────────

_nvidia_client: Optional[requests.Session] = None
_nvidia_client_lock = threading.Lock()


# ─── Rate limiter (async) ────────────────────────────────────────────────

class RateLimiter:
    """
    Token bucket rate limiter with semaphore for parallel request limit.

    Combines:
      - asyncio.Semaphore for max parallel requests (default 14)
      - Token bucket for RPM limit (default 30 RPM = 0.5 req/sec)
    """

    def __init__(self, max_parallel: int = 14, rpm: int = 30):
        self.max_parallel = max_parallel
        self.rpm = rpm
        self._semaphore = asyncio.Semaphore(max_parallel)
        self._tokens = float(rpm)
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()
        self._queue: asyncio.Queue = asyncio.Queue()
        self._worker_task: Optional[asyncio.Task] = None
        self._worker_started = False

    async def _refill_tokens(self):
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self.rpm, self._tokens + elapsed * (self.rpm / 60.0))
        self._last_refill = now

    async def _worker(self):
        """Background worker that drains the queue respecting rate limits."""
        while True:
            try:
                # Wait for a request to process
                coro_func, fut, args, kwargs = await self._queue.get()

                # Acquire semaphore (max parallel)
                async with self._semaphore:
                    # Wait for token (RPM limit)
                    async with self._lock:
                        await self._refill_tokens()
                        while self._tokens < 1.0:
                            # Wait until next token available
                            wait_time = (1.0 - self._tokens) * (60.0 / self.rpm)
                            await asyncio.sleep(wait_time)
                            await self._refill_tokens()
                        self._tokens -= 1.0

                    try:
                        # Execute the actual request by calling the passed coroutine function
                        result = await coro_func(*args, **kwargs)
                        fut.set_result(result)
                    except Exception as exc:
                        fut.set_exception(exc)
                    finally:
                        self._queue.task_done()

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Rate limiter worker error: %s", exc)

    async def acquire(self, coro_func, *args, **kwargs):
        """
        Submit a coroutine to be executed under rate limits.
        Returns the result of the coroutine.
        """
        # Start worker if not started
        if not self._worker_started:
            self._worker_started = True
            self._worker_task = asyncio.create_task(self._worker())

        # Create future for result
        loop = asyncio.get_event_loop()
        fut = loop.create_future()

        # Queue the request along with the coroutine function to call
        await self._queue.put((coro_func, fut, args, kwargs))

        # Wait for result
        return await fut

    async def shutdown(self):
        """Stop the background worker."""
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass


# ─── NVIDIA Translation Service ──────────────────────────────────────────

class NVIDIATranslationError(Exception):
    """Raised when NVIDIA translation fails. Always catchable."""
    pass


def _load_nvidia_settings():
    """Load NVIDIA settings from Django settings."""
    return {
        "api_key": getattr(settings, "NVIDIA_API_KEY", None),
        "api_base": getattr(settings, "NVIDIA_API_BASE", "https://integrate.api.nvidia.com/v1/chat/completions"),
        "model": getattr(settings, "NVIDIA_MODEL", "nvidia/nemotron-3-ultra-550b-a55b"),
        "max_parallel": getattr(settings, "NVIDIA_MAX_PARALLEL", 14),
        "rpm": getattr(settings, "NVIDIA_RPM", 30),
    }


def get_nvidia_client() -> requests.Session:
    """
    Get or create a cached requests.Session for NVIDIA API.
    Returns None if no API key configured.
    """
    global _nvidia_client

    if _nvidia_client is not None:
        return _nvidia_client

    cfg = _load_nvidia_settings()
    if not cfg["api_key"]:
        logger.warning("NVIDIA_API_KEY not set; NVIDIA translation disabled")
        return None

    with _nvidia_client_lock:
        if _nvidia_client is not None:
            return _nvidia_client

        session = requests.Session()
        session.headers.update({
            "Authorization": f"Bearer {cfg['api_key']}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
        # Set default timeout
        session.timeout = 60

        _nvidia_client = session
        logger.info("NVIDIA Nemotron client initialized (model: %s)", cfg["model"])

    return _nvidia_client


def _build_translation_prompt(text: str, source_lang: str, target_lang: str) -> str:
    """Build the translation prompt for Nemotron."""
    lang_names = {
        "en": "English", "hi": "Hindi", "es": "Spanish", "fr": "French",
        "de": "German", "zh": "Chinese", "ja": "Japanese", "ar": "Arabic",
        "pt": "Portuguese", "ru": "Russian", "ko": "Korean", "it": "Italian",
        "nl": "Dutch", "pl": "Polish", "tr": "Turkish", "vi": "Vietnamese",
        "th": "Thai", "id": "Indonesian", "ms": "Malay", "tl": "Tagalog",
    }

    src_name = lang_names.get(source_lang.lower(), source_lang)
    tgt_name = lang_names.get(target_lang.lower(), target_lang)

    return (
        f"Translate the following text from {src_name} to {tgt_name}. "
        f"Return ONLY the translation, nothing else.\n\n"
        f"{text}"
    )


def translate_text(text: str, source_lang: str, target_lang: str) -> str:
    """
    Translate text using NVIDIA Nemotron-3 Ultra.

    Args:
        text: Text to translate
        source_lang: ISO-639-1 source language code
        target_lang: ISO-639-1 target language code

    Returns:
        Translated text

    Raises:
        NVIDIATranslationError on any failure (network, API error, etc.)
    """
    text = (text or "").strip()
    if not text:
        return ""

    client = get_nvidia_client()
    if client is None:
        raise NVIDIATranslationError("NVIDIA client not available (no API key)")

    cfg = _load_nvidia_settings()

    prompt = _build_translation_prompt(text, source_lang, target_lang)

    payload = {
        "model": cfg["model"],
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": 4096,
        "stream": False,
    }

    try:
        response = client.post(cfg["api_base"], json=payload, timeout=60)

        if response.status_code == 429:
            # Rate limited by provider
            raise NVIDIATranslationError("NVIDIA API rate limited (429)")

        if response.status_code != 200:
            raise NVIDIATranslationError(
                f"NVIDIA API error: HTTP {response.status_code} - {response.text[:500]}"
            )

        data = response.json()

        if "choices" not in data or not data["choices"]:
            raise NVIDIATranslationError("NVIDIA API returned empty choices")

        content = data["choices"][0].get("message", {}).get("content", "").strip()

        if not content:
            raise NVIDIATranslationError("NVIDIA API returned empty translation")

        logger.debug(
            "NVIDIA translation: %s->%s (%d chars -> %d chars)",
            source_lang, target_lang, len(text), len(content)
        )
        return content

    except requests.RequestException as exc:
        raise NVIDIATranslationError(f"NVIDIA API request failed: {exc}") from exc
    except (KeyError, ValueError, TypeError) as exc:
        raise NVIDIATranslationError(f"NVIDIA API response parse error: {exc}") from exc


def translate_take_nvidia(take: Take, target_lang: str) -> dict:
    """
    Translate all Whisper segments for a Take using NVIDIA Nemotron.

    Args:
        take: Take instance with whisper_segments populated
        target_lang: ISO-639-1 target language code

    Returns:
        {"source_language": str, "target_language": str, "text": str}

    Raises:
        NVIDIATranslationError on failure
    """
    # Source language: prefer per-take hint, else detected language
    src_lang = take.language_hint.strip().lower() or take.language.strip().lower()
    if not src_lang:
        raise NVIDIATranslationError(
            f"Take {take.id.hex[:8]} has no detected language and no language_hint"
        )

    target = target_lang.strip().lower()
    if not target:
        raise NVIDIATranslationError("target_lang must be non-empty")

    if src_lang == target:
        # No-op: return original transcript text
        segments = list(take.whisper_segments)
        if not segments:
            raise NVIDIATranslationError(f"Take {take.id.hex[:8]} has no Whisper segments")
        full_text = " ".join(s.text for s in segments if s.text).strip()
        if not full_text:
            raise NVIDIATranslationError("Transcript is empty")
        return {"source_language": src_lang, "target_language": target, "text": full_text}

    # Join all Whisper segments into one text
    segments = list(take.whisper_segments)
    if not segments:
        raise NVIDIATranslationError(f"Take {take.id.hex[:8]} has no Whisper segments")
    full_text = " ".join(s.text for s in segments if s.text).strip()
    if not full_text:
        raise NVIDIATranslationError("Transcript is empty (no text in segments)")

    # Translate via NVIDIA
    translated = translate_text(full_text, src_lang, target)

    # Persist (upsert)
    translation, _ = Translation.objects.update_or_create(
        take=take,
        target_language=target,
        defaults={
            "source_language": src_lang,
            "text": translated,
        },
    )
    logger.info(
        "NVIDIA translated Take %s: %s->%s (%d chars -> %d chars)",
        take.id.hex[:8], src_lang, target, len(full_text), len(translated),
    )
    return {
        "source_language": src_lang,
        "target_language": target,
        "text": translated,
    }


def nvidia_translation_enabled() -> bool:
    """Check if NVIDIA translation is available (API key configured)."""
    cfg = _load_nvidia_settings()
    return cfg["api_key"] is not None


# ─── Async queue-based translation (for high-throughput scenarios) ─────────

class NVIDIATranslationQueue:
    """
    Async queue for NVIDIA translation requests with rate limiting.

    Usage:
        queue = NVIDIATranslationQueue(max_parallel=14, rpm=30)
        result = await queue.translate(text, "hi", "en")
        await queue.shutdown()
    """

    def __init__(self, max_parallel: int = 14, rpm: int = 30):
        self._limiter = RateLimiter(max_parallel, rpm)

    async def translate(self, text: str, source_lang: str, target_lang: str) -> str:
        """Translate text through the rate-limited queue."""
        return await self._limiter.acquire(self._translate_impl, text, source_lang, target_lang)

    async def _translate_impl(self, text: str, source_lang: str, target_lang: str) -> str:
        """Actual translation implementation (runs under rate limiter)."""
        # Run sync translation in thread pool
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, translate_text, text, source_lang, target_lang
        )

    async def shutdown(self):
        await self._limiter.shutdown()


def nvidia_status() -> dict:
    """Return NVIDIA translation service status for diagnostics."""
    cfg = _load_nvidia_settings()
    return {
        "enabled": cfg["api_key"] is not None,
        "model": cfg["model"],
        "api_base": cfg["api_base"],
        "max_parallel": cfg["max_parallel"],
        "rpm": cfg["rpm"],
    }