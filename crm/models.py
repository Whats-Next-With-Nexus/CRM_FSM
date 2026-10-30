from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models, transaction
from django.db.models import Max
from django.utils import timezone

CENTS = Decimal("0.01")


def money(value):
    return Decimal(value).quantize(CENTS, rounding=ROUND_HALF_UP)


def plain(quantity):
    return f"{quantity:f}".rstrip("0").rstrip(".")


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

    class LeadSource(models.TextChoices):
        REFERRAL = "referral", "Referral"
        WEBSITE = "website", "Website"
        COLD_CALL = "cold_call", "Cold call / canvassing"
        EXISTING = "existing", "Existing customer"
        CONTRACTOR = "contractor", "Contractor"
        OTHER = "other", "Other"

    name = models.CharField(max_length=200)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.LEAD)
    lead_source = models.CharField(max_length=20, choices=LeadSource.choices, blank=True)
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

    def sync_status(self):
        """A lead becomes a customer once any quote is approved; a lead whose quotes are all lost is Lost."""
        if self.status == self.Status.INACTIVE:
            return
        stages = list(Quote.objects.filter(site__customer=self).values_list("stage", flat=True))
        if any(stage in Quote.WON_STAGES for stage in stages):
            new = self.Status.CUSTOMER
        elif self.status == self.Status.CUSTOMER:
            return
        elif stages and all(stage == Quote.Stage.LOST for stage in stages):
            new = self.Status.LOST
        else:
            new = self.Status.LEAD
        if new != self.status:
            self.status = new
            self.save(update_fields=["status"])

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


STANDARD_DEPOSIT_PERCENT = Decimal("50")


