from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Max

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
