"""API staff en écriture, membres : inscription au comptoir, statut, code de rattachement.

Chaque route appelle les services de apps.members : transitions de statut,
numérotation et audit sont les mêmes que dans l'interface /staff/.
"""
from django.core.exceptions import ValidationError as DjangoValidationError
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.members.models import Member
from apps.members.services import (
    accept_member,
    create_link_code,
    reactivate_member,
    register_member,
    reject_member,
    suspend_member,
)

from .permissions import STAFF_LIBRARY
from .staff_serializers import MemberSerializer
from .staff_views import MemberListView, annotated_members


class MemberCreateSerializer(serializers.Serializer):
    first_name = serializers.CharField(max_length=100)
    last_name = serializers.CharField(max_length=100)
    phone = serializers.CharField(max_length=30, required=False, allow_blank=True, default="")
    member_number = serializers.CharField(
        max_length=50, required=False, allow_blank=True, default="",
        help_text="Laisser vide pour une numérotation automatique (MEM-0001...).",
    )


class LinkCodeSerializer(serializers.Serializer):
    code = serializers.CharField()
    expires_at = serializers.DateTimeField()
    member = serializers.CharField()


def refuse_validation(exc):
    """400 si un champ est invalide (détail par champ), sinon 409 (règle métier)."""
    if hasattr(exc, "error_dict"):
        return Response(exc.message_dict, status=status.HTTP_400_BAD_REQUEST)
    return Response({"detail": " ".join(exc.messages)}, status=status.HTTP_409_CONFLICT)


def get_member(request, pk):
    """Le membre, uniquement s'il appartient à la structure du staff connecté."""
    try:
        return Member.objects.filter(pk=pk, organization=request.organization).first()
    except (ValueError, TypeError, DjangoValidationError):
        return None


def member_not_found():
    return Response({"detail": "Membre introuvable."}, status=status.HTTP_404_NOT_FOUND)


def serialized(member):
    """Le membre avec son nombre de prêts en cours, comme dans les routes de lecture."""
    return MemberSerializer(annotated_members(Member.objects.filter(pk=member.pk)).get()).data


class MemberListCreateView(MemberListView):
    """GET : liste des membres (lecture). POST : inscription au comptoir."""

    @extend_schema(request=MemberCreateSerializer, responses={201: MemberSerializer})
    def post(self, request, organization_id):
        data = MemberCreateSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        try:
            member = register_member(
                organization=request.organization,
                first_name=data.validated_data["first_name"],
                last_name=data.validated_data["last_name"],
                phone=data.validated_data["phone"],
                member_number=data.validated_data["member_number"],
                performed_by=request.user,
            )
        except DjangoValidationError as exc:
            return refuse_validation(exc)
        return Response(serialized(member), status=status.HTTP_201_CREATED)


class MemberStatusView(APIView):
    """Base : change le statut d'un membre via un service."""

    permission_classes = STAFF_LIBRARY
    service = None

    @extend_schema(request=None, responses=MemberSerializer)
    def post(self, request, organization_id, pk):
        member = get_member(request, pk)
        if member is None:
            return member_not_found()
        try:
            member = self.service(member, performed_by=request.user)
        except DjangoValidationError as exc:
            return refuse_validation(exc)
        return Response(serialized(member))


class MemberAcceptView(MemberStatusView):
    """Accepte un membre en attente, ou un membre refusé."""

    service = staticmethod(accept_member)


class MemberRejectView(MemberStatusView):
    """Refuse un membre en attente."""

    service = staticmethod(reject_member)


class MemberSuspendView(MemberStatusView):
    """Suspend un membre accepté."""

    service = staticmethod(suspend_member)


class MemberReactivateView(MemberStatusView):
    """Réactive un membre suspendu."""

    service = staticmethod(reactivate_member)


class MemberLinkCodeView(APIView):
    """Génère un code de rattachement (usage unique, valable 7 jours)."""

    permission_classes = STAFF_LIBRARY

    @extend_schema(request=None, responses={201: LinkCodeSerializer})
    def post(self, request, organization_id, pk):
        member = get_member(request, pk)
        if member is None:
            return member_not_found()
        try:
            link_code = create_link_code(member=member, created_by=request.user)
        except DjangoValidationError as exc:
            return refuse_validation(exc)
        return Response(
            {
                "code": link_code.code,
                "expires_at": link_code.expires_at,
                "member": str(member.pk),
            },
            status=status.HTTP_201_CREATED,
        )