class Quote(models.Model):
    class Stage(models.TextChoices):
        NEW_LEAD = "new_lead", "New Lead"
        QUOTE_SENT = "quote_sent", "Quote Sent"
        APPROVED = "approved", "Approved"
        DEPOSIT = "deposit", "Deposit Obtained"
        SENT_TO_OPS = "sent_to_ops", "Sent to Ops"
        LOST = "lost", "Lost"

    class OpsStatus(models.TextChoices):
        PENDING = "pending", "Waiting on Ops"
        ACCEPTED = "accepted", "Accepted by Ops"
        REFUSED = "refused", "Returned to Sales"

    class LostReason(models.TextChoices):
        PRICE = "price", "Price too high"
        COMPETITOR = "competitor", "Went with a competitor"
        CANCELLED = "cancelled", "Project cancelled or postponed"
        NO_RESPONSE = "no_response", "No response"
        OTHER = "other", "Other"

    PRICED_STAGES = (Stage.QUOTE_SENT, Stage.APPROVED, Stage.DEPOSIT, Stage.SENT_TO_OPS)
    WON_STAGES = (Stage.APPROVED, Stage.DEPOSIT, Stage.SENT_TO_OPS)

    site = models.ForeignKey(Site, on_delete=models.PROTECT, related_name="quotes")
    quote_number = models.PositiveIntegerField(editable=False)
    stage = models.CharField(max_length=20, choices=Stage.choices, default=Stage.NEW_LEAD)
    system_types = models.ManyToManyField(SystemType, blank=True, help_text="Services the customer wants quoted.")
    deposit_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=STANDARD_DEPOSIT_PERCENT,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text="Standard is 50%. Anything lower needs manager approval.",
    )
    salesperson = models.CharField(max_length=80, blank=True)
    notes = models.TextField(blank=True)
    created = models.DateTimeField(auto_now_add=True)
    follow_up_on = models.DateField(null=True, blank=True, help_text="Next time to reach out to the customer.")
    sent_on = models.DateTimeField(null=True, blank=True, editable=False)
    valid_until = models.DateField(null=True, blank=True, help_text="Filled in when the quote is sent.")
    lost_reason = models.CharField(max_length=20, choices=LostReason.choices, blank=True)
    lost_note = models.TextField(blank=True)

    agreement_signed = models.BooleanField(default=False)
    signed_by = models.CharField(max_length=120, blank=True, help_text="Who signed for the customer.")
    agreement_signed_on = models.DateField(null=True, blank=True)
    deposit_received = models.BooleanField(default=False)
    deposit_received_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    deposit_received_on = models.DateField(null=True, blank=True)
    deliverables_verified = models.BooleanField(
        default=False, help_text="Permits, vendor certs, anything the company itself owes before work starts.",
    )
    permits_required = models.BooleanField(null=True, blank=True, verbose_name="Permits required?")

    exceptions_approved = models.BooleanField(
        default=False, help_text="Manager sign-off for below-minimum pricing or a deposit under 50%.",
    )
    exceptions_signature = models.CharField(max_length=2000, blank=True, editable=False)
    exceptions_approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, editable=False, related_name="+",
    )

    ops_status = models.CharField(max_length=20, choices=OpsStatus.choices, blank=True)
    ops_note = models.TextField(blank=True, help_text="Required when Ops returns a quote: say what is missing.")
    revision = models.PositiveIntegerField(default=1, editable=False)

    class Meta:
        ordering = ["site__customer", "quote_number"]
        constraints = [models.UniqueConstraint(fields=["site", "quote_number"], name="unique_quote_number_per_site")]
        permissions = [
            ("view_costs", "Can see costs and margins"),
            ("approve_exceptions", "Can approve below-minimum pricing and low deposits"),
            ("decide_intake", "Can accept or return quotes sent to Ops"),
            ("override_sent_quote", "Can edit a quote after it was sent to Ops"),
            ("revise_quote", "Can revise a sent quote before install begins"),
        ]

    @property
    def customer(self):
        return self.site.customer

    @property
    def number(self):
        return f"{self.customer.number}-q{self.quote_number:02d}"

    def _total(self, attr):
        return money(sum((getattr(line, attr) for line in self.lines.all()), Decimal("0")))

    @property
    def total_price(self):
        return self._total("line_price")

    @property
    def total_cost(self):
        return self._total("line_cost")

    @property
    def margin(self):
        return self.total_price - self.total_cost

    @property
    def margin_percent(self):
        price = self.total_price
        return money(self.margin / price * 100) if price else Decimal("0.00")

    @property
    def deposit_due(self):
        return money(self.total_price * self.deposit_percent / 100)

    @property
    def below_floor_lines(self):
        return [line for line in self.lines.all() if line.below_floor]

    @property
    def deposit_below_standard(self):
        return self.deposit_percent < STANDARD_DEPOSIT_PERCENT

    @property
    def needs_exception_approval(self):
        return bool(self.below_floor_lines) or self.deposit_below_standard

    def current_signature(self):
        parts = [f"{line.pk}:{line.quantity}:{line.unit_price}" for line in self.lines.all()]
        return "|".join(parts + [f"deposit:{self.deposit_percent}"])

    @property
    def exceptions_ok(self):
        if not self.needs_exception_approval:
            return True
        return self.exceptions_approved and self.exceptions_signature == self.current_signature()

    def exception_problem(self):
        """Why this quote can't be sent yet, judged on what is saved right now."""
        if self.exceptions_ok:
            return None
        reasons = []
        if self.below_floor_lines:
            reasons.append("a line is priced below its minimum")
        if self.deposit_below_standard:
            reasons.append(f"the deposit is under {STANDARD_DEPOSIT_PERCENT:g}%")
        stale = " (the earlier approval no longer matches the current pricing)" if self.exceptions_approved else ""
        return "Needs manager approval: " + " and ".join(reasons) + stale + "."

    @property
    def install_begun(self):
        """True once the first visit of an Install work order has been started."""
        return self.work_orders.filter(wo_type="install", visits__started_at__isnull=False).exists()

    @property
    def signed_change_orders(self):
        return [co for co in self.change_orders.all() if co.status == ChangeOrder.Status.SIGNED]

    @property
    def current_value(self):
        """Original total plus signed change orders, except ones marked absorbed."""
        extra = sum((co.net_price for co in self.signed_change_orders if co.billing == ChangeOrder.Billing.INVOICE), Decimal("0"))
        return money(self.total_price + extra)

    def install_summary(self):
        title = f"Install per quote {self.number}" + (f", revision {self.revision}." if self.revision > 1 else ".")
        lines = [title]
        for line in self.lines.all():
            unit = "hr" if line.kind == "labor" else "x"
            lines.append(f"- {plain(line.quantity)} {unit} {line.description}")
        return "\n".join(lines)

    def ensure_install_work_order(self):
        """Ops accepting a quote creates its Install work order, or refreshes the scope if one already exists."""
        from fsm.models import WorkOrder

        if self.ops_status != self.OpsStatus.ACCEPTED:
            return None
        work_order = self.work_orders.filter(wo_type=WorkOrder.Type.INSTALL).first()
        summary = self.install_summary()
        if work_order is None:
            return WorkOrder.objects.create(wo_type=WorkOrder.Type.INSTALL, site=self.site, quote=self, description=summary)
        if work_order.description != summary:
            work_order.description = summary
            work_order.save(update_fields=["description"])
        return work_order

    def snapshot(self):
        """A frozen copy of what the customer was shown, kept when the quote is revised."""
        return {
            "revision": self.revision,
            "stage": self.stage,
            "sent_on": self.sent_on.isoformat() if self.sent_on else None,
            "valid_until": self.valid_until.isoformat() if self.valid_until else None,
            "deposit_percent": str(self.deposit_percent),
            "total": str(self.total_price),
            "deposit_due": str(self.deposit_due),
            "signed_by": self.signed_by,
            "agreement_signed_on": self.agreement_signed_on.isoformat() if self.agreement_signed_on else None,
            "system_types": [t.name for t in self.system_types.all()],
            "lines": [
                {
                    "description": line.description, "quantity": str(line.quantity),
                    "unit_price": str(line.unit_price), "amount": str(line.line_price),
                }
                for line in self.lines.all()
            ],
        }

    def revise(self, user, reason=""):
        """Reopen a sent quote for changes before install begins. The old version is kept and a new signature is needed."""
        if self.install_begun:
            raise ValidationError(
                "Install has begun. Changes to the original scope need a change order, and new equipment needs a new quote."
            )
        if not self.sent_on or self.stage == self.Stage.LOST:
            raise ValidationError("Only a quote that has been sent can be revised.")
        with transaction.atomic():
            QuoteRevision.objects.create(
                quote=self, revision=self.revision, reason=reason, created_by=user, snapshot=self.snapshot(),
            )
            self.revision += 1
            self.stage = self.Stage.NEW_LEAD
            self.sent_on = self.valid_until = self.agreement_signed_on = None
            self.agreement_signed, self.signed_by = False, ""
            self.ops_status, self.ops_note = "", ""
            self.save()

    def stage_problem(self):
        """Problems that depend on the saved line items. Returns (message, stage to fall back to) or None."""
        if self.stage not in self.PRICED_STAGES:
            return None
        if not self.lines.exists():
            return "Add at least one line item before sending the quote. The quote was moved back to New Lead.", self.Stage.NEW_LEAD
        if problem := self.exception_problem():
            return f"{problem} The quote was moved back to New Lead.", self.Stage.NEW_LEAD
        if (
            self.stage in (self.Stage.DEPOSIT, self.Stage.SENT_TO_OPS)
            and self.deposit_received_amount is not None
            and self.deposit_received_amount < self.deposit_due
        ):
            return (
                f"The deposit received (${self.deposit_received_amount:,.2f}) is less than the deposit due "
                f"(${self.deposit_due:,.2f}). The quote was moved back to Approved.",
                self.Stage.APPROVED,
            )
        return None

    def clean(self):
        if self.stage in self.WON_STAGES and not self.sent_on:
            raise ValidationError("Send the quote (Quote Sent) before moving it forward.")
        if self.stage == self.Stage.LOST and not self.lost_reason:
            raise ValidationError("Pick a reason this quote was lost.")
        if self.agreement_signed and not self.signed_by.strip():
            raise ValidationError("Enter who signed the agreement.")
        if self.stage == self.Stage.DEPOSIT and not (self.deposit_received and self.deposit_received_amount):
            raise ValidationError("Record the deposit: tick Deposit received and enter the amount.")
        if self.stage == self.Stage.SENT_TO_OPS:
            missing = [label for ok, label in [
                (self.agreement_signed, "agreement signed"),
                (self.deposit_received and self.deposit_received_amount, "deposit received with its amount"),
                (self.deliverables_verified, "deliverables verified"),
            ] if not ok]
            if missing:
                raise ValidationError("Can't send to Ops yet. Still needed: " + ", ".join(missing) + ".")
        # an approved quote is re-checked after its lines are saved, since the sign-off is stamped then
        if self.pk and self.stage in self.PRICED_STAGES and not self.exceptions_approved and (problem := self.exception_problem()):
            raise ValidationError(problem)
        if self.ops_status == self.OpsStatus.REFUSED and not self.ops_note.strip():
            raise ValidationError("Say what is missing when returning a quote to Sales.")
        if self.ops_status == self.OpsStatus.ACCEPTED and self.stage != self.Stage.SENT_TO_OPS:
            raise ValidationError("Only a quote that was sent to Ops can be accepted.")

    def save(self, *args, **kwargs):
        if not self.quote_number:
            # numbered per customer across all of its sites, so the site is never baked into the number
            siblings = Quote.objects.filter(site__customer=self.site.customer)
            self.quote_number = next_in_sequence(siblings, "quote_number")
        if self.stage == self.Stage.SENT_TO_OPS and self.ops_status in ("", self.OpsStatus.REFUSED):
            self.ops_status = self.OpsStatus.PENDING
        today = timezone.localdate()
        if self.stage == self.Stage.QUOTE_SENT and not self.sent_on:
            self.sent_on = timezone.now()
            self.valid_until = self.valid_until or today + timedelta(days=settings.QUOTE_VALID_DAYS)
        if self.agreement_signed and not self.agreement_signed_on:
            self.agreement_signed_on = today
        if self.deposit_received and not self.deposit_received_on:
            self.deposit_received_on = today
        super().save(*args, **kwargs)
        self.customer.sync_status()

    def __str__(self):
        return f"{self.number} ({self.site.name or self.site.address})"


