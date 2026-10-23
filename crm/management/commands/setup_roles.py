from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand

VIEW = ("view",)
EDIT = ("add", "change", "view")
FULL = ("add", "change", "delete", "view")

# role -> {(app, model): actions}. Extras beyond a role are granted per user in the admin.
ROLES = {
    "Sales": {
        ("crm", "customer"): EDIT, ("crm", "site"): EDIT, ("crm", "quote"): EDIT,
        ("crm", "system"): VIEW, ("crm", "systemtype"): VIEW, ("crm", "repairplan"): VIEW,
        ("crm", "repairplanenrollment"): VIEW,
        ("fsm", "workorder"): VIEW, ("fsm", "visit"): VIEW,
    },
    "Technician": {
        ("crm", "customer"): VIEW, ("crm", "site"): VIEW, ("crm", "system"): VIEW,
        ("crm", "repairplanenrollment"): VIEW,
        ("fsm", "workorder"): VIEW, ("fsm", "visit"): VIEW,
    },
    "Install Manager": {
        ("crm", "customer"): VIEW, ("crm", "site"): VIEW, ("crm", "quote"): VIEW,
        ("crm", "system"): EDIT, ("crm", "systemtype"): VIEW, ("crm", "repairplan"): VIEW,
        ("crm", "repairplanenrollment"): VIEW,
        ("fsm", "workorder"): EDIT, ("fsm", "visit"): FULL,
    },
    "Operations Manager": {
        ("crm", "customer"): EDIT, ("crm", "site"): EDIT, ("crm", "quote"): VIEW,
        ("crm", "system"): EDIT, ("crm", "systemtype"): VIEW, ("crm", "repairplan"): VIEW,
        ("crm", "repairplanenrollment"): EDIT, ("crm", "contractor"): FULL,
        ("fsm", "workorder"): EDIT, ("fsm", "visit"): FULL,
    },
}
EXTRA = {"Operations Manager": [("fsm", "workorder", "manage_subcontract")]}


class Command(BaseCommand):
    help = "Create or reset the role groups (safe to re-run; it rewrites each role's permissions)."

    def handle(self, *args, **options):
        for role, models in ROLES.items():
            codenames = [(app, f"{action}_{model}") for (app, model), actions in models.items() for action in actions]
            codenames += [(app, code) for app, _model, code in EXTRA.get(role, [])]
            perms = [Permission.objects.get(content_type__app_label=app, codename=code) for app, code in codenames]
            group, _ = Group.objects.get_or_create(name=role)
            group.permissions.set(perms)
            self.stdout.write(f"{role}: {len(perms)} permissions")
