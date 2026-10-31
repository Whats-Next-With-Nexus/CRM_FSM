from django.db import migrations

CATEGORIES = ["Sales help", "Misc time"]


def seed(apps, schema_editor):
    Category = apps.get_model("fsm", "InternalTimeCategory")
    for name in CATEGORIES:
        Category.objects.get_or_create(name=name)


class Migration(migrations.Migration):

    dependencies = [
        ("fsm", "0005_internal_time"),
    ]

    operations = [
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