class QuoteLine(models.Model):
    """One priced row. Cost and minimum price are copied from the catalog at quote time and then stay fixed."""

    quote = models.ForeignKey(Quote, on_delete=models.CASCADE, related_name="lines")
    item = models.ForeignKey("inventory.CatalogItem", on_delete=models.PROTECT)
    system_type = models.ForeignKey(
        SystemType, on_delete=models.PROTECT, null=True, blank=True, help_text="Which system this line is for.",
    )
    description = models.CharField(max_length=200, blank=True, help_text="Leave blank to use the catalog name.")
    quantity = models.DecimalField(
        max_digits=8, decimal_places=2, default=Decimal("1"), validators=[MinValueValidator(Decimal("0.01"))],
        help_text="Units, or hours for labor.",
    )
    unit_price = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True, help_text="Leave blank for the list price.",
    )
    unit_cost = models.DecimalField(max_digits=10, decimal_places=2, null=True, editable=False)
    unit_min_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, editable=False)

    class Meta:
        ordering = ["id"]

    @property
    def kind(self):
        return self.item.kind

    @property
    def line_cost(self):
        return money((self.unit_cost or 0) * self.quantity)

    @property
    def line_price(self):
        return money((self.unit_price or 0) * self.quantity)

    @property
    def line_margin(self):
        return self.line_price - self.line_cost

    @property
    def below_floor(self):
        return self.unit_min_price is not None and self.unit_price is not None and self.unit_price < self.unit_min_price

    def save(self, *args, **kwargs):
        if self.unit_cost is None:
            self.unit_cost = self.item.unit_cost
        if self.unit_min_price is None:
            self.unit_min_price = self.item.min_price
        if self.unit_price is None:
            self.unit_price = self.item.list_price
        self.description = self.description or self.item.name
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{plain(self.quantity)} x {self.description or self.item.name}"


