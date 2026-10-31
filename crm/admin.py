from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

from django.conf import settings
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from django.utils.html import format_html, format_html_join
from django.utils.safestring import mark_safe

from .forms import NewLeadForm
from .models import (
    ChangeOrder, ChangeOrderLine, Contractor, Customer, Notification, Quote, QuoteLine, QuoteRevision, RepairPlan,
    RepairPlanEnrollment, Site, System, SystemType, notify, plain,
)


def dollars(value):
    return f"${value:,.2f}"

CONTRACT_FIELDS = ("contracted_through", "bill_direct", "waiver_note")


def sees_contractors(request):
    return request.user.has_perm("crm.view_contractor")


class ContractInfoMixin:
    """Hides who a customer or system is contracted through from roles that can't see contractors."""

    contract_columns = ()
    contract_filters = ()

    def get_list_display(self, request):
        columns = super().get_list_display(request)
        if sees_contractors(request):
            return columns
        return [c for c in columns if c not in self.contract_columns]

    def get_list_filter(self, request):
        filters = super().get_list_filter(request)
        if sees_contractors(request):
            return filters
        return [f for f in filters if f not in self.contract_filters]

    def get_exclude(self, request, obj=None):
        exclude = list(super().get_exclude(request, obj) or [])
        if not sees_contractors(request):
            exclude += CONTRACT_FIELDS
        return exclude


@admin.register(Contractor)
class ContractorAdmin(admin.ModelAdmin):
    list_display = ("name", "customer_count")
    search_fields = ("name",)

    @admin.display(description="Customers")
    def customer_count(self, obj):
        return obj.customers.count()


class SiteInline(admin.TabularInline):
    model = Site
    extra = 0
    fields = ("name", "address", "city", "state", "zip_code")
    show_change_link = True


@admin.register(Customer)
class CustomerAdmin(ContractInfoMixin, admin.ModelAdmin):
    list_display = ("name", "number", "status", "lead_source", "contracted_label", "contact_name", "phone")
    list_filter = ("status", "lead_source", "contracted_through")
    contract_columns = ("contracted_label",)
    contract_filters = ("contracted_through",)
    search_fields = ("name", "contact_name", "sites__address", "sites__name")
    readonly_fields = ("number",)
    inlines = [SiteInline]


class SystemInline(admin.TabularInline):
    model = System
    extra = 0
    fields = ("system_type", "bill_direct", "waiver_note", "installed_on")
    show_change_link = True

    def get_fields(self, request, obj=None):
        fields = super().get_fields(request, obj)
        if sees_contractors(request):
            return fields
        return [f for f in fields if f not in CONTRACT_FIELDS]


class EnrollmentInline(admin.TabularInline):
    model = RepairPlanEnrollment
    extra = 0


@admin.register(Site)
class SiteAdmin(ContractInfoMixin, admin.ModelAdmin):
    list_display = ("number", "customer", "name", "address", "city")
    list_filter = ("customer__contracted_through",)
    contract_filters = ("customer__contracted_through",)
    search_fields = ("address", "name", "city", "customer__name")
    readonly_fields = ("number",)
    inlines = [SystemInline, EnrollmentInline]


@admin.register(SystemType)
class SystemTypeAdmin(admin.ModelAdmin):
    list_display = ("name", "active")


@admin.register(System)
class SystemAdmin(ContractInfoMixin, admin.ModelAdmin):
    list_display = ("__str__", "site", "system_type", "contracted_label")
    list_filter = ("system_type", "bill_direct")
    contract_columns = ("contracted_label",)
    contract_filters = ("bill_direct",)
    search_fields = ("site__address", "site__customer__name", "equipment_notes")


@admin.register(RepairPlan)
class RepairPlanAdmin(admin.ModelAdmin):
    list_display = ("name", "monthly_rate")


