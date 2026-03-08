from datetime import timedelta
from functools import wraps

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


def _log_audit(user, actor, action_type, details=""):
    """Create a MemberAuditLog entry."""
    MemberAuditLog.objects.create(user=user, actor=actor, action_type=action_type, details=details)


def _get_primary_member_rank(user):
    """Return the user's PRIMARY MemberRank (with rank select_related), or None."""
    return MemberRank.objects.filter(
        user=user, rank__rank_type="PRIMARY"
    ).select_related("rank").first()


def _get_character_name(user):
    """Return the character name for a user, falling back to username."""
    try:
        if user.profile.main_character:
            return user.profile.main_character.character_name
    except Exception:
        pass
    return user.username


def _review_tier_required(view_func):
    """Decorator that redirects to index if the user has no review tier permissions."""
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not _get_user_review_tiers(request.user):
            return redirect("codex:index")
        return view_func(request, *args, **kwargs)
    return wrapper


def _prepare_members(config):
    """Fetch users, bulk-load data, assign defaults, build member list.

    Returns (members, user_ids, acks_by_user).
    """
    users = _get_users_queryset(config)
    ranks_by_title = {r.eve_title: r for r in Rank.objects.all()}
    user_ids = [u.pk for u in users]
    acks_by_user = _bulk_fetch_acks(user_ids)
    _assign_default_tags(user_ids)
    ranks_by_user = _bulk_fetch_ranks(user_ids)
    tags_by_user = _bulk_fetch_tags(user_ids)
    members = _build_members(users, ranks_by_title, acks_by_user, tags_by_user, ranks_by_user)
    return members, user_ids, acks_by_user


def detect_member_titles(main, alts, ranks_by_title):
    """Detect a member's ranks and special roles from EVE titles across all characters.

    Returns a dict:
        primary: highest detected PRIMARY rank or None
        primary_mismatch: bool — True when characters disagree on highest primary rank
        detected_primaries: set of PRIMARY ranks found
        detected_specials: set of SPECIAL ranks found
        char_primary_map: list of (character_name, rank) — per-character highest primary
        char_special_map: list of (character_name, rank) — per-character special roles found
    """
    characters = [main] + list(alts)
    per_char_primaries = []
    all_found_primaries = set()
    all_found_specials = set()
    char_primary_map = []
    char_special_map = []

    for char in characters:
        try:
            titles = char.characteraudit.characterroles.titles.all()
            char_primaries = set()
            for t in titles:
                rank = ranks_by_title.get(t.title)
                if rank:
                    if rank.is_special:
                        all_found_specials.add(rank)
                        char_special_map.append((char.character_name, rank))
                    else:
                        char_primaries.add(rank)
            if char_primaries:
                highest = max(char_primaries, key=lambda r: r.priority)
                per_char_primaries.append(highest)
                all_found_primaries.update(char_primaries)
                char_primary_map.append((char.character_name, highest))
        except Exception:
            continue

    primary = None
    primary_mismatch = False
    if per_char_primaries:
        distinct = set(per_char_primaries)
        primary_mismatch = len(distinct) > 1
        primary = max(per_char_primaries, key=lambda r: r.priority)

    return {
        "primary": primary,
        "primary_mismatch": primary_mismatch,
        "detected_primaries": all_found_primaries,
        "detected_specials": all_found_specials,
        "char_primary_map": char_primary_map,
        "char_special_map": char_special_map,
    }


def _bulk_fetch_service_history(users):
    """Return dict mapping CharacterAudit PK to the latest CorporationHistory."""
    char_audit_ids = []
    for user in users:
        main = user.profile.main_character
        if not main:
            continue
        try:
            char_audit_ids.append(main.characteraudit.pk)
        except Exception:
            continue

    if not char_audit_ids:
        return {}

    # Get all corp history for these characters, ordered newest-first
    histories = (
        CorporationHistory.objects.filter(character_id__in=char_audit_ids)
        .order_by("character_id", "-start_date")
    )

    # Keep only the latest entry per character
    result = {}
    for h in histories:
        if h.character_id not in result:
            result[h.character_id] = h

    return result


