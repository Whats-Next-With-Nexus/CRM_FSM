from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand

VIEW = ("view",)
EDIT = ("add", "change", "view")
FULL = ("add", "change", "delete", "view")
REVIEW = ("change", "view")

SALES = {
    ("crm", "customer"): EDIT, ("crm", "site"): EDIT, ("crm", "quote"): EDIT, ("crm", "quoteline"): EDIT,
    ("crm", "quoterevision"): VIEW, ("crm", "changeorder"): REVIEW, ("crm", "changeorderline"): EDIT,
    ("crm", "system"): VIEW, ("crm", "systemtype"): VIEW, ("crm", "repairplan"): VIEW,
    ("crm", "repairplanenrollment"): VIEW, ("inventory", "catalogitem"): VIEW, ("crm", "notification"): REVIEW,
    ("fsm", "workorder"): VIEW, ("fsm", "visit"): VIEW,
}
MANAGER = {
    ("crm", "customer"): VIEW, ("crm", "site"): VIEW, ("crm", "quote"): REVIEW, ("crm", "quoteline"): VIEW,
    ("crm", "quoterevision"): VIEW, ("crm", "changeorder"): EDIT, ("crm", "changeorderline"): EDIT,
    ("crm", "system"): EDIT, ("crm", "systemtype"): VIEW, ("crm", "repairplan"): VIEW,
    ("crm", "repairplanenrollment"): VIEW, ("inventory", "catalogitem"): EDIT, ("crm", "notification"): REVIEW,
    ("fsm", "workorder"): EDIT, ("fsm", "visit"): FULL, ("fsm", "visitassignment"): FULL,
    ("fsm", "skill"): EDIT, ("fsm", "techprofile"): EDIT, ("fsm", "internaltimecategory"): EDIT,
}
MANAGER_EXTRAS = [
    ("crm", "quote", "view_costs"), ("crm", "quote", "approve_exceptions"), ("crm", "quote", "decide_intake"),
    ("crm", "quote", "revise_quote"), ("crm", "changeorder", "set_restocking"), ("fsm", "workorder", "view_labor_bonus"),
]

# role -> (model permissions, extra custom permissions). Anything beyond a role is granted per user in the admin.
ROLES = {
    "Sales": (SALES, [("crm", "quote", "revise_quote")]),
    "Sales Manager": (SALES, [
        ("crm", "quote", "view_costs"), ("crm", "quote", "approve_exceptions"), ("crm", "quote", "revise_quote"),
    ]),
    "Technician": ({
        ("crm", "customer"): VIEW, ("crm", "site"): VIEW, ("crm", "system"): VIEW,
        ("crm", "repairplanenrollment"): VIEW,
        ("fsm", "workorder"): VIEW, ("fsm", "visit"): VIEW,
        ("fsm", "visitassignment"): REVIEW, ("fsm", "skill"): VIEW, ("fsm", "techprofile"): VIEW,
    }, []),
    "Install Manager": (MANAGER, MANAGER_EXTRAS),
    "Operations Manager": (
        {**MANAGER, ("crm", "customer"): EDIT, ("crm", "site"): EDIT, ("crm", "repairplanenrollment"): EDIT,
         ("crm", "contractor"): FULL},
        MANAGER_EXTRAS + [("fsm", "workorder", "manage_subcontract")],
    ),
}


class Command(BaseCommand):
    help = "Create or reset the role groups (safe to re-run; it rewrites each role's permissions)."

    def handle(self, *args, **options):
        for role, (models, extras) in ROLES.items():
            codenames = [(app, f"{action}_{model}") for (app, model), actions in models.items() for action in actions]
            codenames += [(app, code) for app, _model, code in extras]
            perms = [Permission.objects.get(content_type__app_label=app, codename=code) for app, code in codenames]
            group, _ = Group.objects.get_or_create(name=role)
            group.permissions.set(perms)
            self.stdout.write(f"{role}: {len(perms)} permissions")