SALES_GROUPS = ("Sales", "Sales Manager")
OPS_GROUPS = ("Install Manager", "Operations Manager")
EDITABLE_QUOTE_FIELDS = (
    "site", "stage", "system_types", "salesperson", "follow_up_on", "notes", "valid_until", "deposit_percent",
    "agreement_signed", "signed_by", "agreement_signed_on", "deposit_received", "deposit_received_amount",
    "deposit_received_on", "deliverables_verified", "permits_required", "exceptions_approved", "lost_reason", "lost_note",
)


def sees_costs(request):
    return request.user.has_perm("crm.view_costs")


def saved_stage(quote):
    """The stage in the database; a form the gate rejected leaves an unsaved stage on the in-memory object."""
    if not (quote and quote.pk):
        return None
    return Quote.objects.filter(pk=quote.pk).values_list("stage", flat=True).first()


def sent_to_ops(quote):
    return saved_stage(quote) == Quote.Stage.SENT_TO_OPS


class QuoteLineInline(admin.TabularInline):
    model = QuoteLine
    extra = 1
    base_fields = ("item", "description", "quantity", "unit_price", "system_type", "floor_display", "price_display")
    cost_fields = ("cost_display", "margin_display")
    readonly_fields = ("floor_display", "price_display", "cost_display", "margin_display")

    def get_fields(self, request, obj=None):
        return self.base_fields + (self.cost_fields if sees_costs(request) else ())

    @admin.display(description="Minimum price")
    def floor_display(self, obj):
        if not obj.pk:
            return "-"
        text = dollars(obj.unit_min_price)
        return f"{text} BELOW MINIMUM" if obj.below_floor else text

    @admin.display(description="Line price")
    def price_display(self, obj):
        return dollars(obj.line_price) if obj.pk else "-"

    @admin.display(description="Line cost")
    def cost_display(self, obj):
        return dollars(obj.line_cost) if obj.pk else "-"

    @admin.display(description="Line margin")
    def margin_display(self, obj):
        return dollars(obj.line_margin) if obj.pk else "-"

    def _locked(self, request, parent):
        return sent_to_ops(parent) and not request.user.has_perm("crm.override_sent_quote")

    def has_add_permission(self, request, obj=None):
        return super().has_add_permission(request, obj) and not self._locked(request, obj)

    def has_change_permission(self, request, obj=None):
        return super().has_change_permission(request, obj) and not self._locked(request, obj)

    def has_delete_permission(self, request, obj=None):
        return super().has_delete_permission(request, obj) and not self._locked(request, obj)


def signed_dollars(value):
    return f"-{dollars(abs(value))}" if value < 0 else dollars(value)


def quote_document_data(quote, revision=None):
    """What the printable quote shows, from the live quote or from a frozen earlier revision."""
    if revision is None:
        lines = [
            SimpleNamespace(description=l.description, quantity=l.quantity, unit_price=l.unit_price, line_price=l.line_price)
            for l in quote.lines.all()
        ]
        doc = SimpleNamespace(
            number=quote.number, revision=quote.revision, sent_on=quote.sent_on, valid_until=quote.valid_until,
            salesperson=quote.salesperson, total_price=quote.total_price, deposit_percent=quote.deposit_percent,
            deposit_due=quote.deposit_due, system_types=[SimpleNamespace(name=t.name) for t in quote.system_types.all()],
            superseded=False,
        )
        return {"quote": doc, "lines": lines, "watermark": "DRAFT" if quote.stage == Quote.Stage.NEW_LEAD else ""}
    snap = revision.snapshot
    lines = [
        SimpleNamespace(
            description=l["description"], quantity=Decimal(l["quantity"]), unit_price=Decimal(l["unit_price"]),
            line_price=Decimal(l["amount"]),
        )
        for l in snap["lines"]
    ]
    doc = SimpleNamespace(
        number=quote.number, revision=snap["revision"], sent_on=parse_datetime(snap["sent_on"]) if snap["sent_on"] else None,
        valid_until=parse_date(snap["valid_until"]) if snap["valid_until"] else None, salesperson=quote.salesperson,
        total_price=Decimal(snap["total"]), deposit_percent=Decimal(snap["deposit_percent"]),
        deposit_due=Decimal(snap["deposit_due"]), system_types=[SimpleNamespace(name=n) for n in snap["system_types"]],
        superseded=True,
    )
    return {"quote": doc, "lines": lines, "watermark": "SUPERSEDED"}


