from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Max
from django.utils import timezone

from crm.models import Quote, Site, System


class WorkOrder(models.Model):
    class Type(models.TextChoices):
        INSTALL = "install", "Install"
        SUBCONTRACT = "subcontract", "Subcontract Work"
        SERVICE = "service", "Service"
        INSPECTION = "inspection", "Inspection/Testing"

    class Billing(models.TextChoices):
        BILLABLE = "billable", "Billable"
        WARRANTY = "warranty", "Warranty"
        REPAIR_PLAN = "repair_plan", "Repair Plan"

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        SCHEDULED = "scheduled", "Scheduled"
        IN_PROGRESS = "in_progress", "In Progress"
        COMPLETE = "complete", "Complete"
        CANCELLED = "cancelled", "Cancelled"

    wo_type = models.CharField("type", max_length=20, choices=Type.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN)
    site = models.ForeignKey(Site, on_delete=models.PROTECT, related_name="work_orders")
    system = models.ForeignKey(
        System, on_delete=models.PROTECT, null=True, blank=True, related_name="work_orders",
        help_text="The system being worked on. Blank for installs, where it doesn't exist yet.",
    )
    quote = models.ForeignKey(Quote, on_delete=models.PROTECT, null=True, blank=True, related_name="work_orders")
    description = models.TextField(blank=True)
    billing = models.CharField(max_length=20, choices=Billing.choices, blank=True, help_text="Service work orders only.")
    billing_override_reason = models.CharField(
        max_length=300, blank=True, help_text="Required when a covered site is billed outside its repair plan.",
    )
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-id"]
        permissions = [("manage_subcontract", "Can create and edit subcontract work orders")]

    @property
    def number(self):
        return f"WO-{1000 + self.pk}" if self.pk else "(new)"

    @property
    def install_begun(self):
        """Install begins when the first visit is started; before that the quote can still be revised."""
        return self.wo_type == self.Type.INSTALL and self.visits.filter(started_at__isnull=False).exists()

    @property
    def hours_logged(self):
        return sum((a.hours for v in self.visits.all() for a in v.assignments.all()), Decimal("0"))

    def default_billing(self):
        return self.Billing.REPAIR_PLAN if self.site.active_plan else self.Billing.BILLABLE

    def clean(self):
        if self.wo_type != self.Type.SERVICE:
            return
        if self.billing and self.billing != self.Billing.REPAIR_PLAN and self.site_id and self.site.active_plan:
            if not self.billing_override_reason:
                raise ValidationError(
                    f"{self.site.number} has a {self.site.active_plan} repair plan. "
                    "Say why this visit is billed outside it."
                )

    def save(self, *args, **kwargs):
        if self.wo_type == self.Type.SERVICE and not self.billing:
            self.billing = self.default_billing()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.number} {self.get_wo_type_display()} - {self.site.number}"


class Visit(models.Model):
    """A trip to site under a work order. No ID of its own; shown as 'WO-1001 Visit 2'."""

    work_order = models.ForeignKey(WorkOrder, on_delete=models.CASCADE, related_name="visits")
    visit_number = models.PositiveIntegerField(editable=False)
    scheduled_for = models.DateField(null=True, blank=True)
    summary = models.TextField(blank=True, help_text="What was done, including small field changes.")
    started_at = models.DateTimeField(null=True, blank=True, editable=False, help_text="Set when the first technician starts the job.")

    class Meta:
        ordering = ["work_order", "visit_number"]
        constraints = [models.UniqueConstraint(fields=["work_order", "visit_number"], name="unique_visit_number_per_wo")]

    def save(self, *args, **kwargs):
        if not self.visit_number:
            top = Visit.objects.filter(work_order=self.work_order).aggregate(m=Max("visit_number"))["m"] or 0
            self.visit_number = top + 1
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.work_order.number} Visit {self.visit_number}"


class Skill(models.Model):
    name = models.CharField(max_length=60, unique=True)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class TechProfile(models.Model):
    """Marks a user as assignable to visits and records what they are qualified to do."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="tech_profile")
    skills = models.ManyToManyField(Skill, blank=True)

    def __str__(self):
        return self.user.get_full_name() or self.user.get_username()


class VisitAssignment(models.Model):
    """One technician on one visit, there for one skillset. Start and stop times give hours for job costing."""

    visit = models.ForeignKey(Visit, on_delete=models.CASCADE, related_name="assignments")
    tech = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="assignments",
        limit_choices_to={"tech_profile__isnull": False},
    )
    skill = models.ForeignKey(Skill, on_delete=models.PROTECT, help_text="What this technician is there to do.")
    notes = models.TextField(blank=True)
    started_at = models.DateTimeField(null=True, blank=True, editable=False)
    ended_at = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        ordering = ["visit", "tech"]
        constraints = [models.UniqueConstraint(fields=["visit", "tech"], name="one_assignment_per_tech_per_visit")]

    @property
    def hours(self):
        if not (self.started_at and self.ended_at):
            return Decimal("0.00")
        return (Decimal((self.ended_at - self.started_at).total_seconds()) / 3600).quantize(Decimal("0.01"))

    def clean(self):
        profile = getattr(self.tech, "tech_profile", None) if self.tech_id else None
        if profile is None and self.tech_id:
            raise ValidationError("That user has no technician profile.")
        if profile and self.skill_id and not profile.skills.filter(pk=self.skill_id).exists():
            raise ValidationError(f"{self.tech} is not qualified for {self.skill}. Update their skills first.")

    def start(self):
        """Technician arrives. The first start on a visit begins the job and puts the work order In Progress."""
        if self.started_at:
            return
        now = timezone.now()
        self.started_at = now
        self.save(update_fields=["started_at"])
        if not self.visit.started_at:
            self.visit.started_at = now
            self.visit.save(update_fields=["started_at"])
        work_order = self.visit.work_order
        if work_order.status in (WorkOrder.Status.OPEN, WorkOrder.Status.SCHEDULED):
            work_order.status = WorkOrder.Status.IN_PROGRESS
            work_order.save(update_fields=["status"])

    def stop(self):
        if not self.started_at:
            raise ValidationError("Start the job before stopping it.")
        if self.ended_at:
            return
        self.ended_at = timezone.now()
        self.save(update_fields=["ended_at"])

    def __str__(self):
        return f"{self.tech} on {self.visit}"
