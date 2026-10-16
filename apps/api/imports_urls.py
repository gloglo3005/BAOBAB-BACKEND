from django.urls import path

from . import imports_views as v

urlpatterns = [
    path("", v.ImportListCreateView.as_view(), name="staff-imports"),
    path("template/", v.ImportTemplateView.as_view(), name="staff-import-template"),
    path("<str:pk>/", v.ImportDetailView.as_view(), name="staff-import-detail"),
    path("<str:pk>/confirm/", v.ImportConfirmView.as_view(), name="staff-import-confirm"),
]
