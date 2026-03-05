# Codex Module

Corporation member tracking and management system for Alliance Auth (EVE Online).

## Overview

Codex provides member roster management, rank detection from EVE titles, periodic review workflows, a tagging system, notes, and full audit logging. It integrates with Alliance Auth's permission system and the `corptools` module for service time calculation.

## Key Files

- `models.py` - All data models (CodexConfiguration, Rank, ChecklistItem, Tag, TagGroup, MemberTag, ReviewAcknowledgement, MemberChecklistCompletion, MemberNote, MemberAuditLog)
- `views.py` - All views and helper functions (rank detection, service time calculation, review logic)
- `urls.py` - URL routing under `/codex/`
- `auth_hooks.py` - Alliance Auth URL hook and menu hook registration
- `admin.py` - Django admin configuration with inlines for Rank→ChecklistItem and TagGroup→Tag
- `apps.py` - AppConfig (`CodexConfig`, app label: `codex`)

## Models

- **CodexConfiguration** (SingletonModel via `solo`) - Global config linking to an AA `State` to filter visible members
- **Rank** - Corp ranks matched by `eve_title` against character titles; has `priority` ordering, `review_threshold_days`, and `review_tier` (1/2/3) for tiered review permissions
- **ChecklistItem** - Per-rank review tasks
- **ReviewAcknowledgement** - Records when a member review is completed
- **MemberChecklistCompletion** - Tracks individual checklist item completion per user
- **TagGroup** / **Tag** / **MemberTag** - Flexible tagging with color support and default auto-assignment
- **MemberNote** - Free-form notes on members
- **MemberAuditLog** - Action history (TAG_ADDED, TAG_REMOVED, CHECKLIST_COMPLETED, CHECKLIST_UNCOMPLETED, REVIEW_ACKNOWLEDGED, NOTE_ADDED)

## Permissions

Defined as custom permissions on `CodexConfiguration`:

| Permission | Purpose |
|---|---|
| `codex.view_corpmember` | View member roster, tags, details |
| `codex.manage_reviews_r1` | Can manage Tier 1 (R1) reviews, checklists, notes, audit logs, former members |
| `codex.manage_reviews_r2` | Can manage Tier 2 (R2) reviews, checklists, notes, audit logs, former members |
| `codex.manage_reviews_r3` | Can manage Tier 3 (R3) reviews, checklists, notes, audit logs, former members |
| `codex.manage_tags` | Assign/remove tags for any member (without this, users can only edit their own tags) |

Users only see and can act on members whose rank's `review_tier` matches a tier they have permission for. Any single tier permission grants access to notes, audit logs, and former members views.

## URL Endpoints

| Path | Method | View | Permission |
|---|---|---|---|
| `/` | GET | `index` | `view_corpmember` |
| `/review/` | GET | `review` | Any `manage_reviews_r*` |
| `/review/toggle/<user_id>/<item_id>/` | POST | `toggle_checklist` | Matching tier `manage_reviews_r*` |
| `/review/acknowledge/<user_id>/` | POST | `acknowledge_review` | Matching tier `manage_reviews_r*` |
| `/former/` | GET | `former_members` | Any `manage_reviews_r*` |
| `/tags/` | GET/POST | `manage_tags` | `view_corpmember` / `manage_tags` |
| `/member/<user_id>/` | GET | `member_detail` | `view_corpmember` |
| `/member/<user_id>/note/` | POST | `add_note` | Any `manage_reviews_r*` |

## Dependencies

- **Alliance Auth** - Base framework, authentication, permissions, hooks
- **corptools** - `CorporationHistory` model for service time calculation
- **django-solo** - `SingletonModel` for CodexConfiguration
- **EVE Tech Images API** - Character/corp/alliance portraits in templates
- **zKillboard** - External links from templates

## Templates

All templates extend `allianceauth/base-bs5.html` (Bootstrap 5). Located in `templates/codex/`:
- `members.html` - Main roster with client-side search
- `review.html` - Review dashboard with accordion layout
- `member_detail.html` - Full member profile (4-column layout)
- `tags.html` - Tag management form
- `former_members.html` - Former members roster

## Key Business Logic (views.py)

- `detect_member_rank()` - Matches EVE character titles to Rank objects, detects mismatches across alts
- `_compute_service()` - Calculates corp service time from CorporationHistory
- `_is_review_due()` - Checks if review threshold has been crossed since last acknowledgement
- `_build_members()` - Enriches user queryset with rank, service, review status, and tags
- `_assign_default_tags()` - Bulk-assigns default tags to new members with audit logging

## Development Notes

- Uses bulk operations (`bulk_create`) for performance on default tag assignment and audit logging
- Querysets use `select_related` / `prefetch_related` extensively; maintain this pattern
- All mutations create `MemberAuditLog` entries - maintain this pattern when adding new actions
- Forms use standard Django POST with CSRF; no REST API or JSON endpoints
- Version defined in `__init__.py` (currently 1.0.0)
