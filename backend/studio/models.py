"""
TranStream persistence models (Phase 6a).

This module is the durable backbone that turns the working in-memory
pipeline (Phases 2-5.5) into a stored, retryable, API-addressable system.

Design rules encoded here (each one answers a question from the roadmap):

  * Take  = the ASSET (audio on disk + identified language).
    Job   = the PROCESSING STATE MACHINE (retry / lease / progress).
    Segment = the RESULT, stored as a DUAL TIMELINE:
        - source="vad"     -> acoustic speech regions from Silero (no text)
        - source="whisper" -> linguistic utterances from Faster-Whisper (text)
      These two segmentations of the same audio legitimately differ in count
      (Silero splits on silence; Whisper merges across short pauses). Storing
      both, tagged by source, is what enables future word-level alignment.
    ApiKey = per-user bearer credential for the future /api/v1 surface.

  * Persistence is best-effort at the CALLER level (see persistence.py):
    a DB failure must never close a WebSocket or drop a transcript. The
    models themselves are plain ORM; the non-fatal wrapping lives in the
    service layer so the schema stays clean and testable in isolation.

  * IDs: Take/Job/ApiKey use UUID primary keys because they are
    externally addressable (API paths, share links, logs). Segment uses the
    project default BigAutoField (internal, high-cardinality, never exposed).

  * Paths are stored RELATIVE to MEDIA_ROOT (portable across moves/backups);
    properties rehydrate absolute Paths on read.
"""

import hashlib
import uuid

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone


# ─── Enumerated value objects (TextChoices => readable in admin + migrations) ─

class TakeKind(models.TextChoices):
    MICROPHONE = "mic", "Microphone"
    UPLOAD = "upload", "File upload"


class JobState(models.TextChoices):
    QUEUED = "queued", "Queued"
    NORMALIZING = "normalizing", "Normalizing (FFmpeg)"
    SEGMENTING = "segmenting", "Segmenting (Silero VAD)"
    TRANSCRIBING = "transcribing", "Transcribing (Faster-Whisper)"
    DONE = "done", "Done"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"


class SegmentSource(models.TextChoices):
    VAD = "vad", "Silero VAD (acoustic)"
    WHISPER = "whisper", "Faster-Whisper (linguistic)"


# API key scopes (free-form strings stored in a JSON list; kept as constants
# so the future auth layer and the account UI agree on vocabulary).
SCOPE_TRANSCRIBE_WRITE = "transcribe:write"
SCOPE_TRANSCRIBE_READ = "transcribe:read"
DEFAULT_SCOPES = [SCOPE_TRANSCRIBE_WRITE, SCOPE_TRANSCRIBE_READ]

API_KEY_PREFIX = "ts_live_"


def _media_root():
    """Late import of MEDIA_ROOT so models module loads before settings fully
    resolve in exotic import orders (defensive; normally settings is ready)."""
    return settings.MEDIA_ROOT


