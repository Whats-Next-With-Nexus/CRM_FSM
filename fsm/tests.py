from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from crm.models import Contractor, Customer, Site
from fsm.models import Visit, WorkOrder

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
