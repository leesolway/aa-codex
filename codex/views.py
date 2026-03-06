from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required, permission_required
from django.contrib.auth.models import Group
from django.core.paginator import Paginator
from django.http import HttpResponseNotAllowed, JsonResponse, QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from corptools.models import CorporationHistory

from .models import (
    ChecklistItem,
    CodexConfiguration,
    MemberAuditLog,
    MemberChecklistCompletion,
    MemberNote,
    MemberRank,
    MemberTag,
    Rank,
    ReviewAcknowledgement,
    Tag,
    TagGroup,
)

User = get_user_model()


def _get_user_review_tiers(user):
    """Return the set of review tiers this user has permission to manage."""
    tiers = set()
    if user.has_perm("codex.manage_reviews_r1"):
        tiers.add(1)
    if user.has_perm("codex.manage_reviews_r2"):
        tiers.add(2)
    if user.has_perm("codex.manage_reviews_r3"):
        tiers.add(3)
    return tiers


def _can_review_member(user, rank):
    """Check if user has the review permission for the given rank's tier."""
    if not rank:
        return False
    return rank.review_tier in _get_user_review_tiers(user)


def detect_member_rank(main, alts, ranks_by_title):
    """Detect a member's rank from EVE titles across all characters.

    Returns (rank, rank_mismatch, all_ranks).

    Rank mismatch is only flagged when different characters resolve to
    different highest ranks (e.g. main is R1 but an alt is R2).  A single
    character holding multiple rank-related titles is not a mismatch.
    """
    characters = [main] + list(alts)
    per_char_ranks = []
    all_found_ranks = set()

    for char in characters:
        try:
            titles = char.characteraudit.characterroles.titles.all()
            char_ranks = set()
            for t in titles:
                if t.title in ranks_by_title:
                    char_ranks.add(ranks_by_title[t.title])
            if char_ranks:
                highest = max(char_ranks, key=lambda r: r.priority)
                per_char_ranks.append(highest)
                all_found_ranks.update(char_ranks)
        except Exception:
            continue

    if not per_char_ranks:
        return None, False, set()

    # Mismatch when characters disagree on their highest rank
    distinct_char_ranks = set(per_char_ranks)
    rank_mismatch = len(distinct_char_ranks) > 1

    highest = max(per_char_ranks, key=lambda r: r.priority)
    return highest, rank_mismatch, distinct_char_ranks


def _compute_service(main):
    """Compute service length string and days for a member's main character."""
    try:
        history = (
            CorporationHistory.objects.filter(character=main.characteraudit)
            .order_by("-start_date")
            .first()
        )
        if history:
            delta = timezone.now() - history.start_date
            days = delta.days
            if days >= 365:
                years = days // 365
                remaining_days = days % 365
                months = remaining_days // 30
                if months:
                    service_length = f"{years}y {months}m"
                else:
                    service_length = f"{years}y"
            elif days >= 30:
                service_length = f"{days // 30}m {days % 30}d"
            else:
                service_length = f"{days}d"
            return service_length, history.start_date, days
        return "", None, 0
    except Exception:
        return "", None, 0


def _review_status(rank, service_days, user, acknowledgements_by_user):
    """Return (review_due, days_overdue) for a member.

    review_due is True when a review is needed; days_overdue is how many
    days past the threshold (0 when not overdue).
    """
    if not rank or rank.review_threshold_days is None or service_days < rank.review_threshold_days:
        return False, 0

    acks = acknowledgements_by_user.get(user.pk, [])
    latest_ack = None
    for ack in acks:
        if ack.rank_id == rank.pk:
            if latest_ack is None or ack.acknowledged_at > latest_ack.acknowledged_at:
                latest_ack = ack

    if latest_ack is None:
        return True, service_days - rank.review_threshold_days

    elapsed = (timezone.now() - latest_ack.acknowledged_at).days
    overdue = elapsed - rank.review_threshold_days
    if overdue > 0:
        return True, overdue
    return timezone.now() - latest_ack.acknowledged_at > timedelta(days=rank.review_threshold_days), 0


