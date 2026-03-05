from django.conf import settings
from django.db import models
from solo.models import SingletonModel

from allianceauth.authentication.models import State


class CodexConfiguration(SingletonModel):
    aa_state = models.ForeignKey(
        State,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        help_text="Only members in this state will be shown in the corporation codex.",
    )

    class Meta:
        permissions = [
            ("view_corpmember", "Can view corporation member list"),
            ("manage_reviews_r1", "Can manage Tier 1 (R1) reviews"),
            ("manage_reviews_r2", "Can manage Tier 2 (R2) reviews"),
            ("manage_reviews_r3", "Can manage Tier 3 (R3) reviews"),
            ("manage_tags", "Can manage tags for any member"),
        ]

    def __str__(self):
        return "Codex Configuration"


class TagGroup(models.Model):
    name = models.CharField(max_length=100)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order"]

    def __str__(self):
        return self.name


class Tag(models.Model):
    group = models.ForeignKey(TagGroup, on_delete=models.CASCADE, related_name="tags")
    name = models.CharField(max_length=100)
    color = models.CharField(max_length=20, default="secondary")
    default = models.BooleanField(
        default=False,
        help_text="Automatically assign this tag to new members.",
    )
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order"]

    def __str__(self):
        return f"{self.group.name}: {self.name}"


class MemberTag(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="codex_tags",
    )
    tag = models.ForeignKey(Tag, on_delete=models.CASCADE)
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="codex_tags_assigned",
    )
    assigned_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("user", "tag")]

    def __str__(self):
        return f"{self.user} - {self.tag.name}"


class Rank(models.Model):
    REVIEW_TIER_CHOICES = [
        (1, "Tier 1 (R1)"),
        (2, "Tier 2 (R2)"),
        (3, "Tier 3 (R3)"),
    ]

    name = models.CharField(max_length=50, unique=True)
    display_label = models.CharField(max_length=100)
    eve_title = models.CharField(
        max_length=500,
        help_text="Exact EVE title to match against character titles.",
    )
    priority = models.PositiveIntegerField(
        help_text="Lower = lower rank. Used for ordering.",
    )
    review_threshold_days = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Days of service before a review is due. Leave blank if this rank should not be reviewed.",
    )
    review_tier = models.PositiveIntegerField(
        choices=REVIEW_TIER_CHOICES,
        null=True,
        blank=True,
        help_text="Review permission tier. Leave blank if this rank should not be reviewed.",
    )
    default = models.BooleanField(
        default=False,
        help_text="Assign this rank to new members when they join the state.",
    )

    class Meta:
        ordering = ["priority"]

    def __str__(self):
        return f"{self.name} ({self.display_label})"


class ChecklistItem(models.Model):
    rank = models.ForeignKey(
        Rank, on_delete=models.CASCADE, related_name="checklist_items"
    )
    name = models.CharField(max_length=200)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order"]

    def __str__(self):
        return f"{self.rank.name}: {self.name}"


class MemberChecklistCompletion(models.Model):
    checklist_item = models.ForeignKey(ChecklistItem, on_delete=models.CASCADE)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="codex_checklist_completions",
    )
    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="codex_completions_given",
    )
    completed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("checklist_item", "user")]

    def __str__(self):
        return f"{self.user} - {self.checklist_item}"


class ReviewAcknowledgement(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="codex_review_acknowledgements",
    )
    rank = models.ForeignKey(Rank, on_delete=models.CASCADE)
    acknowledged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="codex_acknowledgements_given",
    )
    acknowledged_at = models.DateTimeField(auto_now_add=True)
    note = models.TextField(help_text="Reason for extending / acknowledging review.")

    class Meta:
        ordering = ["-acknowledged_at"]

    def __str__(self):
        return f"Review ack for {self.user} at {self.rank.name}"


class MemberRank(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="codex_rank",
    )
    rank = models.ForeignKey(Rank, on_delete=models.CASCADE)
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="codex_rank_assignments",
    )
    assigned_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user} - {self.rank.name}"


class MemberAuditLog(models.Model):
    ACTION_CHOICES = [
        ("TAG_ADDED", "Tag Added"),
        ("TAG_REMOVED", "Tag Removed"),
        ("CHECKLIST_COMPLETED", "Checklist Completed"),
        ("CHECKLIST_UNCOMPLETED", "Checklist Uncompleted"),
        ("REVIEW_ACKNOWLEDGED", "Review Acknowledged"),
        ("NOTE_ADDED", "Note Added"),
        ("RANK_CHANGED", "Rank Changed"),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="codex_audit_logs",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="codex_audit_actions",
    )
    action_type = models.CharField(max_length=30, choices=ACTION_CHOICES)
    details = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_action_type_display()} on {self.user} by {self.actor}"


class MemberNote(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="codex_notes",
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="codex_notes_written",
    )
    content = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Note on {self.user} by {self.author}"