def _compute_service(main, history=None):
    """Compute service length string and days for a member's main character.

    Uses provided history or falls back to DB query.
    """
    try:
        if history is None:
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

    ranks_by_user maps user PK to a list of MemberRank instances. The stored
    primary rank is authoritative; EVE title detection is still used to flag
    mismatches (when a character's detected rank differs from the stored Codex rank).
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

        # Extract stored primary rank and special roles from MemberRank list
        user_member_ranks = ranks_by_user.get(user.pk, [])
        stored_rank = None
        stored_specials = set()
        for mr in user_member_ranks:
            if mr.rank.is_primary:
                stored_rank = mr.rank
            elif mr.rank.is_special:
                stored_specials.add(mr.rank)

        # Detect EVE-title ranks to check for mismatches
        detection = detect_member_titles(main, alts, ranks_by_title)
        detected_primary = detection["primary"]
        detected_primaries = detection["detected_primaries"]
        detected_specials = detection["detected_specials"]
        char_primary_map = detection["char_primary_map"]
        char_special_map = detection["char_special_map"]

        # Primary mismatch: detected EVE-title rank differs from stored Codex rank
        rank_mismatch = False
        rank_mismatch_details = []
        if stored_rank and detected_primary:
            if any(r != stored_rank for r in detected_primaries):
                rank_mismatch = True
                for char_name, char_rank in char_primary_map:
                    if char_rank != stored_rank:
                        rank_mismatch_details.append(
                            f"{char_name} has EVE title for {char_rank.display_label}"
                            f" — should be {stored_rank.display_label}"
                        )
        elif stored_rank and not detected_primary:
            rank_mismatch = True
            rank_mismatch_details.append(
                f"No character has an EVE title matching {stored_rank.display_label}"
                f" — EVE titles need to be updated"
            )

        # Special role mismatches
        missing_specials = detected_specials - stored_specials
        extra_specials = stored_specials - detected_specials
        special_mismatch = bool(missing_specials or extra_specials)
        special_mismatch_details = []
        if special_mismatch:
            for rank in sorted(missing_specials, key=lambda r: r.priority):
                # EVE title present but role not assigned in Codex — title should be removed
                chars_with = [cn for cn, r in char_special_map if r == rank]
                special_mismatch_details.append({
                    "rank": rank,
                    "type": "title_not_in_codex",
                    "chars": chars_with,
                })
            for rank in sorted(extra_specials, key=lambda r: r.priority):
                # Role assigned in Codex but EVE title missing — title needs to be added
                special_mismatch_details.append({
                    "rank": rank,
                    "type": "codex_not_in_title",
                    "chars": [],
                })

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
            special_mismatch = False
            missing_specials = set()
            extra_specials = set()
            all_eve_ranks = detected_primaries | detected_specials
            if all_eve_ranks:
                inactive_has_roles = True
                inactive_role_names = [r.display_label for r in all_eve_ranks]

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
                "rank_mismatch_details": rank_mismatch_details,
                "all_ranks": detected_primaries,
                "special_roles": sorted(stored_specials, key=lambda r: r.priority),
                "special_mismatch": special_mismatch,
                "special_mismatch_details": special_mismatch_details,
                "missing_specials": sorted(missing_specials, key=lambda r: r.priority),
                "extra_specials": sorted(extra_specials, key=lambda r: r.priority),
                "review_due": review_due,
                "days_overdue": days_overdue,
                "tags": user_tags,
                "inactive_has_roles": inactive_has_roles,
                "inactive_role_names": inactive_role_names,
            }
        )

    members.sort(key=lambda m: m["main"].character_name)
    return members



def _bulk_fetch_ranks(user_ids):
    """Return a dict mapping user PK to list of MemberRank instances."""
    member_ranks = MemberRank.objects.filter(user_id__in=user_ids).select_related("rank")
    result = {}
    for mr in member_ranks:
        result.setdefault(mr.user_id, []).append(mr)
    return result


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
    """Return users in the configured state that have codex data (a MemberRank)."""
    codex_user_ids = set(
        MemberRank.objects.values_list("user_id", flat=True)
    )
    return (
        User.objects.filter(profile__state=config.aa_state, pk__in=codex_user_ids)
        .select_related("profile__main_character")
        .prefetch_related(
            "character_ownerships__character__characteraudit__characterroles__titles",
            "groups",
        )
    )