def _build_members(users, ranks_by_title, acknowledgements_by_user, tags_by_user=None, ranks_by_user=None):
    """Build the member list with stored rank as source of truth.

    ranks_by_user maps user PK to a MemberRank instance. The stored rank is
    authoritative; EVE title detection is still used to flag mismatches (when
    a character's detected rank differs from the stored Codex rank).
    """
    if tags_by_user is None:
        tags_by_user = {}
    if ranks_by_user is None:
        ranks_by_user = {}
    members = []
    for user in users:
        main = user.profile.main_character
        if not main:
            continue

        alts = [
            o.character
            for o in user.character_ownerships.all()
            if o.character and o.character.character_id != main.character_id
        ]

        try:
            titles = ", ".join(
                t.title for t in main.characteraudit.characterroles.titles.all()
            )
        except Exception:
            titles = ""

        service_length, service_date, service_days = _compute_service(main)

        # Use stored rank as the authoritative rank
        member_rank_obj = ranks_by_user.get(user.pk)
        stored_rank = member_rank_obj.rank if member_rank_obj else None

        # Detect EVE-title rank to check for mismatches against stored rank
        detected_rank, _, all_ranks = detect_member_rank(main, alts, ranks_by_title)

        # Mismatch: any character's detected EVE-title rank differs from stored Codex rank
        if stored_rank and detected_rank:
            rank_mismatch = any(r != stored_rank for r in all_ranks)
        elif stored_rank and not detected_rank:
            # Stored rank but no EVE titles found — flag mismatch
            rank_mismatch = True
        else:
            rank_mismatch = False

        rank = stored_rank
        review_due, days_overdue = _review_status(rank, service_days, user, acknowledgements_by_user)

        # Check if member is inactive and has EVE rank titles
        user_tags = tags_by_user.get(user.pk, [])
        inactive_has_roles = False
        inactive_role_names = []
        is_inactive = any(
            t.name == "Inactive" and t.is_system for t in user_tags
        )
        if is_inactive:
            review_due = False
            days_overdue = 0
            rank_mismatch = False
            if all_ranks:
                inactive_has_roles = True
                inactive_role_names = [r.display_label for r in all_ranks]

        members.append(
            {
                "user": user,
                "main": main,
                "alts": alts,
                "alt_count": len(alts),
                "titles": titles,
                "service_length": service_length,
                "service_date": service_date,
                "service_days": service_days,
                "rank": rank,
                "rank_mismatch": rank_mismatch,
                "all_ranks": all_ranks,
                "review_due": review_due,
                "days_overdue": days_overdue,
                "tags": user_tags,
                "inactive_has_roles": inactive_has_roles,
                "inactive_role_names": inactive_role_names,
            }
        )

    members.sort(key=lambda m: m["main"].character_name)
    return members


def _assign_default_ranks(user_ids):
    """Assign the default rank to users who don't already have a MemberRank."""
    default_rank = Rank.objects.filter(default=True).first()
    if not default_rank:
        return

    existing_user_ids = set(
        MemberRank.objects.filter(user_id__in=user_ids).values_list("user_id", flat=True)
    )
    missing = [uid for uid in user_ids if uid not in existing_user_ids]
    if not missing:
        return

    to_create = [MemberRank(user_id=uid, rank=default_rank, assigned_by=None) for uid in missing]
    audit_entries = [
        MemberAuditLog(
            user_id=uid,
            actor=None,
            action_type="RANK_CHANGED",
            details=f"Default rank assigned: {default_rank.name}",
        )
        for uid in missing
    ]
    MemberRank.objects.bulk_create(to_create, ignore_conflicts=True)
    MemberAuditLog.objects.bulk_create(audit_entries)


def _bulk_fetch_ranks(user_ids):
    """Return a dict mapping user PK to MemberRank instance."""
    member_ranks = MemberRank.objects.filter(user_id__in=user_ids).select_related("rank")
    return {mr.user_id: mr for mr in member_ranks}


def _bulk_fetch_acks(user_ids):
    """Return a dict mapping user PK to list of ReviewAcknowledgement instances."""
    all_acks = ReviewAcknowledgement.objects.filter(user_id__in=user_ids).select_related(
        "rank", "acknowledged_by__profile__main_character"
    )
    acks_by_user = {}
    for ack in all_acks:
        acks_by_user.setdefault(ack.user_id, []).append(ack)
    return acks_by_user


def _bulk_fetch_tags(user_ids):
    """Return a dict mapping user PK to list of Tag instances."""
    all_member_tags = MemberTag.objects.filter(user_id__in=user_ids).select_related(
        "tag__group"
    )
    tags_by_user = {}
    for mt in all_member_tags:
        tags_by_user.setdefault(mt.user_id, []).append(mt.tag)
    return tags_by_user


