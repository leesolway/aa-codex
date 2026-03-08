from django.contrib import admin
from solo.admin import SingletonModelAdmin

from .models import ChecklistItem, CodexConfiguration, MemberAuditLog, MemberNote, MemberRank, Rank, Tag, TagGroup

admin.site.register(CodexConfiguration, SingletonModelAdmin)


class ChecklistItemInline(admin.TabularInline):
    model = ChecklistItem
    extra = 1


@admin.register(Rank)
class RankAdmin(admin.ModelAdmin):
    list_display = ("name", "display_label", "eve_title", "priority", "rank_type", "review_threshold_days", "review_tier", "default")
    list_filter = ("rank_type",)
    inlines = [ChecklistItemInline]


class TagInline(admin.TabularInline):
    model = Tag
    extra = 1

    def get_queryset(self, request):
        return super().get_queryset(request).filter(is_system=False)


@admin.register(TagGroup)
class TagGroupAdmin(admin.ModelAdmin):
    list_display = ("name", "order")
    inlines = [TagInline]

    def get_queryset(self, request):
        return super().get_queryset(request).filter(is_system=False)


@admin.register(MemberRank)
class MemberRankAdmin(admin.ModelAdmin):
    list_display = ("user", "rank", "rank_type_display", "assigned_by", "assigned_at")
    list_filter = ("rank", "rank__rank_type")
    readonly_fields = ("user", "rank", "assigned_by", "assigned_at")

    @admin.display(description="Type", ordering="rank__rank_type")
    def rank_type_display(self, obj):
        return obj.rank.get_rank_type_display()


@admin.register(MemberAuditLog)
class MemberAuditLogAdmin(admin.ModelAdmin):
    list_display = ("user", "actor", "action_type", "created_at")
    list_filter = ("action_type",)
    readonly_fields = ("user", "actor", "action_type", "details", "created_at")


@admin.register(MemberNote)
class MemberNoteAdmin(admin.ModelAdmin):
    list_display = ("user", "author", "created_at")
