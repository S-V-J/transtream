"""
TranStream API bearer authentication (Phase 8).

Decorator: @api_key_required
  Applied to API views. Extracts "Authorization: Bearer <key>" from the
  request, resolves the ApiKey row, verifies the secret, checks activity,
  and attaches request.api_key + request.api_user for downstream use.

Rate limiting:
  Per-key, per-minute, Redis-backed sliding window. Key pattern:
    ratelimit:{fingerprint}:{unix_minute}
  INCR + EXPIRE 120s. If count > UPLOAD_RATE_LIMIT_PER_MINUTE (default 10),
  return 429. Redis is already running (channel layer); no new infra.
  If Redis is unavailable, the rate limiter FAILS OPEN (allows the request)
  and logs a warning. A local-first tool must not become unusable because
  Redis restarted.

last_used_at throttling:
  We do NOT write to the DB on every API call. Instead, a Redis SETNX
  with 60s TTL gates the write: at most one DB UPDATE per key per minute.
  Pattern: key_lastused:{fingerprint}

CSRF posture (Phase 8 fix):
  The decorator returns csrf_exempt(wrapper). Bearer endpoints are
  intrinsically CSRF-safe: an attack depends on the browser attaching
  AMBIENT credentials (cookies) to a cross-site request, and no browser
  attaches an Authorization header unprompted. Leaving CsrfViewMiddleware
  in the path added no security whatsoever - it made POST
  /api/v1/transcriptions and DELETE /api/v1/jobs/<id>/cancel return 403 to
  every non-browser client, i.e. it disabled the API for the platforms it
  exists to serve. The browser kept working (it has the cookie), which is
  why a phase of GET-only probes never surfaced it: GET/HEAD/OPTIONS/TRACE
  are never CSRF-checked. Session-authenticated endpoints (/studio/*,
  /account/*) are untouched by this module and keep full CSRF enforcement.

Security notes:
  * The raw key is NEVER logged, stored in sessions, or echoed in errors.
  * Fingerprint lookup is O(1) (unique indexed column). No scanning.
  * Hash verification uses Django's check_password (PBKDF2, constant-time).
  * Revoked keys (is_active=False) are rejected before hash check.
  * Timing: the fingerprint lookup is a DB query (fast); the hash check
    is ~100ms (PBKDF2). Both are acceptable for an API that processes
    multi-second audio. No timing side-channel concern at this scale.
"""

import functools
import logging
import time

from django.conf import settings
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from studio.models import ApiKey

logger = logging.getLogger(__name__)

# ─── Rate limiter (Redis-backed, fail-open) ──────────────────────────────

def _get_redis():
    """Lazy Redis client from the channel layer's connection params."""
    try:
        import redis as redis_lib
        url = getattr(settings, "REDIS_URL", "redis://127.0.0.1:6379/0")
        return redis_lib.Redis.from_url(url, decode_responses=True,
                                        socket_timeout=2, socket_connect_timeout=2)
    except Exception as exc:
        logger.warning("api_auth: Redis unavailable for rate limiting: %s", exc)
        return None