class QuoteRevisionInline(admin.TabularInline):
    model = QuoteRevision
    extra = 0
    fields = ("revision", "created", "created_by", "reason", "copy_link")
    readonly_fields = fields

    @admin.display(description="Customer copy")
    def copy_link(self, obj):
        url = f"{reverse('admin:crm_quote_document', args=[obj.quote_id])}?rev={obj.revision}"
        return format_html('<a href="{}" target="_blank">Open revision {}</a>', url, obj.revision)

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class FollowUpFilter(admin.SimpleListFilter):
    title = "follow-up"
    parameter_name = "follow_up"

    def lookups(self, request, model_admin):
        return [("overdue", "Overdue"), ("today", "Due today"), ("week", "Due this week"), ("none", "None set")]

    def queryset(self, request, queryset):
        today = timezone.localdate()
        open_quotes = queryset.exclude(stage__in=[Quote.Stage.LOST, Quote.Stage.SENT_TO_OPS])
        match self.value():
            case "overdue":
                return open_quotes.filter(follow_up_on__lt=today)
            case "today":
                return open_quotes.filter(follow_up_on=today)
            case "week":
                return open_quotes.filter(follow_up_on__range=(today, today + timedelta(days=7)))
            case "none":
                return open_quotes.filter(follow_up_on__isnull=True)
        return queryset


