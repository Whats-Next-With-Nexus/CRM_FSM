from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Max


def next_in_sequence(queryset, field):
    return (queryset.aggregate(m=Max(field))["m"] or 0) + 1


class Contractor(models.Model):
    """A prime contractor we subcontract for (e.g. a larger security integrator that hands us installs). Direct customers have none."""

    name = models.CharField(max_length=120, unique=True)
    noncompete_notes = models.TextField(blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Customer(models.Model):
    class Status(models.TextChoices):
        LEAD = "lead", "Lead"
        CUSTOMER = "customer", "Customer"
        LOST = "lost", "Lost"
        INACTIVE = "inactive", "Inactive"

    name = models.CharField(max_length=200)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.LEAD)
    contracted_through = models.ForeignKey(
        Contractor, on_delete=models.PROTECT, related_name="customers", null=True, blank=True,
        help_text="Leave blank for Direct (in-house) customers.",
    )
    contact_name = models.CharField(max_length=120, blank=True)
    phone = models.CharField(max_length=40, blank=True)
    email = models.EmailField(blank=True)
    notes = models.TextField(blank=True)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    @property
    def number(self):
        # pk is never reused (autoincrement), so the number is permanent even for lost leads
        return f"cust{1000 + self.pk}" if self.pk else "(new)"

    @property
    def effective_contractor(self):
        return self.contracted_through

    @property
    def contracted_label(self):
        return str(self.contracted_through) if self.contracted_through else "Direct"

    def __str__(self):
        return f"{self.name} ({self.number})"


class Site(models.Model):
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="sites")
    site_number = models.PositiveIntegerField(editable=False)
    name = models.CharField(max_length=120, blank=True, help_text="e.g. Indianapolis")
    address = models.CharField(max_length=200)
    city = models.CharField(max_length=80, blank=True)
    state = models.CharField(max_length=2, blank=True)
    zip_code = models.CharField(max_length=10, blank=True)
    access_notes = models.TextField(blank=True)

    class Meta:
        ordering = ["customer", "site_number"]
        constraints = [models.UniqueConstraint(fields=["customer", "site_number"], name="unique_site_number_per_customer")]

    @property
    def number(self):
        return f"{self.customer.number}-s{self.site_number}"

    @property
    def active_plan(self):
        enrollment = self.repair_plans.filter(active=True).select_related("plan").first()
        return enrollment.plan if enrollment else None

    def __str__(self):
        label = self.name or self.address
        return f"{self.number} {label}"

    def save(self, *args, **kwargs):
        if not self.site_number:
            self.site_number = next_in_sequence(Site.objects.filter(customer=self.customer), "site_number")
        super().save(*args, **kwargs)


class SystemType(models.Model):
    name = models.CharField(max_length=80, unique=True)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class System(models.Model):
    """What is actually installed at a site. This, not the quote trail, answers 'what's there now'."""

    site = models.ForeignKey(Site, on_delete=models.PROTECT, related_name="systems")
    system_type = models.ForeignKey(SystemType, on_delete=models.PROTECT)
    contracted_through = models.ForeignKey(
        Contractor, on_delete=models.PROTECT, null=True, blank=True, related_name="systems",
        help_text="Leave blank to inherit from the customer.",
    )
    bill_direct = models.BooleanField(
        default=False,
        help_text="Override: this system is ours directly even though the customer is under a contractor (e.g. a waived non-compete).",
    )
    waiver_note = models.CharField(max_length=300, blank=True, help_text="Who approved a Direct override, and why.")
    equipment_notes = models.TextField(blank=True, help_text="Current devices, models, locations, programming notes.")
    installed_on = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ["site", "system_type"]

    @property
    def effective_contractor(self):
        if self.bill_direct:
            return None
        return self.contracted_through or self.site.customer.contracted_through

    @property
    def contracted_label(self):
        contractor = self.effective_contractor
        return str(contractor) if contractor else "Direct"

    def __str__(self):
        return f"{self.system_type} @ {self.site.number}"


class RepairPlan(models.Model):
    name = models.CharField(max_length=60, unique=True, help_text="Gold, Silver, Bronze...")
    monthly_rate = models.DecimalField(max_digits=8, decimal_places=2)
    exclusions = models.TextField(blank=True, help_text="Negligence, acts of God, construction damage, etc.")

    def __str__(self):
        return self.name


class RepairPlanEnrollment(models.Model):
    """A plan covers every system at a site. A site with no active enrollment has no plan."""

    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="repair_plans")
    plan = models.ForeignKey(RepairPlan, on_delete=models.PROTECT)
    start_date = models.DateField()
    active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.plan} @ {self.site.number}"


class Quote(models.Model):
    class Stage(models.TextChoices):
        NEW_LEAD = "new_lead", "New Lead"
        QUOTE_SENT = "quote_sent", "Quote Sent"
        APPROVED = "approved", "Approved"
        DEPOSIT = "deposit", "Deposit Obtained"
        SENT_TO_OPS = "sent_to_ops", "Sent to Ops"
        LOST = "lost", "Lost"

    site = models.ForeignKey(Site, on_delete=models.PROTECT, related_name="quotes")
    quote_number = models.PositiveIntegerField(editable=False)
    stage = models.CharField(max_length=20, choices=Stage.choices, default=Stage.NEW_LEAD)
    system_types = models.ManyToManyField(SystemType, blank=True, help_text="Services the customer wants quoted.")
    amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    deposit_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    salesperson = models.CharField(max_length=80, blank=True)
    notes = models.TextField(blank=True)
    created = models.DateTimeField(auto_now_add=True)

    agreement_signed = models.BooleanField(default=False)
    deposit_received = models.BooleanField(default=False)
    deliverables_verified = models.BooleanField(
        default=False, help_text="Permits, vendor certs, anything the company itself owes before work starts.",
    )
    permits_required = models.BooleanField(null=True, blank=True, verbose_name="Permits required?")

    class Meta:
        ordering = ["site__customer", "quote_number"]
        constraints = [models.UniqueConstraint(fields=["site", "quote_number"], name="unique_quote_number_per_site")]

    @property
    def customer(self):
        return self.site.customer

    @property
    def number(self):
        return f"{self.customer.number}-q{self.quote_number:02d}"

    def clean(self):
        if self.stage == self.Stage.SENT_TO_OPS:
            missing = [label for ok, label in [
                (self.agreement_signed, "agreement signed"),
                (self.deposit_received, "deposit received"),
                (self.deliverables_verified, "deliverables verified"),
            ] if not ok]
            if missing:
                raise ValidationError("Can't send to Ops yet. Still needed: " + ", ".join(missing) + ".")

    def save(self, *args, **kwargs):
        if not self.quote_number:
            # numbered per customer across all of its sites, so the site is never baked into the number
            siblings = Quote.objects.filter(site__customer=self.site.customer)
            self.quote_number = next_in_sequence(siblings, "quote_number")
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.number} ({self.site.name or self.site.address})"
