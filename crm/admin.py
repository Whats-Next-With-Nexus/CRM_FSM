from datetime import timedelta

from django.conf import settings
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html

from .forms import NewLeadForm
from .models import (
    Contractor, Customer, Notification, Quote, QuoteLine, RepairPlan, RepairPlanEnrollment, Site, System, SystemType,
    notify,
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
    inlines = [QuoteLineInline]
    cost_columns = ("margin_pct",)

    def get_urls(self):
        custom = [
            path("new-lead/", self.admin_site.admin_view(self.new_lead_view), name="crm_quote_new_lead"),
            path("<int:pk>/document/", self.admin_site.admin_view(self.document_view), name="crm_quote_document"),
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
        context = {
            "quote": quote,
            "customer": quote.customer,
            "site": quote.site,
            "lines": quote.lines.select_related("item"),
            "system_types": quote.system_types.all(),
            "company_name": settings.COMPANY_NAME,
            "terms": settings.QUOTE_TERMS,
            "is_draft": quote.stage == Quote.Stage.NEW_LEAD,
        }
        return TemplateResponse(request, "crm/quote_document.html", context)

    def get_list_display(self, request):
        columns = super().get_list_display(request)
        return columns if sees_costs(request) else [c for c in columns if c not in self.cost_columns]

    def get_fieldsets(self, request, obj=None):
        pricing = ["price_total"]
        if sees_costs(request):
            pricing += ["cost_total", "margin_total", "margin_pct"]
        pricing += ["deposit_percent", "deposit_amount_due", "exceptions_note", "exceptions_approved"]
        return (
            (None, {"fields": (
                "number", "site", "stage", "system_types", "salesperson", "follow_up_on", "notes", "sent_on", "valid_until",
                "document_link",
            )}),
            ("Pricing (save to refresh totals)", {"fields": pricing}),
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
            "deposit_amount_due", "exceptions_note",
        ]
        user = request.user
        if not user.has_perm("crm.approve_exceptions"):
            fields.append("exceptions_approved")
        if not (user.has_perm("crm.decide_intake") and sent_to_ops(obj)):
            fields += ["ops_status", "ops_note"]
        if sent_to_ops(obj) and not user.has_perm("crm.override_sent_quote"):
            fields += [f for f in EDITABLE_QUOTE_FIELDS if f not in fields]
        return fields

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