class Take(models.Model):
    """
    One recorded/uploaded piece of audio + its identified language.

    A Take is created the moment raw bytes are durably on disk (mic finalize
    or upload stream-complete). It then owns one or more Jobs (retries create
    additional attempts on the SAME job row; the 1->N relation is kept for
    audit history, but in practice current_job is the live one).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="takes",
    )
    kind = models.CharField(
        max_length=16, choices=TakeKind.choices, default=TakeKind.MICROPHONE, db_index=True,
    )

    # Original name as it arrived (browser MediaRecorder label or upload filename).
    source_filename = models.CharField(max_length=512, blank=True, default="")

    # Paths RELATIVE to MEDIA_ROOT. raw_relpath is mandatory once finalized;
    # normalized_relpath is null until FFmpeg normalization succeeds.
    raw_relpath = models.CharField(max_length=1024, blank=True, default="")
    normalized_relpath = models.CharField(max_length=1024, null=True, blank=True)

    duration_ms = models.BigIntegerField(default=0)

    # Identified language (filled by the transcription stage; null until then).
    language = models.CharField(max_length=16, blank=True, default="", db_index=True)
    language_probability = models.FloatField(null=True, blank=True)

    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "-created_at"], name="take_user_recent_idx"),
            models.Index(fields=["kind", "-created_at"], name="take_kind_recent_idx"),
        ]

    def __str__(self):
        lang = self.language or "?"
        return f"Take {self.id.hex[:8]} · {self.kind} · {lang} · {self.duration_ms}ms"

    # ── path helpers (relpath <-> absolute) ──────────────────────────────
    @property
    def raw_file(self):
        """Absolute Path to the verbatim capture (may not exist if pruned)."""
        if not self.raw_relpath:
            return None
        p = _media_root() / self.raw_relpath
        return p if p.is_absolute() else (_media_root() / p)

    @property
    def normalized_file(self):
        if not self.normalized_relpath:
            return None
        return _media_root() / self.normalized_relpath

    @property
    def current_job(self):
        """The most recent Job for this take (the live state machine row)."""
        return self.jobs.order_by("-created_at").first()

    @property
    def state(self):
        job = self.current_job
        return job.state if job else JobState.QUEUED

    @property
    def whisper_segments(self):
        return self.segments.filter(source=SegmentSource.WHISPER).order_by("index")

    @property
    def vad_segments(self):
        return self.segments.filter(source=SegmentSource.VAD).order_by("index")


class Job(models.Model):
    """
    Processing state machine for a Take. Carries the retry / lease /
    concurrency substrate that the roadmap questions require:

      * attempts / max_attempts / last_error  -> retry + recovery (Q3)
      * lease_owner / lease_expires_at        -> safe concurrent workers (Q1)
      * next_run_at                           -> exponential backoff scheduling
      * progress_pct                          -> real progress for UI + API GET
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    take = models.ForeignKey(Take, on_delete=models.CASCADE, related_name="jobs")

    state = models.CharField(
        max_length=16, choices=JobState.choices, default=JobState.QUEUED, db_index=True,
    )

    attempts = models.PositiveSmallIntegerField(default=0)
    max_attempts = models.PositiveSmallIntegerField(default=3)
    last_error = models.TextField(blank=True, default="")

    # Concurrency / recovery.
    lease_owner = models.CharField(max_length=128, blank=True, default="")
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    next_run_at = models.DateTimeField(default=timezone.now, db_index=True)

    progress_pct = models.PositiveSmallIntegerField(
        default=0, validators=[MinValueValidator(0), MaxValueValidator(100)],
    )

    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            # The worker's hot poll: "give me the next runnable job in a
            # non-terminal state whose backoff has elapsed".
            models.Index(fields=["state", "next_run_at"], name="job_runnable_idx"),
            # The recovery sweep: "find leases that expired while a worker died".
            models.Index(fields=["lease_expires_at"], name="job_lease_idx"),
        ]

    def __str__(self):
        return f"Job {self.id.hex[:8]} · {self.state} · {self.progress_pct}% · a{self.attempts}/{self.max_attempts}"

    @property
    def is_terminal(self):
        return self.state in {JobState.DONE, JobState.FAILED, JobState.CANCELLED}

    @property
    def can_retry(self):
        return self.state == JobState.FAILED and self.attempts < self.max_attempts


class Segment(models.Model):
    """
    A single timed result row, tagged by source so the acoustic (VAD) and
    linguistic (Whisper) timelines coexist without collision.

    Idempotency: unique_together (take, source, index) + delete-then-insert
    in persistence.py means re-running a stage for the same take overwrites
    cleanly instead of duplicating rows. This is what makes retries safe.
    """

    take = models.ForeignKey(Take, on_delete=models.CASCADE, related_name="segments")
    source = models.CharField(max_length=16, choices=SegmentSource.choices, db_index=True)
    index = models.PositiveIntegerField()

    start_ms = models.BigIntegerField(default=0)
    end_ms = models.BigIntegerField(default=0)
    duration_ms = models.BigIntegerField(default=0)

    # Empty for VAD rows (acoustic regions carry no text).
    text = models.TextField(blank=True, default="")
    confidence = models.FloatField(null=True, blank=True)

    # Reserved for word-level timestamps (Phase: alignment). JSON list of
    # {"word","start_ms","end_ms","confidence"} or null.
    word_json = models.JSONField(null=True, blank=True)

    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        ordering = ["source", "index"]
        constraints = [
            models.UniqueConstraint(fields=["take", "source", "index"], name="uniq_segment_per_source"),
        ]
        indexes = [
            models.Index(fields=["take", "source"], name="seg_take_source_idx"),
        ]

    def __str__(self):
        preview = (self.text[:40] + "…") if len(self.text) > 40 else self.text
        return f"[{self.source}#{self.index}] {self.start_ms}->{self.end_ms}ms {preview!r}"