def _get_former_users_queryset(config):
    """Return users who have a MemberRank but are no longer in the configured state."""
    # Only users with a MemberRank were ever tracked members
    tracked_user_ids = set(
        MemberRank.objects.values_list("user_id", flat=True)
    )

    if not tracked_user_ids:
        return User.objects.none()

    # Subtract users currently in the configured state
    current_user_ids = set(
        User.objects.filter(profile__state=config.aa_state).values_list("pk", flat=True)
    )
    former_ids = tracked_user_ids - current_user_ids

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
def dashboard(request):
    config = CodexConfiguration.get_solo()

    if not config.aa_state:
        return render(request, "codex/dashboard.html", {
            "state": None,
            "total_members": 0,
            "reviews_due": 0,
            "title_mismatches": 0,
            "inactive_with_roles": 0,
            "issue_members": [],
        })

    members, user_ids, acks_by_user = _prepare_members(config)

    # Compute stats
    total_members = len(members)
    reviews_due = sum(1 for m in members if m["review_due"])
    title_mismatches = sum(1 for m in members if m["rank_mismatch"] or m["special_mismatch"])
    inactive_with_roles = sum(1 for m in members if m["inactive_has_roles"])

    # Filter to members with any issue
    issue_members = [
        m for m in members
        if m["review_due"] or m["rank_mismatch"] or m["special_mismatch"] or m["inactive_has_roles"]
    ]

    # Sort by severity: inactive_has_roles (0) > rank/special mismatch (1) > review_due (2)
    def severity_key(m):
        if m["inactive_has_roles"]:
            return (0, -m["days_overdue"], m["main"].character_name)
        if m["rank_mismatch"] or m["special_mismatch"]:
            return (1, -m["days_overdue"], m["main"].character_name)
        return (2, -m["days_overdue"], m["main"].character_name)

    issue_members.sort(key=severity_key)

    return render(request, "codex/dashboard.html", {
        "state": config.aa_state,
        "total_members": total_members,
        "reviews_due": reviews_due,
        "title_mismatches": title_mismatches,
        "inactive_with_roles": inactive_with_roles,
        "issue_members": issue_members,
    })


@login_required
@permission_required("codex.view_corpmember")
def member_list(request):
    config = CodexConfiguration.get_solo()

    if not config.aa_state:
        return render(request, "codex/members.html", {"members": [], "state": None})

    members, user_ids, acks_by_user = _prepare_members(config)

    # Collect filter options
    all_tags = Tag.objects.select_related("group").order_by("group__order", "order")
    all_ranks = Rank.objects.order_by("priority")
    all_groups = Group.objects.filter(user__in=user_ids).distinct().order_by("name")

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
@_review_tier_required
def review(request):
    user_tiers = _get_user_review_tiers(request.user)

    config = CodexConfiguration.get_solo()

    if not config.aa_state:
        return render(request, "codex/review.html", {"members": [], "state": None})

    members, user_ids, acks_by_user = _prepare_members(config)

    # Precompute next rank for each PRIMARY rank (next higher priority)
    all_ranks_ordered = list(Rank.objects.filter(rank_type="PRIMARY").order_by("priority"))
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

        if m["review_due"] or m["rank_mismatch"] or m.get("special_mismatch"):
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
        _log_audit(member, request.user, "CHECKLIST_UNCOMPLETED", item.name)
        completed = False
    else:
        MemberChecklistCompletion.objects.create(
            checklist_item=item, user=member, completed_by=request.user
        )
        _log_audit(member, request.user, "CHECKLIST_COMPLETED", item.name)
        completed = True

    if is_ajax:
        actor_name = _get_character_name(request.user)
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

    # Use stored PRIMARY rank from MemberRank
    member_rank = _get_primary_member_rank(member)
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
        _log_audit(member, request.user, "REVIEW_ACKNOWLEDGED", rank.name)

    return redirect("codex:review")


