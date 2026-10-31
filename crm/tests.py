import re
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from crm.models import (
    ChangeOrder, ChangeOrderLine, Contractor, Customer, Notification, Quote, QuoteLine, RepairPlan,
    RepairPlanEnrollment, Site, System, SystemType, notify,
)
from fsm.models import Skill, TechProfile, Visit, VisitAssignment, WorkOrder
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
        cls.rep = User.objects.create_user("rep1", password="pw", is_staff=True)

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
            "revisions-TOTAL_FORMS": "0", "revisions-INITIAL_FORMS": "0", "revisions-MIN_NUM_FORMS": "0",
            "revisions-MAX_NUM_FORMS": "1000",
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
            "zip_code": "46032", "system_types": [self.cctv.pk], "salesperson": self.rep.pk, "follow_up_on": "",
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
        self.assertEqual((quote.stage, quote.site.state, quote.salesperson), (Stage.NEW_LEAD, "IN", self.rep))
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
        self.assertIn(reverse("admin:crm_quote_new_lead_action"), page)

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


class RevisionAndChangeOrderTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("setup_roles")
        customer = Customer.objects.create(name="Test Co")
        cls.site = Site.objects.create(customer=customer, address="1 Main St")
        cls.switch16 = CatalogItem.objects.create(sku="SW16", name="16-Port PoE Switch", unit_cost=200, list_price=400, min_price=320)
        cls.switch8 = CatalogItem.objects.create(sku="SW8", name="8-Port PoE Switch", unit_cost=100, list_price=220, min_price=180)
        cls.cabling = Skill.objects.create(name="Cabling")

    def as_role(self, role):
        user = User.objects.create_user(username=role.replace(" ", "").lower(), password="pw", is_staff=True)
        user.groups.add(Group.objects.get(name=role))
        self.client.force_login(user)
        return user

    def sent_quote(self):
        """A quote that went all the way to Ops and was accepted, so an Install work order exists."""
        quote = Quote.objects.create(site=self.site, stage=Stage.QUOTE_SENT)
        self.line16 = QuoteLine.objects.create(quote=quote, item=self.switch16, quantity=1)
        quote.stage = Stage.SENT_TO_OPS
        quote.agreement_signed, quote.signed_by = True, "Pat Morgan"
        quote.deposit_received, quote.deposit_received_amount = True, quote.deposit_due
        quote.deliverables_verified = True
        quote.ops_status = Quote.OpsStatus.ACCEPTED
        quote.save()
        quote.ensure_install_work_order()
        return quote

    def begin_install(self, quote):
        work_order = quote.work_orders.get()
        tech = User.objects.create_user("tech1", password="pw", is_staff=True)
        TechProfile.objects.create(user=tech).skills.add(self.cabling)
        assignment = VisitAssignment.objects.create(
            visit=Visit.objects.create(work_order=work_order), tech=tech, skill=self.cabling,
        )
        assignment.start()
        return work_order

    # revisions

    def test_revising_keeps_the_old_version_and_reopens_the_quote(self):
        quote = self.sent_quote()
        user = User.objects.create_user("rev", password="pw")
        quote.revise(user, "Customer wants a bigger switch")
        quote.refresh_from_db()
        old = quote.revisions.get()
        self.assertEqual((old.revision, old.reason, old.created_by), (1, "Customer wants a bigger switch", user))
        self.assertEqual(old.snapshot["total"], "400.00")
        self.assertEqual(old.snapshot["lines"][0]["description"], "16-Port PoE Switch")
        self.assertEqual((quote.revision, quote.stage), (2, Stage.NEW_LEAD))
        self.assertIsNone(quote.sent_on)
        self.assertEqual((quote.agreement_signed, quote.signed_by, quote.ops_status), (False, "", ""))
        self.assertTrue(quote.deposit_received)

    def test_a_revised_quote_needs_a_fresh_signature_to_reach_ops_again(self):
        quote = self.sent_quote()
        quote.revise(User.objects.create_user("rev", password="pw"))
        quote.refresh_from_db()
        quote.stage = Stage.SENT_TO_OPS
        with self.assertRaisesMessage(ValidationError, "Send the quote"):
            quote.full_clean()
        quote.stage = Stage.QUOTE_SENT
        quote.save()
        self.assertIsNotNone(quote.sent_on)
        quote.stage = Stage.SENT_TO_OPS
        with self.assertRaises(ValidationError) as caught:
            quote.full_clean()
        self.assertIn("agreement signed", str(caught.exception))

    def test_revising_is_blocked_once_install_begins(self):
        quote = self.sent_quote()
        self.begin_install(quote)
        with self.assertRaisesMessage(ValidationError, "change order"):
            quote.revise(User.objects.create_user("rev", password="pw"))
        self.assertEqual(quote.revisions.count(), 0)

    def test_only_a_sent_quote_can_be_revised(self):
        draft = Quote.objects.create(site=self.site)
        with self.assertRaisesMessage(ValidationError, "has been sent"):
            draft.revise(User.objects.create_user("rev", password="pw"))

    def test_reaccepting_a_revision_refreshes_the_existing_install_work_order(self):
        quote = self.sent_quote()
        work_order = quote.work_orders.get()
        quote.revise(User.objects.create_user("rev", password="pw"))
        quote.refresh_from_db()
        self.line16.quantity = 2
        self.line16.save()
        quote.ops_status = Quote.OpsStatus.ACCEPTED
        self.assertEqual(quote.ensure_install_work_order(), work_order)
        work_order.refresh_from_db()
        self.assertIn("revision 2", work_order.description)
        self.assertIn("2 x 16-Port PoE Switch", work_order.description)
        self.assertEqual(quote.work_orders.count(), 1)

    def test_revise_screen_alerts_ops_and_is_for_people_with_the_permission(self):
        quote = self.sent_quote()
        url = reverse("admin:crm_quote_revise", args=[quote.pk])
        self.as_role("Technician")
        self.assertEqual(self.client.get(url).status_code, 403)

        self.as_role("Sales")
        self.assertEqual(self.client.get(url).status_code, 200)
        response = self.client.post(url, {"reason": "Add a second camera"}, follow=True)
        self.assertContains(response, "now revision 2")
        quote.refresh_from_db()
        self.assertEqual(quote.revision, 2)
        self.assertTrue(
            Notification.objects.filter(group__name="Install Manager", message__contains="revised to revision 2").exists(),
        )

    def test_revise_screen_points_to_change_orders_after_install_begins(self):
        quote = self.sent_quote()
        self.begin_install(quote)
        self.as_role("Sales")
        response = self.client.get(reverse("admin:crm_quote_revise", args=[quote.pk]), follow=True)
        self.assertContains(response, "need a change order")

    def test_old_revisions_print_as_superseded_with_their_original_numbers(self):
        quote = self.sent_quote()
        quote.revise(User.objects.create_user("rev", password="pw"))
        quote.refresh_from_db()
        self.line16.unit_price = 450
        self.line16.save()
        self.as_role("Sales")
        old = self.client.get(reverse("admin:crm_quote_document", args=[quote.pk]), {"rev": 1}).content.decode()
        self.assertIn("SUPERSEDED", old)
        self.assertIn("$400.00", old)
        self.assertNotIn("$450.00", old)
        current = self.client.get(reverse("admin:crm_quote_document", args=[quote.pk])).content.decode()
        self.assertIn("$450.00", current)
        self.assertIn("rev 2", current)
        self.assertNotIn("SUPERSEDED", current)

    # change orders

    def make_co(self, quote, **fields):
        return ChangeOrder.objects.create(quote=quote, reason="Floor plan changed", **fields)

    def swap_lines(self, co, add_price=None):
        ChangeOrderLine.objects.create(change_order=co, direction="remove", quote_line=self.line16, quantity=1)
        ChangeOrderLine.objects.create(change_order=co, direction="add", item=self.switch8, quantity=2, unit_price=add_price)

    def test_a_change_order_is_refused_until_install_begins(self):
        quote = self.sent_quote()
        co = ChangeOrder(quote=quote, reason="Early change")
        with self.assertRaisesMessage(ValidationError, "Revise the quote"):
            co.full_clean()
        self.begin_install(quote)
        ChangeOrder(quote=quote, reason="On-site change").full_clean()

    def test_numbers_net_price_and_margin_for_a_swap(self):
        quote = self.sent_quote()
        self.begin_install(quote)
        co = self.make_co(quote)
        self.swap_lines(co)
        self.assertEqual(co.number, f"{quote.number}-CO1")
        self.assertEqual(self.make_co(quote).co_number, 2)
        self.assertEqual(co.net_price, Decimal("40.00"))
        self.assertEqual(co.net_cost, Decimal("0.00"))
        self.assertEqual(co.margin_change, Decimal("40.00"))

    def test_returned_lines_default_to_the_original_price_and_cost(self):
        quote = self.sent_quote()
        self.begin_install(quote)
        co = self.make_co(quote)
        self.swap_lines(co)
        removed = co.lines.get(direction="remove")
        self.assertEqual((removed.unit_price, removed.unit_cost, removed.amount), (400, 200, Decimal("-400.00")))

    def test_a_change_order_must_remove_something(self):
        quote = self.sent_quote()
        self.begin_install(quote)
        co = self.make_co(quote)
        ChangeOrderLine.objects.create(change_order=co, direction="add", item=self.switch8, quantity=2)
        co.status = ChangeOrder.Status.PRICED
        self.assertIn("new quote", co.stage_problem())
        self.swap_lines(co)
        self.assertIsNone(co.stage_problem())

    def test_cannot_return_more_than_was_quoted(self):
        quote = self.sent_quote()
        self.begin_install(quote)
        co = self.make_co(quote, status=ChangeOrder.Status.PRICED)
        ChangeOrderLine.objects.create(change_order=co, direction="remove", quote_line=self.line16, quantity=3)
        self.assertIn("Can't return", co.stage_problem())

    def test_added_items_below_minimum_need_approval(self):
        quote = self.sent_quote()
        self.begin_install(quote)
        co = self.make_co(quote, status=ChangeOrder.Status.PRICED)
        self.swap_lines(co, add_price=150)
        self.assertIn("manager approval", co.stage_problem())
        co.exceptions_approved = True
        co.exceptions_signature = co.current_signature()
        self.assertIsNone(co.stage_problem())

    def test_signing_needs_a_name_applies_once_and_changes_the_contract_value(self):
        quote = self.sent_quote()
        work_order = self.begin_install(quote)
        co = self.make_co(quote)
        self.swap_lines(co)
        co.status = ChangeOrder.Status.SIGNED
        with self.assertRaisesMessage(ValidationError, "who signed"):
            co.full_clean()
        co.signed_by = "Pat Morgan"
        co.save()
        self.assertEqual(co.signed_on, timezone.localdate())
        co.apply_to_work_order()
        co.apply_to_work_order()
        work_order.refresh_from_db()
        self.assertEqual(work_order.description.count(co.number), 1)
        self.assertIn("returned 1 x 16-Port PoE Switch; added 2 x 8-Port PoE Switch", work_order.description)
        self.assertEqual(quote.current_value, Decimal("440.00"))

    def test_absorbed_and_unsigned_change_orders_do_not_change_the_contract_value(self):
        quote = self.sent_quote()
        self.begin_install(quote)
        absorbed = self.make_co(quote, status=ChangeOrder.Status.SIGNED, signed_by="Pat", billing=ChangeOrder.Billing.ABSORB)
        self.swap_lines(absorbed)
        pending = self.make_co(quote)
        self.swap_lines(pending)
        self.assertEqual(quote.current_value, Decimal("400.00"))

    def test_change_order_screens_follow_the_roles(self):
        quote = self.sent_quote()
        self.begin_install(quote)
        add_url = reverse("admin:crm_changeorder_add")
        self.as_role("Sales")
        self.assertEqual(self.client.get(add_url).status_code, 403)
        self.as_role("Technician")
        self.assertEqual(self.client.get(add_url).status_code, 403)
        self.as_role("Install Manager")
        self.assertEqual(self.client.get(add_url, {"quote": quote.pk}).status_code, 200)

    def test_requesting_a_change_order_alerts_sales(self):
        quote = self.sent_quote()
        self.begin_install(quote)
        self.as_role("Install Manager")
        response = self.client.post(reverse("admin:crm_changeorder_add"), {
            "quote": quote.pk, "status": "requested", "reason": "Accessibility issue", "billing": "invoice",
            "lines-TOTAL_FORMS": "1", "lines-INITIAL_FORMS": "0", "lines-MIN_NUM_FORMS": "0", "lines-MAX_NUM_FORMS": "1000",
            "lines-0-direction": "remove", "lines-0-quote_line": self.line16.pk, "lines-0-quantity": "1",
        }, follow=True)
        co = ChangeOrder.objects.get()
        self.assertContains(response, "was added successfully")
        self.assertEqual(co.requested_by.username, "installmanager")
        self.assertEqual(co.lines.count(), 1)
        self.assertTrue(Notification.objects.filter(group__name="Sales", message__contains=co.number).exists())

    def test_a_change_order_before_install_cannot_be_requested_through_the_form(self):
        quote = self.sent_quote()
        self.as_role("Install Manager")
        response = self.client.post(reverse("admin:crm_changeorder_add"), {
            "quote": quote.pk, "status": "requested", "reason": "Too early", "billing": "invoice",
            "lines-TOTAL_FORMS": "0", "lines-INITIAL_FORMS": "0", "lines-MIN_NUM_FORMS": "0", "lines-MAX_NUM_FORMS": "1000",
        })
        self.assertContains(response, "Revise the quote")
        self.assertEqual(ChangeOrder.objects.count(), 0)

    def co_post(self, co, **fields):
        data = {
            "status": co.status, "reason": co.reason, "billing": co.billing,
            "lines-TOTAL_FORMS": "0", "lines-INITIAL_FORMS": "0", "lines-MIN_NUM_FORMS": "0", "lines-MAX_NUM_FORMS": "1000",
        }
        data.update(fields)
        return self.client.post(reverse("admin:crm_changeorder_change", args=[co.pk]), data, follow=True)

    def test_marking_a_removal_free_change_order_ready_bounces_it_back(self):
        quote = self.sent_quote()
        self.begin_install(quote)
        co = self.make_co(quote)
        ChangeOrderLine.objects.create(change_order=co, direction="add", item=self.switch8, quantity=2)
        self.as_role("Sales")
        response = self.co_post(co, status="priced")
        self.assertContains(response, "new quote")
        co.refresh_from_db()
        self.assertEqual(co.status, ChangeOrder.Status.REQUESTED)

    def test_signing_through_the_screen_updates_the_work_order_and_alerts_ops(self):
        quote = self.sent_quote()
        work_order = self.begin_install(quote)
        co = self.make_co(quote, status=ChangeOrder.Status.PRICED)
        self.swap_lines(co)
        self.as_role("Sales")
        self.co_post(co, status="signed", signed_by="Pat Morgan")
        co.refresh_from_db()
        self.assertEqual((co.status, co.signed_by), (ChangeOrder.Status.SIGNED, "Pat Morgan"))
        work_order.refresh_from_db()
        self.assertIn(co.number, work_order.description)
        self.assertTrue(Notification.objects.filter(group__name="Install Manager", message__contains="signed").exists())

    def test_a_declined_change_order_alerts_ops_and_leaves_the_scope_alone(self):
        quote = self.sent_quote()
        work_order = self.begin_install(quote)
        before = work_order.description
        co = self.make_co(quote, status=ChangeOrder.Status.PRICED)
        self.swap_lines(co)
        self.as_role("Sales")
        self.co_post(co, status="declined")
        work_order.refresh_from_db()
        self.assertEqual(work_order.description, before)
        self.assertTrue(Notification.objects.filter(message__contains="declined").exists())

    def test_a_signed_change_order_is_locked(self):
        quote = self.sent_quote()
        self.begin_install(quote)
        co = self.make_co(quote, status=ChangeOrder.Status.SIGNED, signed_by="Pat")
        self.swap_lines(co)
        self.as_role("Sales")
        self.co_post(co, status="signed", reason="tampered", signed_by="Someone Else")
        co.refresh_from_db()
        self.assertEqual((co.reason, co.signed_by), ("Floor plan changed", "Pat"))
        page = self.client.get(reverse("admin:crm_changeorder_change", args=[co.pk])).content.decode()
        self.assertNotIn('name="reason"', page)

    def test_only_managers_can_set_the_restocking_fee(self):
        quote = self.sent_quote()
        self.begin_install(quote)
        co = self.make_co(quote)
        self.swap_lines(co)
        url = reverse("admin:crm_changeorder_change", args=[co.pk])
        self.as_role("Sales")
        self.assertNotIn('name="lines-0-restocking_percent"', self.client.get(url).content.decode())
        self.as_role("Install Manager")
        self.assertIn('name="lines-0-restocking_percent"', self.client.get(url).content.decode())

    def test_sales_cannot_see_cost_or_margin_on_a_change_order(self):
        quote = self.sent_quote()
        self.begin_install(quote)
        co = self.make_co(quote)
        self.swap_lines(co)
        url = reverse("admin:crm_changeorder_change", args=[co.pk])
        self.as_role("Sales")
        self.assertNotIn("Margin change", self.client.get(url).content.decode())
        self.as_role("Install Manager")
        self.assertIn("Margin change", self.client.get(url).content.decode())

    def test_the_customer_copy_shows_prices_the_restocking_notice_and_no_costs(self):
        quote = self.sent_quote()
        self.begin_install(quote)
        co = self.make_co(quote, status=ChangeOrder.Status.PRICED)
        self.swap_lines(co)
        self.as_role("Sales")
        page = self.client.get(reverse("admin:crm_changeorder_document", args=[co.pk])).content.decode()
        self.assertIn(co.number, page)
        self.assertIn("16-Port PoE Switch", page)
        self.assertIn("8-Port PoE Switch", page)
        self.assertIn("to be confirmed", page)
        self.assertIn("restocking fee", page)
        self.assertIn("No additional deposit", page)
        self.assertNotIn("$200.00", page)
        self.assertNotIn("$100.00", page)
        self.assertNotIn("DRAFT", page)

    def test_a_returned_line_shows_its_restocking_percent_once_set(self):
        quote = self.sent_quote()
        self.begin_install(quote)
        co = self.make_co(quote, status=ChangeOrder.Status.PRICED)
        self.swap_lines(co)
        line = co.lines.get(direction="remove")
        line.restocking_percent = Decimal("15")
        line.save()
        self.as_role("Sales")
        page = self.client.get(reverse("admin:crm_changeorder_document", args=[co.pk])).content.decode()
        self.assertIn("15%", page)

    def test_the_quote_page_offers_a_change_order_only_after_install_begins(self):
        quote = self.sent_quote()
        self.as_role("Install Manager")
        url = reverse("admin:crm_quote_change", args=[quote.pk])
        before = self.client.get(url).content.decode()
        self.assertNotIn("Add a change order", before)
        self.assertIn("Revise this quote", before)
        self.begin_install(quote)
        after = self.client.get(url).content.decode()
        self.assertIn("Add a change order", after)
        self.assertNotIn("Revise this quote", after)


class CustomerOverviewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("setup_roles")
        cls.prime = Contractor.objects.create(name="Northgate")
        cls.customer = Customer.objects.create(
            name="Overview Co", contracted_through=cls.prime, contact_name="Pat Morgan", phone="555-0100",
            lead_source=Customer.LeadSource.REFERRAL, notes="Prefers morning visits.",
        )
        cls.east = Site.objects.create(customer=cls.customer, name="East", address="1 Main St", access_notes="Gate code at front desk")
        cls.west = Site.objects.create(customer=cls.customer, name="West", address="2 Oak Ave")
        cctv = SystemType.objects.create(name="Overview CCTV")
        cls.system = System.objects.create(site=cls.east, system_type=cctv, bill_direct=True, equipment_notes="8 domes, 1 NVR")
        plan = RepairPlan.objects.create(name="Gold", monthly_rate=20)
        RepairPlanEnrollment.objects.create(site=cls.east, plan=plan, start_date=timezone.localdate())

        cls.switch = CatalogItem.objects.create(sku="SW", name="Switch", unit_cost=100, list_price=400, min_price=320)
        cls.won = Quote.objects.create(site=cls.east, stage=Stage.APPROVED, sent_on=timezone.now())
        QuoteLine.objects.create(quote=cls.won, item=cls.switch, quantity=2)
        change = ChangeOrder.objects.create(quote=cls.won, reason="Add one", status="signed", signed_by="Pat")
        ChangeOrderLine.objects.create(change_order=change, direction="add", item=cls.switch, quantity=1, unit_price=220)
        cls.open_quote = Quote.objects.create(
            site=cls.west, stage=Stage.QUOTE_SENT, follow_up_on=timezone.localdate() - timedelta(days=2),
        )
        QuoteLine.objects.create(quote=cls.open_quote, item=cls.switch, quantity=1)
        cls.returned = Quote.objects.create(
            site=cls.west, stage=Stage.DEPOSIT, ops_status=Quote.OpsStatus.REFUSED, ops_note="Scope unclear", sent_on=timezone.now(),
        )
        QuoteLine.objects.create(quote=cls.returned, item=cls.switch, quantity=1)
        cls.waiting_co = ChangeOrder.objects.create(quote=cls.won, reason="Swap", status="priced")

        tech = User.objects.create_user("leadtech", password="pw")
        cls.install = WorkOrder.objects.create(
            wo_type="install", site=cls.east, quote=cls.won, status=WorkOrder.Status.IN_PROGRESS, lead_tech=tech,
        )
        WorkOrder.objects.create(wo_type="service", site=cls.east, status=WorkOrder.Status.COMPLETE)

    def as_role(self, role):
        user = User.objects.create_user(username=role.replace(" ", "").lower(), password="pw", is_staff=True)
        user.groups.add(Group.objects.get(name=role))
        self.client.force_login(user)

    def page(self, role):
        self.as_role(role)
        response = self.client.get(reverse("admin:crm_customer_overview", args=[self.customer.pk]))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_the_page_brings_the_customer_together(self):
        page = self.page("Sales")
        for expected in (
            "Overview Co", self.customer.number, "Pat Morgan", "555-0100", "Referral", "Prefers morning visits.",
            self.east.number, self.west.number, "Gate code at front desk", "1 Main St", "2 Oak Ave",
            self.won.number, self.open_quote.number, self.returned.number, self.install.number, "Overview CCTV",
            "8 domes, 1 NVR", "Repair plan: Gold", "No repair plan",
        ):
            self.assertIn(expected, page)

    def test_the_summary_adds_up_signed_change_orders_into_the_contract_value(self):
        page = self.page("Sales")
        self.assertIn("$1,420", page)
        self.assertIn("$400 pending", page)
        self.assertIn("$1,020.00", page)
        self.assertIn("was $800.00", page)

    def test_needs_attention_lists_what_a_salesperson_has_to_act_on(self):
        page = self.page("Sales")
        self.assertIn(f"{self.open_quote.number}: follow-up overdue", page)
        self.assertIn(f"{self.returned.number}: returned by ops", page)
        self.assertIn(f"{self.waiting_co.number} is waiting on the customer", page)

    def test_a_technician_sees_sites_systems_and_jobs_but_not_quotes(self):
        page = self.page("Technician")
        self.assertIn("Gate code at front desk", page)
        self.assertIn("8 domes, 1 NVR", page)
        self.assertIn(self.install.number, page)
        self.assertNotIn(self.won.number, page)
        self.assertNotIn("Won contract value", page)
        self.assertNotIn("Needs attention", page)

    def test_contractor_details_show_only_to_operations(self):
        self.assertNotIn("Contracted through", self.page("Sales"))
        self.assertNotIn("Billed through", self.page("Install Manager"))
        page = self.page("Operations Manager")
        self.assertIn("Contracted through: Northgate", page)
        self.assertIn("Billed through", page)

    def test_margin_shows_only_to_roles_that_can_see_cost(self):
        self.assertNotIn(">Margin<", self.page("Sales"))
        self.assertIn(">Margin<", self.page("Sales Manager"))

    def test_people_without_customer_access_are_turned_away(self):
        stranger = User.objects.create_user("stranger", password="pw", is_staff=True)
        self.client.force_login(stranger)
        self.assertEqual(self.client.get(reverse("admin:crm_customer_overview", args=[self.customer.pk])).status_code, 403)

    def test_a_customer_without_sites_says_so(self):
        empty = Customer.objects.create(name="Empty Co")
        self.as_role("Sales")
        page = self.client.get(reverse("admin:crm_customer_overview", args=[empty.pk])).content.decode()
        self.assertIn("no sites yet", page)

    def test_the_overview_is_linked_from_the_list_and_both_detail_pages(self):
        self.as_role("Operations Manager")
        overview = reverse("admin:crm_customer_overview", args=[self.customer.pk])
        self.assertIn(overview, self.client.get(reverse("admin:crm_customer_changelist")).content.decode())
        customer_page = self.client.get(reverse("admin:crm_customer_change", args=[self.customer.pk])).content.decode()
        self.assertIn(reverse("admin:crm_customer_overview_action", args=[self.customer.pk]), customer_page)
        quote_page = self.client.get(reverse("admin:crm_quote_change", args=[self.won.pk])).content.decode()
        self.assertIn(reverse("admin:crm_quote_customer_overview_action", args=[self.won.pk]), quote_page)
        self.assertIn(reverse("admin:crm_quote_printable_action", args=[self.won.pk]), quote_page)

    def test_the_header_buttons_land_on_the_right_pages(self):
        self.as_role("Operations Manager")
        response = self.client.get(reverse("admin:crm_customer_overview_action", args=[self.customer.pk]))
        self.assertRedirects(response, reverse("admin:crm_customer_overview", args=[self.customer.pk]), fetch_redirect_response=False)
        response = self.client.get(reverse("admin:crm_quote_customer_overview_action", args=[self.won.pk]))
        self.assertRedirects(response, reverse("admin:crm_customer_overview", args=[self.customer.pk]), fetch_redirect_response=False)
        response = self.client.get(reverse("admin:crm_quote_printable_action", args=[self.won.pk]))
        self.assertRedirects(response, reverse("admin:crm_quote_document", args=[self.won.pk]), fetch_redirect_response=False)
        response = self.client.get(reverse("admin:fsm_workorder_labor_report_action"))
        self.assertRedirects(response, reverse("admin:fsm_workorder_labor_report"), fetch_redirect_response=False)

    def test_header_buttons_respect_permissions(self):
        self.as_role("Technician")
        self.assertEqual(self.client.get(reverse("admin:fsm_workorder_labor_report_action")).status_code, 403)
        self.assertEqual(self.client.get(reverse("admin:crm_quote_new_lead_action")).status_code, 403)
        self.as_role("Sales")
        response = self.client.get(reverse("admin:crm_quote_new_lead_action"))
        self.assertRedirects(response, reverse("admin:crm_quote_new_lead"), fetch_redirect_response=False)


class DashboardTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("setup_roles")
        today = timezone.localdate()
        cls.today = today
        prime = Contractor.objects.create(name="Northgate")
        customer = Customer.objects.create(name="Dash Co", contracted_through=prime)
        cls.site = Site.objects.create(customer=customer, name="HQ", address="1 Main St", access_notes="Gate code 4411")
        cls.item = CatalogItem.objects.create(sku="SW", name="Switch", unit_cost=100, list_price=400, min_price=320)
        cls.labor = CatalogItem.objects.create(
            sku="LAB", name="Labor", kind=CatalogItem.Kind.LABOR, unit_cost=45, list_price=95, min_price=80,
        )
        cls.skill = Skill.objects.create(name="Cabling")

        cls.rep = cls.person("rep", "Sales")
        cls.other_rep = cls.person("otherrep", "Sales")
        cls.sales_manager = cls.person("salesmanager", "Sales Manager")
        cls.install = cls.person("installmgr", "Install Manager")
        cls.ops = cls.person("opsmgr", "Operations Manager")
        cls.tech = cls.person("tech", "Technician", tech=True)
        cls.other_tech = cls.person("othertech", "Technician", tech=True)

        day = timedelta(days=1)
        cls.a = cls.quote(cls.rep, Stage.NEW_LEAD, follow=today - day)
        cls.b = cls.quote(cls.rep, Stage.QUOTE_SENT, follow=today + 3 * day)
        cls.c = cls.quote(cls.rep, Stage.QUOTE_SENT, valid=today + 2 * day)
        cls.d = cls.quote(cls.rep, Stage.DEPOSIT, ops_status=Quote.OpsStatus.REFUSED, sent_on=timezone.now())
        cls.e = cls.quote(cls.other_rep, Stage.QUOTE_SENT, follow=today - 2 * day)

    @classmethod
    def person(cls, username, role, tech=False):
        user = User.objects.create_user(username=username, password="pw", is_staff=True)
        user.groups.add(Group.objects.get(name=role))
        if tech:
            TechProfile.objects.create(user=user).skills.add(cls.skill)
        return user

    @classmethod
    def quote(cls, rep, stage, follow=None, valid=None, price=None, **fields):
        quote = Quote.objects.create(site=cls.site, salesperson=rep, stage=stage, follow_up_on=follow, valid_until=valid, **fields)
        QuoteLine.objects.create(quote=quote, item=cls.item, quantity=1, unit_price=price)
        return quote

    def page(self, user):
        self.client.force_login(user)
        response = self.client.get(reverse("admin:index"))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def tile(self, page, label):
        match = re.search(rf"<small>{re.escape(label)}</small>\s*<b>(.*?)</b>\s*(?:<span>(.*?)</span>)?", page, re.S)
        self.assertIsNotNone(match, f"no tile called {label}")
        return match.group(1).strip(), (match.group(2) or "").strip()

    def tile_href(self, page, label):
        match = re.search(rf'<a class="tile[^"]*" href="([^"]*)">\s*<small>{re.escape(label)}</small>', page)
        self.assertIsNotNone(match, f"no tile called {label}")
        return match.group(1)

    # sales

    def test_a_rep_sees_only_their_own_quotes_and_a_manager_sees_everyone(self):
        page = self.page(self.rep)
        self.assertEqual(self.tile(page, "Open quotes"), ("3", "$1,200 pending"))
        self.assertEqual(self.tile(page, "Follow-ups overdue")[0], "1")
        self.assertIn("My quotes and follow-ups", page)
        page = self.page(self.sales_manager)
        self.assertEqual(self.tile(page, "Open quotes"), ("4", "$1,600 pending"))
        self.assertEqual(self.tile(page, "Follow-ups overdue")[0], "2")
        self.assertNotIn("My quotes and follow-ups", page)

    def test_tile_links_carry_the_rep_filter_only_for_reps(self):
        self.assertIn(f"salesperson__id__exact={self.rep.pk}", self.tile_href(self.page(self.rep), "Follow-ups overdue"))
        self.assertNotIn("salesperson__id", self.tile_href(self.page(self.sales_manager), "Follow-ups overdue"))
        self.assertIn("follow_up=overdue", self.tile_href(self.page(self.rep), "Follow-ups overdue"))

    def test_due_returned_and_expiring_tiles(self):
        page = self.page(self.rep)
        self.assertEqual(self.tile(page, "Due this week")[0], "1")
        self.assertEqual(self.tile(page, "Returned by Ops")[0], "1")
        self.assertEqual(self.tile(page, "Expiring soon")[0], "1")
        self.assertIn("ops_status__exact=refused", self.tile_href(page, "Returned by Ops"))

    def test_the_follow_up_table_is_ordered_by_date_and_names_the_customer(self):
        page = self.page(self.rep)
        self.assertIn("Next follow-ups", page)
        self.assertIn("Dash Co", page)
        self.assertLess(page.index(self.a.number), page.index(self.b.number))
        self.assertNotIn(self.e.number, page)

    def test_only_sales_manager_level_users_see_performance(self):
        Quote.objects.filter(pk=self.a.pk).update(created=timezone.now() - timedelta(days=100), stage=Stage.LOST)
        self.quote(self.rep, Stage.APPROVED)
        self.quote(self.rep, Stage.LOST, lost_reason="price")
        page = self.page(self.sales_manager)
        # won: deposit and approved; lost within 90 days: one (the other lost quote is older)
        self.assertEqual(self.tile(page, "Win rate, last 90 days"), ("67%", "2 won, 1 lost"))
        self.assertIn("Pipeline value by stage", page)
        self.assertIn("Price too high", page)
        self.assertIn("Average margin, open quotes", page)
        self.assertNotIn("Pipeline value by stage", self.page(self.rep))
        self.assertNotIn("Win rate", self.page(self.rep))

    def test_needs_approval_counts_quotes_held_for_pricing_or_deposit(self):
        held = self.quote(self.rep, Stage.NEW_LEAD, price=300)
        low_deposit = self.quote(self.rep, Stage.NEW_LEAD)
        Quote.objects.filter(pk=low_deposit.pk).update(deposit_percent=Decimal("25"))
        self.assertEqual(self.tile(self.page(self.sales_manager), "Needs your approval")[0], "2")
        self.assertEqual(self.tile(self.page(self.install), "Needs your approval")[0], "2")
        held.refresh_from_db()
        held.exceptions_approved = True
        held.exceptions_signature = held.current_signature()
        held.save()
        self.assertEqual(self.tile(self.page(self.sales_manager), "Needs your approval")[0], "1")
        self.assertNotIn("Needs your approval", self.page(self.rep))

    # operations

    def make_install(self, hours, estimated=10, lead=None, status=WorkOrder.Status.IN_PROGRESS):
        quote = Quote.objects.create(site=self.site, stage=Stage.SENT_TO_OPS, ops_status=Quote.OpsStatus.ACCEPTED)
        QuoteLine.objects.create(quote=quote, item=self.labor, quantity=estimated)
        work_order = WorkOrder.objects.create(wo_type="install", site=self.site, quote=quote, status=status, lead_tech=lead)
        if hours:
            assignment = VisitAssignment.objects.create(
                visit=Visit.objects.create(work_order=work_order, scheduled_for=self.today), tech=self.tech, skill=self.skill,
            )
            assignment.start()
            assignment.started_at = timezone.now() - timedelta(hours=hours)
            assignment.save()
            assignment.stop()
        return work_order

    def test_the_install_manager_sees_operations_tiles_but_no_sales_block(self):
        self.make_install(hours=12, lead=self.tech)
        self.make_install(hours=2, lead=None)
        Quote.objects.filter(pk=self.b.pk).update(ops_status=Quote.OpsStatus.PENDING)
        page = self.page(self.install)
        self.assertEqual(self.tile(page, "Waiting on Ops")[0], "1")
        self.assertEqual(self.tile(page, "Installs in progress"), ("2", "1 over estimate"))
        self.assertEqual(self.tile(page, "Installs with no lead")[0], "1")
        self.assertNotIn("My quotes and follow-ups", page)
        self.assertNotIn("Next follow-ups", page)
        self.assertNotIn("Subcontract work open", page)

    def test_visits_and_unassigned_visits_this_week(self):
        work_order = self.make_install(hours=0)
        assigned = Visit.objects.create(work_order=work_order, scheduled_for=self.today + timedelta(days=2))
        VisitAssignment.objects.create(visit=assigned, tech=self.tech, skill=self.skill)
        Visit.objects.create(work_order=work_order, scheduled_for=self.today)
        Visit.objects.create(work_order=work_order, scheduled_for=self.today + timedelta(days=30))
        page = self.page(self.install)
        self.assertEqual(self.tile(page, "Visits, next 7 days"), ("2", "1 with no technician"))
        self.assertIn("Today&#x27;s visits", page.replace("'", "&#x27;"))
        self.assertIn("No technician yet", page)

    def test_open_change_orders_and_unread_alerts(self):
        quote = self.quote(self.rep, Stage.SENT_TO_OPS)
        ChangeOrder.objects.create(quote=quote, reason="x", status="requested")
        ChangeOrder.objects.create(quote=quote, reason="y", status="priced")
        ChangeOrder.objects.create(quote=quote, reason="z", status="signed", signed_by="Pat")
        notify(["Install Manager"], "one")
        notify(["Install Manager"], "two")
        notify(["Sales"], "not for installers")
        page = self.page(self.install)
        self.assertEqual(self.tile(page, "Change orders open")[0], "2")
        self.assertEqual(self.tile(page, "Unread alerts")[0], "2")

    def test_the_operations_manager_also_sees_subcontract_service_and_bonus(self):
        WorkOrder.objects.create(wo_type="subcontract", site=self.site)
        WorkOrder.objects.create(wo_type="subcontract", site=self.site, status=WorkOrder.Status.COMPLETE)
        WorkOrder.objects.create(wo_type="service", site=self.site, billing="billable")
        WorkOrder.objects.create(wo_type="service", site=self.site, billing="warranty")
        done = self.make_install(hours=6, estimated=10, lead=self.tech, status=WorkOrder.Status.COMPLETE)
        page = self.page(self.ops)
        self.assertEqual(self.tile(page, "Subcontract work open"), ("1", "Northgate 1"))
        value, sub = self.tile(page, "Service calls open")
        self.assertEqual(value, "2")
        self.assertIn("billable", sub)
        self.assertIn("warranty", sub)
        self.assertEqual(self.tile(page, "Labor bonus earned")[0], f"${done.bonus:,.0f}")

    # technicians

    def make_job(self, tech, day, status="open"):
        work_order = WorkOrder.objects.create(wo_type="install", site=self.site, description="Pull cable to the east wing")
        return VisitAssignment.objects.create(
            visit=Visit.objects.create(work_order=work_order, scheduled_for=day), tech=tech, skill=self.skill,
        )

    def test_a_technician_sees_their_jobs_with_access_notes_and_no_money(self):
        mine = self.make_job(self.tech, self.today)
        theirs = self.make_job(self.other_tech, self.today)
        self.make_job(self.tech, self.today + timedelta(days=3))
        page = self.page(self.tech)
        self.assertIn("My jobs", page)
        self.assertIn("Gate code 4411", page)
        self.assertIn("Pull cable to the east wing", page)
        self.assertIn(reverse("dashboard_assignment", args=[mine.pk, "start"]), page)
        self.assertNotIn(reverse("dashboard_assignment", args=[theirs.pk, "start"]), page)
        self.assertIn("Next 7 days", page)
        self.assertNotIn("Open quotes", page)
        self.assertIsNone(re.search(r"\$\d", page.split("My jobs")[1]))

    def test_the_buttons_start_and_stop_a_job_and_show_a_running_card(self):
        job = self.make_job(self.tech, self.today)
        self.client.force_login(self.tech)
        response = self.client.post(reverse("dashboard_assignment", args=[job.pk, "start"]), follow=True)
        job.refresh_from_db()
        self.assertIsNotNone(job.started_at)
        self.assertContains(response, "Job started")
        self.assertContains(response, "Stop job")
        self.assertContains(response, reverse("dashboard_assignment", args=[job.pk, "stop"]))
        self.client.post(reverse("dashboard_assignment", args=[job.pk, "stop"]))
        job.refresh_from_db()
        self.assertIsNotNone(job.ended_at)

    def test_hours_this_week_come_from_finished_jobs(self):
        job = self.make_job(self.tech, self.today)
        job.start()
        job.started_at = timezone.now() - timedelta(hours=3)
        job.save()
        job.stop()
        page = self.page(self.tech)
        self.assertIn("Hours this week", page)
        self.assertRegex(page, r"<small>Hours this week</small>\s*<b>3</b>")

    def test_the_buttons_are_post_only_and_respect_ownership(self):
        job = self.make_job(self.tech, self.today)
        url = reverse("dashboard_assignment", args=[job.pk, "start"])
        self.client.force_login(self.tech)
        self.assertEqual(self.client.get(url).status_code, 405)
        self.client.force_login(self.other_tech)
        self.assertEqual(self.client.post(url).status_code, 403)
        job.refresh_from_db()
        self.assertIsNone(job.started_at)
        self.client.force_login(self.install)
        self.client.post(url)
        job.refresh_from_db()
        self.assertIsNotNone(job.started_at)
        self.client.force_login(self.tech)
        self.assertEqual(self.client.post(reverse("dashboard_assignment", args=[job.pk, "bogus"])).status_code, 404)

    def test_anonymous_visitors_are_sent_to_login(self):
        job = self.make_job(self.tech, self.today)
        response = self.client.post(reverse("dashboard_assignment", args=[job.pk, "start"]))
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])

    # everyone else

    def test_someone_with_no_dashboard_role_gets_the_plain_app_list(self):
        viewer = User.objects.create_user("viewer", password="pw", is_staff=True)
        viewer.user_permissions.add(Permission.objects.get(codename="view_customer"))
        page = self.page(viewer)
        self.assertNotIn("Welcome,", page)
        self.assertIn("Customers", page)

    def test_a_superuser_sees_sales_and_operations_but_not_the_technician_block(self):
        boss = User.objects.create_superuser("boss", "b@example.com", "pw")
        page = self.page(boss)
        self.assertIn("Quotes and follow-ups", page)
        self.assertIn("Pipeline value by stage", page)
        self.assertIn("Waiting on Ops", page)
        self.assertNotIn("My jobs", page)