@admin.register(Quote)
class QuoteAdmin(admin.ModelAdmin):
    list_display = ("number", "site", "stage", "ops_status", "price_total", "margin_pct", "follow_up_on", "salesperson", "created")
    list_filter = ("stage", "ops_status", FollowUpFilter, "salesperson", "lost_reason")
    search_fields = ("site__address", "site__customer__name")
    filter_horizontal = ("system_types",)
    inlines = [QuoteLineInline, QuoteRevisionInline]
    cost_columns = ("margin_pct",)

    def get_urls(self):
        custom = [
            path("new-lead/", self.admin_site.admin_view(self.new_lead_view), name="crm_quote_new_lead"),
            path("<int:pk>/document/", self.admin_site.admin_view(self.document_view), name="crm_quote_document"),
            path("<int:pk>/revise/", self.admin_site.admin_view(self.revise_view), name="crm_quote_revise"),
        ]
        return custom + super().get_urls()

    def new_lead_view(self, request):
        needed = ("crm.add_customer", "crm.add_site", "crm.add_quote")
        if not all(request.user.has_perm(p) for p in needed):
            raise PermissionDenied
        initial = {"salesperson": request.user.get_full_name() or request.user.get_username()}
        form = NewLeadForm(request.POST or None, initial=initial)
        if request.method == "POST" and form.is_valid():
            quote = form.save()
            self.message_user(
                request, f"Lead created: {quote.customer.number}, {quote.number}. Add line items below.", messages.SUCCESS,
            )
            return redirect("admin:crm_quote_change", quote.pk)
        context = {**self.admin_site.each_context(request), "title": "New lead", "form": form, "opts": self.model._meta}
        return TemplateResponse(request, "admin/crm/quote/new_lead.html", context)

    def document_view(self, request, pk):
        quote = get_object_or_404(Quote.objects.select_related("site__customer"), pk=pk)
        if not self.has_view_permission(request, quote):
            raise PermissionDenied
        revision = None
        if request.GET.get("rev"):
            revision = get_object_or_404(QuoteRevision, quote=quote, revision=request.GET["rev"])
        context = {
            **quote_document_data(quote, revision),
            "customer": quote.customer,
            "site": quote.site,
            "company_name": settings.COMPANY_NAME,
            "terms": settings.QUOTE_TERMS,
        }
        return TemplateResponse(request, "crm/quote_document.html", context)

    def revise_view(self, request, pk):
        quote = get_object_or_404(Quote.objects.select_related("site__customer"), pk=pk)
        if not request.user.has_perm("crm.revise_quote"):
            raise PermissionDenied
        back = redirect("admin:crm_quote_change", quote.pk)
        if request.method == "POST":
            was_with_ops = quote.ops_status in (Quote.OpsStatus.PENDING, Quote.OpsStatus.ACCEPTED)
            try:
                quote.revise(request.user, request.POST.get("reason", "").strip())
            except ValidationError as error:
                self.message_user(request, error.messages[0], messages.ERROR)
                return back
            if was_with_ops:
                notify(OPS_GROUPS, f"{quote.number} was revised to revision {quote.revision} before install began. It needs to be accepted again.", quote)
            self.message_user(request, f"{quote.number} is now revision {quote.revision}. The customer needs to sign again.", messages.SUCCESS)
            return back
        if quote.install_begun:
            self.message_user(
                request, "Install has begun. Changes to the original scope need a change order, and new equipment needs a new quote.",
                messages.ERROR,
            )
            return back
        context = {
            **self.admin_site.each_context(request), "title": f"Revise {quote.number}", "quote": quote,
            "next_revision": quote.revision + 1, "opts": self.model._meta,
        }
        return TemplateResponse(request, "admin/crm/quote/revise.html", context)

    def get_list_display(self, request):
        columns = super().get_list_display(request)
        return columns if sees_costs(request) else [c for c in columns if c not in self.cost_columns]

    def get_fieldsets(self, request, obj=None):
        pricing = ["price_total"]
        if sees_costs(request):
            pricing += ["cost_total", "margin_total", "margin_pct"]
        pricing += ["deposit_percent", "deposit_amount_due", "contract_value", "exceptions_note", "exceptions_approved"]
        return (
            (None, {"fields": (
                "number", "site", "stage", "system_types", "salesperson", "follow_up_on", "notes", "sent_on", "valid_until",
                "document_link", "presale_time",
            )}),
            ("Pricing (save to refresh totals)", {"fields": pricing}),
            ("Revisions and change orders", {"fields": ("revision_note", "revise_link", "change_order_links")}),
            ("Agreement, deposit and Sent to Ops checklist", {"fields": (
                "agreement_signed", "signed_by", "agreement_signed_on", "deposit_received", "deposit_received_amount",
                "deposit_received_on", "deliverables_verified", "permits_required",
            )}),
            ("If the quote is lost", {"fields": ("lost_reason", "lost_note"), "classes": ("collapse",)}),
            ("Ops intake", {"fields": ("ops_status", "ops_note")}),
        )

    @admin.display(description="Printable quote")
    def document_link(self, obj):
        if not obj.pk:
            return "Save the quote first."
        return format_html('<a href="{}" target="_blank">Open the customer copy</a>', reverse("admin:crm_quote_document", args=[obj.pk]))

    def get_readonly_fields(self, request, obj=None):
        fields = [
            "number", "sent_on", "document_link", "price_total", "cost_total", "margin_total", "margin_pct",
            "deposit_amount_due", "exceptions_note", "contract_value", "revision_note", "revise_link", "change_order_links",
            "presale_time",
        ]
        user = request.user
        if not user.has_perm("crm.approve_exceptions"):
            fields.append("exceptions_approved")
        if not (user.has_perm("crm.decide_intake") and sent_to_ops(obj)):
            fields += ["ops_status", "ops_note"]
        if sent_to_ops(obj) and not user.has_perm("crm.override_sent_quote"):
            fields += [f for f in EDITABLE_QUOTE_FIELDS if f not in fields]
        return fields

    @admin.display(description="Pre-sale tech time")
    def presale_time(self, obj):
        if not obj.pk:
            return "-"
        hours = obj.presale_hours
        return f"{plain(hours)} hours of technician time (walk-throughs and sales help)" if hours else "None logged."

    @admin.display(description="Revision")
    def revision_note(self, obj):
        return f"Revision {obj.revision}" if obj.pk else "-"

    @admin.display(description="Contract value")
    def contract_value(self, obj):
        if not obj.pk:
            return "-"
        signed = obj.signed_change_orders
        if not signed:
            return dollars(obj.total_price)
        return f"{dollars(obj.current_value)} ({dollars(obj.total_price)} original plus {len(signed)} signed change order(s))"

    @admin.display(description="Revise")
    def revise_link(self, obj):
        if not obj.pk or not obj.sent_on or obj.stage == Quote.Stage.LOST:
            return "A quote can be revised once it has been sent."
        if obj.install_begun:
            return "Install has begun. Use a change order for changes to the original scope, or a new quote for new equipment."
        return format_html('<a href="{}">Revise this quote</a>', reverse("admin:crm_quote_revise", args=[obj.pk]))

    @admin.display(description="Change orders")
    def change_order_links(self, obj):
        if not obj.pk:
            return "-"
        rows = [
            format_html('<a href="{}">{}</a> ({})', reverse("admin:crm_changeorder_change", args=[co.pk]), co.number, co.get_status_display())
            for co in obj.change_orders.all()
        ]
        if obj.install_begun:
            rows.append(format_html('<a href="{}?quote={}">Add a change order</a>', reverse("admin:crm_changeorder_add"), obj.pk))
        elif not rows:
            return "None. Change orders open once the first visit has started."
        return format_html_join(mark_safe("<br>"), "{}", ((row,) for row in rows))

    @admin.display(description="Total price")
    def price_total(self, obj):
        return dollars(obj.total_price) if obj.pk else "-"

    @admin.display(description="Total cost")
    def cost_total(self, obj):
        return dollars(obj.total_cost) if obj.pk else "-"

    @admin.display(description="Margin")
    def margin_total(self, obj):
        return dollars(obj.margin) if obj.pk else "-"

    @admin.display(description="Margin %")
    def margin_pct(self, obj):
        return f"{obj.margin_percent}%" if obj.pk else "-"

    @admin.display(description="Deposit due")
    def deposit_amount_due(self, obj):
        return dollars(obj.deposit_due) if obj.pk else "-"

    @admin.display(description="Exceptions")
    def exceptions_note(self, obj):
        if not obj.pk:
            return "-"
        if not obj.needs_exception_approval:
            return "None: pricing and deposit are within standard."
        return obj.exception_problem() or f"Approved by {obj.exceptions_approved_by or 'a manager'}."

    def save_model(self, request, obj, form, change):
        obj._stage_before = saved_stage(obj) if change else None
        if obj.ops_status == Quote.OpsStatus.REFUSED and "ops_status" in form.changed_data:
            obj.stage = Quote.Stage.DEPOSIT
        super().save_model(request, obj, form, change)

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        quote, user = form.instance, request.user
        before = quote._stage_before
        changed = set(form.changed_data)

        if user.has_perm("crm.approve_exceptions") and quote.exceptions_approved:
            quote.exceptions_signature = quote.current_signature()
            quote.exceptions_approved_by = user
            quote.save(update_fields=["exceptions_signature", "exceptions_approved_by"])

        if problem := quote.stage_problem():
            message, fallback = problem
            reset = {"stage": fallback}
            if fallback == Quote.Stage.NEW_LEAD:
                reset |= {"sent_on": None, "valid_until": None}
            Quote.objects.filter(pk=quote.pk).update(**reset)
            quote.refresh_from_db()
            quote.customer.sync_status()
            self.message_user(request, message, messages.ERROR)
            return

        if before == Quote.Stage.SENT_TO_OPS:
            edits = (changed - {"ops_status", "ops_note"}) or any(fs.has_changed() for fs in formsets)
            if edits:
                notify(OPS_GROUPS, f"{quote.number} was edited after being sent to Ops, by {user.get_username()}.", quote)
        elif quote.stage == Quote.Stage.SENT_TO_OPS:
            notify(OPS_GROUPS, f"{quote.number} was sent to Ops. Accept it or return it to Sales.", quote)

        if "ops_status" in changed:
            if quote.ops_status == Quote.OpsStatus.ACCEPTED:
                work_order = quote.ensure_install_work_order()
                notify(SALES_GROUPS, f"{quote.number} was accepted by Ops as {work_order.number}.", quote)
            elif quote.ops_status == Quote.OpsStatus.REFUSED:
                notify(SALES_GROUPS, f"{quote.number} was returned by Ops: {quote.ops_note}", quote)


