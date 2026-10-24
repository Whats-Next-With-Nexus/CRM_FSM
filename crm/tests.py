from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from crm.models import Customer, Notification, Quote, QuoteLine, Site, SystemType, notify
from fsm.models import WorkOrder
from inventory.models import CatalogItem

User = get_user_model()
Stage = Quote.Stage


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
        quote = self.make_quote(switch=140)
        quote.stage = Stage.QUOTE_SENT
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
        quote = self.make_quote()
        quote.ops_status = Quote.OpsStatus.REFUSED
        with self.assertRaisesMessage(ValidationError, "what is missing"):
            quote.full_clean()
        quote.ops_status = Quote.OpsStatus.ACCEPTED
        with self.assertRaisesMessage(ValidationError, "sent to Ops"):
            quote.full_clean()

    def test_sending_marks_the_quote_pending_and_does_not_create_a_work_order(self):
        quote = self.make_quote()
        quote.stage = Stage.SENT_TO_OPS
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

    def test_sending_stamps_the_date_and_a_valid_until_date_once(self):
        quote = self.make_quote()
        self.assertIsNone(quote.sent_on)
        quote.stage = Stage.QUOTE_SENT
        quote.save()
        sent_on = quote.sent_on
        self.assertIsNotNone(sent_on)
        self.assertEqual(quote.valid_until, timezone.localdate() + timedelta(days=30))
        quote.notes = "touched"
        quote.save()
        self.assertEqual(quote.sent_on, sent_on)

    def test_a_quote_cannot_skip_being_sent(self):
        quote = self.make_quote()
        quote.stage = Stage.APPROVED
        with self.assertRaisesMessage(ValidationError, "Send the quote"):
            quote.full_clean()
        quote.stage = Stage.QUOTE_SENT
        quote.save()
        quote.stage = Stage.APPROVED
        quote.full_clean()

    def test_deposit_stage_needs_the_amount_recorded(self):
        quote = self.make_quote()
        quote.stage = Stage.QUOTE_SENT
        quote.save()
        quote.stage = Stage.DEPOSIT
        with self.assertRaisesMessage(ValidationError, "Record the deposit"):
            quote.full_clean()
        quote.deposit_received = True
        quote.deposit_received_amount = Decimal("630")
        quote.full_clean()

    def test_signing_needs_a_name_and_dates_fill_themselves(self):
        quote = self.make_quote()
        quote.agreement_signed = True
        with self.assertRaisesMessage(ValidationError, "who signed"):
            quote.full_clean()
        quote.signed_by = "Pat Morgan"
        quote.deposit_received = True
        quote.save()
        self.assertEqual(quote.agreement_signed_on, timezone.localdate())
        self.assertEqual(quote.deposit_received_on, timezone.localdate())

    def test_a_lost_quote_needs_a_reason(self):
        quote = self.make_quote()
        quote.stage = Stage.LOST
        with self.assertRaisesMessage(ValidationError, "reason"):
            quote.full_clean()
        quote.lost_reason = Quote.LostReason.PRICE
        quote.full_clean()

    def test_stage_problems_that_depend_on_the_lines(self):
        empty = Quote.objects.create(site=self.site, stage=Stage.QUOTE_SENT)
        message, fallback = empty.stage_problem()
        self.assertIn("at least one line item", message)
        self.assertEqual(fallback, Stage.NEW_LEAD)

        quote = self.make_quote()
        quote.stage = Stage.DEPOSIT
        quote.deposit_received_amount = Decimal("100")
        message, fallback = quote.stage_problem()
        self.assertIn("less than the deposit due", message)
        self.assertEqual(fallback, Stage.APPROVED)
        quote.deposit_received_amount = Decimal("630")
        self.assertIsNone(quote.stage_problem())


class CustomerStatusTests(TestCase):
    def setUp(self):
        self.customer = Customer.objects.create(name="Status Co")
        self.site = Site.objects.create(customer=self.customer, address="1 Main St")

    def status(self):
        self.customer.refresh_from_db()
        return self.customer.status

    def test_a_lead_becomes_a_customer_when_a_quote_is_approved(self):
        quote = Quote.objects.create(site=self.site, stage=Stage.QUOTE_SENT)
        self.assertEqual(self.status(), Customer.Status.LEAD)
        quote.stage = Stage.APPROVED
        quote.save()
        self.assertEqual(self.status(), Customer.Status.CUSTOMER)

    def test_a_customer_stays_a_customer_when_a_later_quote_is_lost(self):
        won = Quote.objects.create(site=self.site, stage=Stage.APPROVED)
        lost = Quote.objects.create(site=self.site, stage=Stage.LOST)
        self.assertEqual(self.status(), Customer.Status.CUSTOMER)
        self.assertEqual((won.quote_number, lost.quote_number), (1, 2))

    def test_a_lead_with_only_lost_quotes_is_lost_and_a_new_quote_revives_it(self):
        Quote.objects.create(site=self.site, stage=Stage.LOST)
        self.assertEqual(self.status(), Customer.Status.LOST)
        Quote.objects.create(site=self.site)
        self.assertEqual(self.status(), Customer.Status.LEAD)

    def test_inactive_customers_are_left_alone(self):
        self.customer.status = Customer.Status.INACTIVE
        self.customer.save()
        Quote.objects.create(site=self.site, stage=Stage.APPROVED)
        self.assertEqual(self.status(), Customer.Status.INACTIVE)


