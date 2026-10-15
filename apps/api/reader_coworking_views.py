"""API lecteur du coworking : /api/v1/reader/coworking/<organization_id>/...

Un lecteur consulte l'espace (présentation, ressources, tarifs, disponibilités),
crée sa fiche client, réserve et annule SES réservations. Toutes les règles
passent par apps.coworking.reader_services (mêmes contrôles que l'accueil). Un
lecteur ne voit que ses propres réservations, sessions et paiements.
"""
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Q
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import generics, serializers, status
from rest_framework.permissions import BasePermission
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.coworking import reader_services, services
from apps.coworking.models import AccessSession, Customer, Payment, Rate, Reservation, Resource
from apps.organizations.models import Organization

from .coworking_serializers import (
    CustomerSerializer,
    RateSerializer,
    ReservationSerializer,
    SessionSerializer,
)
from .coworking_views import error_response, invalid_identifier, with_paid
from .permissions import MALFORMED_ID_ERRORS, IsReady
from .reader_views import ReaderPagination
from .throttles import ReaderBookingRateThrottle


class ReaderCoworkingSpace(BasePermission):
    """Structure active avec le module coworking. Place `request.organization` et
    `request.customer` (la fiche client du lecteur dans cette structure, ou None)."""

    message = "Cet espace de coworking n'est pas disponible."

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        try:
            organization = Organization.objects.filter(
                pk=view.kwargs.get("organization_id"),
                status=Organization.Status.ACTIVE,
                coworking_module=True,
            ).first()
        except MALFORMED_ID_ERRORS:
            return False
        if organization is None:
            return False
        request.organization = organization
        request.customer = Customer.objects.filter(organization=organization, user=user).first()
        return True


READER_COWORKING = [IsReady, ReaderCoworkingSpace]
class BookingThrottleMixin:
    """Limite supplémentaire sur les écritures (en plus des limites générales)."""

    def get_throttles(self):
        throttles = super().get_throttles()
        if self.request.method == "POST":
            throttles.append(ReaderBookingRateThrottle())
        return throttles


NO_CUSTOMER = "Créez d'abord votre fiche client dans cet espace."


def no_customer():
    return Response({"detail": NO_CUSTOMER}, status=status.HTTP_404_NOT_FOUND)


# ------------------------------------------------------------------ serializers


class ReaderResourceSerializer(serializers.ModelSerializer):
    zone_name = serializers.CharField(source="zone.name", read_only=True)

    class Meta:
        model = Resource
        fields = ["id", "zone", "zone_name", "code", "name", "resource_type", "capacity"]


class ReaderReservationSerializer(ReservationSerializer):
    """Comme celle du staff, mais les notes saisies à l'accueil restent internes."""

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if instance.source != Reservation.Source.APP:
            data["notes"] = ""
        return data


class ReaderSessionSerializer(SessionSerializer):
    class Meta(SessionSerializer.Meta):
        fields = [f for f in SessionSerializer.Meta.fields if f != "notes"]
        read_only_fields = fields


class ReaderPaymentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Payment
        fields = [
            "id", "receipt_number", "amount", "currency", "method", "status",
            "paid_at", "reservation", "session",
        ]
        read_only_fields = fields


class ReaderCustomerInputSerializer(serializers.Serializer):
    first_name = serializers.CharField(required=False, allow_blank=True, default="", max_length=100)
    last_name = serializers.CharField(required=False, allow_blank=True, default="", max_length=100)
    phone = serializers.CharField(required=False, allow_blank=True, default="", max_length=30)
    email = serializers.EmailField(required=False, allow_blank=True, default="")


class ReaderReservationInputSerializer(serializers.Serializer):
    resource = serializers.CharField(help_text="Identifiant de la ressource.")
    starts_at = serializers.DateTimeField()
    ends_at = serializers.DateTimeField()
    notes = serializers.CharField(required=False, allow_blank=True, default="", max_length=500)


class ReaderCancelInputSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=200)


# ------------------------------------------------------------------ consultation


