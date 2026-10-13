"""API staff du coworking : /api/v1/staff/<organization_id>/coworking/...

Chaque route appelle les services de apps.coworking : règles métier, verrous et
journal d'audit sont donc toujours les mêmes. Chaque requête est limitée à la
structure du staff connecté (request.organization) et au module coworking.
"""
from datetime import datetime, timedelta

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Q, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import generics, serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.coworking import services
from apps.coworking.models import (
    AccessSession,
    Customer,
    Payment,
    Rate,
    Reservation,
    Resource,
    Zone,
)

from .coworking_permissions import STAFF_COWORKING
from .coworking_serializers import (
    CheckInSerializer,
    CheckOutSerializer,
    CorrectSessionSerializer,
    CustomerSerializer,
    PaymentCreateSerializer,
    PaymentSerializer,
    ProfileSerializer,
    RateSerializer,
    ReasonSerializer,
    RequiredReasonSerializer,
    ReservationCreateSerializer,
    ReservationSerializer,
    ResourceSerializer,
    SessionSerializer,
    ZoneSerializer,
)
from .staff_views import TRUE_VALUES, StaffPagination, safe_filter


def error_response(exc):
    """400 si un champ est invalide, 404 si introuvable, 409 pour une règle métier."""
    if hasattr(exc, "error_dict"):
        return Response(exc.message_dict, status=status.HTTP_400_BAD_REQUEST)
    text = " ".join(exc.messages)
    code = status.HTTP_404_NOT_FOUND if "introuvable" in text.lower() else status.HTTP_409_CONFLICT
    return Response({"detail": text}, status=code)


def invalid_identifier():
    return Response({"detail": "Identifiant invalide."}, status=status.HTTP_404_NOT_FOUND)


def _plain(value):
    return getattr(value, "pk", value)


def save_from_serializer(request, serializer, model, instance=None):
    """Applique les données validées, puis enregistre via le service (audit compris)."""
    created = instance is None
    if created:
        instance = model()
    changes = {}
    for name, value in serializer.validated_data.items():
        if not created:
            old = getattr(instance, name, None)
            if old != value:
                changes[name] = {"from": _plain(old), "to": _plain(value)}
        setattr(instance, name, value)
    return services.save_object(
        organization=request.organization,
        user=request.user,
        instance=instance,
        created=created,
        metadata={"changes": changes} if changes else None,
    )


class CoworkingMixin:
    permission_classes = STAFF_COWORKING
    pagination_class = StaffPagination
    model = None

    def base_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return self.model.objects.none()
        return self.model.objects.filter(organization=self.request.organization)


class CrudListCreateView(CoworkingMixin, generics.ListCreateAPIView):
    """GET : liste. POST : création (enregistrée et auditée par le service)."""

    def get_queryset(self):
        return self.base_queryset()

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            instance = save_from_serializer(request, serializer, self.model)
        except DjangoValidationError as exc:
            return error_response(exc)
        return Response(self.get_serializer(instance).data, status=status.HTTP_201_CREATED)


class CrudDetailView(CoworkingMixin, generics.RetrieveUpdateAPIView):
    """GET : détail. PATCH : modification partielle (jamais de suppression)."""

    http_method_names = ["get", "patch", "head", "options"]

    def get_queryset(self):
        return self.base_queryset()

    def update(self, request, *args, **kwargs):
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        try:
            instance = save_from_serializer(request, serializer, self.model, instance)
        except DjangoValidationError as exc:
            return error_response(exc)
        return Response(self.get_serializer(instance).data)


# ---------------------------------------------------------------- fiche, zones, ressources, tarifs


