from django.db import migrations


def create_member_status_group(apps, schema_editor):
    TagGroup = apps.get_model("codex", "TagGroup")
    Tag = apps.get_model("codex", "Tag")

    group = TagGroup.objects.create(
        name="Member Status",
        order=0,
        is_system=True,
    )

    Tag.objects.create(
        group=group,
        name="Active",
        color="success",
        default=True,
        order=0,
        is_system=True,
    )
    Tag.objects.create(
        group=group,
        name="Short Break",
        color="warning",
        default=False,
        order=1,
        is_system=True,
    )
    Tag.objects.create(
        group=group,
        name="Inactive",
        color="danger",
        default=False,
        order=2,
        is_system=True,
    )


def remove_member_status_group(apps, schema_editor):
    TagGroup = apps.get_model("codex", "TagGroup")
    TagGroup.objects.filter(name="Member Status", is_system=True).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("codex", "0004_add_is_system_fields"),
    ]

    operations = [
        migrations.RunPython(create_member_status_group, remove_member_status_group),
    ]