class ReaderCoworkingInfoView(APIView):
    """Présentation de l'espace, règles de réservation et ma fiche client (null si absente)."""

    permission_classes = READER_COWORKING

    @extend_schema(responses=OpenApiTypes.OBJECT)
    def get(self, request, organization_id):
        org = request.organization
        profile = services.get_profile(org)
        return Response({
            "organization": {
                "id": org.pk, "name": org.name, "description": org.description,
                "address": org.address, "city": org.city, "country": org.country,
                "phone": org.phone, "email": org.email, "website": org.website,
            },
            "public_description": profile.public_description,
            "opening_hours": profile.opening_hours,
            "booking_enabled": profile.booking_enabled,
            "min_booking_minutes": profile.min_booking_minutes,
            "max_booking_minutes": profile.max_booking_minutes,
            "cancellation_deadline_hours": profile.cancellation_deadline_hours,
            "max_upcoming_reservations": profile.max_upcoming_reservations,
            "my_customer": CustomerSerializer(request.customer).data if request.customer else None,
        })


class ReaderResourceListView(generics.ListAPIView):
    """Ressources réservables (actives, dans une zone active). ?type=DESK|ROOM|OTHER"""

    permission_classes = READER_COWORKING
    pagination_class = None
    serializer_class = ReaderResourceSerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Resource.objects.none()
        qs = Resource.objects.filter(
            organization=self.request.organization, active=True, zone__active=True
        ).select_related("zone").order_by("zone__name", "code")
        kind = (self.request.query_params.get("type") or "").upper()
        return qs.filter(resource_type=kind) if kind else qs


class ReaderRateListView(generics.ListAPIView):
    """Tarifs en vigueur aujourd'hui."""

    permission_classes = READER_COWORKING
    pagination_class = None
    serializer_class = RateSerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Rate.objects.none()
        today = timezone.localdate()
        return (
            Rate.objects.filter(organization=self.request.organization, active=True)
            .filter(Q(valid_from__isnull=True) | Q(valid_from__lte=today))
            .filter(Q(valid_until__isnull=True) | Q(valid_until__gte=today))
        )


