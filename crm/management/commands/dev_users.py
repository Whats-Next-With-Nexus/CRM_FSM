import os

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand, CommandError

DEFAULT_PASSWORD = "gr8password"
ACCOUNTS = [
    ("dev", None), ("sales", "Sales"), ("salesmgr", "Sales Manager"),
    ("install", "Install Manager"), ("ops", "Operations Manager"), ("tech", "Technician"),
]


class Command(BaseCommand):
    help = (
        "Create or reset one login per role for local testing, all with the same simple password "
        f"('{DEFAULT_PASSWORD}', or DEV_PASSWORD if set). Refuses to run unless DEBUG is on."
    )

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("dev_users only runs with DEBUG on. Create real users on a deployed site.")
        password = os.environ.get("DEV_PASSWORD", DEFAULT_PASSWORD)
        User = get_user_model()
        for username, role in ACCOUNTS:
            user, _ = User.objects.get_or_create(username=username, defaults={"email": f"{username}@example.com"})
            user.set_password(password)
            user.is_staff = True
            user.is_superuser = role is None
            user.save()
            user.groups.set([Group.objects.get(name=role)] if role else [])
        self.stdout.write(f"Logins ready: {', '.join(name for name, _ in ACCOUNTS)}")