class ApiKey(models.Model):
    """
    Per-user bearer credential for the future /api/v1 surface.

    Security model (no invented crypto):
      * The RAW key (e.g. "ts_live_<43-char secret>") is generated once,
        returned to the caller a SINGLE time, and NEVER stored.
      * `fingerprint` = SHA-256(raw key) hexdigest, stored UNIQUE+INDEXED.
        It is irreversible and gives O(1) lookup of the owning row from a
        presented key without scanning/hashing every key.
      * `hashed_secret` = Django make_password(raw key) (PBKDF2, salted,
        constant-time compare). Defense-in-depth: even if the fingerprint
        index were ever abused, the secret itself is only ever checked
        against a slow hash, never compared directly.
      * `hint` is a non-secret display fragment ("ts_live_ab12…9xyz") so the
        account list can show WHICH key is which without exposing it.
      * Revocation sets is_active=False + revoked_at; verify() refuses
        inactive keys. Rotation (next turn, view-level) = revoke old + create new.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="api_keys",
    )
    name = models.CharField(max_length=128)

    prefix = models.CharField(max_length=16, default=API_KEY_PREFIX)
    fingerprint = models.CharField(max_length=64, unique=True, db_index=True)
    hashed_secret = models.CharField(max_length=255)
    hint = models.CharField(max_length=64, blank=True, default="")

    scopes = models.JSONField(default=list, blank=True)

    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "is_active"], name="key_user_active_idx"),
        ]

    def __str__(self):
        state = "active" if self.is_active else "revoked"
        return f"ApiKey {self.hint} · {self.name} · {state}"

    # ── lifecycle ────────────────────────────────────────────────────────
    @classmethod
    def create_for_user(cls, user, name, scopes=None):
        """
        Mint a key. Returns (instance, raw_key). The raw_key MUST be shown to
        the user exactly once; it is not recoverable afterwards.
        """
        import secrets

        secret = secrets.token_urlsafe(32)            # ~256 bits of entropy
        raw_key = f"{API_KEY_PREFIX}{secret}"
        fp = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
        obj = cls.objects.create(
            user=user,
            name=name,
            prefix=API_KEY_PREFIX,
            fingerprint=fp,
            hashed_secret=make_password(raw_key),
            hint=cls._make_hint(raw_key),
            scopes=list(scopes) if scopes else list(DEFAULT_SCOPES),
        )
        return obj, raw_key

    @staticmethod
    def _make_hint(raw_key):
        if len(raw_key) <= 16:
            return raw_key[:4] + "…"
        return f"{raw_key[:12]}…{raw_key[-4:]}"

    def verify(self, raw_key):
        """
        Constant-time-ish verification of a presented key against this row.
        Callers should first locate the row by fingerprint (O(1)), then call
        this to confirm the secret via the slow hash.
        """
        if not self.is_active:
            return False
        return check_password(raw_key, self.hashed_secret)

    @classmethod
    def lookup_by_raw(cls, raw_key):
        """O(1) row resolution by irreversible fingerprint (no hash scan)."""
        fp = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
        return cls.objects.filter(fingerprint=fp, is_active=True).first()

    def revoke(self):
        self.is_active = False
        self.revoked_at = timezone.now()
        self.save(update_fields=["is_active", "revoked_at", "updated_at"] if False else ["is_active", "revoked_at"])
