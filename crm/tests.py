from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from crm.models import Customer, Notification, Quote, QuoteLine, Site
from fsm.models import WorkOrder
from inventory.models import CatalogItem

User = get_user_model()


class QuoteRulesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        customer = Customer.objects.create(name="Test Co")
        cls.site = Site.objects.create(customer=customer, address="1 Main St")
        cls.switch = CatalogItem.objects.create(sku="SW", name="PoE Switch", unit_cost=100, list_price=180, min_price=150)
        cls.labor = CatalogItem.objects.create(
            sku="LAB", name="Install", kind=CatalogItem.Kind.LABOR, unit_cost=40, list_price=90, min_price=75,
        )

    def make_quote(self, **line_prices):
        quote = Quote.objects.create(site=self.site)
        QuoteLine.objects.create(quote=quote, item=self.switch, quantity=2, unit_price=line_prices.get("switch"))
        QuoteLine.objects.create(quote=quote, item=self.labor, quantity=10, unit_price=line_prices.get("labor"))
        return quote

    def test_line_snapshots_catalog_values_and_keeps_them(self):
        line = self.make_quote().lines.first()
        self.assertEqual((line.unit_cost, line.unit_price, line.unit_min_price), (100, 180, 150))
        self.assertEqual(line.description, "PoE Switch")
        self.switch.unit_cost = self.switch.list_price = self.switch.min_price = 999
        self.switch.save()
        line.refresh_from_db()
        self.assertEqual((line.unit_cost, line.unit_price, line.unit_min_price), (100, 180, 150))

    def test_totals_margin_and_deposit(self):
        quote = self.make_quote()
        self.assertEqual(quote.total_price, Decimal("1260.00"))
        self.assertEqual(quote.total_cost, Decimal("600.00"))
        self.assertEqual(quote.margin, Decimal("660.00"))
        self.assertEqual(quote.margin_percent, Decimal("52.38"))
        self.assertEqual(quote.deposit_due, Decimal("630.00"))

    def test_empty_quote_has_zero_margin_percent(self):
        self.assertEqual(Quote.objects.create(site=self.site).margin_percent, Decimal("0.00"))

    def test_minimum_price_is_checked_per_line(self):
        self.assertFalse(self.make_quote().needs_exception_approval)
        self.assertTrue(self.make_quote(switch=149).needs_exception_approval)
        self.assertFalse(self.make_quote(switch=150).needs_exception_approval)

    def test_deposit_under_standard_needs_approval(self):
        quote = self.make_quote()
        quote.deposit_percent = Decimal("49.9")
        self.assertTrue(quote.needs_exception_approval)
        quote.deposit_percent = Decimal("50")
        self.assertFalse(quote.needs_exception_approval)

    def test_priced_stage_is_refused_until_exceptions_are_approved_and_approval_goes_stale(self):
        from django.core.exceptions import ValidationError

        quote = self.make_quote(switch=140)
        quote.stage = Quote.Stage.QUOTE_SENT
        with self.assertRaisesMessage(ValidationError, "below its minimum"):
            quote.full_clean()

        quote.exceptions_approved = True
        quote.exceptions_signature = quote.current_signature()
        quote.full_clean()
        self.assertTrue(quote.exceptions_ok)

        line = quote.lines.first()
        line.unit_price = 130
        line.save()
        self.assertFalse(quote.exceptions_ok)
        self.assertIn("no longer matches", quote.exception_problem())

    def test_returning_a_quote_needs_a_reason_and_accepting_needs_it_to_be_sent(self):
        from django.core.exceptions import ValidationError

        quote = self.make_quote()
        quote.ops_status = Quote.OpsStatus.REFUSED
        with self.assertRaisesMessage(ValidationError, "what is missing"):
            quote.full_clean()
        quote.ops_status = Quote.OpsStatus.ACCEPTED
        with self.assertRaisesMessage(ValidationError, "sent to Ops"):
            quote.full_clean()

    def test_sending_marks_the_quote_pending_and_does_not_create_a_work_order(self):
        quote = self.make_quote()
        quote.stage = Quote.Stage.SENT_TO_OPS
        quote.save()
        self.assertEqual(quote.ops_status, Quote.OpsStatus.PENDING)
        self.assertIsNone(quote.ensure_install_work_order())
        self.assertEqual(WorkOrder.objects.count(), 0)

    def test_quote_numbers_follow_the_customer_not_the_site(self):
        second_site = Site.objects.create(customer=self.site.customer, address="2 Main St")
        first = Quote.objects.create(site=self.site)
        second = Quote.objects.create(site=second_site)
        self.assertEqual((first.quote_number, second.quote_number), (1, 2))
        self.assertEqual(second.number, f"{self.site.customer.number}-q02")


