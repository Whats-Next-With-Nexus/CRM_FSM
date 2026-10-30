from django.db import migrations

SKILLS = ["Cabling and pre-wire", "CCTV", "Access control", "Intrusion", "Fire alarm", "Programming and commissioning"]


def seed(apps, schema_editor):
    Skill = apps.get_model("fsm", "Skill")
    for name in SKILLS:
        Skill.objects.get_or_create(name=name)


class Migration(migrations.Migration):

    dependencies = [
        ("fsm", "0003_technician_assignments"),
    ]

    operations = [
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
