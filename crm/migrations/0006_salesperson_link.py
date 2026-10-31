import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def link_existing_names(apps, schema_editor):
    """Point each quote at the user whose username or full name matches the old free-text salesperson."""
    Quote = apps.get_model("crm", "Quote")
    User = apps.get_model(*settings.AUTH_USER_MODEL.split("."))
    for quote in Quote.objects.exclude(salesperson=""):
        name = quote.salesperson.strip().lower()
        for user in User.objects.all():
            full = f"{user.first_name} {user.last_name}".strip().lower()
            if name in (user.username.lower(), full):
                quote.sales_rep = user
                quote.save(update_fields=["sales_rep"])
                break


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("crm", "0005_revisions_and_change_orders"),
    ]

    operations = [
        migrations.AddField(
            model_name="quote",
            name="sales_rep",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.RunPython(link_existing_names, migrations.RunPython.noop),
        migrations.RemoveField(model_name="quote", name="salesperson"),
        migrations.RenameField(model_name="quote", old_name="sales_rep", new_name="salesperson"),
        migrations.AlterField(
            model_name="quote",
            name="salesperson",
            field=models.ForeignKey(
                blank=True, help_text="Defaults to whoever creates the quote.", null=True,
                on_delete=django.db.models.deletion.SET_NULL, related_name="quotes_sold",
                limit_choices_to={"is_staff": True, "is_active": True}, to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
