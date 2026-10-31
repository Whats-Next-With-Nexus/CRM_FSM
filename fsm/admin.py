from decimal import Decimal

from django.conf import settings
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import path

from unfold.admin import ModelAdmin, TabularInline
from unfold.decorators import action, display
from .models import InternalTimeCategory, Skill, TechProfile, Visit, VisitAssignment, WorkOrder


class VisitInline(TabularInline):
    model = Visit
    extra = 0
    fields = ("scheduled_for", "summary", "started_at")
    readonly_fields = ("started_at",)
    show_change_link = True


@admin.register(WorkOrder)
class WorkOrderAdmin(ModelAdmin):
    list_display = ("number", "wo_type", "internal_category", "status_label", "site", "system", "billing", "hours_logged")
    actions_list = ["labor_report_action"]

    @display(
        description="Status", ordering="status",
        label={"open": "info", "scheduled": "warning", "in_progress": "warning", "complete": "success", "cancelled": "danger"},
    )
    def status_label(self, obj):
        return obj.status, obj.get_status_display()

    def has_labor_report_permission(self, request):
        return request.user.has_perm("fsm.view_labor_bonus")

    @action(description="Labor and bonus report", icon="payments", permissions=["labor_report"])
    def labor_report_action(self, request):
        return redirect("admin:fsm_workorder_labor_report")
    list_filter = ("wo_type", "internal_category", "status", "billing")
    search_fields = ("site__address", "site__customer__name", "description")
    readonly_fields = ("number", "hours_logged", "estimated_display", "saved_display", "labor_cost_display", "bonus_display")
    inlines = [VisitInline]

    def get_urls(self):
        custom = [path("labor-report/", self.admin_site.admin_view(self.labor_report_view), name="fsm_workorder_labor_report")]
        return custom + super().get_urls()

    def labor_report_view(self, request):
        if not request.user.has_perm("fsm.view_labor_bonus"):
            raise PermissionDenied
        installs = WorkOrder.objects.filter(wo_type=WorkOrder.Type.INSTALL).select_related("quote", "site", "lead_tech")
        if request.GET.get("status") == "complete":
            installs = installs.filter(status=WorkOrder.Status.COMPLETE)
        rows, by_lead = [], {}
        for work_order in installs.order_by("-id"):
            rows.append(work_order)
            if work_order.bonus_earned and work_order.bonus:
                lead = work_order.lead_tech.get_username() if work_order.lead_tech else "No lead technician set"
                by_lead[lead] = by_lead.get(lead, Decimal("0")) + work_order.bonus
        context = {
            **self.admin_site.each_context(request), "title": "Install labor and bonus report", "rows": rows,
            "by_lead": sorted(by_lead.items()), "earned_total": sum(by_lead.values(), Decimal("0")),
            "rate": settings.LABOR_COST_RATE, "percent": settings.INSTALL_BONUS_PERCENT, "opts": self.model._meta,
            "complete_only": request.GET.get("status") == "complete",
        }
        return TemplateResponse(request, "admin/fsm/workorder/labor_report.html", context)

    def get_fieldsets(self, request, obj=None):
        fieldsets = [
            (None, {"fields": (
                "number", "wo_type", "internal_category", "status", "site", "system", "quote", "lead_tech", "description",
                "hours_logged",
            )}),
            ("Service billing", {"fields": ("billing", "billing_override_reason")}),
        ]
        if obj and obj.wo_type == WorkOrder.Type.INSTALL and request.user.has_perm("fsm.view_labor_bonus"):
            fieldsets.append(("Labor and bonus", {"fields": (
                "estimated_display", "hours_logged", "saved_display", "labor_cost_display", "bonus_display",
            )}))
        return fieldsets

    @admin.display(description="Estimated hours")
    def estimated_display(self, obj):
        hours = obj.estimated_hours
        return "No quote linked." if hours is None else f"{hours:f}".rstrip("0").rstrip(".") + " hours sold, including signed change orders"

    @admin.display(description="Hours saved")
    def saved_display(self, obj):
        saved = obj.hours_saved
        if saved is None:
            return "-"
        return f"{saved:f}".rstrip("0").rstrip(".") + (" hours" if saved > 0 else " hours (over the estimate, no bonus)" if saved < 0 else " hours")

    @admin.display(description="Labor cost")
    def labor_cost_display(self, obj):
        return f"${obj.labor_cost:,.2f} ({settings.LABOR_COST_RATE:f}/hr)"

    @admin.display(description="Lead technician bonus")
    def bonus_display(self, obj):
        who = obj.lead_tech.get_username() if obj.lead_tech else "no lead technician set"
        state = "earned" if obj.bonus_earned else "projected until the install is complete"
        return f"${obj.bonus:,.2f} for {who}, {state}"

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


