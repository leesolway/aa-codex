from django.contrib import admin
from solo.admin import SingletonModelAdmin

from .models import ChecklistItem, CodexConfiguration, MemberAuditLog, MemberNote, Rank, Tag, TagGroup

admin.site.register(CodexConfiguration, SingletonModelAdmin)


class ChecklistItemInline(admin.TabularInline):
    model = ChecklistItem
    extra = 1


@admin.register(Rank)
class RankAdmin(admin.ModelAdmin):
    list_display = ("name", "display_label", "eve_title", "priority", "review_threshold_days", "review_tier")
    inlines = [ChecklistItemInline]


class TagInline(admin.TabularInline):
    model = Tag
    extra = 1


@admin.register(TagGroup)
class TagGroupAdmin(admin.ModelAdmin):
    list_display = ("name", "order")
    inlines = [TagInline]


@admin.register(MemberAuditLog)
class MemberAuditLogAdmin(admin.ModelAdmin):
    list_display = ("user", "actor", "action_type", "created_at")
    list_filter = ("action_type",)
    readonly_fields = ("user", "actor", "action_type", "details", "created_at")


@admin.register(MemberNote)
class MemberNoteAdmin(admin.ModelAdmin):
    list_display = ("user", "author", "created_at")
