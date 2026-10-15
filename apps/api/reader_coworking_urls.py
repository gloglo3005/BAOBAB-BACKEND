from django.urls import path

from . import reader_coworking_views as v

urlpatterns = [
    path("", v.ReaderCoworkingInfoView.as_view(), name="reader-cw-info"),
    path("customer/", v.ReaderCustomerView.as_view(), name="reader-cw-customer"),
    path("resources/", v.ReaderResourceListView.as_view(), name="reader-cw-resources"),
    path("resources/availability/", v.ReaderAvailabilityView.as_view(), name="reader-cw-availability"),
    path("rates/", v.ReaderRateListView.as_view(), name="reader-cw-rates"),
    path("reservations/", v.ReaderReservationListCreateView.as_view(), name="reader-cw-reservations"),
    path("reservations/<str:pk>/", v.ReaderReservationDetailView.as_view(), name="reader-cw-reservation-detail"),
    path("reservations/<str:pk>/cancel/", v.ReaderReservationCancelView.as_view(), name="reader-cw-reservation-cancel"),
    path("sessions/", v.ReaderSessionListView.as_view(), name="reader-cw-sessions"),
    path("payments/", v.ReaderPaymentListView.as_view(), name="reader-cw-payments"),
]