class Notification(models.Model):
    """An in-app alert for everyone in a group, shown until someone acknowledges it."""

    group = models.ForeignKey("auth.Group", on_delete=models.CASCADE, related_name="notifications")
    message = models.CharField(max_length=300)
    quote = models.ForeignKey(Quote, on_delete=models.SET_NULL, null=True, blank=True, related_name="notifications")
    created = models.DateTimeField(auto_now_add=True)
    acknowledged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    acknowledged_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created"]

    def __str__(self):
        return self.message


def notify(group_names, message, quote=None):
    from django.contrib.auth.models import Group

    for group in Group.objects.filter(name__in=group_names):
        Notification.objects.create(group=group, message=message[:300], quote=quote)


class QuoteRevision(models.Model):
    """An earlier version of a quote, frozen when it was revised."""

    quote = models.ForeignKey(Quote, on_delete=models.CASCADE, related_name="revisions")
    revision = models.PositiveIntegerField()
    reason = models.TextField(blank=True)
    snapshot = models.JSONField()
    created = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")

    class Meta:
        ordering = ["revision"]
        constraints = [models.UniqueConstraint(fields=["quote", "revision"], name="one_snapshot_per_revision")]

    def __str__(self):
        return f"{self.quote.number} revision {self.revision}"


class ChangeOrder(models.Model):
    """A signed change to the original scope after install has begun. Money differences never need a new deposit."""

    class Status(models.TextChoices):
        REQUESTED = "requested", "Requested"
        PRICED = "priced", "Ready for customer"
        SIGNED = "signed", "Signed by customer"
        DECLINED = "declined", "Declined by customer"

    class Billing(models.TextChoices):
        INVOICE = "invoice", "Add the difference to the final invoice"
        ABSORB = "absorb", "Absorb the difference"

    quote = models.ForeignKey(Quote, on_delete=models.PROTECT, related_name="change_orders")
    co_number = models.PositiveIntegerField(editable=False)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.REQUESTED)
    reason = models.TextField(help_text="What changed on site and why.")
    billing = models.CharField(max_length=20, choices=Billing.choices, default=Billing.INVOICE)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, editable=False, related_name="+",
    )
    exceptions_approved = models.BooleanField(default=False, help_text="Manager sign-off for added items priced below their minimum.")
    exceptions_signature = models.CharField(max_length=2000, blank=True, editable=False)
    exceptions_approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, editable=False, related_name="+",
    )
    signed_by = models.CharField(max_length=120, blank=True, help_text="Who signed for the customer.")
    signed_on = models.DateField(null=True, blank=True)
    applied_at = models.DateTimeField(null=True, blank=True, editable=False)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["quote", "co_number"]
        constraints = [models.UniqueConstraint(fields=["quote", "co_number"], name="unique_co_number_per_quote")]
        permissions = [("set_restocking", "Can set restocking fees on returned items")]

    @property
    def number(self):
        return f"{self.quote.number}-CO{self.co_number}" if self.pk else "(new)"

    @property
    def removed_lines(self):
        return [line for line in self.lines.all() if line.direction == ChangeOrderLine.Direction.REMOVE]

    @property
    def added_lines(self):
        return [line for line in self.lines.all() if line.direction == ChangeOrderLine.Direction.ADD]

    @property
    def net_price(self):
        return money(sum((line.amount for line in self.lines.all()), Decimal("0")))

    @property
    def net_cost(self):
        return money(sum((line.cost_effect for line in self.lines.all()), Decimal("0")))

    @property
    def margin_change(self):
        return self.net_price - self.net_cost

    @property
    def below_floor_lines(self):
        return [line for line in self.added_lines if line.below_floor]

    def current_signature(self):
        parts = [f"{line.pk}:{line.direction}:{line.quantity}:{line.unit_price}" for line in self.lines.all()]
        return "|".join(parts)

    @property
    def exceptions_ok(self):
        if not self.below_floor_lines:
            return True
        return self.exceptions_approved and self.exceptions_signature == self.current_signature()

    def line_problem(self):
        original = {line.pk: line for line in self.quote.lines.all()}
        for line in self.removed_lines:
            source = original.get(line.quote_line_id)
            if source is None:
                return "A returned line must come from this quote."
            if line.quantity > source.quantity:
                return f"Can't return {plain(line.quantity)} of {source.description}; the quote has {plain(source.quantity)}."
        return None

    def stage_problem(self):
        """Why this change order can't be marked ready or signed yet, judged on what is saved."""
        if self.status not in (self.Status.PRICED, self.Status.SIGNED):
            return None
        if not self.removed_lines:
            return (
                "A change order has to return or remove at least one item from the original scope. "
                "Added equipment on its own belongs on a new quote."
            )
        if problem := self.line_problem():
            return problem
        if not self.exceptions_ok:
            return "Needs manager approval: an added item is priced below its minimum."
        return None

    def summary(self):
        parts = [f"returned {plain(l.quantity)} x {l.description}" for l in self.removed_lines]
        parts += [f"added {plain(l.quantity)} x {l.description}" for l in self.added_lines]
        return f"Change order {self.number}: " + "; ".join(parts) + "."

    def apply_to_work_order(self):
        """Record a signed change order on the Install work order so Ops sees the current scope."""
        if self.status != self.Status.SIGNED or self.applied_at:
            return
        work_order = self.quote.work_orders.filter(wo_type="install").first()
        if work_order:
            work_order.description = f"{work_order.description}\n\n{self.summary()}".strip()
            work_order.save(update_fields=["description"])
        self.applied_at = timezone.now()
        self.save(update_fields=["applied_at"])

    def clean(self):
        if not self.pk and self.quote_id and not self.quote.install_begun:
            raise ValidationError(
                "Install hasn't begun, so this isn't a change order. Revise the quote instead; "
                "new equipment goes on a new quote."
            )
        if self.status == self.Status.SIGNED and not self.signed_by.strip():
            raise ValidationError("Enter who signed the change order.")

    def save(self, *args, **kwargs):
        if not self.co_number:
            self.co_number = next_in_sequence(ChangeOrder.objects.filter(quote=self.quote), "co_number")
        if self.status == self.Status.SIGNED and not self.signed_on:
            self.signed_on = timezone.localdate()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.number


