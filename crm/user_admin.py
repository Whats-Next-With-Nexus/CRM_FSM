from django import forms
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.forms import AdminUserCreationForm, UserChangeForm
from django.contrib.auth.models import Group

User = get_user_model()


def role_field():
    return forms.ModelChoiceField(
        queryset=Group.objects.all(), required=False, label="Role",
        help_text="One role per user. Extras are added per user under 'User permissions' after saving.",
    )


class RoleCreationForm(AdminUserCreationForm):
    role = role_field()


class RoleChangeForm(UserChangeForm):
    role = role_field()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.fields["role"].initial = self.instance.groups.first()


admin.site.unregister(User)


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    form = RoleChangeForm
    add_form = RoleCreationForm
    add_fieldsets = DjangoUserAdmin.add_fieldsets + (("Role", {"fields": ("role",)}),)
    list_display = ("username", "first_name", "last_name", "role_name", "is_staff")
    list_filter = ("is_staff", "is_superuser", "groups")
    fieldsets = (
        (None, {"fields": ("username", "password")}),
        ("Personal info", {"fields": ("first_name", "last_name", "email")}),
        ("Role & permissions", {"fields": ("role", "is_active", "is_staff", "is_superuser", "user_permissions")}),
        ("Important dates", {"fields": ("last_login", "date_joined")}),
    )
    filter_horizontal = ("user_permissions",)

    @admin.display(description="Role")
    def role_name(self, obj):
        group = obj.groups.first()
        return group.name if group else "-"

    def save_model(self, request, obj, form, change):
        if form.cleaned_data.get("role"):
            obj.is_staff = True
        super().save_model(request, obj, form, change)

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        role = form.cleaned_data.get("role")
        form.instance.groups.set([role] if role else [])