def _assign_default_tags(user_ids):
    """Assign all default tags to users who don't already have them.

    For system tags (e.g. status tags), skip assignment if the user already
    has any sibling tag in the same system group — this respects mutual
    exclusivity set by set_status().
    """
    default_tags = list(Tag.objects.filter(default=True).select_related("group"))
    if not default_tags:
        return

    existing = set(
        MemberTag.objects.filter(
            user_id__in=user_ids,
            tag_id__in=[t.pk for t in default_tags],
        ).values_list("user_id", "tag_id")
    )

    # For system tags, check if the user already has ANY tag in the same system group
    system_groups = {t.group_id for t in default_tags if t.is_system}
    users_with_system_group_tag = set()
    if system_groups:
        users_with_system_group_tag = set(
            MemberTag.objects.filter(
                user_id__in=user_ids,
                tag__group_id__in=system_groups,
                tag__is_system=True,
            ).values_list("user_id", "tag__group_id")
        )

    to_create = []
    audit_entries = []
    for user_id in user_ids:
        for tag in default_tags:
            if (user_id, tag.pk) in existing:
                continue
            # Skip system default if user already has a sibling in that group
            if tag.is_system and (user_id, tag.group_id) in users_with_system_group_tag:
                continue
            to_create.append(
                MemberTag(user_id=user_id, tag_id=tag.pk, assigned_by=None)
            )
            audit_entries.append(
                MemberAuditLog(
                    user_id=user_id,
                    actor=None,
                    action_type="TAG_ADDED",
                    details=f"{tag.name} (default)",
                )
            )

    if to_create:
        MemberTag.objects.bulk_create(to_create, ignore_conflicts=True)
        MemberAuditLog.objects.bulk_create(audit_entries)


def _get_users_queryset(config):
    """Return the base users queryset with needed prefetches."""
    return (
        User.objects.filter(profile__state=config.aa_state)
        .select_related("profile__main_character")
        .prefetch_related(
            "character_ownerships__character__characteraudit__characterroles__titles",
            "groups",
        )
    )


def _get_former_users_queryset(config):
    """Return users who have codex data but are no longer in the configured state."""
    # Collect user IDs that have ANY codex data
    codex_user_ids = set()
    codex_user_ids.update(MemberTag.objects.values_list("user_id", flat=True))
    codex_user_ids.update(MemberNote.objects.values_list("user_id", flat=True))
    codex_user_ids.update(MemberAuditLog.objects.values_list("user_id", flat=True))
    codex_user_ids.update(
        MemberChecklistCompletion.objects.values_list("user_id", flat=True)
    )
    codex_user_ids.update(
        ReviewAcknowledgement.objects.values_list("user_id", flat=True)
    )

    if not codex_user_ids:
        return User.objects.none()

    # Subtract users currently in the configured state
    current_user_ids = set(
        User.objects.filter(profile__state=config.aa_state).values_list("pk", flat=True)
    )
    former_ids = codex_user_ids - current_user_ids

    if not former_ids:
        return User.objects.none()

    return (
        User.objects.filter(pk__in=former_ids)
        .select_related("profile__main_character")
        .prefetch_related("character_ownerships__character")
    )


def _build_former_members(users, tags_by_user=None):
    """Build member list for former members without corptools data."""
    if tags_by_user is None:
        tags_by_user = {}
    members = []
    for user in users:
        main = None
        character_name = user.username
        character_id = None
        corporation_name = ""
        corporation_id = None

        try:
            main = user.profile.main_character
        except Exception:
            pass

        if main:
            character_name = main.character_name
            character_id = main.character_id
            corporation_name = main.corporation_name
            corporation_id = main.corporation_id
        else:
            # Fallback: try first character ownership
            ownerships = list(user.character_ownerships.all())
            if ownerships and ownerships[0].character:
                char = ownerships[0].character
                character_name = char.character_name
                character_id = char.character_id
                corporation_name = char.corporation_name
                corporation_id = char.corporation_id

        alts = []
        if main:
            alts = [
                o.character
                for o in user.character_ownerships.all()
                if o.character and o.character.character_id != main.character_id
            ]

        members.append(
            {
                "user": user,
                "main": main,
                "character_name": character_name,
                "character_id": character_id,
                "corporation_name": corporation_name,
                "corporation_id": corporation_id,
                "alts": alts,
                "alt_count": len(alts),
                "titles": "",
                "service_length": "",
                "service_date": None,
                "service_days": 0,
                "rank": None,
                "rank_mismatch": False,
                "all_ranks": set(),
                "review_due": False,
                "is_former": True,
                "tags": tags_by_user.get(user.pk, []),
            }
        )

    members.sort(key=lambda m: m["character_name"])
    return members