class ChangeOrderLine(models.Model):
    class Direction(models.TextChoices):
        REMOVE = "remove", "Returned / removed"
        ADD = "add", "Added"

    change_order = models.ForeignKey(ChangeOrder, on_delete=models.CASCADE, related_name="lines")
    direction = models.CharField(max_length=10, choices=Direction.choices, default=Direction.REMOVE)
    quote_line = models.ForeignKey(
        QuoteLine, on_delete=models.PROTECT, null=True, blank=True, related_name="+",
        help_text="For returned items: the original line.",
    )
    item = models.ForeignKey("inventory.CatalogItem", on_delete=models.PROTECT, null=True, blank=True, help_text="For added items.")
    description = models.CharField(max_length=200, blank=True)
    quantity = models.DecimalField(
        max_digits=8, decimal_places=2, default=Decimal("1"), validators=[MinValueValidator(Decimal("0.01"))],
    )
    unit_price = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text="Blank uses the original price for returns and the list price for additions.",
    )
    unit_cost = models.DecimalField(max_digits=10, decimal_places=2, null=True, editable=False)
    unit_min_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, editable=False)
    restocking_percent = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text="Returns only. Blank means to be decided by Ops.",
    )

    class Meta:
        ordering = ["id"]

    @property
    def amount(self):
        value = money((self.unit_price or 0) * self.quantity)
        return -value if self.direction == self.Direction.REMOVE else value

    @property
    def cost_effect(self):
        value = money((self.unit_cost or 0) * self.quantity)
        return -value if self.direction == self.Direction.REMOVE else value

    @property
    def below_floor(self):
        return (
            self.direction == self.Direction.ADD and self.unit_min_price is not None
            and self.unit_price is not None and self.unit_price < self.unit_min_price
        )

    def clean(self):
        if self.direction == self.Direction.REMOVE and not self.quote_line_id:
            raise ValidationError("Pick the original line being returned.")
        if self.direction == self.Direction.ADD and not self.item_id:
            raise ValidationError("Pick the catalog item being added.")

    def save(self, *args, **kwargs):
        if self.direction == self.Direction.REMOVE and self.quote_line_id:
            source = self.quote_line
            self.description = self.description or source.description
            if self.unit_cost is None:
                self.unit_cost = source.unit_cost
            if self.unit_price is None:
                self.unit_price = source.unit_price
        elif self.direction == self.Direction.ADD and self.item_id:
            self.description = self.description or self.item.name
            if self.unit_cost is None:
                self.unit_cost = self.item.unit_cost
            if self.unit_min_price is None:
                self.unit_min_price = self.item.min_price
            if self.unit_price is None:
                self.unit_price = self.item.list_price
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.get_direction_display()}: {plain(self.quantity)} x {self.description}"
