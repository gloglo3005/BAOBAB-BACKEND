"""API du responsable de structure : /api/v1/staff/<organization_id>/...

Le responsable (rôle ADMIN) crée et désactive les agents de SA structure,
réinitialise leur mot de passe, règle la politique de prêt, met à jour les
informations de la structure et consulte son journal d'audit. Les agents (rôle
STAFF) peuvent seulement consulter les informations de la structure.
"""
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from drf_spectacular.utils import extend_schema
from rest_framework import generics, status
from rest_framework.generics import get_object_or_404
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit.models import AuditLog
from apps.audit.services import log_action
from apps.organizations import services
from apps.organizations.models import LoanPolicy, OrganizationStaff

from . import admin_serializers as s
from .coworking_views import error_response
from .permissions import IsReady, IsStaffOfOrganization, IsStructureAdmin
from .platform_views import AdminThrottleMixin, PlatformPagination, filter_audit

STAFF_ANY = [IsReady, IsStaffOfOrganization]
STAFF_ADMIN = [IsReady, IsStructureAdmin]
NOT_YOURS = "Vous ne pouvez gérer que les agents de votre structure."


def agent_of(request, pk):
    return get_object_or_404(
        OrganizationStaff.objects.select_related("user"), pk=pk, organization=request.organization
    )


class TeamListCreateView(AdminThrottleMixin, generics.ListAPIView):
    """GET : l'équipe. POST : crée un agent (nouveau compte, mot de passe temporaire affiché une fois)."""

    permission_classes = STAFF_ADMIN
    pagination_class = None
    serializer_class = s.StaffMemberSerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return OrganizationStaff.objects.none()
        return OrganizationStaff.objects.filter(
            organization=self.request.organization
        ).select_related("user").order_by("role", "user__last_name", "user__email")

    @extend_schema(request=s.StaffCreateSerializer, responses={201: s.StaffCredentialsSerializer})
    def post(self, request, organization_id):
        data = s.StaffCreateSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        values = data.validated_data
        try:
            user, password = services.create_staff_account(
                organization=request.organization, email=values["email"],
                first_name=values["first_name"], last_name=values["last_name"], phone=values["phone"],
                role=OrganizationStaff.Role.STAFF, allow_existing=False, performed_by=request.user,
            )
        except DjangoValidationError as exc:
            return error_response(exc)
        membership = OrganizationStaff.objects.select_related("user").get(user=user, organization=request.organization)
        return Response(
            {"member": s.StaffMemberSerializer(membership).data, "temporary_password": password},
            status=status.HTTP_201_CREATED,
        )


class TeamDetailView(AdminThrottleMixin, APIView):
    """PATCH : désactive ou réactive un agent."""

    permission_classes = STAFF_ADMIN

    @extend_schema(request=s.StaffUpdateSerializer, responses=s.StaffMemberSerializer)
    def patch(self, request, organization_id, pk):
        membership = agent_of(request, pk)
        if membership.pk == request.membership.pk:
            return Response({"detail": "Vous ne pouvez pas modifier votre propre compte."}, status=status.HTTP_409_CONFLICT)
        if not services.responsable_can_manage(membership):
            return Response({"detail": NOT_YOURS}, status=status.HTTP_403_FORBIDDEN)
        data = s.StaffUpdateSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        services.update_staff_membership(
            membership=membership, performed_by=request.user, active=data.validated_data.get("active")
        )
        return Response(s.StaffMemberSerializer(agent_of(request, pk)).data)


class TeamResetPasswordView(AdminThrottleMixin, APIView):
    permission_classes = STAFF_ADMIN

    @extend_schema(request=None, responses=s.StaffCredentialsSerializer)
    def post(self, request, organization_id, pk):
        membership = agent_of(request, pk)
        if membership.pk == request.membership.pk or not services.responsable_can_manage(membership):
            return Response({"detail": NOT_YOURS}, status=status.HTTP_403_FORBIDDEN)
        try:
            password = services.reset_staff_password(membership=membership, performed_by=request.user)
        except DjangoValidationError as exc:
            return error_response(exc)
        return Response({"member": s.StaffMemberSerializer(agent_of(request, pk)).data, "temporary_password": password})


class StructureOrganizationView(AdminThrottleMixin, generics.RetrieveUpdateAPIView):
    """Informations de la structure : lisibles par tout le personnel, modifiables par le responsable."""

    serializer_class = s.StructureOrganizationSerializer
    http_method_names = ["get", "patch", "head", "options"]

    def get_permissions(self):
        classes = STAFF_ADMIN if self.request.method == "PATCH" else STAFF_ANY
        return [c() for c in classes]

    def get_object(self):
        return self.request.organization

    def perform_update(self, serializer):
        with transaction.atomic():
            organization = serializer.save()
            log_action(
                organization=organization, action=AuditLog.Action.ORGANIZATION_UPDATED,
                entity=organization, user=self.request.user,
                description="Informations de la structure modifiées.",
                metadata={"fields": sorted(serializer.validated_data)},
            )


class LoanPolicyView(AdminThrottleMixin, generics.RetrieveUpdateAPIView):
    """Politique de prêt : lisible par tout le personnel, modifiable par le responsable."""

    serializer_class = s.LoanPolicySerializer
    http_method_names = ["get", "patch", "head", "options"]

    def get_permissions(self):
        classes = STAFF_ADMIN if self.request.method == "PATCH" else STAFF_ANY
        return [c() for c in classes]

    def get_object(self):
        policy, _ = LoanPolicy.objects.get_or_create(organization=self.request.organization)
        return policy

    def perform_update(self, serializer):
        with transaction.atomic():
            policy = serializer.save()
            log_action(
                organization=policy.organization, action=AuditLog.Action.LOAN_POLICY_UPDATED,
                entity=policy, user=self.request.user, description="Politique de prêt modifiée.",
                metadata={k: v for k, v in serializer.validated_data.items()},
            )


class StructureAuditListView(generics.ListAPIView):
    """Journal d'audit de la structure (responsable) : ?action=, ?q=, ?date_from=, ?date_to="""

    permission_classes = STAFF_ADMIN
    pagination_class = PlatformPagination
    serializer_class = s.AuditLogSerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return AuditLog.objects.none()
        qs = AuditLog.objects.filter(organization=self.request.organization).select_related("user", "organization")
        return filter_audit(qs, self.request.query_params)