@login_required
@permission_required("codex.view_corpmember")
def index(request):
    config = CodexConfiguration.get_solo()

    if not config.aa_state:
        return render(request, "codex/members.html", {"members": [], "state": None})

    users = _get_users_queryset(config)
    ranks_by_title = {r.eve_title: r for r in Rank.objects.all()}

    # Bulk-fetch acknowledgements
    user_ids = [u.pk for u in users]
    acks_by_user = _bulk_fetch_acks(user_ids)

    # Assign default tags and ranks to any members missing them
    _assign_default_tags(user_ids)
    _assign_default_ranks(user_ids)

    # Bulk-fetch ranks
    ranks_by_user = _bulk_fetch_ranks(user_ids)

    # Bulk-fetch tags
    tags_by_user = _bulk_fetch_tags(user_ids)

    members = _build_members(users, ranks_by_title, acks_by_user, tags_by_user, ranks_by_user)

    # Collect filter options
    all_tags = Tag.objects.select_related("group").order_by("group__order", "order")
    all_ranks = Rank.objects.order_by("priority")
    all_groups = Group.objects.filter(user__in=users).distinct().order_by("name")

    # Read filter params
    active_tag_ids = [int(x) for x in request.GET.getlist("tag") if x.isdigit()]
    active_rank_ids = [int(x) for x in request.GET.getlist("rank") if x.isdigit()]
    active_group_ids = [int(x) for x in request.GET.getlist("group") if x.isdigit()]
    title_filter = request.GET.get("title", "").strip()
    search_query = request.GET.get("search", "").strip()

    total_count = len(members)

    # Apply filters
    if active_tag_ids:
        tag_id_set = set(active_tag_ids)
        members = [m for m in members if tag_id_set & {t.pk for t in m["tags"]}]

    if active_rank_ids:
        rank_id_set = set(active_rank_ids)
        members = [m for m in members if m["rank"] and m["rank"].pk in rank_id_set]

    if title_filter:
        title_lower = title_filter.lower()
        members = [m for m in members if title_lower in m["titles"].lower()]

    if active_group_ids:
        group_id_set = set(active_group_ids)
        members = [
            m for m in members
            if group_id_set & {g.pk for g in m["user"].groups.all()}
        ]

    if search_query:
        search_lower = search_query.lower()
        members = [
            m for m in members
            if search_lower in m["main"].character_name.lower()
            or any(search_lower in alt.character_name.lower() for alt in m["alts"])
        ]

    filtered_count = len(members)

    # Paginate
    paginator = Paginator(members, 50)
    page_num = request.GET.get("page", 1)
    page_obj = paginator.get_page(page_num)

    has_active_filters = bool(active_tag_ids or active_rank_ids or active_group_ids or title_filter or search_query)

    # Build query string for pagination links (excludes 'page')
    filter_qd = QueryDict(mutable=True)
    for tid in active_tag_ids:
        filter_qd.appendlist("tag", str(tid))
    for rid in active_rank_ids:
        filter_qd.appendlist("rank", str(rid))
    for gid in active_group_ids:
        filter_qd.appendlist("group", str(gid))
    if title_filter:
        filter_qd["title"] = title_filter
    if search_query:
        filter_qd["search"] = search_query
    filter_query = filter_qd.urlencode()

    return render(
        request,
        "codex/members.html",
        {
            "members": page_obj,
            "page_obj": page_obj,
            "state": config.aa_state,
            "member_count": total_count,
            "filtered_count": filtered_count,
            "all_tags": all_tags,
            "all_ranks": all_ranks,
            "all_groups": all_groups,
            "active_tag_ids": active_tag_ids,
            "active_rank_ids": active_rank_ids,
            "active_group_ids": active_group_ids,
            "title_filter": title_filter,
            "has_active_filters": has_active_filters,
            "filter_query": filter_query,
            "search_query": search_query,
        },
    )


