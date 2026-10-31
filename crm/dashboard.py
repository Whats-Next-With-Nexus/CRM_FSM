from collections import Counter
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import urlencode

from django.urls import reverse
from django.utils import timezone

from fsm.models import Visit, VisitAssignment, WorkOrder

from .models import ChangeOrder, Notification, Quote

OPEN_STAGES = (Quote.Stage.NEW_LEAD, Quote.Stage.QUOTE_SENT)
LIVE_STAGES = tuple(s for s in Quote.Stage.values if s not in (Quote.Stage.LOST, Quote.Stage.SENT_TO_OPS))
FINISHED = (WorkOrder.Status.COMPLETE, WorkOrder.Status.CANCELLED)
WIN_WINDOW_DAYS = 90


def _url(name, **params):
    base = reverse(name)
    return f"{base}?{urlencode(params)}" if params else base


def _tile(label, value, sub="", url=None, level=""):
    return SimpleNamespace(label=label, value=value, sub=sub, url=url, level=level)


def _cell(text, url=None, level="", num=False):
    return SimpleNamespace(text=text, url=url, level=level, num=num)


def _dollars(value):
    return f"${value:,.0f}"


def _bar(label, value_text, value, biggest, level=""):
    return SimpleNamespace(label=label, value_text=value_text, pct=int(100 * value / biggest) if biggest else 0, level=level)


def _sales_blocks(user, today, manager_scope):
    quotes = Quote.objects.select_related("site__customer", "salesperson").prefetch_related("lines__item")
    scope = {}
    if not manager_scope:
        quotes = quotes.filter(salesperson=user)
        scope = {"salesperson__id__exact": user.pk}
    quotes = list(quotes)
    name = "admin:crm_quote_changelist"

    open_quotes = [q for q in quotes if q.stage in OPEN_STAGES]
    live = [q for q in quotes if q.stage in LIVE_STAGES]
    overdue = [q for q in live if q.follow_up_on and q.follow_up_on < today]
    due_week = [q for q in live if q.follow_up_on and today <= q.follow_up_on <= today + timedelta(days=7)]
    returned = [q for q in quotes if q.ops_status == Quote.OpsStatus.REFUSED]
    expiring = [q for q in quotes if q.stage == Quote.Stage.QUOTE_SENT and q.valid_until and q.valid_until <= today + timedelta(days=7)]

    title = "Quotes and follow-ups" if manager_scope else "My quotes and follow-ups"
    blocks = [{
        "kind": "tiles", "title": title, "tiles": [
            _tile("Open quotes", len(open_quotes), _dollars(sum((q.total_price for q in open_quotes), Decimal("0"))) + " pending",
                  _url(name, stage__in="new_lead,quote_sent", **scope)),
            _tile("Follow-ups overdue", len(overdue), "", _url(name, follow_up="overdue", **scope), "bad" if overdue else "ok"),
            _tile("Due this week", len(due_week), "", _url(name, follow_up="week", **scope)),
            _tile("Returned by Ops", len(returned), "Fix and send again", _url(name, ops_status__exact="refused", **scope), "warn" if returned else ""),
            _tile("Expiring soon", len(expiring), "Within 7 days or past due", _url(name, stage__exact="quote_sent", **scope), "warn" if expiring else ""),
        ],
    }]

    upcoming = sorted((q for q in live if q.follow_up_on), key=lambda q: q.follow_up_on)[:10]
    rows = [
        [
            _cell(q.number, reverse("admin:crm_quote_change", args=[q.pk])), _cell(q.customer.name), _cell(q.get_stage_display()),
            _cell(f"${q.total_price:,.2f}", num=True),
            _cell(q.follow_up_on.strftime("%b %d").replace(" 0", " "), level="bad" if q.follow_up_on < today else ""),
        ]
        for q in upcoming
    ]
    blocks.append({
        "kind": "table", "title": "Next follow-ups", "link": ("Open all quotes", _url(name, **scope)),
        "columns": ["Quote", "Customer", "Stage", "Total", "Follow-up"], "num_columns": [3], "rows": rows,
        "empty": "Nothing to follow up. Set a follow-up date on a quote to see it here.",
    })
    return blocks


