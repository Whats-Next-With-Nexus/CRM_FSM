from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from crm.models import Contractor, Customer, Site
from fsm.models import Skill, TechProfile, Visit, VisitAssignment, WorkOrder

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