def _check_rate_limit(fingerprint: str) -> bool:
    """
    Returns True if the request is ALLOWED, False if rate-limited.
    Fail-open: if Redis is down, allow the request.
    """
    limit = int(getattr(settings, "UPLOAD_RATE_LIMIT_PER_MINUTE", 10))
    r = _get_redis()
    if r is None:
        return True  # fail-open

    try:
        minute = int(time.time() // 60)
        key = f"ratelimit:{fingerprint}:{minute}"
        pipe = r.pipeline()
        pipe.incr(key)
        pipe.expire(key, 120)  # TTL > 60s to cover clock skew
        count, _ = pipe.execute()
        if int(count) > limit:
            logger.warning("api_auth: rate limit exceeded for key …%s "
                           "(%d/%d per minute)", fingerprint[:8], count, limit)
            return False
        return True
    except Exception as exc:
        logger.warning("api_auth: rate limit check failed (fail-open): %s", exc)
        return True  # fail-open


def _throttle_last_used(fingerprint: str, api_key_obj: ApiKey) -> None:
    """
    Write last_used_at to the DB at most once per minute per key.
    Uses Redis SETNX as the gate. Fail-open: if Redis is down, skip
    the write (last_used_at is informational, not security-critical).
    """
    r = _get_redis()
    if r is None:
        return
    try:
        gate_key = f"key_lastused:{fingerprint}"
        # SETNX returns True if the key was set (i.e. first call this minute)
        if r.set(gate_key, "1", nx=True, ex=60):
            api_key_obj.last_used_at = timezone.now()
            api_key_obj.save(update_fields=["last_used_at"])
    except Exception as exc:
        logger.debug("api_auth: last_used throttle skipped: %s", exc)


# ─── Decorator ───────────────────────────────────────────────────────────

def api_key_required(view_func):
    """
    Decorator for API views that require a valid bearer key.

    On success: attaches request.api_key (ApiKey instance) and
    request.api_user (User instance) and calls the view.

    On failure: returns a JsonResponse and does NOT call the view.
    401 for every authentication problem (missing header, empty
    token, unknown key, revoked key, hash mismatch) and 429 for rate
    limiting. There is no 403 path: a 403 here would mean
    CsrfViewMiddleware is back in the bearer path. The auth failure
    message never reveals whether the key exists (timing-oracle
    prevention): all such failures say
    "Invalid or revoked API key."

    CSRF: the RETURNED callable is csrf_exempt - see the module
    docstring. The attribute must land on the outermost callable,
    because that is the one CsrfViewMiddleware inspects; applying it
    to `view_func` or inside `wrapper` is invisible to the middleware
    and is caught by scripts/guard_layers.py G4 via resolve().func.
    """
    @functools.wraps(view_func)
    def wrapper(request, *args, **kwargs):
        auth_header = request.META.get("HTTP_AUTHORIZATION", "")
        if not auth_header.startswith("Bearer "):
            return JsonResponse(
                {"error": "Missing or malformed Authorization header. "
                          "Expected: 'Bearer <api-key>'"},
                status=401,
            )

        raw_key = auth_header[7:].strip()
        if not raw_key:
            return JsonResponse({"error": "Empty bearer token."}, status=401)

        # O(1) fingerprint lookup (unique indexed column)
        api_key = ApiKey.lookup_by_raw(raw_key)
        if api_key is None:
            # Either the key doesn't exist or it's revoked.
            # We do NOT distinguish these in the error message
            # (timing oracle: an attacker probing for valid keys
            # should not learn existence vs revocation from response
            # timing or content).
            return JsonResponse(
                {"error": "Invalid or revoked API key."},
                status=401,
            )

        # Explicit activity check. lookup_by_raw is implemented to filter
        # is_active=True, so a revoked key should already be invisible above —
        # but a SECURITY property must never be inherited from a collaborator's
        # implicit filter. If that filter is ever relaxed (e.g. to power an
        # audit/analytics view), this line still refuses revoked keys.
        # Identical message to the None branch: no oracle distinguishing
        # "key absent" from "key revoked".
        if not api_key.is_active:
            return JsonResponse(
                {"error": "Invalid or revoked API key."},
                status=401,
            )

        # Constant-time hash verification (defense-in-depth: the
        # fingerprint lookup already narrowed to one row, but we
        # verify the secret via PBKDF2 to guard against fingerprint
        # collisions or DB tampering).
        if not api_key.verify(raw_key):
            return JsonResponse(
                {"error": "Invalid or revoked API key."},
                status=401,
            )

        # Rate limit (per-key, per-minute, fail-open)
        if not _check_rate_limit(api_key.fingerprint):
            return JsonResponse(
                {"error": "Rate limit exceeded. Try again in 60 seconds.",
                 "retry_after": 60},
                status=429,
                headers={"Retry-After": "60"},
            )

        # Throttled last_used_at write
        _throttle_last_used(api_key.fingerprint, api_key)

        # Attach to request for downstream use
        request.api_key = api_key
        request.api_user = api_key.user

        return view_func(request, *args, **kwargs)

    # csrf_exempt MUST be applied to the callable we RETURN, not to
    # view_func and not inside wrapper: CsrfViewMiddleware.process_view
    # reads the attribute off the object the URL resolver hands back.
    # functools.wraps already propagated __name__/__doc__ through
    # wrapper, and csrf_exempt re-wraps with its own @wraps, so the
    # resolved callback still reports the real view name - which is
    # what guard_layers.py G4 asserts via resolve(path).func.
    return csrf_exempt(wrapper)