class ReaderAvailabilityView(APIView):
    """Ressources libres sur toute une période : ?start=...&end=...&type=DESK"""

    permission_classes = READER_COWORKING

    @extend_schema(
        parameters=[
            OpenApiParameter("start", OpenApiTypes.DATETIME, required=True),
            OpenApiParameter("end", OpenApiTypes.DATETIME, required=True),
            OpenApiParameter("type", OpenApiTypes.STR, required=False),
        ],
        responses=ReaderResourceSerializer(many=True),
    )
    def get(self, request, organization_id):
        field = serializers.DateTimeField()
        try:
            starts_at = field.to_internal_value(request.query_params.get("start", ""))
            ends_at = field.to_internal_value(request.query_params.get("end", ""))
        except serializers.ValidationError:
            return Response(
                {"detail": "Paramètres start et end obligatoires (date-heure ISO 8601)."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            resources = services.available_resources(
                request.organization, starts_at, ends_at,
                resource_type=(request.query_params.get("type") or "").upper() or None,
            )
        except DjangoValidationError as exc:
            return error_response(exc)
        return Response(ReaderResourceSerializer(resources, many=True).data)


# ------------------------------------------------------------------ ma fiche client


class ReaderCustomerView(BookingThrottleMixin, APIView):
    """GET : ma fiche client (404 si absente). POST : je la crée (active tout de suite)."""

    permission_classes = READER_COWORKING

    @extend_schema(responses=CustomerSerializer)
    def get(self, request, organization_id):
        if request.customer is None:
            return no_customer()
        return Response(CustomerSerializer(request.customer).data)

    @extend_schema(request=ReaderCustomerInputSerializer, responses={201: CustomerSerializer})
    def post(self, request, organization_id):
        data = ReaderCustomerInputSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        user = request.user
        values = data.validated_data
        try:
            customer = reader_services.register_customer(
                organization=request.organization,
                user=user,
                first_name=values["first_name"] or user.first_name,
                last_name=values["last_name"] or user.last_name,
                phone=values["phone"] or user.phone,
                email=values["email"] or user.email,
            )
        except DjangoValidationError as exc:
            return error_response(exc)
        return Response(CustomerSerializer(customer).data, status=status.HTTP_201_CREATED)


# ------------------------------------------------------------------ mes réservations


class ReaderReservationListCreateView(BookingThrottleMixin, generics.ListAPIView):
    """GET : mes réservations (?status=CONFIRMED...). POST : je réserve une ressource."""

    permission_classes = READER_COWORKING
    pagination_class = ReaderPagination
    serializer_class = ReaderReservationSerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False) or self.request.customer is None:
            return Reservation.objects.none()
        qs = with_paid(
            Reservation.objects.filter(
                organization=self.request.organization, customer=self.request.customer
            ).select_related("customer", "resource")
        )
        if self.request.query_params.get("status"):
            qs = qs.filter(status=self.request.query_params["status"].upper())
        return qs.order_by("-starts_at", "-pk")

    @extend_schema(
        request=ReaderReservationInputSerializer, responses={201: ReaderReservationSerializer}
    )
    def post(self, request, organization_id):
        if request.customer is None:
            return no_customer()
        data = ReaderReservationInputSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        values = data.validated_data
        try:
            reservation = reader_services.create_own_reservation(
                organization=request.organization,
                user=request.user,
                resource_id=values["resource"],
                starts_at=values["starts_at"],
                ends_at=values["ends_at"],
                notes=values["notes"],
            )
        except DjangoValidationError as exc:
            return error_response(exc)
        except (ValueError, TypeError):
            return invalid_identifier()
        return Response(ReaderReservationSerializer(reservation).data, status=status.HTTP_201_CREATED)


class ReaderReservationDetailView(generics.RetrieveAPIView):
    permission_classes = READER_COWORKING
    serializer_class = ReaderReservationSerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False) or self.request.customer is None:
            return Reservation.objects.none()
        return with_paid(
            Reservation.objects.filter(
                organization=self.request.organization, customer=self.request.customer
            ).select_related("customer", "resource")
        )


class ReaderReservationCancelView(BookingThrottleMixin, APIView):
    """J'annule ma réservation, avant le délai d'annulation, avec un motif."""

    permission_classes = READER_COWORKING

    @extend_schema(request=ReaderCancelInputSerializer, responses=ReaderReservationSerializer)
    def post(self, request, organization_id, pk):
        if request.customer is None:
            return no_customer()
        data = ReaderCancelInputSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        try:
            reservation = reader_services.cancel_own_reservation(
                organization=request.organization,
                user=request.user,
                reservation_id=pk,
                reason=data.validated_data["reason"],
            )
        except DjangoValidationError as exc:
            return error_response(exc)
        except (ValueError, TypeError):
            return invalid_identifier()
        return Response(ReaderReservationSerializer(reservation).data)


# ------------------------------------------------------------------ mon historique


class ReaderSessionListView(generics.ListAPIView):
    """Mes passages (arrivées et sorties) : ?status=OPEN|CLOSED"""

    permission_classes = READER_COWORKING
    pagination_class = ReaderPagination
    serializer_class = ReaderSessionSerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False) or self.request.customer is None:
            return AccessSession.objects.none()
        qs = with_paid(
            AccessSession.objects.filter(
                organization=self.request.organization, customer=self.request.customer
            ).select_related("customer", "resource")
        )
        if self.request.query_params.get("status"):
            qs = qs.filter(status=self.request.query_params["status"].upper())
        return qs.order_by("-checked_in_at", "-pk")


class ReaderPaymentListView(generics.ListAPIView):
    """Mes paiements enregistrés par l'espace (lecture seule)."""

    permission_classes = READER_COWORKING
    pagination_class = ReaderPagination
    serializer_class = ReaderPaymentSerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False) or self.request.customer is None:
            return Payment.objects.none()
        return Payment.objects.filter(
            organization=self.request.organization, customer=self.request.customer
        ).order_by("-paid_at", "-pk")