def _performance_blocks(user, today):
    """Manager-level view of the whole pipeline: win rate, stage bars and why quotes are lost."""
    quotes = list(Quote.objects.prefetch_related("lines__item"))
    cutoff = timezone.now() - timedelta(days=WIN_WINDOW_DAYS)
    recent = [q for q in quotes if q.created >= cutoff]
    won = [q for q in recent if q.stage in Quote.WON_STAGES]
    lost = [q for q in recent if q.stage == Quote.Stage.LOST]
    rate = f"{round(100 * len(won) / (len(won) + len(lost)))}%" if won or lost else "-"

    tiles = [_approval_tile(today), _tile(f"Win rate, last {WIN_WINDOW_DAYS} days", rate, f"{len(won)} won, {len(lost)} lost")]
    if user.has_perm("crm.view_costs"):
        margins = [q.margin_percent for q in quotes if q.stage in OPEN_STAGES and q.lines.all()]
        average = f"{(sum(margins) / len(margins)):.1f}%" if margins else "-"
        tiles.append(_tile("Average margin, open quotes", average, f"{len(margins)} quotes"))
    reasons = Counter(q.get_lost_reason_display() for q in lost if q.lost_reason).most_common(3)
    blocks = [{"kind": "tiles", "title": "Performance", "tiles": tiles}]

    stages = [Quote.Stage.NEW_LEAD, Quote.Stage.QUOTE_SENT, Quote.Stage.APPROVED, Quote.Stage.DEPOSIT, Quote.Stage.SENT_TO_OPS]
    totals = {s: [q for q in quotes if q.stage == s] for s in stages}
    values = {s: sum((q.total_price for q in qs), Decimal("0")) for s, qs in totals.items()}
    biggest = max(values.values(), default=0)
    bars = [
        _bar(f"{Quote.Stage(s).label} ({len(totals[s])})", _dollars(values[s]), values[s], biggest) for s in stages
    ]
    blocks.append({"kind": "bars", "title": "Pipeline value by stage", "items": bars, "empty": "No quotes yet."})
    if reasons:
        top = reasons[0][1]
        blocks.append({
            "kind": "bars", "title": f"Top reasons quotes were lost, last {WIN_WINDOW_DAYS} days",
            "items": [_bar(reason, str(count), count, top, "bad") for reason, count in reasons], "empty": "",
        })
    return blocks


def _approval_tile(today):
    waiting = [
        q for q in Quote.objects.filter(stage=Quote.Stage.NEW_LEAD).prefetch_related("lines__item")
        if q.lines.all() and q.needs_exception_approval and not q.exceptions_ok
    ]
    return _tile(
        "Needs your approval", len(waiting), "Below-minimum pricing or a low deposit",
        _url("admin:crm_quote_changelist", stage__exact="new_lead"), "warn" if waiting else "ok",
    )


def _ops_blocks(user, today):
    can = user.has_perm
    week = today + timedelta(days=6)
    quote_list = "admin:crm_quote_changelist"
    wo_list = "admin:fsm_workorder_changelist"

    installs = list(
        WorkOrder.objects.filter(wo_type=WorkOrder.Type.INSTALL).exclude(status__in=FINISHED)
        .select_related("lead_tech", "quote").prefetch_related("visits__assignments", "quote__lines__item", "quote__change_orders__lines__item", "quote__change_orders__lines__quote_line__item")
    )
    running = [w for w in installs if w.status == WorkOrder.Status.IN_PROGRESS]
    over = [w for w in running if w.hours_saved is not None and w.hours_saved < 0] if can("fsm.view_labor_bonus") else []
    no_lead = [w for w in installs if w.lead_tech_id is None]
    waiting = Quote.objects.filter(ops_status=Quote.OpsStatus.PENDING).count()
    visits = list(Visit.objects.filter(scheduled_for__range=(today, week)).prefetch_related("assignments"))
    unassigned = [v for v in visits if not v.assignments.all()]
    open_cos = ChangeOrder.objects.filter(status__in=[ChangeOrder.Status.REQUESTED, ChangeOrder.Status.PRICED]).count()
    alerts = Notification.objects.filter(acknowledged_at__isnull=True)
    if not user.is_superuser:
        alerts = alerts.filter(group__in=user.groups.all())

    tiles = [
        _tile("Waiting on Ops", waiting, "Accept or return", _url(quote_list, ops_status__exact="pending"), "warn" if waiting else "ok"),
        _tile("Installs in progress", len(running), f"{len(over)} over estimate" if can("fsm.view_labor_bonus") else "",
              _url(wo_list, wo_type__exact="install", status__exact="in_progress"), "bad" if over else ""),
        _tile("Installs with no lead", len(no_lead), "Set a lead technician", _url(wo_list, wo_type__exact="install"), "warn" if no_lead else "ok"),
        _tile("Visits, next 7 days", len(visits), f"{len(unassigned)} with no technician", reverse("admin:fsm_visit_changelist"),
              "warn" if unassigned else ""),
        _tile("Change orders open", open_cos, "To price or sign", _url("admin:crm_changeorder_changelist", status__in="requested,priced"), "warn" if open_cos else "ok"),
        _tile("Unread alerts", alerts.count(), "", _url("admin:crm_notification_changelist", acknowledged_at__isnull="True"), "warn" if alerts.exists() else "ok"),
    ]
    if can("crm.approve_exceptions"):
        tiles.insert(1, _approval_tile(today))
    blocks = [{"kind": "tiles", "title": "Operations", "tiles": tiles}]

    if can("crm.view_contractor"):
        sub_open = list(
            WorkOrder.objects.filter(wo_type=WorkOrder.Type.SUBCONTRACT).exclude(status__in=FINISHED)
            .select_related("site__customer__contracted_through")
        )
        by_contractor = Counter(w.site.customer.contracted_label for w in sub_open)
        service_open = list(WorkOrder.objects.filter(wo_type=WorkOrder.Type.SERVICE).exclude(status__in=FINISHED))
        billing = Counter(w.get_billing_display() for w in service_open if w.billing)
        extras = [
            _tile("Subcontract work open", len(sub_open), " · ".join(f"{n} {c}" for n, c in by_contractor.most_common()) or "None",
                  _url(wo_list, wo_type__exact="subcontract")),
            _tile("Service calls open", len(service_open), " · ".join(f"{c} {n.lower()}" for n, c in billing.most_common()) or "None",
                  _url(wo_list, wo_type__exact="service")),
        ]
        if can("fsm.view_labor_bonus"):
            every = list(
                WorkOrder.objects.filter(wo_type=WorkOrder.Type.INSTALL).exclude(status=WorkOrder.Status.CANCELLED)
                .select_related("quote").prefetch_related("visits__assignments", "quote__lines__item", "quote__change_orders__lines__item", "quote__change_orders__lines__quote_line__item")
            )
            earned = sum((w.bonus for w in every if w.bonus_earned), Decimal("0"))
            projected = sum((w.bonus for w in every if not w.bonus_earned), Decimal("0"))
            extras.append(_tile("Labor bonus earned", f"${earned:,.0f}", f"${projected:,.0f} projected", reverse("admin:fsm_workorder_labor_report")))
        blocks.append({"kind": "tiles", "title": "Subcontract, service and labor", "tiles": extras})

    today_visits = Visit.objects.filter(scheduled_for=today).select_related("work_order__site").prefetch_related("assignments__tech", "assignments__skill")
    rows = []
    for visit in today_visits:
        crew = ", ".join(f"{a.tech.get_username()} ({a.skill})" for a in visit.assignments.all()) or "No technician yet"
        state = f"Started {timezone.localtime(visit.started_at):%I:%M %p}".replace(" 0", " ") if visit.started_at else "Not started"
        rows.append([
            _cell(str(visit), reverse("admin:fsm_visit_change", args=[visit.pk])), _cell(str(visit.work_order.site or "Internal")),
            _cell(crew, level="" if visit.assignments.all() else "bad"), _cell(state),
        ])
    blocks.append({
        "kind": "table", "title": "Today's visits", "link": ("All visits", reverse("admin:fsm_visit_changelist")),
        "columns": ["Visit", "Site", "Crew", "Status"], "num_columns": [], "rows": rows, "empty": "No visits scheduled for today.",
    })
    return blocks