@login_required
def review(request):
    user_tiers = _get_user_review_tiers(request.user)
    if not user_tiers:
        return redirect("codex:index")

    config = CodexConfiguration.get_solo()

    if not config.aa_state:
        return render(request, "codex/review.html", {"members": [], "state": None})

    users = _get_users_queryset(config)
    ranks_by_title = {r.eve_title: r for r in Rank.objects.all()}

    # Bulk-fetch acknowledgements
    user_ids = [u.pk for u in users]
    acks_by_user = _bulk_fetch_acks(user_ids)

    # Assign default ranks to any members missing them
    _assign_default_ranks(user_ids)

    # Bulk-fetch ranks
    ranks_by_user = _bulk_fetch_ranks(user_ids)

    # Bulk-fetch tags
    tags_by_user = _bulk_fetch_tags(user_ids)

    members = _build_members(users, ranks_by_title, acks_by_user, tags_by_user, ranks_by_user)

    # Precompute next rank for each rank (next higher priority)
    all_ranks_ordered = list(Rank.objects.order_by("priority"))
    next_rank_map = {}
    for i, r in enumerate(all_ranks_ordered):
        if i + 1 < len(all_ranks_ordered):
            next_rank_map[r.pk] = all_ranks_ordered[i + 1]

    # Determine which checklist items each member's rank requires
    all_checklist_items = ChecklistItem.objects.select_related("rank").all()
    items_by_rank = {}
    for item in all_checklist_items:
        items_by_rank.setdefault(item.rank_id, []).append(item)

    # Bulk-fetch completions
    completions = MemberChecklistCompletion.objects.filter(
        user_id__in=user_ids
    ).select_related("checklist_item", "completed_by__profile__main_character")
    completions_by_user_item = {}
    for comp in completions:
        completions_by_user_item[(comp.user_id, comp.checklist_item_id)] = comp

    # Filter to flagged members and attach checklist info
    flagged = []
    action_needed = []
    for m in members:
        rank = m["rank"]

        # Only show members whose rank tier the user can manage
        if not rank or rank.review_tier not in user_tiers:
            continue

        checklist_items = items_by_rank.get(rank.pk, [])

        incomplete_checklist = False
        items_with_status = []
        for item in checklist_items:
            comp = completions_by_user_item.get((m["user"].pk, item.pk))
            items_with_status.append({"item": item, "completion": comp})
            if not comp:
                incomplete_checklist = True

        m["checklist_items"] = items_with_status
        m["incomplete_checklist"] = incomplete_checklist
        m["next_rank"] = next_rank_map.get(rank.pk)

        # Acknowledgement history for this user+rank
        m["acknowledgements"] = [
            a for a in acks_by_user.get(m["user"].pk, []) if a.rank_id == rank.pk
        ]

        if m["review_due"] or m["rank_mismatch"]:
            flagged.append(m)
        elif incomplete_checklist:
            action_needed.append(m)

    flagged.sort(key=lambda m: m["days_overdue"], reverse=True)
    action_needed.sort(key=lambda m: m["days_overdue"], reverse=True)

    # Collect ranks the user is permitted to see for filter options
    available_ranks = Rank.objects.filter(review_tier__in=user_tiers).order_by("priority")

    # Apply rank filter
    active_rank_ids = [int(x) for x in request.GET.getlist("rank") if x.isdigit()]
    total_flagged = len(flagged)
    total_action_needed = len(action_needed)

    if active_rank_ids:
        rank_id_set = set(active_rank_ids)
        flagged = [m for m in flagged if m["rank"] and m["rank"].pk in rank_id_set]
        action_needed = [m for m in action_needed if m["rank"] and m["rank"].pk in rank_id_set]

    return render(
        request,
        "codex/review.html",
        {
            "members": flagged,
            "action_needed": action_needed,
            "state": config.aa_state,
            "member_count": total_flagged,
            "action_needed_count": total_action_needed,
            "filtered_count": len(flagged),
            "filtered_action_needed_count": len(action_needed),
            "available_ranks": available_ranks,
            "active_rank_ids": active_rank_ids,
            "has_active_filters": bool(active_rank_ids),
        },
    )