def saved_co_status(co):
    if not (co and co.pk):
        return None
    return ChangeOrder.objects.filter(pk=co.pk).values_list("status", flat=True).first()


def co_locked(co):
    return saved_co_status(co) in (ChangeOrder.Status.SIGNED, ChangeOrder.Status.DECLINED)


class ChangeOrderLineInline(admin.TabularInline):
    model = ChangeOrderLine
    extra = 1
    fields = ("direction", "quote_line", "item", "description", "quantity", "unit_price", "restocking_percent", "amount_display")
    readonly_fields = ("amount_display",)

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))
        if not request.user.has_perm("crm.set_restocking"):
            fields.append("restocking_percent")
        return fields

    def _quote_id(self, request):
        match = request.resolver_match
        object_id = match.kwargs.get("object_id") if match else None
        if object_id:
            return ChangeOrder.objects.filter(pk=object_id).values_list("quote_id", flat=True).first()
        return request.GET.get("quote") or request.POST.get("quote")

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "quote_line":
            kwargs["queryset"] = QuoteLine.objects.filter(quote_id=self._quote_id(request) or 0)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    @admin.display(description="Amount")
    def amount_display(self, obj):
        return signed_dollars(obj.amount) if obj.pk else "-"

    def has_add_permission(self, request, obj=None):
        return super().has_add_permission(request, obj) and not co_locked(obj)

    def has_change_permission(self, request, obj=None):
        return super().has_change_permission(request, obj) and not co_locked(obj)

    def has_delete_permission(self, request, obj=None):
        return super().has_delete_permission(request, obj) and not co_locked(obj)


