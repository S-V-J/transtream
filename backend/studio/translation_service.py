"""
TranStream Translation Service (Slice A / Task H).

Single source of truth for text translation.

Providers (in preference order):
  1. NVIDIA Nemotron-3 Ultra (online NMT via API) — requires NVIDIA_API_KEY
  2. argostranslate (offline NMT) — local fallback, no API key needed

Architecture:
  * translate_text(text, source_lang, target_lang) -> str
      Translates a single string using preferred provider.
      Raises TranslationError on failure.
  * translate_take(take, target_lang) -> dict
      Translates all Whisper segments for a Take, joins into full text,
      creates/updates Translation row. Returns {source, target, text}.
  * TranslationError on any failure (both providers unavailable, etc.)

Thread-safety:
  NVIDIA client cache is process-wide dict guarded by lock.
  argostranslate translator cache is process-wide dict guarded by lock.
  Multiple concurrent requests may need the same language pair.
  Rate limiting: NVIDIA service has asyncio.Semaphore(14) + token bucket (30 RPM).

Language codes:
  Uses ISO-639-1 (two-letter) codes matching Whisper's output:
  "en", "hi", "es", "fr", "de", "zh", "ja", "ko", "ar", "pt", "ru", etc.
  argostranslate uses the same codes.

Provider selection:
  NVIDIA preferred when NVIDIA_API_KEY is configured. Falls back to
  argostranslate if NVIDIA is unavailable or returns an error.
"""

import logging
import threading
from typing import Optional

from django.conf import settings
from django.utils import timezone

from studio.models import Take, Translation

logger = logging.getLogger(__name__)

# ─── Process-wide translator cache (argostranslate) ─────────────────────

_translators: dict[tuple[str, str], object] = {}   # (from, to) -> ITranslation
_translators_lock = threading.Lock()
_packages_checked: set[tuple[str, str]] = set()


class TranslationError(Exception):
    """Raised when translation fails. Always catchable, never a raw import error."""
    pass


# ─── NVIDIA provider (lazy import to avoid hard dependency) ───────────────

def _try_nvidia_translate(text: str, source_lang: str, target_lang: str):
    """
    Attempt translation via NVIDIA Nemotron-3 Ultra.
    Returns translated text on success, raises NVIDIATranslationError on failure.
    Returns None (sentinel) if NVIDIA not configured — caller falls back.
    """
    from studio.nvidia_translation_service import (
        NVIDIATranslationError,
        nvidia_translation_enabled,
        translate_text as nvidia_translate_text,
    )

    if not nvidia_translation_enabled():
        logger.debug("NVIDIA translation disabled (no API key); falling back to argostranslate")
        raise TranslationError("NVIDIA not configured; using argostranslate fallback")

    try:
        return nvidia_translate_text(text, source_lang, target_lang)
    except NVIDIATranslationError as exc:
        logger.warning("NVIDIA translation failed: %s; falling back to argostranslate", exc)
        raise TranslationError(f"NVIDIA translation failed, falling back: {exc}")


def _load_argostranslate() -> None:
    """
    Import argostranslate.translate lazily.
    This is where the ~7.9 s import cost is paid.
    """
    global _argostranslate
    if "_argostranslate" not in globals():
        from argostranslate import translate as _argostranslate
        logger.info("argostranslate.translate loaded (deferred import)")
    return _argostranslate


