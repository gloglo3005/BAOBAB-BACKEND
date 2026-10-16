from django.urls import path

from . import platform_views as v

urlpatterns = [
    path("dashboard/", v.PlatformDashboardView.as_view(), name="platform-dashboard"),
    path("organizations/", v.PlatformOrganizationListCreateView.as_view(), name="platform-organizations"),
    path("organizations/<str:pk>/", v.PlatformOrganizationDetailView.as_view(), name="platform-organization-detail"),
    path("organizations/<str:pk>/status/", v.PlatformOrganizationStatusView.as_view(), name="platform-organization-status"),
    path("organizations/<str:organization_id>/staff/", v.PlatformStaffListCreateView.as_view(), name="platform-staff"),
    path("organizations/<str:organization_id>/staff/<str:pk>/", v.PlatformStaffDetailView.as_view(), name="platform-staff-detail"),
    path("organizations/<str:organization_id>/staff/<str:pk>/reset-password/", v.PlatformStaffResetPasswordView.as_view(), name="platform-staff-reset"),
    path("audit/", v.PlatformAuditListView.as_view(), name="platform-audit"),
]
