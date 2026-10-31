from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from crm.models import ChangeOrder, ChangeOrderLine, Contractor, Customer, Quote, QuoteLine, Site
from inventory.models import CatalogItem
from fsm.models import InternalTimeCategory, Skill, TechProfile, Visit, VisitAssignment, WorkOrder

User = get_user_model()


class RoleTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("setup_roles")
        prime = Contractor.objects.create(name="Northgate")
        customer = Customer.objects.create(name="First Meridian", contracted_through=prime)
        cls.site = Site.objects.create(customer=customer, address="1 Main St")
        cls.sub_wo = WorkOrder.objects.create(wo_type="subcontract", site=cls.site, description="Prime contractor job")
        cls.install_wo = WorkOrder.objects.create(wo_type="install", site=cls.site, description="Ours")

    def login(self, role):
        user = User.objects.create_user(username=role.replace(" ", "").lower(), password="pw", is_staff=True)
        user.groups.add(self.client_group(role))
        self.client.force_login(user)
        return user

    def client_group(self, role):
        from django.contrib.auth.models import Group
        return Group.objects.get(name=role)

    def change_url(self, wo):
        return reverse("admin:fsm_workorder_change", args=[wo.pk])

    def test_install_manager_cannot_edit_subcontract_but_can_schedule(self):
        self.login("Install Manager")
        response = self.client.get(self.change_url(self.sub_wo))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("name=\"description\"", response.content.decode())

        data = {
            "visits-TOTAL_FORMS": "1", "visits-INITIAL_FORMS": "0", "visits-MIN_NUM_FORMS": "0",
            "visits-MAX_NUM_FORMS": "1000", "visits-0-scheduled_for": "2026-11-02", "visits-0-summary": "",
            "description": "tampered",
        }
        self.client.post(self.change_url(self.sub_wo), data)
        self.sub_wo.refresh_from_db()
        self.assertEqual(self.sub_wo.description, "Prime contractor job")
        self.assertEqual(Visit.objects.filter(work_order=self.sub_wo).count(), 1)

    def test_install_manager_can_edit_install_work_orders(self):
        self.login("Install Manager")
        response = self.client.get(self.change_url(self.install_wo))
        self.assertIn("name=\"description\"", response.content.decode())

    def test_operations_manager_can_edit_subcontract(self):
        self.login("Operations Manager")
        response = self.client.get(self.change_url(self.sub_wo))
        self.assertIn("name=\"description\"", response.content.decode())

    def test_contractors_hidden_from_install_manager(self):
        self.login("Install Manager")
        self.assertEqual(self.client.get(reverse("admin:crm_contractor_changelist")).status_code, 403)
        page = self.client.get(reverse("admin:crm_customer_change", args=[self.site.customer.pk]))
        self.assertNotIn("contracted_through", page.content.decode())

    def test_contractors_visible_to_operations_manager(self):
        self.login("Operations Manager")
        self.assertEqual(self.client.get(reverse("admin:crm_contractor_changelist")).status_code, 200)
        page = self.client.get(reverse("admin:crm_customer_change", args=[self.site.customer.pk]))
        self.assertIn("contracted_through", page.content.decode())

    def test_sales_and_technician_are_view_only_on_work_orders(self):
        for role in ("Sales", "Technician"):
            with self.subTest(role=role):
                self.login(role)
                self.assertEqual(self.client.get(self.change_url(self.install_wo)).status_code, 200)
                data = {"description": "tampered", "wo_type": "install", "status": "open", "site": self.site.pk}
                self.assertEqual(self.client.post(self.change_url(self.install_wo), data).status_code, 403)
                self.assertEqual(self.client.get(reverse("admin:fsm_workorder_add")).status_code, 403)

    def test_user_form_has_single_role_and_no_groups_picker(self):
        admin_user = User.objects.create_superuser("boss", "b@example.com", "pw")
        self.client.force_login(admin_user)
        page = self.client.get(reverse("admin:auth_user_change", args=[admin_user.pk])).content.decode()
        self.assertIn('name="role"', page)
        self.assertNotIn('name="groups"', page)


class TechnicianAssignmentTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("setup_roles")
        customer = Customer.objects.create(name="Test Co")
        cls.site = Site.objects.create(customer=customer, address="1 Main St")
        cls.work_order = WorkOrder.objects.create(wo_type="install", site=cls.site, status=WorkOrder.Status.SCHEDULED)
        cls.visit = Visit.objects.create(work_order=cls.work_order)
        cls.cabling = Skill.objects.create(name="Cabling")
        cls.programming = Skill.objects.create(name="Programming")
        cls.alex = cls.make_tech("alex", [cls.cabling])
        cls.blair = cls.make_tech("blair", [cls.programming])

    @classmethod
    def make_tech(cls, username, skills):
        user = User.objects.create_user(username=username, password="pw", is_staff=True)
        user.groups.add(Group.objects.get(name="Technician"))
        TechProfile.objects.create(user=user).skills.set(skills)
        return user

    def assign(self, tech, skill):
        return VisitAssignment.objects.create(visit=self.visit, tech=tech, skill=skill)

    def test_a_tech_can_only_be_assigned_to_work_they_are_qualified_for(self):
        assignment = VisitAssignment(visit=self.visit, tech=self.alex, skill=self.programming)
        with self.assertRaisesMessage(ValidationError, "not qualified"):
            assignment.full_clean()
        VisitAssignment(visit=self.visit, tech=self.alex, skill=self.cabling).full_clean()

    def test_one_assignment_per_tech_per_visit(self):
        self.assign(self.alex, self.cabling)
        with self.assertRaises(Exception):
            self.assign(self.alex, self.cabling)

    def test_starting_the_first_job_begins_the_visit_and_the_install(self):
        assignment = self.assign(self.alex, self.cabling)
        self.assertFalse(self.work_order.install_begun)
        assignment.start()
        self.visit.refresh_from_db()
        self.work_order.refresh_from_db()
        self.assertIsNotNone(self.visit.started_at)
        self.assertEqual(self.work_order.status, WorkOrder.Status.IN_PROGRESS)
        self.assertTrue(self.work_order.install_begun)

    def test_a_second_tech_starting_later_does_not_move_the_visit_start(self):
        first, second = self.assign(self.alex, self.cabling), self.assign(self.blair, self.programming)
        first.start()
        started = Visit.objects.get(pk=self.visit.pk).started_at
        second.start()
        self.assertEqual(Visit.objects.get(pk=self.visit.pk).started_at, started)

    def test_hours_come_from_start_and_stop(self):
        assignment = self.assign(self.alex, self.cabling)
        with self.assertRaisesMessage(ValidationError, "Start the job"):
            assignment.stop()
        assignment.start()
        assignment.started_at = timezone.now() - timedelta(hours=2, minutes=30)
        assignment.save()
        assignment.stop()
        self.assertEqual(assignment.hours, Decimal("2.50"))
        self.assertEqual(self.work_order.hours_logged, Decimal("2.50"))

    def test_stopping_twice_keeps_the_first_end_time(self):
        assignment = self.assign(self.alex, self.cabling)
        assignment.start()
        assignment.stop()
        ended = assignment.ended_at
        assignment.stop()
        self.assertEqual(assignment.ended_at, ended)

    def act(self, action, *assignments):
        return self.client.post(
            reverse("admin:fsm_visitassignment_changelist"),
            {"action": action, "_selected_action": [a.pk for a in assignments]}, follow=True,
        )

    def test_a_tech_sees_only_their_own_assignments_and_can_start_and_stop_them(self):
        mine, theirs = self.assign(self.alex, self.cabling), self.assign(self.blair, self.programming)
        self.client.force_login(self.alex)
        page = self.client.get(reverse("admin:fsm_visitassignment_changelist")).content.decode()
        self.assertIn(f'/fsm/visitassignment/{mine.pk}/change/', page)
        self.assertNotIn(f'/fsm/visitassignment/{theirs.pk}/change/', page)

        self.act("start_job", mine, theirs)
        mine.refresh_from_db()
        theirs.refresh_from_db()
        self.assertIsNotNone(mine.started_at)
        self.assertIsNone(theirs.started_at)
        self.act("stop_job", mine)
        mine.refresh_from_db()
        self.assertIsNotNone(mine.ended_at)

    def test_a_tech_cannot_start_someone_elses_job_by_id(self):
        theirs = self.assign(self.blair, self.programming)
        self.client.force_login(self.alex)
        self.act("start_job", theirs)
        theirs.refresh_from_db()
        self.assertIsNone(theirs.started_at)

    def test_a_tech_can_only_edit_notes_on_their_assignment(self):
        mine = self.assign(self.alex, self.cabling)
        self.client.force_login(self.alex)
        page = self.client.get(reverse("admin:fsm_visitassignment_change", args=[mine.pk])).content.decode()
        self.assertIn('name="notes"', page)
        self.assertNotIn('name="skill"', page)
        self.assertNotIn('name="tech"', page)

    def test_a_tech_cannot_assign_people(self):
        self.client.force_login(self.alex)
        self.assertEqual(self.client.get(reverse("admin:fsm_visit_add")).status_code, 403)

    def test_a_manager_sees_everyone_and_can_assign_from_the_visit_page(self):
        first, second = self.assign(self.alex, self.cabling), self.assign(self.blair, self.programming)
        manager = User.objects.create_user(username="mgr", password="pw", is_staff=True)
        manager.groups.add(Group.objects.get(name="Install Manager"))
        self.client.force_login(manager)
        page = self.client.get(reverse("admin:fsm_visitassignment_changelist")).content.decode()
        self.assertIn(f'/fsm/visitassignment/{first.pk}/change/', page)
        self.assertIn(f'/fsm/visitassignment/{second.pk}/change/', page)
        visit_page = self.client.get(reverse("admin:fsm_visit_change", args=[self.visit.pk]))
        self.assertEqual(visit_page.status_code, 200)
        self.act("start_job", first)
        first.refresh_from_db()
        self.assertIsNotNone(first.started_at)


class InternalTimeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("setup_roles")
        customer = Customer.objects.create(name="Test Co")
        cls.site = Site.objects.create(customer=customer, address="1 Main St")
        cls.quote = Quote.objects.create(site=cls.site, stage="quote_sent")
        cls.sales_help = InternalTimeCategory.objects.get(name="Sales help")
        cls.cabling = Skill.objects.create(name="Cabling")
        cls.tech = User.objects.create_user(username="alex", password="pw", is_staff=True)
        cls.tech.groups.add(Group.objects.get(name="Technician"))
        TechProfile.objects.create(user=cls.tech).skills.add(cls.cabling)

    def internal_wo(self, **fields):
        return WorkOrder.objects.create(wo_type="internal", internal_category=self.sales_help, **fields)

    def log_hours(self, work_order, hours):
        assignment = VisitAssignment.objects.create(
            visit=Visit.objects.create(work_order=work_order), tech=self.tech, skill=self.cabling,
        )
        assignment.start()
        assignment.started_at = timezone.now() - timedelta(hours=hours)
        assignment.save()
        assignment.stop()
        return assignment

    def test_the_standard_categories_exist(self):
        self.assertEqual(
            set(InternalTimeCategory.objects.values_list("name", flat=True)), {"Sales help", "Misc time"},
        )

    def test_internal_time_needs_a_category_but_not_a_site(self):
        with self.assertRaisesMessage(ValidationError, "what the internal time"):
            WorkOrder(wo_type="internal").full_clean()
        WorkOrder(wo_type="internal", internal_category=self.sales_help).full_clean()
        self.assertIn("Sales help", str(self.internal_wo()))

    def test_other_work_orders_still_need_a_site_and_no_category(self):
        with self.assertRaisesMessage(ValidationError, "Pick the site"):
            WorkOrder(wo_type="install").full_clean()
        with self.assertRaisesMessage(ValidationError, "Only internal time"):
            WorkOrder(wo_type="install", site=self.site, internal_category=self.sales_help).full_clean()

    def test_a_walk_through_never_begins_the_install(self):
        walk_through = self.internal_wo(site=self.site, quote=self.quote)
        self.log_hours(walk_through, 2)
        walk_through.refresh_from_db()
        self.assertEqual(walk_through.status, WorkOrder.Status.IN_PROGRESS)
        self.assertFalse(walk_through.install_begun)
        self.assertFalse(self.quote.install_begun)

    def test_hours_land_on_the_internal_work_order_and_show_on_the_quote(self):
        install = WorkOrder.objects.create(wo_type="install", site=self.site, quote=self.quote)
        walk_through = self.internal_wo(site=self.site, quote=self.quote)
        self.log_hours(walk_through, 3)
        self.assertEqual(walk_through.hours_logged, Decimal("3.00"))
        self.assertEqual(install.hours_logged, Decimal("0"))
        self.assertEqual(self.quote.presale_hours, Decimal("3.00"))

    def test_misc_time_needs_no_site_or_quote(self):
        shop = WorkOrder.objects.create(
            wo_type="internal", internal_category=InternalTimeCategory.objects.get(name="Misc time"),
        )
        self.log_hours(shop, 1)
        self.assertEqual(shop.hours_logged, Decimal("1.00"))
        self.assertEqual(self.quote.presale_hours, Decimal("0"))

    def test_a_manager_can_create_internal_time_without_a_site_from_the_form(self):
        manager = User.objects.create_user(username="mgr", password="pw", is_staff=True)
        manager.groups.add(Group.objects.get(name="Install Manager"))
        self.client.force_login(manager)
        response = self.client.post(reverse("admin:fsm_workorder_add"), {
            "wo_type": "internal", "internal_category": self.sales_help.pk, "status": "open", "description": "Walk-through",
            "visits-TOTAL_FORMS": "0", "visits-INITIAL_FORMS": "0", "visits-MIN_NUM_FORMS": "0", "visits-MAX_NUM_FORMS": "1000",
        }, follow=True)
        self.assertContains(response, "was added successfully")
        self.assertEqual(WorkOrder.objects.get().site, None)

    def test_the_form_still_demands_a_site_for_an_install(self):
        manager = User.objects.create_user(username="mgr", password="pw", is_staff=True)
        manager.groups.add(Group.objects.get(name="Install Manager"))
        self.client.force_login(manager)
        response = self.client.post(reverse("admin:fsm_workorder_add"), {
            "wo_type": "install", "status": "open",
            "visits-TOTAL_FORMS": "0", "visits-INITIAL_FORMS": "0", "visits-MIN_NUM_FORMS": "0", "visits-MAX_NUM_FORMS": "1000",
        })
        self.assertContains(response, "Pick the site")
        self.assertEqual(WorkOrder.objects.count(), 0)

    def test_a_technician_can_start_and_stop_an_internal_assignment(self):
        assignment = self.log_hours(self.internal_wo(), 1)
        self.assertIsNotNone(assignment.ended_at)

    def test_the_quote_page_shows_pre_sale_time(self):
        manager = User.objects.create_user(username="mgr", password="pw", is_staff=True)
        manager.groups.add(Group.objects.get(name="Install Manager"))
        self.client.force_login(manager)
        url = reverse("admin:crm_quote_change", args=[self.quote.pk])
        self.assertIn("None logged", self.client.get(url).content.decode())
        self.log_hours(self.internal_wo(site=self.site, quote=self.quote), 2)
        self.assertIn("2 hours of technician time", self.client.get(url).content.decode())


class LaborBonusTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("setup_roles")
        customer = Customer.objects.create(name="Test Co")
        cls.site = Site.objects.create(customer=customer, address="1 Main St")
        cls.labor = CatalogItem.objects.create(
            sku="LAB", name="Technician labor", kind=CatalogItem.Kind.LABOR, unit_cost=45, list_price=95, min_price=80,
        )
        cls.camera = CatalogItem.objects.create(sku="CAM", name="Camera", unit_cost=100, list_price=200, min_price=150)
        cls.cabling = Skill.objects.create(name="Cabling")
        cls.lead = cls.make_tech("lead")
        cls.helper = cls.make_tech("helper")

    @classmethod
    def make_tech(cls, username):
        user = User.objects.create_user(username=username, password="pw", is_staff=True)
        user.groups.add(Group.objects.get(name="Technician"))
        TechProfile.objects.create(user=user).skills.add(cls.cabling)
        return user

    def install(self, estimated=20, status=WorkOrder.Status.OPEN, lead="lead"):
        quote = Quote.objects.create(site=self.site, stage="quote_sent")
        self.labor_line = QuoteLine.objects.create(quote=quote, item=self.labor, quantity=estimated)
        QuoteLine.objects.create(quote=quote, item=self.camera, quantity=5)
        return WorkOrder.objects.create(
            wo_type="install", site=self.site, quote=quote, status=status,
            lead_tech=User.objects.get(username=lead) if lead else None,
        )

    def log(self, work_order, tech, minutes):
        assignment = VisitAssignment.objects.create(
            visit=Visit.objects.create(work_order=work_order), tech=tech, skill=self.cabling,
        )
        assignment.start()
        assignment.started_at = timezone.now() - timedelta(minutes=minutes)
        assignment.save()
        assignment.stop()

    def manager(self, role="Install Manager", username="mgr"):
        user = User.objects.create_user(username=username, password="pw", is_staff=True)
        user.groups.add(Group.objects.get(name=role))
        return user

    def test_the_bonus_is_twenty_percent_of_the_saved_hours_at_the_labor_rate(self):
        work_order = self.install(estimated=20)
        self.log(work_order, self.lead, 600)
        self.assertEqual(work_order.estimated_hours, Decimal("20.00"))
        self.assertEqual(work_order.hours_saved, Decimal("10.00"))
        self.assertEqual(work_order.labor_cost, Decimal("450.00"))
        self.assertEqual(work_order.bonus, Decimal("90.00"))

    def test_hours_from_every_technician_count_against_the_estimate(self):
        work_order = self.install(estimated=20)
        self.log(work_order, self.lead, 360)
        self.log(work_order, self.helper, 240)
        self.assertEqual(work_order.hours_logged, Decimal("10.00"))
        self.assertEqual(work_order.bonus, Decimal("90.00"))

    def test_nothing_is_paid_or_deducted_at_or_over_the_estimate(self):
        work_order = self.install(estimated=20)
        self.log(work_order, self.lead, 1200)
        self.assertEqual((work_order.hours_saved, work_order.bonus), (Decimal("0.00"), Decimal("0.00")))
        over = self.install(estimated=20)
        self.log(over, self.lead, 1500)
        self.assertEqual((over.hours_saved, over.bonus), (Decimal("-5.00"), Decimal("0.00")))

    def test_fractional_hours_round_to_the_cent(self):
        work_order = self.install(estimated=20)
        self.log(work_order, self.lead, 441)
        self.assertEqual(work_order.hours_logged, Decimal("7.35"))
        self.assertEqual(work_order.bonus, Decimal("113.85"))

    def test_the_rate_and_percent_come_from_settings(self):
        work_order = self.install(estimated=20)
        self.log(work_order, self.lead, 600)
        with override_settings(LABOR_COST_RATE=Decimal("50"), INSTALL_BONUS_PERCENT=Decimal("10")):
            self.assertEqual(work_order.bonus, Decimal("50.00"))

    def test_only_signed_change_orders_adjust_the_estimate_and_only_labor_counts(self):
        work_order = self.install(estimated=20)
        quote = work_order.quote
        signed = ChangeOrder.objects.create(quote=quote, reason="Scope change", status="signed", signed_by="Pat")
        ChangeOrderLine.objects.create(change_order=signed, direction="add", item=self.labor, quantity=4)
        ChangeOrderLine.objects.create(change_order=signed, direction="remove", quote_line=self.labor_line, quantity=2)
        ChangeOrderLine.objects.create(change_order=signed, direction="add", item=self.camera, quantity=9)
        pending = ChangeOrder.objects.create(quote=quote, reason="Not signed", status="priced")
        ChangeOrderLine.objects.create(change_order=pending, direction="add", item=self.labor, quantity=50)
        self.assertEqual(work_order.estimated_hours, Decimal("22.00"))

    def test_only_installs_have_an_estimate_or_a_bonus(self):
        service = WorkOrder.objects.create(wo_type="service", site=self.site)
        self.assertEqual((service.estimated_hours, service.hours_saved, service.bonus), (None, None, Decimal("0.00")))
        no_quote = WorkOrder.objects.create(wo_type="install", site=self.site)
        self.assertIsNone(no_quote.estimated_hours)
        self.assertEqual(no_quote.bonus, Decimal("0.00"))

    def test_a_bonus_is_earned_only_once_the_install_is_complete(self):
        work_order = self.install(estimated=20)
        self.assertFalse(work_order.bonus_earned)
        work_order.status = WorkOrder.Status.COMPLETE
        self.assertTrue(work_order.bonus_earned)

    def test_the_lead_technician_must_be_a_technician(self):
        User.objects.create_user(username="plainuser", password="pw")
        self.client.force_login(self.manager())
        page = self.client.get(reverse("admin:fsm_workorder_add")).content.decode()
        select = page[page.index('name="lead_tech"'):]
        select = select[:select.index("</select>")]
        self.assertIn("lead", select)
        self.assertNotIn("plainuser", select)

    def test_managers_see_the_labor_section_and_others_do_not(self):
        work_order = self.install(estimated=20)
        self.log(work_order, self.lead, 600)
        url = reverse("admin:fsm_workorder_change", args=[work_order.pk])
        self.client.force_login(self.manager())
        page = self.client.get(url).content.decode()
        self.assertIn("Labor and bonus", page)
        self.assertIn("$90.00 for lead", page)
        self.assertIn("projected", page)
        self.client.force_login(self.manager("Sales", "rep"))
        self.assertNotIn("Labor and bonus", self.client.get(url).content.decode())
        self.client.force_login(self.lead)
        self.assertNotIn("Labor and bonus", self.client.get(url).content.decode())

    def test_the_report_totals_earned_bonuses_by_lead_and_is_for_managers(self):
        done = self.install(estimated=20, status=WorkOrder.Status.COMPLETE)
        self.log(done, self.lead, 600)
        running = self.install(estimated=20)
        self.log(running, self.lead, 300)
        unset = self.install(estimated=20, status=WorkOrder.Status.COMPLETE, lead=None)
        self.log(unset, self.helper, 600)
        url = reverse("admin:fsm_workorder_labor_report")

        self.client.force_login(self.manager("Operations Manager"))
        page = self.client.get(url).content.decode()
        self.assertIn("$180.00", page)
        self.assertIn("(projected)", page)
        self.assertIn("No lead technician set", page)

        self.client.force_login(self.lead)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.client.force_login(self.manager("Sales", "rep"))
        self.assertEqual(self.client.get(url).status_code, 403)
