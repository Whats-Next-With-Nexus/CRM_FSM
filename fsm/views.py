from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST

from .models import VisitAssignment


@staff_member_required
@require_POST
def assignment_action(request, pk, verb):
    """Start or stop a job from the dashboard. Technicians act on their own jobs; managers on anyone's."""
    if verb not in ("start", "stop"):
        raise Http404
    assignment = get_object_or_404(VisitAssignment, pk=pk)
    user = request.user
    if not user.has_perm("fsm.change_visitassignment"):
        raise PermissionDenied
    if assignment.tech_id != user.pk and not user.has_perm("fsm.add_visitassignment"):
        raise PermissionDenied
    try:
        getattr(assignment, verb)()
    except ValidationError as error:
        messages.error(request, error.messages[0])
    else:
        site = assignment.visit.work_order.site
        where = f" at {site.address}" if site else ""
        messages.success(request, ("Job started" if verb == "start" else "Job stopped") + where + ".")
    return redirect("admin:index")
