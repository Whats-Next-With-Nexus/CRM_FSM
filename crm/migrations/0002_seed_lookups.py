from django.db import migrations

SYSTEM_TYPES = ["Intrusion Detection", "Access Control", "CCTV", "Fire Protection", "Intercom"]


def seed(apps, schema_editor):
    SystemType = apps.get_model("crm", "SystemType")
    for name in SYSTEM_TYPES:
        SystemType.objects.get_or_create(name=name)


class Migration(migrations.Migration):

    dependencies = [
        ("crm", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