class StartupSafetyTests(TestCase):
    def test_admin_actions_never_name_permissions_with_a_dot(self):
        """A dotted permission makes Unfold's startup check query the database, which breaks migrate on a new one."""
        from django.contrib import admin as django_admin

        offenders = []
        for model, model_admin in django_admin.site._registry.items():
            if not hasattr(model_admin, "_get_base_actions_list"):
                continue
            actions = [
                *model_admin._get_base_actions_list(), *model_admin._get_base_actions_detail(),
                *model_admin._get_base_actions_row(), *model_admin._get_base_actions_submit_line(),
            ]
            for item in actions:
                for permission in getattr(item.method, "allowed_permissions", []):
                    if "." in permission:
                        offenders.append(f"{type(model_admin).__name__}.{item.method.original_function_name}: {permission}")
        self.assertEqual(offenders, [])

    def test_every_action_permission_method_exists(self):
        from django.contrib import admin as django_admin

        for model, model_admin in django_admin.site._registry.items():
            if not hasattr(model_admin, "_get_base_actions_list"):
                continue
            for item in [*model_admin._get_base_actions_list(), *model_admin._get_base_actions_detail()]:
                for permission in getattr(item.method, "allowed_permissions", []):
                    self.assertTrue(
                        hasattr(model_admin, f"has_{permission}_permission"),
                        f"{type(model_admin).__name__} is missing has_{permission}_permission",
                    )


class SiteRootTests(TestCase):
    def test_the_site_root_sends_people_to_the_dashboard(self):
        response = self.client.get("/")
        self.assertRedirects(response, reverse("admin:index"), fetch_redirect_response=False)
