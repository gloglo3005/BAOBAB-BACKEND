from django.urls import path

from . import coworking_views as v

urlpatterns = [
    path("dashboard/", v.DashboardView.as_view(), name="cw-dashboard"),
    path("profile/", v.ProfileView.as_view(), name="cw-profile"),
    # Zones, ressources, tarifs
    path("zones/", v.ZoneListCreateView.as_view(), name="cw-zones"),
    path("zones/<str:pk>/", v.ZoneDetailView.as_view(), name="cw-zone-detail"),
    path("resources/", v.ResourceListCreateView.as_view(), name="cw-resources"),
    path("resources/availability/", v.AvailabilityView.as_view(), name="cw-availability"),
    path("resources/<str:pk>/", v.ResourceDetailView.as_view(), name="cw-resource-detail"),
    path("rates/", v.RateListCreateView.as_view(), name="cw-rates"),
    path("rates/<str:pk>/", v.RateDetailView.as_view(), name="cw-rate-detail"),
    # Clients
    path("customers/", v.CustomerListCreateView.as_view(), name="cw-customers"),
    path("customers/<str:pk>/", v.CustomerDetailView.as_view(), name="cw-customer-detail"),
    # Réservations
    path("reservations/", v.ReservationListCreateView.as_view(), name="cw-reservations"),
    path("reservations/<str:pk>/", v.ReservationDetailView.as_view(), name="cw-reservation-detail"),
    path("reservations/<str:pk>/cancel/", v.ReservationCancelView.as_view(), name="cw-reservation-cancel"),
    path("reservations/<str:pk>/no-show/", v.ReservationNoShowView.as_view(), name="cw-reservation-no-show"),
    # Arrivées et sorties
    path("sessions/", v.SessionListView.as_view(), name="cw-sessions"),
    path("sessions/check-in/", v.CheckInView.as_view(), name="cw-check-in"),
    path("sessions/<str:pk>/", v.SessionDetailView.as_view(), name="cw-session-detail"),
    path("sessions/<str:pk>/check-out/", v.CheckOutView.as_view(), name="cw-check-out"),
    path("sessions/<str:pk>/correct/", v.SessionCorrectView.as_view(), name="cw-session-correct"),
    # Paiements manuels
    path("payments/", v.PaymentListCreateView.as_view(), name="cw-payments"),
    path("payments/<str:pk>/", v.PaymentDetailView.as_view(), name="cw-payment-detail"),
    path("payments/<str:pk>/void/", v.PaymentVoidView.as_view(), name="cw-payment-void"),
]