class ProfileView(APIView):
    """Fiche et règles du coworking (horaires, délais, réservation, accueil sans réservation)."""

    permission_classes = STAFF_COWORKING
    http_method_names = ["get", "patch", "head", "options"]

    @extend_schema(responses=ProfileSerializer)
    def get(self, request, organization_id):
        return Response(ProfileSerializer(services.get_profile(request.organization)).data)

    @extend_schema(request=ProfileSerializer, responses=ProfileSerializer)
    def patch(self, request, organization_id):
        profile = services.get_profile(request.organization)
        serializer = ProfileSerializer(profile, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        try:
            profile = save_from_serializer(request, serializer, type(profile), profile)
        except DjangoValidationError as exc:
            return error_response(exc)
        return Response(ProfileSerializer(profile).data)


class ZoneListCreateView(CrudListCreateView):
    model = Zone
    serializer_class = ZoneSerializer


class ZoneDetailView(CrudDetailView):
    model = Zone
    serializer_class = ZoneSerializer


class ResourceListCreateView(CrudListCreateView):
    model = Resource
    serializer_class = ResourceSerializer

    def get_queryset(self):
        qs = self.base_queryset().select_related("zone")
        params = self.request.query_params
        q = params.get("q", "").strip()
        if q:
            qs = qs.filter(Q(code__icontains=q) | Q(name__icontains=q))
        if params.get("zone"):
            qs = safe_filter(qs, zone_id=params["zone"])
        if params.get("type"):
            qs = qs.filter(resource_type=params["type"].upper())
        if "active" in params:
            qs = qs.filter(active=params["active"].lower() in TRUE_VALUES)
        return qs.order_by("code", "pk")


class ResourceDetailView(CrudDetailView):
    model = Resource
    serializer_class = ResourceSerializer

    def get_queryset(self):
        return self.base_queryset().select_related("zone")


class AvailabilityView(APIView):
    """Ressources libres sur toute une période : ?start=...&end=...&type=DESK"""

    permission_classes = STAFF_COWORKING

    @extend_schema(
        parameters=[
            OpenApiParameter("start", OpenApiTypes.DATETIME, required=True),
            OpenApiParameter("end", OpenApiTypes.DATETIME, required=True),
            OpenApiParameter("type", OpenApiTypes.STR, required=False),
        ],
        responses=ResourceSerializer(many=True),
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
        return Response(ResourceSerializer(resources, many=True).data)


class RateListCreateView(CrudListCreateView):
    model = Rate
    serializer_class = RateSerializer

    def get_queryset(self):
        qs = self.base_queryset()
        params = self.request.query_params
        if params.get("resource"):
            qs = safe_filter(qs, resource_id=params["resource"])
        if params.get("type"):
            qs = qs.filter(resource_type=params["type"].upper())
        if "active" in params:
            qs = qs.filter(active=params["active"].lower() in TRUE_VALUES)
        return qs.order_by("resource_type", "unit", "pk")


class RateDetailView(CrudDetailView):
    model = Rate
    serializer_class = RateSerializer


# ---------------------------------------------------------------- clients


class CustomerListCreateView(CoworkingMixin, generics.ListCreateAPIView):
    model = Customer
    serializer_class = CustomerSerializer

    def get_queryset(self):
        qs = self.base_queryset()
        params = self.request.query_params
        q = params.get("q", "").strip()
        if q:
            qs = qs.filter(
                Q(customer_number__icontains=q) | Q(first_name__icontains=q)
                | Q(last_name__icontains=q) | Q(phone__icontains=q) | Q(email__icontains=q)
            )
        if params.get("status"):
            qs = qs.filter(status=params["status"].upper())
        return qs.order_by("last_name", "first_name", "pk")

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            customer = services.create_customer(
                organization=request.organization,
                performed_by=request.user,
                first_name=data["first_name"],
                last_name=data["last_name"],
                phone=data.get("phone", ""),
                email=data.get("email", ""),
            )
        except DjangoValidationError as exc:
            return error_response(exc)
        return Response(self.get_serializer(customer).data, status=status.HTTP_201_CREATED)


class CustomerDetailView(CrudDetailView):
    model = Customer
    serializer_class = CustomerSerializer


# ---------------------------------------------------------------- réservations


def with_paid(qs):
    return qs.annotate(
        paid=Coalesce(Sum("payments__amount", filter=Q(payments__status=Payment.Status.RECORDED)), 0)
    )


class ReservationListCreateView(CoworkingMixin, generics.ListCreateAPIView):
    model = Reservation
    serializer_class = ReservationSerializer

    def get_queryset(self):
        qs = with_paid(self.base_queryset().select_related("customer", "resource"))
        params = self.request.query_params
        if params.get("status"):
            qs = qs.filter(status=params["status"].upper())
        if params.get("resource"):
            qs = safe_filter(qs, resource_id=params["resource"])
        if params.get("customer"):
            qs = safe_filter(qs, customer_id=params["customer"])
        if params.get("date"):
            try:
                day = datetime.strptime(params["date"], "%Y-%m-%d")
            except ValueError:
                return qs.none()
            start = timezone.make_aware(day)
            qs = qs.filter(starts_at__gte=start, starts_at__lt=start + timedelta(days=1))
        return qs.order_by("-starts_at", "-pk")

    @extend_schema(request=ReservationCreateSerializer, responses={201: ReservationSerializer})
    def post(self, request, organization_id):
        data = ReservationCreateSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        try:
            reservation = services.create_reservation(
                organization=request.organization,
                customer_id=data.validated_data["customer"],
                resource_id=data.validated_data["resource"],
                starts_at=data.validated_data["starts_at"],
                ends_at=data.validated_data["ends_at"],
                notes=data.validated_data["notes"],
                created_by=request.user,
            )
        except DjangoValidationError as exc:
            return error_response(exc)
        except (ValueError, TypeError):
            return invalid_identifier()
        return Response(ReservationSerializer(reservation).data, status=status.HTTP_201_CREATED)


class ReservationDetailView(CoworkingMixin, generics.RetrieveAPIView):
    model = Reservation
    serializer_class = ReservationSerializer

    def get_queryset(self):
        return with_paid(self.base_queryset().select_related("customer", "resource"))


class ReservationCancelView(APIView):
    permission_classes = STAFF_COWORKING

    @extend_schema(request=ReasonSerializer, responses=ReservationSerializer)
    def post(self, request, organization_id, pk):
        data = ReasonSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        try:
            reservation = services.cancel_reservation(
                organization=request.organization, reservation_id=pk,
                performed_by=request.user, reason=data.validated_data["reason"],
            )
        except DjangoValidationError as exc:
            return error_response(exc)
        except (ValueError, TypeError):
            return invalid_identifier()
        return Response(ReservationSerializer(reservation).data)


class ReservationNoShowView(APIView):
    permission_classes = STAFF_COWORKING

    @extend_schema(request=None, responses=ReservationSerializer)
    def post(self, request, organization_id, pk):
        try:
            reservation = services.mark_no_show(
                organization=request.organization, reservation_id=pk, performed_by=request.user,
            )
        except DjangoValidationError as exc:
            return error_response(exc)
        except (ValueError, TypeError):
            return invalid_identifier()
        return Response(ReservationSerializer(reservation).data)


# ---------------------------------------------------------------- sessions (arrivée / sortie)


class SessionListView(CoworkingMixin, generics.ListAPIView):
    model = AccessSession
    serializer_class = SessionSerializer

    def get_queryset(self):
        qs = with_paid(self.base_queryset().select_related("customer", "resource"))
        params = self.request.query_params
        if params.get("status"):
            qs = qs.filter(status=params["status"].upper())
        if params.get("resource"):
            qs = safe_filter(qs, resource_id=params["resource"])
        if params.get("customer"):
            qs = safe_filter(qs, customer_id=params["customer"])
        return qs.order_by("-checked_in_at", "-pk")


class SessionDetailView(CoworkingMixin, generics.RetrieveAPIView):
    model = AccessSession
    serializer_class = SessionSerializer

    def get_queryset(self):
        return with_paid(self.base_queryset().select_related("customer", "resource"))


class CheckInView(APIView):
    """Arrivée : avec `reservation`, ou sans (`customer` + `resource`)."""

    permission_classes = STAFF_COWORKING

    @extend_schema(request=CheckInSerializer, responses={201: SessionSerializer})
    def post(self, request, organization_id):
        data = CheckInSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        try:
            session = services.check_in(
                organization=request.organization,
                performed_by=request.user,
                reservation_id=data.validated_data["reservation"],
                customer_id=data.validated_data["customer"],
                resource_id=data.validated_data["resource"],
                notes=data.validated_data["notes"],
            )
        except DjangoValidationError as exc:
            return error_response(exc)
        except (ValueError, TypeError):
            return invalid_identifier()
        return Response(SessionSerializer(session).data, status=status.HTTP_201_CREATED)


class CheckOutView(APIView):
    permission_classes = STAFF_COWORKING

    @extend_schema(request=CheckOutSerializer, responses=SessionSerializer)
    def post(self, request, organization_id, pk):
        data = CheckOutSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        try:
            session = services.check_out(
                organization=request.organization, session_id=pk,
                performed_by=request.user, notes=data.validated_data["notes"],
            )
        except DjangoValidationError as exc:
            return error_response(exc)
        except (ValueError, TypeError):
            return invalid_identifier()
        return Response(SessionSerializer(session).data)


class SessionCorrectView(APIView):
    """Correction manuelle (heures, montant) : motif obligatoire, tout est journalisé."""

    permission_classes = STAFF_COWORKING

    @extend_schema(request=CorrectSessionSerializer, responses=SessionSerializer)
    def post(self, request, organization_id, pk):
        data = CorrectSessionSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        try:
            session = services.correct_session(
                organization=request.organization, session_id=pk, performed_by=request.user,
                reason=data.validated_data["reason"],
                checked_in_at=data.validated_data["checked_in_at"],
                checked_out_at=data.validated_data["checked_out_at"],
                amount=data.validated_data["amount"],
            )
        except DjangoValidationError as exc:
            return error_response(exc)
        except (ValueError, TypeError):
            return invalid_identifier()
        return Response(SessionSerializer(session).data)


# ---------------------------------------------------------------- paiements


class PaymentListCreateView(CoworkingMixin, generics.ListCreateAPIView):
    model = Payment
    serializer_class = PaymentSerializer

    def get_queryset(self):
        qs = self.base_queryset().select_related("customer")
        params = self.request.query_params
        if params.get("customer"):
            qs = safe_filter(qs, customer_id=params["customer"])
        if params.get("reservation"):
            qs = safe_filter(qs, reservation_id=params["reservation"])
        if params.get("session"):
            qs = safe_filter(qs, session_id=params["session"])
        if params.get("status"):
            qs = qs.filter(status=params["status"].upper())
        return qs.order_by("-paid_at", "-pk")

    @extend_schema(request=PaymentCreateSerializer, responses={201: PaymentSerializer})
    def post(self, request, organization_id):
        data = PaymentCreateSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        v = data.validated_data
        try:
            payment = services.record_payment(
                organization=request.organization, performed_by=request.user,
                customer_id=v["customer"], amount=v["amount"], method=v["method"],
                reservation_id=v["reservation"], session_id=v["session"],
                paid_at=v["paid_at"], note=v["note"],
            )
        except DjangoValidationError as exc:
            return error_response(exc)
        except (ValueError, TypeError):
            return invalid_identifier()
        return Response(PaymentSerializer(payment).data, status=status.HTTP_201_CREATED)


class PaymentDetailView(CoworkingMixin, generics.RetrieveAPIView):
    model = Payment
    serializer_class = PaymentSerializer

    def get_queryset(self):
        return self.base_queryset().select_related("customer")


class PaymentVoidView(APIView):
    permission_classes = STAFF_COWORKING

    @extend_schema(request=RequiredReasonSerializer, responses=PaymentSerializer)
    def post(self, request, organization_id, pk):
        data = RequiredReasonSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        try:
            payment = services.void_payment(
                organization=request.organization, payment_id=pk,
                performed_by=request.user, reason=data.validated_data["reason"],
            )
        except DjangoValidationError as exc:
            return error_response(exc)
        except (ValueError, TypeError):
            return invalid_identifier()
        return Response(PaymentSerializer(payment).data)


# ---------------------------------------------------------------- tableau de bord


class DashboardView(APIView):
    permission_classes = STAFF_COWORKING

    @extend_schema(responses=OpenApiTypes.OBJECT)
    def get(self, request, organization_id):
        stats = services.dashboard_stats(request.organization)
        open_sessions = with_paid(
            AccessSession.objects.filter(
                organization=request.organization, status=AccessSession.Status.OPEN
            ).select_related("customer", "resource")
        ).order_by("checked_in_at", "pk")[:50]
        stats["open_sessions"] = SessionSerializer(open_sessions, many=True).data
        return Response(stats)
