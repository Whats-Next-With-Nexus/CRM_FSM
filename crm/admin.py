from django.contrib import admin

from .models import (
    Contractor, Customer, Quote, RepairPlan, RepairPlanEnrollment, Site, System, SystemType,
)

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
    list_display = ("name", "number", "status", "contracted_label", "contact_name", "phone")
    list_filter = ("status", "contracted_through")
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


@admin.register(Quote)
class QuoteAdmin(admin.ModelAdmin):
    list_display = ("number", "site", "stage", "amount", "salesperson", "created")
    list_filter = ("stage", "salesperson")
    search_fields = ("site__address", "site__customer__name")
    readonly_fields = ("number",)
    filter_horizontal = ("system_types",)
    fieldsets = (
        (None, {"fields": ("number", "site", "stage", "system_types", "amount", "deposit_amount", "salesperson", "notes")}),
        ("Sent to Ops checklist", {"fields": ("agreement_signed", "deposit_received", "deliverables_verified", "permits_required")}),
    )


from . import user_admin  # noqa: E402,F401