def get_translator(source_lang: str, target_lang: str):
    """
    Get or create an argostranslate ITranslation instance for the language pair.

    Returns a callable with .translate(text: str) -> str method.
    Raises TranslationError if the package is unavailable and cannot be installed.

    Cache key is normalized (lowercase) ISO-639-1 codes.
    """
    src = (source_lang or "").strip().lower()
    tgt = (target_lang or "").strip().lower()
    if not src or not tgt:
        raise TranslationError("source_lang and target_lang must be non-empty")

    key = (src, tgt)
    with _translators_lock:
        if key in _translators:
            return _translators[key]

    # Outside lock: import + potential package install (can be slow)
    mod = _load_argostranslate()

    # Check installed languages for this pair
    installed = mod.get_installed_languages()
    from_lang = None
    to_lang = None
    for lang in installed:
        if lang.code == src:
            from_lang = lang
        if lang.code == tgt:
            to_lang = lang
        if from_lang and to_lang:
            break

    if from_lang is None or to_lang is None:
        # Try to install the package (requires network, ~40 MB, explicit approval)
        # Only attempt once per session to avoid repeated network calls.
        if key not in _packages_checked:
            _packages_checked.add(key)
            logger.info(
                "Translation package %s->%s not installed; attempting install...",
                src, tgt,
            )
            try:
                mod.package.install_package_for_language_pair(src, tgt)
                # Re-check after install
                installed = mod.get_installed_languages()
                for lang in installed:
                    if lang.code == src:
                        from_lang = lang
                    if lang.code == tgt:
                        to_lang = lang
                    if from_lang and to_lang:
                        break
            except Exception as exc:
                raise TranslationError(
                    f"Failed to install translation package {src}->{tgt}: {exc}"
                ) from exc

    if from_lang is None or to_lang is None:
        raise TranslationError(
            f"Translation package {src}->{tgt} not installed and install not permitted "
            f"(requires explicit approval)."
        )

    # Get the translator
    translator = from_lang.get_translation(to_lang)
    if translator is None:
        raise TranslationError(f"argostranslate returned no translator for {src}->{tgt}")

    with _translators_lock:
        _translators[key] = translator
    logger.info("Loaded translator for %s->%s", src, tgt)
    return translator


def translate_text(text: str, source_lang: str, target_lang: str) -> str:
    """
    Translate a single string from source_lang to target_lang.

    Uses NVIDIA Nemotron-3 Ultra when configured; falls back to argostranslate.

    Args:
        text: Text to translate. Empty/whitespace returns empty string.
        source_lang: ISO-639-1 code (e.g., "hi").
        target_lang: ISO-639-1 code (e.g., "en").

    Returns:
        Translated text.

    Raises:
        TranslationError on any failure (both providers unavailable, etc.).
    """
    text = (text or "").strip()
    if not text:
        return ""

    # Try NVIDIA first
    try:
        return _try_nvidia_translate(text, source_lang, target_lang)
    except TranslationError:
        pass  # Fall through to argostranslate

    # Fallback: argostranslate
    translator = get_translator(source_lang, target_lang)
    try:
        return translator.translate(text)
    except Exception as exc:
        raise TranslationError(f"Translation failed: {exc}") from exc


def translate_take(take: Take, target_lang: str) -> dict:
    """
    Translate all Whisper segments for a Take into target_lang.

    Reads the Take's detected language as the source (or falls back to
    take.language_hint if set). Joins segment texts with spaces, translates
    the full text, and creates/updates the Translation row.

    Args:
        take: Take instance with whisper_segments populated.
        target_lang: ISO-639-1 target language code (e.g., "en").

    Returns:
        {
            "source_language": str,
            "target_language": str,
            "text": str,              # full translated text
        }

    Raises:
        TranslationError if no Whisper segments, no source language,
        or translation fails.
    """
    # Source language: prefer per-take hint, else detected language, else error
    src_lang = take.language_hint.strip().lower() or take.language.strip().lower()
    if not src_lang:
        raise TranslationError(
            f"Take {take.id.hex[:8]} has no detected language and no language_hint; "
            f"cannot determine source for translation."
        )

    target = target_lang.strip().lower()
    if not target:
        raise TranslationError("target_lang must be non-empty")

    if src_lang == target:
        # No-op: return original transcript text
        segments = list(take.whisper_segments)
        if not segments:
            raise TranslationError(f"Take {take.id.hex[:8]} has no Whisper segments")
        full_text = " ".join(s.text for s in segments if s.text).strip()
        if not full_text:
            raise TranslationError("Transcript is empty")
        return {"source_language": src_lang, "target_language": target, "text": full_text}

    # Join all Whisper segments into one text
    segments = list(take.whisper_segments)
    if not segments:
        raise TranslationError(f"Take {take.id.hex[:8]} has no Whisper segments")
    full_text = " ".join(s.text for s in segments if s.text).strip()
    if not full_text:
        raise TranslationError("Transcript is empty (no text in segments)")

    # Translate (NVIDIA preferred, argostranslate fallback)
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
        "Translated Take %s: %s->%s (%d chars -> %d chars)",
        take.id.hex[:8], src_lang, target, len(full_text), len(translated),
    )
    return {
        "source_language": src_lang,
        "target_language": target,
        "text": translated,
    }