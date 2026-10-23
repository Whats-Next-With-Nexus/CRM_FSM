from django.contrib import admin

from .models import Visit, WorkOrder


class VisitInline(admin.TabularInline):
    model = Visit
    extra = 0
    fields = ("scheduled_for", "summary")


@admin.register(WorkOrder)
class WorkOrderAdmin(admin.ModelAdmin):
    list_display = ("number", "wo_type", "status", "site", "system", "billing")
    list_filter = ("wo_type", "status", "billing")
    search_fields = ("site__address", "site__customer__name", "description")
    readonly_fields = ("number",)
    inlines = [VisitInline]
    fieldsets = (
        (None, {"fields": ("number", "wo_type", "status", "site", "system", "quote", "description")}),
        ("Service billing", {"fields": ("billing", "billing_override_reason")}),
    )

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))
        locked = obj and obj.wo_type == WorkOrder.Type.SUBCONTRACT and not request.user.has_perm("fsm.manage_subcontract")
        if locked:
            # visits stay editable through the inline, so scheduling still works
            fields += [f.name for f in WorkOrder._meta.fields if f.name not in fields]
        return fields

    def formfield_for_choice_field(self, db_field, request, **kwargs):
        if db_field.name == "wo_type" and not request.user.has_perm("fsm.manage_subcontract"):
            kwargs["choices"] = [c for c in db_field.choices if c[0] != WorkOrder.Type.SUBCONTRACT]
        return super().formfield_for_choice_field(db_field, request, **kwargs)
