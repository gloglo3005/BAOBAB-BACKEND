"""API de l'administrateur de la plateforme : /api/v1/platform/...

Structures (création, modification, statut, modules), comptes du personnel de
chaque structure (surtout le responsable, qui gère ensuite lui-même ses agents),
journal d'audit global.
"""
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Count, Q
from django.utils.dateparse import parse_date
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import generics, status
from rest_framework.generics import get_object_or_404
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit.models import AuditLog
from apps.audit.services import log_action
from apps.organizations import services
from apps.organizations.models import Organization, OrganizationStaff

from . import admin_serializers as s
from .coworking_views import error_response
from .permissions import IsPlatformAdmin, IsReady
from .throttles import AdminActionRateThrottle

PLATFORM = [IsReady, IsPlatformAdmin]


class PlatformPagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = "page_size"
    max_page_size = 100


class AdminThrottleMixin:
    """Limite supplémentaire sur les écritures sensibles (comptes, mots de passe)."""

    def get_throttles(self):
        throttles = super().get_throttles()
        if self.request.method in ("POST", "PATCH"):
            throttles.append(AdminActionRateThrottle())
        return throttles


def organizations_queryset():
    active = Q(staff_members__active=True)
    return Organization.objects.annotate(
        staff_count=Count("staff_members", filter=active, distinct=True),
        admin_count=Count(
            "staff_members",
            filter=active & Q(staff_members__role=OrganizationStaff.Role.ADMIN),
            distinct=True,
        ),
    ).order_by("name")


# ------------------------------------------------------------------ tableau de bord


class PlatformDashboardView(APIView):
    permission_classes = PLATFORM

    @extend_schema(responses=OpenApiTypes.OBJECT)
    def get(self, request):
        orgs = Organization.objects
        counts = {row["status"]: row["n"] for row in orgs.values("status").annotate(n=Count("id"))}
        without_admin = organizations_queryset().filter(
            status=Organization.Status.ACTIVE, admin_count=0
        )
        recent = AuditLog.objects.select_related("user", "organization")[:10]
        return Response({
            "organizations": {
                "total": sum(counts.values()),
                "pending": counts.get(Organization.Status.PENDING, 0),
                "active": counts.get(Organization.Status.ACTIVE, 0),
                "suspended": counts.get(Organization.Status.SUSPENDED, 0),
                "rejected": counts.get(Organization.Status.REJECTED, 0),
                "with_library": orgs.filter(library_module=True).count(),
                "with_coworking": orgs.filter(coworking_module=True).count(),
            },
            "without_admin": [{"id": o.pk, "name": o.name} for o in without_admin[:10]],
            "recent_activity": s.AuditLogSerializer(recent, many=True).data,
        })


# ------------------------------------------------------------------ structures


class PlatformOrganizationListCreateView(generics.ListAPIView):
    """GET : structures (?q=, ?status=, ?module=LIBRARY|COWORKING). POST : crée une structure."""

    permission_classes = PLATFORM
    pagination_class = PlatformPagination
    serializer_class = s.PlatformOrganizationSerializer

    def get_queryset(self):
        qs = organizations_queryset()
        params = self.request.query_params
        if params.get("q"):
            term = params["q"]
            qs = qs.filter(Q(name__icontains=term) | Q(city__icontains=term) | Q(slug__icontains=term))
        if params.get("status"):
            qs = qs.filter(status=params["status"].upper())
        module = (params.get("module") or "").upper()
        if module == "LIBRARY":
            qs = qs.filter(library_module=True)
        elif module == "COWORKING":
            qs = qs.filter(coworking_module=True)
        return qs

    @extend_schema(request=s.PlatformOrganizationCreateSerializer, responses={201: s.PlatformOrganizationSerializer})
    def post(self, request):
        data = s.PlatformOrganizationCreateSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        values = dict(data.validated_data)
        name = values.pop("name")
        slug = values.pop("slug") or services.unique_slug(name)
        with transaction.atomic():
            organization = services.create_organization(name=name, slug=slug, **values)
            log_action(
                organization=organization, action=AuditLog.Action.ORGANIZATION_CREATED,
                entity=organization, user=request.user, description=f"Structure « {name} » créée.",
            )
        organization = organizations_queryset().get(pk=organization.pk)
        return Response(s.PlatformOrganizationSerializer(organization).data, status=status.HTTP_201_CREATED)


class PlatformOrganizationDetailView(generics.RetrieveUpdateAPIView):
    """GET / PATCH d'une structure (le statut a sa propre route)."""

    permission_classes = PLATFORM
    serializer_class = s.PlatformOrganizationSerializer
    http_method_names = ["get", "patch", "head", "options"]

    def get_queryset(self):
        return organizations_queryset()

    def perform_update(self, serializer):
        with transaction.atomic():
            organization = serializer.save()
            log_action(
                organization=organization, action=AuditLog.Action.ORGANIZATION_UPDATED,
                entity=organization, user=self.request.user,
                description="Structure modifiée.", metadata={"fields": sorted(serializer.validated_data)},
            )


