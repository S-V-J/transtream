"""
TranStream admin registration (Phase 6a).

Purpose during wiring: let you SEE takes/jobs/segments/keys in /admin/ to
verify the persistence layer independently of the browser pipeline. All
externally-meaningful fields are read-only here (ids, fingerprints, hashes,
timestamps, paths) so an admin can inspect but never accidentally forge a
key hash or rewrite a UUID primary key.
"""

from django.contrib import admin

from studio.models import ApiKey, Job, Segment, Take


class JobInline(admin.TabularInline):
    model = Job
    extra = 0
    can_delete = False
    readonly_fields = (
        "id", "state", "attempts", "max_attempts", "progress_pct",
        "last_error", "lease_owner", "lease_expires_at", "next_run_at",
        "created_at", "started_at", "finished_at",
    )
    ordering = ("-created_at",)


class SegmentInline(admin.TabularInline):
    model = Segment
    extra = 0
    can_delete = True
    fields = ("source", "index", "start_ms", "end_ms", "duration_ms", "text")
    readonly_fields = ("created_at",)
    ordering = ("source", "index")
    max_num = 50  # keep the form manageable for long takes


@admin.register(Take)
class TakeAdmin(admin.ModelAdmin):
    list_display = ("short_id", "user", "kind", "language", "duration_ms", "state", "created_at")
    list_filter = ("kind", "language", "created_at")
    search_fields = ("id", "source_filename", "raw_relpath", "user__username")
    readonly_fields = (
        "id", "raw_relpath", "normalized_relpath", "language",
        "language_probability", "created_at", "updated_at",
    )
    inlines = [JobInline, SegmentInline]

    @admin.display(description="ID")
    def short_id(self, obj):
        return obj.id.hex[:8]

    @admin.display(description="Job state")
    def state(self, obj):
        return obj.state


@admin.register(Job)
class JobAdmin(admin.ModelAdmin):
    list_display = ("short_id", "take", "state", "progress_pct", "attempts", "max_attempts", "next_run_at", "created_at")
    list_filter = ("state", "created_at")
    search_fields = ("id", "take__id")
    readonly_fields = (
        "id", "take", "state", "attempts", "max_attempts", "last_error",
        "lease_owner", "lease_expires_at", "next_run_at", "progress_pct",
        "created_at", "updated_at", "started_at", "finished_at",
    )

    @admin.display(description="ID")
    def short_id(self, obj):
        return obj.id.hex[:8]


@admin.register(Segment)
class SegmentAdmin(admin.ModelAdmin):
    list_display = ("take", "source", "index", "start_ms", "end_ms", "text_preview")
    list_filter = ("source",)
    search_fields = ("take__id", "text")
    readonly_fields = ("created_at",)

    @admin.display(description="Text")
    def text_preview(self, obj):
        t = obj.text or ""
        return (t[:60] + "…") if len(t) > 60 else t


@admin.register(ApiKey)
class ApiKeyAdmin(admin.ModelAdmin):
    list_display = ("hint", "name", "user", "is_active", "created_at", "last_used_at", "revoked_at")
    list_filter = ("is_active", "created_at")
    search_fields = ("name", "hint", "user__username")
    # fingerprint + hashed_secret are NEVER editable or even shown in full.
    readonly_fields = ("id", "prefix", "fingerprint", "hashed_secret", "hint", "created_at", "last_used_at", "revoked_at")
    exclude = ("hashed_secret", "fingerprint")  # keep secrets off the change form entirely

    def has_add_permission(self, request):
        # Keys are minted only via ApiKey.create_for_user() (which returns the
        # raw secret once). Admin must not hand-craft a row with an unknown hash.
        return False
