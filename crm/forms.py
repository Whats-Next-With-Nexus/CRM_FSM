from django import forms
from django.db import transaction

from .models import Customer, Quote, Site, SystemType


class NewLeadForm(forms.Form):
    """One screen that creates the customer, their first site and a draft quote."""

    customer_name = forms.CharField(max_length=200)
    contact_name = forms.CharField(max_length=120, required=False)
    phone = forms.CharField(max_length=40, required=False)
    email = forms.EmailField(required=False)
    lead_source = forms.ChoiceField(choices=[("", "---------")] + Customer.LeadSource.choices, required=False)

    site_name = forms.CharField(max_length=120, required=False, help_text="For example, Indianapolis.")
    address = forms.CharField(max_length=200)
    city = forms.CharField(max_length=80, required=False)
    state = forms.CharField(max_length=2, required=False)
    zip_code = forms.CharField(max_length=10, required=False)

    system_types = forms.ModelMultipleChoiceField(
        queryset=SystemType.objects.filter(active=True), widget=forms.CheckboxSelectMultiple,
        label="Services wanted", error_messages={"required": "Pick at least one service."},
    )
    salesperson = forms.CharField(max_length=80, required=False)
    follow_up_on = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))

    label_suffix = ""

    GROUPS = [
        ("Customer", ["customer_name", "contact_name", "phone", "email", "lead_source"]),
        ("Site", ["site_name", "address", "city", "state", "zip_code"]),
        ("Quote", ["system_types", "salesperson", "follow_up_on"]),
    ]

    def grouped(self):
        return [(title, [self[name] for name in names]) for title, names in self.GROUPS]

    def clean(self):
        data = super().clean()
        name, address = data.get("customer_name"), data.get("address")
        if name and address:
            twin = Site.objects.filter(address__iexact=address, customer__name__iexact=name).select_related("customer").first()
            if twin:
                raise forms.ValidationError(
                    f"{twin.customer.number} already has a site at {twin.address}. "
                    "Add a quote to that site instead of creating a new lead."
                )
        return data

    @transaction.atomic
    def save(self):
        data = self.cleaned_data
        customer = Customer.objects.create(
            name=data["customer_name"], contact_name=data["contact_name"], phone=data["phone"], email=data["email"],
            lead_source=data["lead_source"], status=Customer.Status.LEAD,
        )
        site = Site.objects.create(
            customer=customer, name=data["site_name"], address=data["address"], city=data["city"],
            state=data["state"].upper(), zip_code=data["zip_code"],
        )
        quote = Quote.objects.create(site=site, salesperson=data["salesperson"], follow_up_on=data["follow_up_on"])
        quote.system_types.set(data["system_types"])
        return quote