class QuoteWorkflowTests(TestCase):
    """The quote admin as each role sees it."""

    @classmethod
    def setUpTestData(cls):
        call_command("setup_roles")
        customer = Customer.objects.create(name="Test Co")
        cls.site = Site.objects.create(customer=customer, address="1 Main St")
        cls.switch = CatalogItem.objects.create(sku="SW", name="PoE Switch", unit_cost=100, list_price=180, min_price=150)

    def as_role(self, role):
        user = User.objects.create_user(username=role.replace(" ", "").lower(), password="pw", is_staff=True)
        user.groups.add(Group.objects.get(name=role))
        self.client.force_login(user)
        return user

    def quote(self, **fields):
        quote = Quote.objects.create(site=self.site, **fields)
        QuoteLine.objects.create(quote=quote, item=self.switch, quantity=2)
        return quote

    def url(self, quote):
        return reverse("admin:crm_quote_change", args=[quote.pk])

    def post(self, quote, **fields):
        data = {
            "site": quote.site_id, "stage": quote.stage, "salesperson": "", "notes": "",
            "deposit_percent": str(quote.deposit_percent),
            "lines-TOTAL_FORMS": "0", "lines-INITIAL_FORMS": "0", "lines-MIN_NUM_FORMS": "0", "lines-MAX_NUM_FORMS": "1000",
        }
        data.update(fields)
        return self.client.post(self.url(quote), data, follow=True)

    def sendable(self):
        return self.quote(agreement_signed=True, deposit_received=True, deliverables_verified=True)

    def test_sales_cannot_see_cost_or_margin_but_managers_can(self):
        quote = self.quote()
        self.as_role("Sales")
        page = self.client.get(self.url(quote)).content.decode()
        self.assertNotIn("Total cost", page)
        self.assertNotIn("Margin", page)
        self.assertIn("Minimum price", page)
        self.as_role("Sales Manager")
        page = self.client.get(self.url(quote)).content.decode()
        self.assertIn("Total cost", page)

    def test_sales_cannot_see_catalog_cost(self):
        self.as_role("Sales")
        page = self.client.get(reverse("admin:inventory_catalogitem_changelist")).content.decode()
        self.assertIn("List price", page)
        self.assertNotIn("Unit cost", page)

    def test_below_minimum_quote_is_bounced_back_to_new_lead(self):
        quote = self.quote()
        line = quote.lines.get()
        line.unit_price = 120
        line.save()
        self.as_role("Sales")
        response = self.post(quote, stage="quote_sent")
        self.assertContains(response, "Needs manager approval")
        quote.refresh_from_db()
        self.assertEqual(quote.stage, Quote.Stage.NEW_LEAD)

    def test_only_an_approver_can_approve_and_it_unlocks_the_stage(self):
        quote = self.quote()
        line = quote.lines.get()
        line.unit_price = 120
        line.save()
        self.as_role("Sales")
        self.assertNotIn('name="exceptions_approved"', self.client.get(self.url(quote)).content.decode())
        self.post(quote, stage="quote_sent", exceptions_approved="on")
        quote.refresh_from_db()
        self.assertEqual((quote.stage, quote.exceptions_approved), (Quote.Stage.NEW_LEAD, False))

        manager = self.as_role("Sales Manager")
        self.post(quote, stage="quote_sent", exceptions_approved="on")
        quote.refresh_from_db()
        self.assertEqual((quote.stage, quote.exceptions_approved_by), (Quote.Stage.QUOTE_SENT, manager))

    def test_send_to_ops_alerts_ops_and_locks_the_quote_for_sales(self):
        quote = self.sendable()
        self.as_role("Sales")
        self.post(quote, stage="sent_to_ops", agreement_signed="on", deposit_received="on", deliverables_verified="on")
        quote.refresh_from_db()
        self.assertEqual((quote.stage, quote.ops_status), (Quote.Stage.SENT_TO_OPS, Quote.OpsStatus.PENDING))
        self.assertEqual(WorkOrder.objects.count(), 0)
        self.assertEqual(
            set(Notification.objects.values_list("group__name", flat=True)), {"Install Manager", "Operations Manager"},
        )

        self.post(quote, stage="sent_to_ops", notes="tampered")
        quote.refresh_from_db()
        self.assertEqual(quote.notes, "")
        self.assertNotIn('name="notes"', self.client.get(self.url(quote)).content.decode())

    def test_changing_prices_after_approval_bounces_the_quote_again(self):
        quote = self.quote()
        line = quote.lines.get()
        line.unit_price = 120
        line.save()
        self.as_role("Sales Manager")
        self.post(quote, stage="quote_sent", exceptions_approved="on")
        quote.refresh_from_db()
        self.assertEqual(quote.stage, Quote.Stage.QUOTE_SENT)

        line.unit_price = 110
        line.save()
        self.as_role("Sales")
        response = self.post(quote, stage="quote_sent", notes="touched")
        quote.refresh_from_db()
        self.assertFalse(quote.exceptions_ok)
        self.assertEqual(quote.stage, Quote.Stage.NEW_LEAD)
        self.assertContains(response, "Needs manager approval")

    def test_sales_manager_is_locked_out_too(self):
        quote = self.sendable()
        quote.stage = Quote.Stage.SENT_TO_OPS
        quote.save()
        self.as_role("Sales Manager")
        self.post(quote, stage="sent_to_ops", notes="tampered")
        quote.refresh_from_db()
        self.assertEqual(quote.notes, "")

    def test_ops_accepting_creates_the_install_work_order_and_tells_sales(self):
        quote = self.sendable()
        quote.stage = Quote.Stage.SENT_TO_OPS
        quote.save()
        self.as_role("Install Manager")
        self.post(quote, stage="sent_to_ops", ops_status="accepted")
        work_order = WorkOrder.objects.get()
        self.assertEqual((work_order.wo_type, work_order.quote), ("install", quote))
        self.assertIn("2 x PoE Switch", work_order.description)
        self.assertTrue(Notification.objects.filter(group__name="Sales", message__contains=work_order.number).exists())

    def test_ops_returning_needs_a_reason_then_unlocks_for_sales_and_flags_it(self):
        quote = self.sendable()
        quote.stage = Quote.Stage.SENT_TO_OPS
        quote.save()
        self.as_role("Operations Manager")
        self.post(quote, stage="sent_to_ops", ops_status="refused")
        quote.refresh_from_db()
        self.assertEqual(quote.stage, Quote.Stage.SENT_TO_OPS)

        self.post(quote, stage="sent_to_ops", ops_status="refused", ops_note="Scope of work is unclear.")
        quote.refresh_from_db()
        self.assertEqual((quote.stage, quote.ops_status), (Quote.Stage.DEPOSIT, Quote.OpsStatus.REFUSED))
        self.assertEqual(WorkOrder.objects.count(), 0)
        self.assertTrue(Notification.objects.filter(group__name="Sales", message__contains="unclear").exists())

        self.as_role("Sales")
        self.assertIn('name="notes"', self.client.get(self.url(quote)).content.decode())
        self.post(
            quote, stage="sent_to_ops", agreement_signed="on", deposit_received="on", deliverables_verified="on",
        )
        quote.refresh_from_db()
        self.assertEqual((quote.stage, quote.ops_status), (Quote.Stage.SENT_TO_OPS, Quote.OpsStatus.PENDING))

    def test_sales_cannot_make_the_ops_decision(self):
        quote = self.sendable()
        quote.stage = Quote.Stage.SENT_TO_OPS
        quote.save()
        self.as_role("Sales")
        self.post(quote, stage="sent_to_ops", ops_status="accepted")
        self.assertEqual(WorkOrder.objects.count(), 0)

    def test_editing_after_send_by_an_override_user_alerts_ops(self):
        quote = self.sendable()
        quote.stage = Quote.Stage.SENT_TO_OPS
        quote.save()
        boss = User.objects.create_superuser("boss", "b@example.com", "pw")
        self.client.force_login(boss)
        self.post(quote, stage="sent_to_ops", notes="changed", agreement_signed="on", deposit_received="on", deliverables_verified="on")
        quote.refresh_from_db()
        self.assertEqual(quote.notes, "changed")
        self.assertTrue(Notification.objects.filter(message__contains="edited after being sent to Ops").exists())

    def test_alerts_are_visible_only_to_the_group_they_were_sent_to(self):
        from crm.models import notify

        notify(["Install Manager"], "for installers")
        self.as_role("Sales")
        page = self.client.get(reverse("admin:crm_notification_changelist")).content.decode()
        self.assertNotIn("for installers", page)
        self.as_role("Install Manager")
        page = self.client.get(reverse("admin:crm_notification_changelist")).content.decode()
        self.assertIn("for installers", page)
