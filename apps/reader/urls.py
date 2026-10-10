from django.urls import path

from . import views

app_name = "reader"

urlpatterns = [
    path("", views.home, name="home"),
    path("connexion/", views.ReaderLoginView.as_view(), name="login"),
    path("inscription/", views.register, name="register"),
    path("deconnexion/", views.logout_view, name="logout"),
    path("rejoindre/", views.join, name="join"),
    path("rejoindre/<int:org_id>/demander/", views.join_request, name="join_request"),
    path("profil/", views.profile, name="profile"),
    path("profil/mot-de-passe/", views.ReaderPasswordChangeView.as_view(), name="password"),
    path("bibliotheque/<int:pk>/", views.library, name="library"),
    path("bibliotheque/<int:pk>/catalogue/", views.catalog, name="catalog"),
    path("bibliotheque/<int:pk>/livres/<int:book_id>/", views.book_detail, name="book_detail"),
    path("bibliotheque/<int:pk>/emprunts/", views.loans, {"kind": "active"}, name="loans"),
    path("bibliotheque/<int:pk>/retards/", views.loans, {"kind": "overdue"}, name="overdue"),
    path("bibliotheque/<int:pk>/historique/", views.loans, {"kind": "history"}, name="history"),
]
