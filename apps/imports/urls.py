from django.urls import path

from . import views

app_name = "imports"

urlpatterns = [
    path("", views.import_upload, name="import_upload"),
    path("modele.csv", views.import_template, name="import_template"),
    path("historique/", views.import_history, name="import_history"),
    path("<int:pk>/", views.import_detail, name="import_detail"),
    path("<int:pk>/confirmer/", views.import_confirm, name="import_confirm"),
]
