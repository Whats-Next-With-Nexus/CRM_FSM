from decimal import Decimal
from types import SimpleNamespace

from django.urls import reverse
from django.utils import timezone

from .models import ChangeOrder, Quote


def _flag(text, level):
    return SimpleNamespace(text=text, level=level)


def _quote_flags(quote, today):
    flags = []
    if quote.ops_status == Quote.OpsStatus.REFUSED:
        flags.append(_flag("Returned by Ops", "warn"))
    elif quote.ops_status == Quote.OpsStatus.PENDING:
        flags.append(_flag("Waiting on Ops", "info"))
    elif quote.ops_status == Quote.OpsStatus.ACCEPTED:
        flags.append(_flag("Accepted by Ops", "ok"))
    open_stage = quote.stage not in (Quote.Stage.LOST, Quote.Stage.SENT_TO_OPS)
    if open_stage and quote.follow_up_on and quote.follow_up_on < today:
        flags.append(_flag("Follow-up overdue", "warn"))
    if quote.stage == Quote.Stage.QUOTE_SENT and quote.valid_until and quote.valid_until < today:
        flags.append(_flag("Quote expired", "warn"))
    if quote.revision > 1:
        flags.append(_flag(f"Revision {quote.revision}", "info"))
    return flags


def build_overview(customer, user):
    """Everything the customer page shows, limited to what this user's permissions allow."""
    today = timezone.localdate()
    can = user.has_perm
    sees_quotes, sees_systems = can("crm.view_quote"), can("crm.view_system")
    sees_work_orders, sees_contractors = can("fsm.view_workorder"), can("crm.view_contractor")
    sees_costs, sees_plans = can("crm.view_costs"), can("crm.view_repairplanenrollment")

    sites = customer.sites.prefetch_related(
        "systems__system_type", "repair_plans__plan", "quotes__lines__item", "quotes__system_types",
        "quotes__change_orders__lines__item", "quotes__change_orders__lines__quote_line__item",
        "work_orders__visits__assignments", "work_orders__lead_tech",
    )
    cards, attention = [], []
    all_quotes, all_work_orders = [], []

    for site in sites:
        plan = next((e.plan for e in site.repair_plans.all() if e.active), None) if sees_plans else None
        card = SimpleNamespace(site=site, plan=plan, systems=[], quotes=[], work_orders=[])

        if sees_systems:
            for system in site.systems.all():
                card.systems.append(SimpleNamespace(
                    system=system, contract=system.contracted_label if sees_contractors else None,
                ))

        if sees_quotes:
            for quote in site.quotes.all():
                all_quotes.append(quote)
                flags = _quote_flags(quote, today)
                open_orders = [co for co in quote.change_orders.all() if co.status in (ChangeOrder.Status.REQUESTED, ChangeOrder.Status.PRICED)]
                row = SimpleNamespace(
                    quote=quote, flags=flags, total=quote.total_price, current=quote.current_value,
                    margin_percent=quote.margin_percent if sees_costs else None,
                    change_orders=list(quote.change_orders.all()),
                    url=reverse("admin:crm_quote_change", args=[quote.pk]),
                    document_url=reverse("admin:crm_quote_document", args=[quote.pk]),
                )
                card.quotes.append(row)
                for flag in flags:
                    if flag.level == "warn":
                        attention.append(SimpleNamespace(text=f"{quote.number}: {flag.text.lower()}", url=row.url, level="warn"))
                for co in open_orders:
                    text = "needs pricing" if co.status == ChangeOrder.Status.REQUESTED else "is waiting on the customer's signature"
                    attention.append(SimpleNamespace(
                        text=f"{co.number} {text}", url=reverse("admin:crm_changeorder_change", args=[co.pk]), level="info",
                    ))

        if sees_work_orders:
            for work_order in site.work_orders.all():
                all_work_orders.append(work_order)
                card.work_orders.append(SimpleNamespace(
                    work_order=work_order, hours=work_order.hours_logged,
                    url=reverse("admin:fsm_workorder_change", args=[work_order.pk]),
                ))
        cards.append(card)

    won = [q for q in all_quotes if q.stage in Quote.WON_STAGES]
    open_quotes = [q for q in all_quotes if q.stage in (Quote.Stage.NEW_LEAD, Quote.Stage.QUOTE_SENT)]
    follow_ups = [q.follow_up_on for q in open_quotes if q.follow_up_on]
    summary = SimpleNamespace(
        sites=len(cards),
        won_value=sum((q.current_value for q in won), Decimal("0")),
        open_value=sum((q.total_price for q in open_quotes), Decimal("0")),
        open_quotes=len(open_quotes),
        open_work_orders=sum(1 for w in all_work_orders if w.status not in ("complete", "cancelled")),
        next_follow_up=min(follow_ups) if follow_ups else None,
    )
    return SimpleNamespace(
        customer=customer, cards=cards, attention=attention, summary=summary, today=today,
        contract=customer.contracted_label if sees_contractors else None,
        sees_quotes=sees_quotes, sees_systems=sees_systems, sees_work_orders=sees_work_orders,
        sees_costs=sees_costs, sees_plans=sees_plans,
        can_edit=can("crm.change_customer"), can_add_site=can("crm.add_site"), can_add_quote=can("crm.add_quote"),
    )
