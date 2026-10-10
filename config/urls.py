from django.contrib import admin
from django.urls import include, path
from django.views.generic import RedirectView

admin.site.site_header = "Plateforme : administration globale"
admin.site.site_title = "Administration globale"
admin.site.index_title = "Gestion de la plateforme"

urlpatterns = [
    path("", RedirectView.as_view(url="/staff/", permanent=False)),
    path("global/", admin.site.urls),
    path("api/v1/", include("apps.api.urls")),
    path("staff/import/", include("apps.imports.urls")),
    path("staff/", include("apps.staff.urls")),
    path("reader/", include("apps.reader.urls")),
]