@login_required
def set_rank(request, user_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    user_tiers = _get_user_review_tiers(request.user)
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    if not user_tiers:
        if is_ajax:
            return JsonResponse({"error": "forbidden"}, status=403)
        return redirect("codex:index")

    member = get_object_or_404(User, pk=user_id)
    rank_id = request.POST.get("rank_id", "").strip()

    if not rank_id or rank_id == "0":
        # Remove primary rank
        existing = _get_primary_member_rank(member)
        if existing and existing.rank.review_tier and existing.rank.review_tier not in user_tiers:
            if is_ajax:
                return JsonResponse({"error": "forbidden"}, status=403)
            return redirect("codex:member_detail", user_id=user_id)
        if existing:
            old_rank_name = existing.rank.name
            existing.delete()
            _log_audit(member, request.user, "RANK_CHANGED", f"Rank removed (was {old_rank_name})")
    else:
        new_rank = get_object_or_404(Rank, pk=int(rank_id))
        if new_rank.review_tier and new_rank.review_tier not in user_tiers:
            if is_ajax:
                return JsonResponse({"error": "forbidden"}, status=403)
            return redirect("codex:member_detail", user_id=user_id)

        if new_rank.is_primary:
            # Primary ranks are mutually exclusive — replace any existing PRIMARY
            existing = _get_primary_member_rank(member)
            old_rank_name = existing.rank.name if existing else None
            if existing:
                if existing.rank_id != new_rank.pk:
                    existing.rank = new_rank
                    existing.assigned_by = request.user
                    existing.save()
                    _log_audit(member, request.user, "RANK_CHANGED", f"Rank changed from {old_rank_name} to {new_rank.name}")
            else:
                MemberRank.objects.create(
                    user=member,
                    rank=new_rank,
                    assigned_by=request.user,
                )
                _log_audit(member, request.user, "RANK_CHANGED", f"Rank set to {new_rank.name}")
        else:
            # Special roles: toggle (add if missing, remove if present)
            existing = MemberRank.objects.filter(user=member, rank=new_rank).first()
            if existing:
                existing.delete()
                _log_audit(member, request.user, "RANK_CHANGED", f"Special role removed: {new_rank.name}")
            else:
                MemberRank.objects.create(
                    user=member,
                    rank=new_rank,
                    assigned_by=request.user,
                )
                _log_audit(member, request.user, "RANK_CHANGED", f"Special role added: {new_rank.name}")

    if is_ajax:
        primary_mr = _get_primary_member_rank(member)
        rank_data = {"id": primary_mr.rank.pk, "label": primary_mr.rank.display_label} if primary_mr else None
        special_mrs = MemberRank.objects.filter(user=member, rank__rank_type="SPECIAL").select_related("rank")
        special_data = [{"id": mr.rank.pk, "label": mr.rank.display_label} for mr in special_mrs]
        return JsonResponse({"success": True, "rank": rank_data, "special_roles": special_data})

    return redirect("codex:member_detail", user_id=user_id)


@login_required
@permission_required("codex.manage_tags")
def set_status(request, user_id):
    """Set a member's status tag (system-managed, mutually exclusive)."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    member = get_object_or_404(User, pk=user_id)
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    tag_id = request.POST.get("tag_id", "").strip()
    if not tag_id:
        if is_ajax:
            return JsonResponse({"error": "missing tag_id"}, status=400)
        return redirect("codex:member_detail", user_id=user_id)

    new_tag = get_object_or_404(Tag, pk=int(tag_id), is_system=True)

    # Remove other tags in the same system group
    sibling_tags = Tag.objects.filter(group=new_tag.group, is_system=True).exclude(pk=new_tag.pk)
    removed = MemberTag.objects.filter(user=member, tag__in=sibling_tags)
    for mt in removed.select_related("tag"):
        _log_audit(member, request.user, "TAG_REMOVED", mt.tag.name)
    removed.delete()

    # Add the new tag if not already present
    _, created = MemberTag.objects.get_or_create(
        user=member,
        tag=new_tag,
        defaults={"assigned_by": request.user},
    )
    if created:
        _log_audit(member, request.user, "TAG_ADDED", new_tag.name)

    if is_ajax:
        return JsonResponse({"success": True, "tag": {"id": new_tag.pk, "name": new_tag.name, "color": new_tag.color}})

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
                _log_audit(target_user, request.user, "TAG_REMOVED", removed_tag_names.get(tag_id, ""))

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
            _log_audit(target_user, request.user, "TAG_ADDED", added_tag_names.get(tag_id, ""))

        return redirect("codex:member_list")

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
@_review_tier_required
def former_members(request):
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
        ranks_by_user = _bulk_fetch_ranks([target_user.pk])
        members = _build_members([target_user], ranks_by_title, acks_by_user, tags_by_user, ranks_by_user)

    if not members:
        return redirect("codex:index")

    member = members[0]

    has_any_review_perm = bool(user_tiers)

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

    # Separate primary ranks and special roles for the UI
    all_primary_ranks = Rank.objects.filter(rank_type="PRIMARY").order_by("priority") if has_any_review_perm else Rank.objects.none()
    all_special_roles = Rank.objects.filter(rank_type="SPECIAL").order_by("priority") if has_any_review_perm else Rank.objects.none()

    context = {
        "member": member,
        "is_former": is_former,
        "can_manage_reviews": has_any_review_perm,
        "can_manage_tags": can_manage_tags,
        "all_ranks": all_primary_ranks,
        "all_special_roles": all_special_roles,
        "can_set_rank": has_any_review_perm and not is_former,
        "status_tags": status_tags,
        "current_status_tag": current_status_tag,
        "can_set_status": request.user.has_perm("codex.manage_tags") and not is_former,
    }
    return render(request, "codex/member_detail.html", context)


@login_required
@_review_tier_required
def member_notes_partial(request, user_id):
    target_user = get_object_or_404(User, pk=user_id)
    notes = MemberNote.objects.filter(user=target_user).select_related(
        "author__profile__main_character"
    ).order_by("-created_at")
    return render(request, "codex/_notes_tab.html", {"notes": notes, "user_id": user_id})


@login_required
@_review_tier_required
def member_audit_partial(request, user_id):
    target_user = get_object_or_404(User, pk=user_id)
    audit_logs = MemberAuditLog.objects.filter(user=target_user).select_related(
        "actor__profile__main_character"
    ).order_by("-created_at")
    return render(request, "codex/_audit_tab.html", {"audit_logs": audit_logs})


@login_required
@permission_required("codex.view_corpmember")
def member_reviews_partial(request, user_id):
    target_user = get_object_or_404(User, pk=user_id)
    acks = ReviewAcknowledgement.objects.filter(user=target_user).select_related(
        "rank", "acknowledged_by__profile__main_character"
    ).order_by("-acknowledged_at")
    return render(request, "codex/_reviews_tab.html", {"acknowledgements": list(acks)})


@login_required
@_review_tier_required
def add_note(request, user_id):
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
        _log_audit(target_user, request.user, "NOTE_ADDED", content[:100])

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse({"ok": True})
    return redirect("codex:member_detail", user_id=user_id)


@login_required
def promote_member(request, user_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    member = get_object_or_404(User, pk=user_id)
    member_rank = _get_primary_member_rank(member)
    if not member_rank:
        return redirect("codex:review")

    current_rank = member_rank.rank

    # Check reviewer has permission for the current rank's review tier
    if not _can_review_member(request.user, current_rank):
        return redirect("codex:review")

    # Find the next PRIMARY rank (next higher priority)
    next_rank = Rank.objects.filter(
        priority__gt=current_rank.priority, rank_type="PRIMARY"
    ).order_by("priority").first()
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

    _log_audit(member, request.user, "RANK_CHANGED", f"Promoted from {old_rank_name} to {next_rank.name}")

    return redirect("codex:review")