@admin.register(ChangeOrder)
class ChangeOrderAdmin(admin.ModelAdmin):
    list_display = ("number", "quote", "status", "net_display", "billing", "created")
    list_filter = ("status", "billing")
    search_fields = ("quote__site__customer__name", "quote__site__address", "reason")
    inlines = [ChangeOrderLineInline]
    editable_fields = ("status", "reason", "billing", "exceptions_approved", "signed_by", "signed_on")

    def get_urls(self):
        custom = [path("<int:pk>/document/", self.admin_site.admin_view(self.document_view), name="crm_changeorder_document")]
        return custom + super().get_urls()

    def document_view(self, request, pk):
        co = get_object_or_404(ChangeOrder.objects.select_related("quote__site__customer"), pk=pk)
        if not self.has_view_permission(request, co):
            raise PermissionDenied
        removed = [
            SimpleNamespace(
                description=l.description, quantity=l.quantity, unit_price=l.unit_price, credit=abs(l.amount),
                restocking_percent=l.restocking_percent,
            )
            for l in co.removed_lines
        ]
        added = [SimpleNamespace(description=l.description, quantity=l.quantity, unit_price=l.unit_price, amount=l.amount) for l in co.added_lines]
        if co.billing == ChangeOrder.Billing.ABSORB:
            billing_note = "The difference is absorbed. The contract price does not change."
        elif co.net_price:
            billing_note = "The difference is added to the final invoice."
        else:
            billing_note = "No change to the contract price."
        context = {
            "co": co, "quote": co.quote, "customer": co.quote.customer, "site": co.quote.site, "removed": removed,
            "added": added, "net_abs": abs(co.net_price), "billing_note": billing_note, "date": co.signed_on or co.created,
            "company_name": settings.COMPANY_NAME, "restocking_notice": settings.RESTOCKING_NOTICE,
            "watermark": {"requested": "DRAFT", "declined": "DECLINED"}.get(co.status, ""),
        }
        return TemplateResponse(request, "crm/changeorder_document.html", context)

    def get_changeform_initial_data(self, request):
        return {"quote": request.GET["quote"]} if request.GET.get("quote") else {}

    def get_fieldsets(self, request, obj=None):
        pricing = ["net_display"]
        if sees_costs(request):
            pricing += ["cost_display", "margin_display"]
        pricing += ["exceptions_note", "exceptions_approved"]
        return (
            (None, {"fields": ("number", "quote", "status", "reason", "billing", "document_link")}),
            ("Pricing (save to refresh totals)", {"fields": pricing}),
            ("Customer signature", {"fields": ("signed_by", "signed_on")}),
        )

    def get_readonly_fields(self, request, obj=None):
        fields = ["number", "net_display", "cost_display", "margin_display", "exceptions_note", "document_link"]
        if obj and obj.pk:
            fields.append("quote")
        if not request.user.has_perm("crm.approve_exceptions"):
            fields.append("exceptions_approved")
        if co_locked(obj):
            fields += [f for f in self.editable_fields if f not in fields]
        return fields

    @admin.display(description="Net change")
    def net_display(self, obj):
        return signed_dollars(obj.net_price) if obj.pk else "-"

    @admin.display(description="Cost change")
    def cost_display(self, obj):
        return signed_dollars(obj.net_cost) if obj.pk else "-"

    @admin.display(description="Margin change")
    def margin_display(self, obj):
        return signed_dollars(obj.margin_change) if obj.pk else "-"

    @admin.display(description="Exceptions")
    def exceptions_note(self, obj):
        if not obj.pk or not obj.below_floor_lines:
            return "None."
        return "Approved." if obj.exceptions_ok else "Needs manager approval: an added item is priced below its minimum."

    @admin.display(description="Customer copy")
    def document_link(self, obj):
        if not obj.pk:
            return "Save the change order first."
        return format_html('<a href="{}" target="_blank">Open the customer copy</a>', reverse("admin:crm_changeorder_document", args=[obj.pk]))

    def save_model(self, request, obj, form, change):
        if not change:
            obj.requested_by = request.user
        super().save_model(request, obj, form, change)

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        co, user = form.instance, request.user
        changed = set(form.changed_data)

        if user.has_perm("crm.approve_exceptions") and co.exceptions_approved:
            co.exceptions_signature = co.current_signature()
            co.exceptions_approved_by = user
            co.save(update_fields=["exceptions_signature", "exceptions_approved_by"])

        if problem := co.stage_problem():
            ChangeOrder.objects.filter(pk=co.pk).update(status=ChangeOrder.Status.REQUESTED)
            co.refresh_from_db()
            self.message_user(request, f"{problem} The change order was moved back to Requested.", messages.ERROR)
            return

        if not change:
            notify(SALES_GROUPS, f"{co.number} was requested. Price anything added, then take it to the customer to sign.", co.quote)
        if "status" in changed:
            if co.status == ChangeOrder.Status.SIGNED:
                co.apply_to_work_order()
                notify(OPS_GROUPS, f"{co.number} was signed by the customer. The change is noted on the work order.", co.quote)
            elif co.status == ChangeOrder.Status.DECLINED:
                notify(OPS_GROUPS, f"{co.number} was declined by the customer. The original scope stands.", co.quote)


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("message", "group", "quote", "created", "acknowledged_by")
    list_filter = ("acknowledged_at", "group")
    actions = ["acknowledge"]

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        return queryset if request.user.is_superuser else queryset.filter(group__in=request.user.groups.all())

    def has_add_permission(self, request):
        return False

    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in Notification._meta.fields]

    @admin.action(description="Acknowledge selected alerts")
    def acknowledge(self, request, queryset):
        queryset.filter(acknowledged_at__isnull=True).update(acknowledged_by=request.user, acknowledged_at=timezone.now())


from . import user_admin  # noqa: E402,F401