class QuoteWorkflowTests(TestCase):
    """The quote screens as each role sees them."""

    @classmethod
    def setUpTestData(cls):
        call_command("setup_roles")
        customer = Customer.objects.create(name="Test Co", contact_name="Pat Morgan")
        cls.site = Site.objects.create(customer=customer, address="1 Main St")
        cls.switch = CatalogItem.objects.create(sku="SW", name="PoE Switch", unit_cost=100, list_price=180, min_price=150)
        cls.cctv = SystemType.objects.create(name="Test CCTV")

    def as_role(self, role):
        user = User.objects.create_user(username=role.replace(" ", "").lower(), password="pw", is_staff=True)
        user.groups.add(Group.objects.get(name=role))
        self.client.force_login(user)
        return user

    def quote(self, with_lines=True, **fields):
        quote = Quote.objects.create(site=self.site, **fields)
        if with_lines:
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

    def checklist(self, quote):
        return {
            "agreement_signed": "on", "signed_by": "Pat Morgan", "deposit_received": "on",
            "deposit_received_amount": str(quote.deposit_due), "deliverables_verified": "on",
        }

    def ready_quote(self):
        """A quote sitting at Deposit Obtained with everything Ops needs, one step from Sent to Ops."""
        quote = self.quote(stage=Stage.QUOTE_SENT)
        quote.stage = Stage.DEPOSIT
        quote.agreement_signed, quote.signed_by = True, "Pat Morgan"
        quote.deposit_received, quote.deposit_received_amount = True, quote.deposit_due
        quote.deliverables_verified = True
        quote.save()
        return quote

    def sent_quote(self):
        quote = self.ready_quote()
        quote.stage = Stage.SENT_TO_OPS
        quote.save()
        return quote

    # pricing visibility and exceptions

    def test_sales_cannot_see_cost_or_margin_but_managers_can(self):
        quote = self.quote()
        self.as_role("Sales")
        page = self.client.get(self.url(quote)).content.decode()
        self.assertNotIn("Total cost", page)
        self.assertNotIn("Margin", page)
        self.assertIn("Minimum price", page)
        self.as_role("Sales Manager")
        self.assertIn("Total cost", self.client.get(self.url(quote)).content.decode())

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
        self.assertEqual(quote.stage, Stage.NEW_LEAD)

    def test_only_an_approver_can_approve_and_it_unlocks_the_stage(self):
        quote = self.quote()
        line = quote.lines.get()
        line.unit_price = 120
        line.save()
        self.as_role("Sales")
        self.assertNotIn('name="exceptions_approved"', self.client.get(self.url(quote)).content.decode())
        self.post(quote, stage="quote_sent", exceptions_approved="on")
        quote.refresh_from_db()
        self.assertEqual((quote.stage, quote.exceptions_approved), (Stage.NEW_LEAD, False))

        manager = self.as_role("Sales Manager")
        self.post(quote, stage="quote_sent", exceptions_approved="on")
        quote.refresh_from_db()
        self.assertEqual((quote.stage, quote.exceptions_approved_by), (Stage.QUOTE_SENT, manager))

    def test_changing_prices_after_approval_bounces_the_quote_again(self):
        quote = self.quote()
        line = quote.lines.get()
        line.unit_price = 120
        line.save()
        self.as_role("Sales Manager")
        self.post(quote, stage="quote_sent", exceptions_approved="on")
        quote.refresh_from_db()
        self.assertEqual(quote.stage, Stage.QUOTE_SENT)

        line.unit_price = 110
        line.save()
        self.as_role("Sales")
        response = self.post(quote, stage="quote_sent", notes="touched")
        quote.refresh_from_db()
        self.assertFalse(quote.exceptions_ok)
        self.assertEqual(quote.stage, Stage.NEW_LEAD)
        self.assertIsNone(quote.sent_on)
        self.assertContains(response, "Needs manager approval")

    # sending, deposit and the stage rules

    def test_sending_needs_line_items(self):
        quote = self.quote(with_lines=False)
        self.as_role("Sales")
        response = self.post(quote, stage="quote_sent")
        self.assertContains(response, "at least one line item")
        quote.refresh_from_db()
        self.assertEqual(quote.stage, Stage.NEW_LEAD)

    def test_sending_a_quote_stamps_it_and_moves_the_customer_forward_only_once_approved(self):
        quote = self.quote()
        self.as_role("Sales")
        self.post(quote, stage="quote_sent")
        quote.refresh_from_db()
        self.assertEqual(quote.stage, Stage.QUOTE_SENT)
        self.assertIsNotNone(quote.sent_on)
        self.assertEqual(quote.customer.status, Customer.Status.LEAD)
        self.post(quote, stage="approved")
        quote.customer.refresh_from_db()
        self.assertEqual(quote.customer.status, Customer.Status.CUSTOMER)

    def test_the_form_refuses_to_skip_stages(self):
        quote = self.quote()
        self.as_role("Sales")
        response = self.post(quote, stage="sent_to_ops", **self.checklist(quote))
        self.assertContains(response, "Send the quote")
        quote.refresh_from_db()
        self.assertEqual(quote.stage, Stage.NEW_LEAD)

    def test_a_short_deposit_bounces_back_to_approved(self):
        quote = self.quote(stage=Stage.APPROVED, sent_on=timezone.now())
        self.as_role("Sales")
        self.post(quote, stage="deposit", deposit_received="on", deposit_received_amount="10")
        quote.refresh_from_db()
        self.assertEqual(quote.stage, Stage.APPROVED)

    def test_a_lost_quote_records_why(self):
        quote = self.quote()
        self.as_role("Sales")
        response = self.post(quote, stage="lost")
        self.assertContains(response, "reason")
        self.post(quote, stage="lost", lost_reason="price")
        quote.refresh_from_db()
        self.assertEqual((quote.stage, quote.lost_reason), (Stage.LOST, "price"))
        quote.customer.refresh_from_db()
        self.assertEqual(quote.customer.status, Customer.Status.LOST)

    def test_follow_up_filter_finds_overdue_quotes(self):
        overdue = self.quote(follow_up_on=timezone.localdate() - timedelta(days=2))
        later = self.quote(follow_up_on=timezone.localdate() + timedelta(days=9))
        self.as_role("Sales")
        page = self.client.get(reverse("admin:crm_quote_changelist"), {"follow_up": "overdue"}).content.decode()
        self.assertIn(overdue.number, page)
        self.assertNotIn(later.number, page)

    # sending to Ops and the lock

    def test_send_to_ops_alerts_ops_and_locks_the_quote_for_sales(self):
        quote = self.ready_quote()
        self.as_role("Sales")
        self.post(quote, stage="sent_to_ops", **self.checklist(quote))
        quote.refresh_from_db()
        self.assertEqual((quote.stage, quote.ops_status), (Stage.SENT_TO_OPS, Quote.OpsStatus.PENDING))
        self.assertEqual(WorkOrder.objects.count(), 0)
        self.assertEqual(
            set(Notification.objects.values_list("group__name", flat=True)), {"Install Manager", "Operations Manager"},
        )

        self.post(quote, stage="sent_to_ops", notes="tampered")
        quote.refresh_from_db()
        self.assertEqual(quote.notes, "")
        self.assertNotIn('name="notes"', self.client.get(self.url(quote)).content.decode())

    def test_sales_manager_is_locked_out_too(self):
        quote = self.sent_quote()
        self.as_role("Sales Manager")
        self.post(quote, stage="sent_to_ops", notes="tampered")
        quote.refresh_from_db()
        self.assertEqual(quote.notes, "")

    def test_ops_accepting_creates_the_install_work_order_and_tells_sales(self):
        quote = self.sent_quote()
        self.as_role("Install Manager")
        self.post(quote, stage="sent_to_ops", ops_status="accepted")
        work_order = WorkOrder.objects.get()
        self.assertEqual((work_order.wo_type, work_order.quote), ("install", quote))
        self.assertIn("2 x PoE Switch", work_order.description)
        self.assertTrue(Notification.objects.filter(group__name="Sales", message__contains=work_order.number).exists())

    def test_ops_returning_needs_a_reason_then_unlocks_for_sales_and_flags_it(self):
        quote = self.sent_quote()
        self.as_role("Operations Manager")
        self.post(quote, stage="sent_to_ops", ops_status="refused")
        quote.refresh_from_db()
        self.assertEqual(quote.stage, Stage.SENT_TO_OPS)

        self.post(quote, stage="sent_to_ops", ops_status="refused", ops_note="Scope of work is unclear.")
        quote.refresh_from_db()
        self.assertEqual((quote.stage, quote.ops_status), (Stage.DEPOSIT, Quote.OpsStatus.REFUSED))
        self.assertEqual(WorkOrder.objects.count(), 0)
        self.assertTrue(Notification.objects.filter(group__name="Sales", message__contains="unclear").exists())

        self.as_role("Sales")
        self.assertIn('name="notes"', self.client.get(self.url(quote)).content.decode())
        self.post(quote, stage="sent_to_ops", **self.checklist(quote))
        quote.refresh_from_db()
        self.assertEqual((quote.stage, quote.ops_status), (Stage.SENT_TO_OPS, Quote.OpsStatus.PENDING))

    def test_sales_cannot_make_the_ops_decision(self):
        quote = self.sent_quote()
        self.as_role("Sales")
        self.post(quote, stage="sent_to_ops", ops_status="accepted")
        self.assertEqual(WorkOrder.objects.count(), 0)

    def test_editing_after_send_by_an_override_user_alerts_ops(self):
        quote = self.sent_quote()
        boss = User.objects.create_superuser("boss", "b@example.com", "pw")
        self.client.force_login(boss)
        self.post(quote, stage="sent_to_ops", notes="changed", **self.checklist(quote))
        quote.refresh_from_db()
        self.assertEqual(quote.notes, "changed")
        self.assertTrue(Notification.objects.filter(message__contains="edited after being sent to Ops").exists())

    def test_alerts_are_visible_only_to_the_group_they_were_sent_to(self):
        notify(["Install Manager"], "for installers")
        self.as_role("Sales")
        page = self.client.get(reverse("admin:crm_notification_changelist")).content.decode()
        self.assertNotIn("for installers", page)
        self.as_role("Install Manager")
        page = self.client.get(reverse("admin:crm_notification_changelist")).content.decode()
        self.assertIn("for installers", page)

    # the new-lead screen

    def lead_data(self, **overrides):
        data = {
            "customer_name": "Fresh Lead LLC", "contact_name": "Sam Lee", "phone": "555-0100", "email": "sam@example.com",
            "lead_source": "referral", "site_name": "HQ", "address": "9 Oak Ave", "city": "Carmel", "state": "in",
            "zip_code": "46032", "system_types": [self.cctv.pk], "salesperson": "Dakota", "follow_up_on": "",
        }
        data.update(overrides)
        return data

    def test_new_lead_creates_customer_site_and_draft_quote_in_one_step(self):
        self.as_role("Sales")
        response = self.client.post(reverse("admin:crm_quote_new_lead"), self.lead_data())
        quote = Quote.objects.get(site__address="9 Oak Ave")
        self.assertRedirects(response, self.url(quote))
        customer = quote.customer
        self.assertEqual((customer.status, customer.lead_source, customer.name), (Customer.Status.LEAD, "referral", "Fresh Lead LLC"))
        self.assertEqual((quote.stage, quote.site.state, quote.salesperson), (Stage.NEW_LEAD, "IN", "Dakota"))
        self.assertEqual(list(quote.system_types.all()), [self.cctv])

    def test_new_lead_needs_a_service_and_blocks_a_duplicate_site(self):
        self.as_role("Sales")
        response = self.client.post(reverse("admin:crm_quote_new_lead"), self.lead_data(system_types=[]))
        self.assertContains(response, "Pick at least one service")
        self.assertFalse(Customer.objects.filter(name="Fresh Lead LLC").exists())

        response = self.client.post(
            reverse("admin:crm_quote_new_lead"), self.lead_data(customer_name="test co", address="1 main st"),
        )
        self.assertContains(response, "already has a site")

    def test_new_lead_screen_is_for_people_who_can_add_customers(self):
        self.as_role("Technician")
        self.assertEqual(self.client.get(reverse("admin:crm_quote_new_lead")).status_code, 403)
        self.as_role("Sales")
        self.assertEqual(self.client.get(reverse("admin:crm_quote_new_lead")).status_code, 200)
        page = self.client.get(reverse("admin:crm_quote_changelist")).content.decode()
        self.assertIn(reverse("admin:crm_quote_new_lead"), page)

    # the printable quote

    def test_printable_quote_shows_prices_but_never_cost_or_minimums(self):
        quote = self.quote(stage=Stage.QUOTE_SENT)
        self.as_role("Sales")
        page = self.client.get(reverse("admin:crm_quote_document", args=[quote.pk])).content.decode()
        self.assertIn(quote.number, page)
        self.assertIn("$360.00", page)
        self.assertIn("$180.00", page)
        self.assertNotIn("$100.00", page)
        self.assertNotIn("$150.00", page)
        self.assertNotIn("DRAFT", page)
        self.assertIn("Valid until", page)

    def test_printable_quote_is_marked_draft_until_sent_and_hidden_from_technicians(self):
        quote = self.quote()
        self.as_role("Sales")
        page = self.client.get(reverse("admin:crm_quote_document", args=[quote.pk])).content.decode()
        self.assertIn("DRAFT", page)
        self.assertIn("Not yet sent", page)
        self.as_role("Technician")
        self.assertEqual(self.client.get(reverse("admin:crm_quote_document", args=[quote.pk])).status_code, 403)
