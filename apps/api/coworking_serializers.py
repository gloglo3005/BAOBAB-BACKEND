"""Sérialiseurs de l'API coworking.

Toute relation (zone, ressource, client...) est limitée à la structure du staff
connecté : l'identifiant d'une autre structure est refusé comme un identifiant
qui n'existe pas.
"""
from rest_framework import serializers

from apps.coworking.models import (
    AccessSession,
    CoworkingProfile,
    Customer,
    Payment,
    Rate,
    Reservation,
    Resource,
    Zone,
)
from apps.coworking.services import amount_paid


class OrgPrimaryKeyField(serializers.PrimaryKeyRelatedField):
    """Clé étrangère limitée à la structure de la requête."""

    def get_queryset(self):
        queryset = super().get_queryset()
        organization = getattr(self.context.get("request"), "organization", None)
        if organization is None:
            return queryset.none()
        return queryset.filter(organization=organization)


class ProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = CoworkingProfile
        fields = [
            "public_description", "opening_hours", "booking_enabled", "walk_in_enabled",
            "min_booking_minutes", "max_booking_minutes", "cancellation_deadline_hours",
            "no_show_grace_minutes", "walk_in_buffer_minutes", "check_in_early_minutes",
        ]


class ZoneSerializer(serializers.ModelSerializer):
    class Meta:
        model = Zone
        fields = ["id", "name", "description", "active"]


class ResourceSerializer(serializers.ModelSerializer):
    zone = OrgPrimaryKeyField(queryset=Zone.objects.all())
    zone_name = serializers.CharField(source="zone.name", read_only=True)

    class Meta:
        model = Resource
        fields = [
            "id", "zone", "zone_name", "code", "name", "resource_type",
            "capacity", "active", "notes",
        ]


class RateSerializer(serializers.ModelSerializer):
    resource = OrgPrimaryKeyField(
        queryset=Resource.objects.all(), required=False, allow_null=True
    )
    resource_type = serializers.ChoiceField(
        choices=Resource.Type.choices, required=False, allow_blank=True, default=""
    )

    class Meta:
        model = Rate
        fields = [
            "id", "resource", "resource_type", "unit", "amount", "currency",
            "valid_from", "valid_until", "active",
        ]
        read_only_fields = ["currency"]


class CustomerSerializer(serializers.ModelSerializer):
    class Meta:
        model = Customer
        fields = [
            "id", "customer_number", "first_name", "last_name", "phone", "email", "status",
        ]
        read_only_fields = ["customer_number"]


class PaidMixin(serializers.Serializer):
    """amount_paid / balance : calculés (ou lus depuis l'annotation `paid` de la liste)."""

    amount_paid = serializers.SerializerMethodField()
    balance = serializers.SerializerMethodField()

    def _paid(self, obj):
        paid = getattr(obj, "paid", None)
        if paid is None:
            if isinstance(obj, Reservation):
                paid = amount_paid(reservation=obj)
            else:
                paid = amount_paid(session=obj)
        return paid

    def get_amount_paid(self, obj) -> int:
        return self._paid(obj)

    def get_balance(self, obj) -> int | None:
        if isinstance(obj, AccessSession) and obj.status == AccessSession.Status.OPEN:
            return None  # le montant dû n'est connu qu'à la sortie
        due = obj.quoted_amount if isinstance(obj, Reservation) else obj.amount
        return due - self._paid(obj)


class ReservationSerializer(PaidMixin, serializers.ModelSerializer):
    customer_number = serializers.CharField(source="customer.customer_number", read_only=True)
    customer_name = serializers.SerializerMethodField()
    resource_code = serializers.CharField(source="resource.code", read_only=True)
    resource_name = serializers.CharField(source="resource.name", read_only=True)

    class Meta:
        model = Reservation
        fields = [
            "id", "customer", "customer_number", "customer_name",
            "resource", "resource_code", "resource_name",
            "starts_at", "ends_at", "status", "rate", "quoted_amount", "currency",
            "amount_paid", "balance", "notes", "cancelled_at", "cancel_reason", "created_at",
        ]
        read_only_fields = fields

    def get_customer_name(self, obj) -> str:
        return f"{obj.customer.first_name} {obj.customer.last_name}"


class ReservationCreateSerializer(serializers.Serializer):
    customer = serializers.CharField(help_text="Identifiant du client.")
    resource = serializers.CharField(help_text="Identifiant de la ressource.")
    starts_at = serializers.DateTimeField()
    ends_at = serializers.DateTimeField()
    notes = serializers.CharField(required=False, allow_blank=True, default="")


class ReasonSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True, default="", max_length=200)


class RequiredReasonSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=200)


class SessionSerializer(PaidMixin, serializers.ModelSerializer):
    customer_number = serializers.CharField(source="customer.customer_number", read_only=True)
    customer_name = serializers.SerializerMethodField()
    resource_code = serializers.CharField(source="resource.code", read_only=True)
    resource_name = serializers.CharField(source="resource.name", read_only=True)

    class Meta:
        model = AccessSession
        fields = [
            "id", "customer", "customer_number", "customer_name",
            "resource", "resource_code", "resource_name", "reservation",
            "checked_in_at", "checked_out_at", "status", "rate", "amount", "currency",
            "amount_paid", "balance", "notes",
        ]
        read_only_fields = fields

    def get_customer_name(self, obj) -> str:
        return f"{obj.customer.first_name} {obj.customer.last_name}"


class CheckInSerializer(serializers.Serializer):
    reservation = serializers.CharField(
        required=False, allow_null=True, default=None,
        help_text="Avec réservation : seul cet identifiant est nécessaire.",
    )
    customer = serializers.CharField(required=False, allow_null=True, default=None)
    resource = serializers.CharField(required=False, allow_null=True, default=None)
    notes = serializers.CharField(required=False, allow_blank=True, default="")


class CheckOutSerializer(serializers.Serializer):
    notes = serializers.CharField(required=False, allow_blank=True, default="")


class CorrectSessionSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=200)
    checked_in_at = serializers.DateTimeField(required=False, allow_null=True, default=None)
    checked_out_at = serializers.DateTimeField(required=False, allow_null=True, default=None)
    amount = serializers.IntegerField(required=False, allow_null=True, default=None, min_value=0)


class PaymentSerializer(serializers.ModelSerializer):
    customer_number = serializers.CharField(source="customer.customer_number", read_only=True)

    class Meta:
        model = Payment
        fields = [
            "id", "receipt_number", "customer", "customer_number", "reservation", "session",
            "amount", "currency", "method", "status", "paid_at", "note",
            "voided_at", "void_reason", "created_at",
        ]
        read_only_fields = fields


class PaymentCreateSerializer(serializers.Serializer):
    customer = serializers.CharField()
    amount = serializers.IntegerField(min_value=1, help_text="Montant entier en francs CFA (XOF).")
    method = serializers.ChoiceField(choices=Payment.Method.choices, default=Payment.Method.CASH)
    reservation = serializers.CharField(required=False, allow_null=True, default=None)
    session = serializers.CharField(required=False, allow_null=True, default=None)
    paid_at = serializers.DateTimeField(required=False, allow_null=True, default=None)
    note = serializers.CharField(required=False, allow_blank=True, default="", max_length=200)