@admin.register(Skill)
class SkillAdmin(ModelAdmin):
    list_display = ("name", "active")


@admin.register(InternalTimeCategory)
class InternalTimeCategoryAdmin(ModelAdmin):
    list_display = ("name", "active")


@admin.register(TechProfile)
class TechProfileAdmin(ModelAdmin):
    list_display = ("user", "skill_list")
    filter_horizontal = ("skills",)

    @admin.display(description="Skills")
    def skill_list(self, obj):
        return ", ".join(skill.name for skill in obj.skills.all()) or "-"


class AssignmentInline(TabularInline):
    model = VisitAssignment
    extra = 1
    fields = ("tech", "skill", "notes", "started_at", "ended_at", "hours_display")
    readonly_fields = ("started_at", "ended_at", "hours_display")

    @admin.display(description="Hours")
    def hours_display(self, obj):
        return obj.hours if obj.pk else "-"


@admin.register(Visit)
class VisitAdmin(ModelAdmin):
    list_display = ("__str__", "work_order", "scheduled_for", "started_at", "crew")
    list_filter = ("scheduled_for",)
    search_fields = ("work_order__site__address", "work_order__site__customer__name")
    readonly_fields = ("started_at",)
    inlines = [AssignmentInline]

    @admin.display(description="Assigned")
    def crew(self, obj):
        return ", ".join(f"{a.tech} ({a.skill})" for a in obj.assignments.all()) or "-"


@admin.register(VisitAssignment)
class VisitAssignmentAdmin(ModelAdmin):
    """Technicians see only their own assignments here and can start and stop the job; managers see everyone's."""

    list_display = ("visit", "site", "tech", "skill", "started_at", "ended_at", "hours")
    list_filter = ("visit__work_order__wo_type", "visit__work_order__internal_category", "skill", "tech")
    actions = ["start_job", "stop_job"]

    def _is_manager(self, request):
        return request.user.has_perm("fsm.add_visitassignment")

    def get_queryset(self, request):
        queryset = super().get_queryset(request).select_related("visit__work_order__site", "tech", "skill")
        return queryset if self._is_manager(request) else queryset.filter(tech=request.user)

    def get_readonly_fields(self, request, obj=None):
        fields = ["visit", "tech", "skill", "started_at", "ended_at"]
        return fields if not self._is_manager(request) else ["started_at", "ended_at"]

    def has_add_permission(self, request):
        return False

    @admin.display(description="Site")
    def site(self, obj):
        return obj.visit.work_order.site

    def _act(self, request, queryset, verb):
        done = 0
        for assignment in queryset:
            if assignment.tech_id != request.user.pk and not self._is_manager(request):
                continue
            try:
                getattr(assignment, verb)()
                done += 1
            except ValidationError as error:
                self.message_user(request, f"{assignment}: {error.messages[0]}", messages.ERROR)
        if done:
            self.message_user(request, f"{done} job(s) updated.", messages.SUCCESS)

    @admin.action(description="Start job (arrived on site)", permissions=["change"])
    def start_job(self, request, queryset):
        self._act(request, queryset, "start")

    @admin.action(description="Stop job (leaving site)", permissions=["change"])
    def stop_job(self, request, queryset):
        self._act(request, queryset, "stop")