@login_required
def toggle_checklist(request, user_id, item_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    member = get_object_or_404(User, pk=user_id)
    item = get_object_or_404(ChecklistItem, pk=item_id)

    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"

    # Check tier permission for the item's rank
    if not _can_review_member(request.user, item.rank):
        if is_ajax:
            return JsonResponse({"error": "forbidden"}, status=403)
        return redirect("codex:review")

    existing = MemberChecklistCompletion.objects.filter(
        checklist_item=item, user=member
    ).first()
    if existing:
        existing.delete()
        MemberAuditLog.objects.create(
            user=member,
            actor=request.user,
            action_type="CHECKLIST_UNCOMPLETED",
            details=item.name,
        )
        completed = False
    else:
        comp = MemberChecklistCompletion.objects.create(
            checklist_item=item, user=member, completed_by=request.user
        )
        MemberAuditLog.objects.create(
            user=member,
            actor=request.user,
            action_type="CHECKLIST_COMPLETED",
            details=item.name,
        )
        completed = True

    if is_ajax:
        actor_name = ""
        try:
            main = request.user.profile.main_character
            if main:
                actor_name = main.character_name
        except Exception:
            actor_name = request.user.username
        return JsonResponse({
            "completed": completed,
            "completed_by": actor_name if completed else "",
            "completed_at": timezone.now().strftime("%Y-%m-%d") if completed else "",
        })

    return redirect("codex:review")


@login_required
def acknowledge_review(request, user_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    member = get_object_or_404(User, pk=user_id)
    note = request.POST.get("note", "").strip()
    if not note:
        return redirect("codex:review")

    # Use stored rank from MemberRank
    member_rank = MemberRank.objects.filter(user=member).select_related("rank").first()
    rank = member_rank.rank if member_rank else None

    if rank and not _can_review_member(request.user, rank):
        return redirect("codex:review")
    if rank:
        ReviewAcknowledgement.objects.create(
            user=member,
            rank=rank,
            acknowledged_by=request.user,
            note=note,
        )
        MemberAuditLog.objects.create(
            user=member,
            actor=request.user,
            action_type="REVIEW_ACKNOWLEDGED",
            details=rank.name,
        )

    return redirect("codex:review")


@login_required
def set_rank(request, user_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    user_tiers = _get_user_review_tiers(request.user)
    if not user_tiers:
        return redirect("codex:index")

    member = get_object_or_404(User, pk=user_id)
    rank_id = request.POST.get("rank_id", "").strip()

    existing = MemberRank.objects.filter(user=member).select_related("rank").first()
    old_rank_name = existing.rank.name if existing else None

    if not rank_id or rank_id == "0":
        # Remove rank — only allowed if current rank's tier is within reviewer's tiers (or has no tier)
        if existing and existing.rank.review_tier and existing.rank.review_tier not in user_tiers:
            return redirect("codex:member_detail", user_id=user_id)
        if existing:
            existing.delete()
            MemberAuditLog.objects.create(
                user=member,
                actor=request.user,
                action_type="RANK_CHANGED",
                details=f"Rank removed (was {old_rank_name})",
            )
    else:
        new_rank = get_object_or_404(Rank, pk=int(rank_id))
        # Ranks with a review tier require the matching permission; ranks without a tier
        # (like R3/R4) are assignable by anyone with any review permission.
        if new_rank.review_tier and new_rank.review_tier not in user_tiers:
            return redirect("codex:member_detail", user_id=user_id)
        if existing:
            if existing.rank_id != new_rank.pk:
                existing.rank = new_rank
                existing.assigned_by = request.user
                existing.save()
                MemberAuditLog.objects.create(
                    user=member,
                    actor=request.user,
                    action_type="RANK_CHANGED",
                    details=f"Rank changed from {old_rank_name} to {new_rank.name}",
                )
        else:
            MemberRank.objects.create(
                user=member,
                rank=new_rank,
                assigned_by=request.user,
            )
            MemberAuditLog.objects.create(
                user=member,
                actor=request.user,
                action_type="RANK_CHANGED",
                details=f"Rank set to {new_rank.name}",
            )

    return redirect("codex:member_detail", user_id=user_id)


@login_required
@permission_required("codex.manage_tags")
def set_status(request, user_id):
    """Set a member's status tag (system-managed, mutually exclusive)."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    member = get_object_or_404(User, pk=user_id)
    tag_id = request.POST.get("tag_id", "").strip()
    if not tag_id:
        return redirect("codex:member_detail", user_id=user_id)

    new_tag = get_object_or_404(Tag, pk=int(tag_id), is_system=True)

    # Remove other tags in the same system group
    sibling_tags = Tag.objects.filter(group=new_tag.group, is_system=True).exclude(pk=new_tag.pk)
    removed = MemberTag.objects.filter(user=member, tag__in=sibling_tags)
    for mt in removed.select_related("tag"):
        MemberAuditLog.objects.create(
            user=member,
            actor=request.user,
            action_type="TAG_REMOVED",
            details=mt.tag.name,
        )
    removed.delete()

    # Add the new tag if not already present
    _, created = MemberTag.objects.get_or_create(
        user=member,
        tag=new_tag,
        defaults={"assigned_by": request.user},
    )
    if created:
        MemberAuditLog.objects.create(
            user=member,
            actor=request.user,
            action_type="TAG_ADDED",
            details=new_tag.name,
        )

    return redirect("codex:member_detail", user_id=user_id)


@login_required
@permission_required("codex.view_corpmember")
def manage_tags(request):
    target_user_id = request.GET.get("user_id") or request.POST.get("user_id")

    if target_user_id and int(target_user_id) != request.user.pk:
        if not request.user.has_perm("codex.manage_tags"):
            return redirect("codex:index")
        target_user = get_object_or_404(User, pk=target_user_id)
    else:
        target_user = request.user

    if request.method == "POST":
        selected_tag_ids = set(map(int, request.POST.getlist("tags")))
        existing = MemberTag.objects.filter(user=target_user)
        existing_tag_ids = set(existing.values_list("tag_id", flat=True))

        # Delete removed tags
        to_remove = existing_tag_ids - selected_tag_ids
        if to_remove:
            removed_tag_names = dict(
                Tag.objects.filter(pk__in=to_remove).values_list("pk", "name")
            )
            MemberTag.objects.filter(user=target_user, tag_id__in=to_remove).delete()
            for tag_id in to_remove:
                MemberAuditLog.objects.create(
                    user=target_user,
                    actor=request.user,
                    action_type="TAG_REMOVED",
                    details=removed_tag_names.get(tag_id, ""),
                )

        # Create new tags
        to_add = selected_tag_ids - existing_tag_ids
        added_tag_names = dict(
            Tag.objects.filter(pk__in=to_add).values_list("pk", "name")
        ) if to_add else {}
        for tag_id in to_add:
            MemberTag.objects.create(
                user=target_user,
                tag_id=tag_id,
                assigned_by=request.user,
            )
            MemberAuditLog.objects.create(
                user=target_user,
                actor=request.user,
                action_type="TAG_ADDED",
                details=added_tag_names.get(tag_id, ""),
            )

        return redirect("codex:index")

    # GET: render tag form (exclude system groups)
    tag_groups = TagGroup.objects.filter(is_system=False).prefetch_related("tags")
    current_tag_ids = set(
        MemberTag.objects.filter(user=target_user).values_list("tag_id", flat=True)
    )

    main_char = target_user.profile.main_character

    return render(
        request,
        "codex/tags.html",
        {
            "target_user": target_user,
            "main_char": main_char,
            "tag_groups": tag_groups,
            "current_tag_ids": current_tag_ids,
        },
    )


@login_required
def former_members(request):
    if not _get_user_review_tiers(request.user):
        return redirect("codex:index")
    config = CodexConfiguration.get_solo()

    if not config.aa_state:
        return render(
            request, "codex/former_members.html", {"members": [], "state": None}
        )

    users = _get_former_users_queryset(config)
    user_ids = [u.pk for u in users]

    # Bulk-fetch tags
    tags_by_user = _bulk_fetch_tags(user_ids)

    members = _build_former_members(users, tags_by_user)

    return render(
        request,
        "codex/former_members.html",
        {
            "members": members,
            "state": config.aa_state,
            "member_count": len(members),
        },
    )


@login_required
@permission_required("codex.view_corpmember")
def member_detail(request, user_id):
    config = CodexConfiguration.get_solo()
    if not config.aa_state:
        return redirect("codex:index")

    target_user = get_object_or_404(User, pk=user_id)

    # Determine if this is a current or former member
    is_former = not hasattr(target_user, "profile") or target_user.profile.state != config.aa_state

    # Former members require at least one review tier permission
    user_tiers = _get_user_review_tiers(request.user)
    if is_former and not user_tiers:
        return redirect("codex:index")

    # Acknowledgements for this user
    acks = ReviewAcknowledgement.objects.filter(user=target_user).select_related(
        "rank", "acknowledged_by__profile__main_character"
    )

    # Fetch tags
    member_tags = MemberTag.objects.filter(user=target_user).select_related("tag__group")
    tags_by_user = {target_user.pk: [mt.tag for mt in member_tags]}

    if is_former:
        members = _build_former_members([target_user], tags_by_user)
    else:
        ranks_by_title = {r.eve_title: r for r in Rank.objects.all()}
        acks_by_user = {target_user.pk: list(acks)}
        _assign_default_tags([target_user.pk])
        _assign_default_ranks([target_user.pk])
        ranks_by_user = _bulk_fetch_ranks([target_user.pk])
        members = _build_members([target_user], ranks_by_title, acks_by_user, tags_by_user, ranks_by_user)

    if not members:
        return redirect("codex:index")

    member = members[0]

    # Notes and audit log (visible if user has any review tier permission)
    has_any_review_perm = bool(user_tiers)
    notes = []
    audit_logs = []
    if has_any_review_perm:
        notes = MemberNote.objects.filter(user=target_user).select_related("author__profile__main_character")
        audit_logs = MemberAuditLog.objects.filter(user=target_user).select_related("actor__profile__main_character")

    # Fetch system status tags for the status selector
    status_group = TagGroup.objects.filter(is_system=True, name="Member Status").first()
    status_tags = list(status_group.tags.order_by("order")) if status_group else []
    current_status_tag = None
    if status_tags:
        member_tag_ids = {t.pk for t in member.get("tags", [])}
        for st in status_tags:
            if st.pk in member_tag_ids:
                current_status_tag = st
                break

    can_manage_tags = (
        request.user.pk == target_user.pk
        or request.user.has_perm("codex.manage_tags")
    )

    context = {
        "member": member,
        "notes": notes,
        "audit_logs": audit_logs,
        "acknowledgements": list(acks),
        "is_former": is_former,
        "can_manage_reviews": has_any_review_perm,
        "can_manage_tags": can_manage_tags,
        "all_ranks": Rank.objects.order_by("priority") if has_any_review_perm else Rank.objects.none(),
        "can_set_rank": has_any_review_perm and not is_former,
        "status_tags": status_tags,
        "current_status_tag": current_status_tag,
        "can_set_status": request.user.has_perm("codex.manage_tags") and not is_former,
    }
    return render(request, "codex/member_detail.html", context)


@login_required
def add_note(request, user_id):
    if not _get_user_review_tiers(request.user):
        return redirect("codex:index")
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    target_user = get_object_or_404(User, pk=user_id)
    content = request.POST.get("content", "").strip()
    if content:
        MemberNote.objects.create(
            user=target_user,
            author=request.user,
            content=content,
        )
        MemberAuditLog.objects.create(
            user=target_user,
            actor=request.user,
            action_type="NOTE_ADDED",
            details=content[:100],
        )

    return redirect("codex:member_detail", user_id=user_id)


@login_required
def promote_member(request, user_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    member = get_object_or_404(User, pk=user_id)
    member_rank = MemberRank.objects.filter(user=member).select_related("rank").first()
    if not member_rank:
        return redirect("codex:review")

    current_rank = member_rank.rank

    # Check reviewer has permission for the current rank's review tier
    if not _can_review_member(request.user, current_rank):
        return redirect("codex:review")

    # Find the next rank (next higher priority)
    next_rank = Rank.objects.filter(priority__gt=current_rank.priority).order_by("priority").first()
    if not next_rank:
        return redirect("codex:review")

    # Check all checklist items for the current rank are completed
    checklist_items = ChecklistItem.objects.filter(rank=current_rank)
    completed_count = MemberChecklistCompletion.objects.filter(
        user=member, checklist_item__in=checklist_items
    ).count()
    if completed_count < checklist_items.count():
        return redirect("codex:review")

    # Promote: update MemberRank
    old_rank_name = current_rank.name
    member_rank.rank = next_rank
    member_rank.assigned_by = request.user
    member_rank.save()

    MemberAuditLog.objects.create(
        user=member,
        actor=request.user,
        action_type="RANK_CHANGED",
        details=f"Promoted from {old_rank_name} to {next_rank.name}",
    )

    return redirect("codex:review")
