from datetime import date

from django.core.management.base import BaseCommand
from django.db import transaction

from crm.models import (
    Contractor, Customer, Quote, RepairPlan, RepairPlanEnrollment, Site, System, SystemType,
)
from fsm.models import WorkOrder


class Command(BaseCommand):
    help = "Load the example accounts from the design notes (safe to run once on an empty database)."

    @transaction.atomic
    def handle(self, *args, **options):
        if Customer.objects.exists():
            self.stdout.write("Customers already exist; skipping.")
            return

        types = {t.name: t for t in SystemType.objects.all()}
        northgate = Contractor.objects.create(name="Northgate Security Services")
        apex = Contractor.objects.create(
            name="Apex Security Networks",
            noncompete_notes="Example note: sole provider for core systems; direct quotes only with a written waiver.",
        )
        gold = RepairPlan.objects.create(name="Gold", monthly_rate=20, exclusions="Negligence, acts of God, construction damage.")
        silver = RepairPlan.objects.create(name="Silver", monthly_rate=12, exclusions="Negligence, acts of God, construction damage.")
        bronze = RepairPlan.objects.create(name="Bronze", monthly_rate=6, exclusions="Negligence, acts of God, construction damage.")

        # Direct account with several sites and per-site repair plans
        brightpath = Customer.objects.create(name="Brightpath Academy", status=Customer.Status.CUSTOMER)
        for name, address, plan in [
            ("Indianapolis", "100 Meridian St", gold),
            ("Greenwood", "200 Madison Ave", silver),
            ("Lawrence", "300 Pendleton Pike", bronze),
            ("Carmel", "400 Range Line Rd", None),
        ]:
            site = Site.objects.create(customer=brightpath, name=name, address=address, state="IN")
            if plan:
                RepairPlanEnrollment.objects.create(site=site, plan=plan, start_date=date(2026, 1, 1))
            if name in ("Indianapolis", "Greenwood"):
                System.objects.create(site=site, system_type=types["CCTV"])
                System.objects.create(site=site, system_type=types["Intrusion Detection"])

        # Subcontract account: contractor -> customer -> sites
        bank = Customer.objects.create(name="First Meridian Bank", status=Customer.Status.CUSTOMER, contracted_through=northgate)
        for name, address in [("Downtown", "10 Main St"), ("High School Rd.", "20 High School Rd"), ("56th St.", "30 E 56th St")]:
            Site.objects.create(customer=bank, name=name, address=address, state="IN")

        # Apex customer with a waived-non-compete Direct override on one system
        boutique = Customer.objects.create(name="Lumen Beauty", status=Customer.Status.CUSTOMER, contracted_through=apex)
        mall = Site.objects.create(customer=boutique, name="Castleton", address="6020 E 82nd St", state="IN")
        System.objects.create(site=mall, system_type=types["Intrusion Detection"])
        System.objects.create(site=mall, system_type=types["Access Control"])
        System.objects.create(
            site=mall, system_type=types["Intercom"], bill_direct=True,
            waiver_note="Example: contractor doesn't offer intercom and approved a one-off waiver.",
        )

        # A fresh lead with a quote in progress
        lead = Customer.objects.create(name="Summit Property Group", status=Customer.Status.LEAD)
        building = Site.objects.create(customer=lead, name="Building 8", address="500 Commerce Dr", state="IN")
        quote = Quote.objects.create(site=building, stage=Quote.Stage.QUOTE_SENT, amount=14500, salesperson="Dakota")
        quote.system_types.add(types["CCTV"])

        WorkOrder.objects.create(
            wo_type=WorkOrder.Type.SERVICE, site=Site.objects.get(customer=brightpath, site_number=1),
            description="Front door camera offline.",
        )
        self.stdout.write(self.style.SUCCESS("Demo data loaded."))
