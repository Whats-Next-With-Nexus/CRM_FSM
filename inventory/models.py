from django.core.exceptions import ValidationError
from django.db import models


class CatalogItem(models.Model):
    """Anything that can go on a quote line. Reps see list and minimum price; only managers see cost."""

    class Kind(models.TextChoices):
        PART = "part", "Part"
        LABOR = "labor", "Labor"
        TRIP_FEE = "trip_fee", "Trip Fee"
        MISC = "misc", "Miscellaneous"
        MATERIAL = "material", "Material"

    sku = models.CharField(max_length=40, unique=True)
    name = models.CharField(max_length=200)
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.PART)
    unit_cost = models.DecimalField(max_digits=10, decimal_places=2)
    list_price = models.DecimalField(max_digits=10, decimal_places=2)
    min_price = models.DecimalField(
        max_digits=10, decimal_places=2, help_text="Lowest price a rep may quote without manager approval.",
    )
    qty_on_hand = models.IntegerField(default=0, help_text="Parts only.")
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["kind", "name"]

    def clean(self):
        if self.min_price is not None and self.list_price is not None and self.min_price > self.list_price:
            raise ValidationError("The minimum price can't be higher than the list price.")

    def __str__(self):
        return f"{self.name} ({self.sku})"