class PlatformOrganizationStatusView(APIView):
    """Valide, suspend, refuse ou remet en attente une structure."""

    permission_classes = PLATFORM

    @extend_schema(request=s.StatusInputSerializer, responses=s.PlatformOrganizationSerializer)
    def post(self, request, pk):
        organization = get_object_or_404(Organization, pk=pk)
        data = s.StatusInputSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        services.change_organization_status(
            organization=organization, status=data.validated_data["status"], performed_by=request.user
        )
        return Response(s.PlatformOrganizationSerializer(organizations_queryset().get(pk=pk)).data)


# ------------------------------------------------------------------ comptes du personnel


def membership_of(organization_id, pk):
    return get_object_or_404(
        OrganizationStaff.objects.select_related("user", "organization"),
        pk=pk, organization_id=organization_id,
    )


class PlatformStaffListCreateView(AdminThrottleMixin, generics.ListAPIView):
    """GET : comptes du personnel d'une structure. POST : crée un responsable (ou un agent)."""

    permission_classes = PLATFORM
    pagination_class = None
    serializer_class = s.StaffMemberSerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return OrganizationStaff.objects.none()
        get_object_or_404(Organization, pk=self.kwargs["organization_id"])
        return OrganizationStaff.objects.filter(
            organization_id=self.kwargs["organization_id"]
        ).select_related("user").order_by("role", "user__last_name", "user__email")

    @extend_schema(request=s.StaffCreateSerializer, responses={201: s.StaffCredentialsSerializer})
    def post(self, request, organization_id):
        organization = get_object_or_404(Organization, pk=organization_id)
        data = s.StaffCreateSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        values = data.validated_data
        try:
            user, password = services.create_staff_account(
                organization=organization, email=values["email"],
                first_name=values["first_name"], last_name=values["last_name"], phone=values["phone"],
                role=values.get("role") or OrganizationStaff.Role.ADMIN,
                allow_existing=True, performed_by=request.user,
            )
        except DjangoValidationError as exc:
            return error_response(exc)
        membership = membership_of(organization.pk, OrganizationStaff.objects.get(user=user, organization=organization).pk)
        return Response(
            {"member": s.StaffMemberSerializer(membership).data, "temporary_password": password},
            status=status.HTTP_201_CREATED,
        )


class PlatformStaffDetailView(AdminThrottleMixin, APIView):
    """PATCH : active/désactive un compte ou change son rôle."""

    permission_classes = PLATFORM

    @extend_schema(request=s.StaffUpdateSerializer, responses=s.StaffMemberSerializer)
    def patch(self, request, organization_id, pk):
        membership = membership_of(organization_id, pk)
        data = s.StaffUpdateSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        services.update_staff_membership(
            membership=membership, performed_by=request.user,
            active=data.validated_data.get("active"), role=data.validated_data.get("role"),
        )
        return Response(s.StaffMemberSerializer(membership_of(organization_id, pk)).data)


class PlatformStaffResetPasswordView(AdminThrottleMixin, APIView):
    permission_classes = PLATFORM

    @extend_schema(request=None, responses=s.StaffCredentialsSerializer)
    def post(self, request, organization_id, pk):
        membership = membership_of(organization_id, pk)
        try:
            password = services.reset_staff_password(membership=membership, performed_by=request.user)
        except DjangoValidationError as exc:
            return error_response(exc)
        return Response({"member": s.StaffMemberSerializer(membership_of(organization_id, pk)).data, "temporary_password": password})


# ------------------------------------------------------------------ journal global


class PlatformAuditListView(generics.ListAPIView):
    """Journal de toutes les structures : ?organization=, ?action=, ?q=, ?date_from=, ?date_to= (AAAA-MM-JJ)."""

    permission_classes = PLATFORM
    pagination_class = PlatformPagination
    serializer_class = s.AuditLogSerializer

    def get_queryset(self):
        qs = AuditLog.objects.select_related("user", "organization")
        return filter_audit(qs, self.request.query_params, with_organization=True)


def filter_audit(qs, params, with_organization=False):
    if with_organization and params.get("organization"):
        qs = qs.filter(organization_id=params["organization"]) if str(params["organization"]).isdigit() else qs.none()
    if params.get("action"):
        qs = qs.filter(action=params["action"].upper())
    if params.get("entity_type"):
        qs = qs.filter(entity_type=params["entity_type"])
    if params.get("q"):
        term = params["q"]
        qs = qs.filter(Q(description__icontains=term) | Q(user__email__icontains=term) | Q(entity_id=term))
    for key, lookup in (("date_from", "created_at__date__gte"), ("date_to", "created_at__date__lte")):
        if params.get(key):
            day = parse_date(params[key])
            qs = qs.filter(**{lookup: day}) if day else qs.none()
    return qs
