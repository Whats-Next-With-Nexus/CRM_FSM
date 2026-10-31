"""URL configuration for the CRM FSM project."""
from django.contrib import admin
from django.urls import path
from django.views.generic import RedirectView

from fsm.views import assignment_action

urlpatterns = [
    path("", RedirectView.as_view(pattern_name="admin:index"), name="home"),
    path("dashboard/assignments/<int:pk>/<str:verb>/", assignment_action, name="dashboard_assignment"),
    path("admin/", admin.site.urls),
]
