import logging

from django.dispatch import receiver

from allianceauth.authentication.signals import state_changed

from .models import CodexConfiguration, MemberAuditLog, MemberRank, Rank

logger = logging.getLogger(__name__)


@receiver(state_changed)
def assign_default_rank_on_state_change(sender, user, state, **kwargs):
    """Assign the default rank to a user when they enter the configured state."""
    config = CodexConfiguration.get_solo()
    if not config.aa_state or state != config.aa_state:
        return

    if MemberRank.objects.filter(user=user).exists():
        return

    default_rank = Rank.objects.filter(default=True).first()
    if not default_rank:
        return

    MemberRank.objects.create(
        user=user,
        rank=default_rank,
        assigned_by=None,
    )
    MemberAuditLog.objects.create(
        user=user,
        actor=None,
        action_type="RANK_CHANGED",
        details=f"Default rank assigned: {default_rank.name}",
    )
    logger.info("Assigned default rank %s to user %s", default_rank.name, user)
