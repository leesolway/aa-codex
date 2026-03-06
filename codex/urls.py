from django.urls import path

from . import views

app_name = "codex"

urlpatterns = [
    path("", views.index, name="index"),
    path("review/", views.review, name="review"),
    path(
        "review/toggle/<int:user_id>/<int:item_id>/",
        views.toggle_checklist,
        name="toggle_checklist",
    ),
    path(
        "review/acknowledge/<int:user_id>/",
        views.acknowledge_review,
        name="acknowledge_review",
    ),
    path(
        "review/promote/<int:user_id>/",
        views.promote_member,
        name="promote_member",
    ),
    path("former/", views.former_members, name="former_members"),
    path("tags/", views.manage_tags, name="manage_tags"),
    path("member/<int:user_id>/", views.member_detail, name="member_detail"),
    path("member/<int:user_id>/note/", views.add_note, name="add_note"),
    path("member/<int:user_id>/set-rank/", views.set_rank, name="set_rank"),
    path("member/<int:user_id>/set-status/", views.set_status, name="set_status"),
]
