from django.contrib import admin, messages
from django.core.exceptions import ValidationError

from .models import Skill, TechProfile, Visit, VisitAssignment, WorkOrder


class VisitInline(admin.TabularInline):
    model = Visit
    extra = 0
    fields = ("scheduled_for", "summary", "started_at")
    readonly_fields = ("started_at",)
    show_change_link = True


@admin.register(WorkOrder)
class WorkOrderAdmin(admin.ModelAdmin):
    list_display = ("number", "wo_type", "status", "site", "system", "billing")
    list_filter = ("wo_type", "status", "billing")
    search_fields = ("site__address", "site__customer__name", "description")
    readonly_fields = ("number", "hours_logged")
    inlines = [VisitInline]
    fieldsets = (
        (None, {"fields": ("number", "wo_type", "status", "site", "system", "quote", "description", "hours_logged")}),
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


@admin.register(Skill)
class SkillAdmin(admin.ModelAdmin):
    list_display = ("name", "active")


@admin.register(TechProfile)
class TechProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "skill_list")
    filter_horizontal = ("skills",)

    @admin.display(description="Skills")
    def skill_list(self, obj):
        return ", ".join(skill.name for skill in obj.skills.all()) or "-"


class AssignmentInline(admin.TabularInline):
    model = VisitAssignment
    extra = 1
    fields = ("tech", "skill", "notes", "started_at", "ended_at", "hours_display")
    readonly_fields = ("started_at", "ended_at", "hours_display")

    @admin.display(description="Hours")
    def hours_display(self, obj):
        return obj.hours if obj.pk else "-"


@admin.register(Visit)
class VisitAdmin(admin.ModelAdmin):
    list_display = ("__str__", "work_order", "scheduled_for", "started_at", "crew")
    list_filter = ("scheduled_for",)
    search_fields = ("work_order__site__address", "work_order__site__customer__name")
    readonly_fields = ("started_at",)
    inlines = [AssignmentInline]

    @admin.display(description="Assigned")
    def crew(self, obj):
        return ", ".join(f"{a.tech} ({a.skill})" for a in obj.assignments.all()) or "-"


@admin.register(VisitAssignment)
class VisitAssignmentAdmin(admin.ModelAdmin):
    """Technicians see only their own assignments here and can start and stop the job; managers see everyone's."""

    list_display = ("visit", "site", "tech", "skill", "started_at", "ended_at", "hours")
    list_filter = ("skill", "tech")
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