def _technician_blocks(user, today):
    assignments = list(
        VisitAssignment.objects.filter(tech=user).select_related("visit__work_order__site", "skill")
    )
    week_start = today - timedelta(days=today.weekday())

    def job(a):
        site = a.visit.work_order.site
        state = "done" if a.ended_at else "running" if a.started_at else "ready"
        return SimpleNamespace(
            pk=a.pk, visit=str(a.visit), site=str(site) if site else "Internal time", address=site.address if site else "",
            access=site.access_notes if site else "", skill=str(a.skill), state=state, level="warn" if state == "running" else "",
            description=a.visit.work_order.description.splitlines()[0] if a.visit.work_order.description else "",
        )

    running = [a for a in assignments if a.started_at and not a.ended_at]
    todays = [a for a in assignments if a.visit.scheduled_for == today and a not in running]
    later = [a for a in assignments if a.visit.scheduled_for and today < a.visit.scheduled_for <= today + timedelta(days=7)]
    hours = sum((a.hours for a in assignments if a.ended_at and timezone.localtime(a.ended_at).date() >= week_start), Decimal("0"))
    return [{
        "kind": "jobs", "title": "My jobs",
        "running": [job(a) for a in running], "today": [job(a) for a in todays],
        "later": [(a.visit.scheduled_for, job(a)) for a in sorted(later, key=lambda a: a.visit.scheduled_for)],
        "hours": f"{hours:.2f}".rstrip("0").rstrip("."),
    }]


def build_dashboard(user):
    today = timezone.localdate()
    can = user.has_perm
    blocks = []
    if can("crm.add_quote"):
        manager_scope = user.is_superuser or can("crm.approve_exceptions") or can("crm.decide_intake")
        blocks += _sales_blocks(user, today, manager_scope)
        if can("crm.approve_exceptions"):
            blocks += _performance_blocks(user, today)
    if can("crm.decide_intake"):
        blocks += _ops_blocks(user, today)
    elif can("crm.approve_exceptions") and can("crm.view_quote") and not can("crm.add_quote"):
        blocks.append({"kind": "tiles", "title": "Approvals", "tiles": [_approval_tile(today)]})
    if can("fsm.view_visitassignment") and not can("fsm.add_visitassignment"):
        blocks += _technician_blocks(user, today)
    return SimpleNamespace(blocks=blocks, today=today, name=user.get_full_name() or user.get_username())


def dashboard_callback(request, context):
    context["dash"] = build_dashboard(request.user)
    return context
