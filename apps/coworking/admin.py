from django.contrib import admin

from .models import (
    AccessSession,
    CoworkingProfile,
    Customer,
    Payment,
    Rate,
    Reservation,
    Resource,
    Zone,
)


class ReadOnlyHistoryAdmin(admin.ModelAdmin):
    """Réservations, sessions et paiements se gèrent par l'API (règles, verrous, audit) :
    l'administration globale les consulte sans les modifier ni les supprimer."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(CoworkingProfile)
class CoworkingProfileAdmin(admin.ModelAdmin):
    list_display = ("organization", "booking_enabled", "walk_in_enabled")
    readonly_fields = ("next_customer_number", "next_receipt_number")


@admin.register(Zone)
class ZoneAdmin(admin.ModelAdmin):
    list_display = ("name", "organization", "active")
    list_filter = ("organization", "active")
    search_fields = ("name",)


@admin.register(Resource)
class ResourceAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "zone", "resource_type", "organization", "active")
    list_filter = ("organization", "resource_type", "active")
    search_fields = ("code", "name")


@admin.register(Rate)
class RateAdmin(admin.ModelAdmin):
    list_display = ("__str__", "organization", "active", "valid_from", "valid_until")
    list_filter = ("organization", "active", "unit")


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ("customer_number", "first_name", "last_name", "phone", "organization", "status")
    list_filter = ("organization", "status")
    search_fields = ("customer_number", "first_name", "last_name", "phone", "email")


@admin.register(Reservation)
class ReservationAdmin(ReadOnlyHistoryAdmin):
    list_display = ("resource", "customer", "starts_at", "ends_at", "status", "quoted_amount")
    list_filter = ("organization", "status")


@admin.register(AccessSession)
class AccessSessionAdmin(ReadOnlyHistoryAdmin):
    list_display = ("resource", "customer", "checked_in_at", "checked_out_at", "status", "amount")
    list_filter = ("organization", "status")


@admin.register(Payment)
class PaymentAdmin(ReadOnlyHistoryAdmin):
    list_display = ("receipt_number", "customer", "amount", "method", "status", "paid_at")
    list_filter = ("organization", "status", "method")
