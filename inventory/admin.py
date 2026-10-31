from django.contrib import admin
from unfold.admin import ModelAdmin
from unfold.decorators import display

from .models import CatalogItem


@admin.register(CatalogItem)
class CatalogItemAdmin(ModelAdmin):
    list_display = ("sku", "name", "kind_label", "unit_cost", "list_price", "min_price", "qty_on_hand", "active")
    list_filter = ("kind", "active")
    search_fields = ("sku", "name")

    def _sees_costs(self, request):
        return request.user.has_perm("crm.view_costs")

    def get_list_display(self, request):
        columns = super().get_list_display(request)
        return columns if self._sees_costs(request) else [c for c in columns if c != "unit_cost"]

    def get_exclude(self, request, obj=None):
        exclude = list(super().get_exclude(request, obj) or [])
        return exclude if self._sees_costs(request) else exclude + ["unit_cost"]

    @display(description="Kind", ordering="kind", label=True)
    def kind_label(self, obj):
        return obj.get_kind_display()
