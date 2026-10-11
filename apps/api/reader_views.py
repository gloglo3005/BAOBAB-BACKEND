"""API lecteur : /api/v1/reader/...

Même logique que l'interface /reader/ : les requêtes viennent de
apps.reader.queries et les adhésions des services apps.members.services.
Un lecteur ne voit que ses propres données, et jamais de code d'inventaire.
"""
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Q
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import generics, serializers, status
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.catalog.models import Book, Category
from apps.circulation.models import Loan
from apps.members.models import Member
from apps.members.services import redeem_link_code, request_membership
from apps.organizations.models import Organization
from apps.reader import queries

from .permissions import READER_LIBRARY, IsReady
from .reader_serializers import (
    LinkCodeInputSerializer,
    MembershipRequestSerializer,
    ReaderMembershipSerializer,
    OrganizationPublicSerializer,
    ReaderBookDetailSerializer,
    ReaderBookListSerializer,
    ReaderCategorySerializer,
    ReaderLoanSerializer,
)
from .staff_members_views import refuse_validation
from .throttles import LinkCodeRateThrottle

TRUE_VALUES = {"1", "true", "yes", "oui"}
LOAN_KINDS = ("active", "overdue", "history")


class ReaderPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100


def not_found(message):
    return Response({"detail": message}, status=status.HTTP_404_NOT_FOUND)


def fake_schema_view(view):
    return getattr(view, "swagger_fake_view", False)


def membership_data(user, member):
    """La fiche avec ses compteurs de prêts, comme dans la liste des adhésions."""
    return ReaderMembershipSerializer(queries.my_memberships(user).get(pk=member.pk)).data


# ------------------------------------------------------------------ sans fiche de membre


class ReaderOrganizationListView(generics.ListAPIView):
    """Structures où l'on peut s'inscrire (actives, avec le module bibliothèque)."""

    permission_classes = [IsReady]
    pagination_class = ReaderPagination
    serializer_class = OrganizationPublicSerializer

    def get_queryset(self):
        qs = Organization.objects.filter(
            status=Organization.Status.ACTIVE, library_module=True
        ).order_by("name")
        q = self.request.query_params.get("q", "").strip()
        if q:
            qs = qs.filter(Q(name__icontains=q) | Q(city__icontains=q))
        return qs

    def get_serializer_context(self):
        context = super().get_serializer_context()
        user = getattr(self.request, "user", None)
        if user is not None and user.is_authenticated:
            context["mine"] = {
                member.organization_id: member
                for member in Member.objects.filter(user=user)
            }
        return context


class MembershipListCreateView(generics.ListAPIView):
    """GET : mes adhésions. POST : demande à rejoindre une structure (en attente)."""

    permission_classes = [IsReady]
    pagination_class = None
    serializer_class = ReaderMembershipSerializer

    def get_queryset(self):
        if fake_schema_view(self):
            return Member.objects.none()
        return queries.my_memberships(self.request.user)

    @extend_schema(request=MembershipRequestSerializer, responses={201: ReaderMembershipSerializer})
    def post(self, request):
        data = MembershipRequestSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        try:
            organization = Organization.objects.filter(
                pk=data.validated_data["organization"],
                status=Organization.Status.ACTIVE,
                library_module=True,
            ).first()
        except (ValueError, TypeError, DjangoValidationError):
            organization = None
        if organization is None:
            return not_found("Structure introuvable.")
        try:
            member = request_membership(user=request.user, organization=organization)
        except DjangoValidationError as exc:
            return refuse_validation(exc)
        return Response(membership_data(request.user, member), status=status.HTTP_201_CREATED)


class LinkCodeRedeemView(APIView):
    """Relie mon compte à ma fiche de membre grâce à un code remis au comptoir."""

    permission_classes = [IsReady]
    throttle_classes = [LinkCodeRateThrottle]

    @extend_schema(request=LinkCodeInputSerializer, responses=ReaderMembershipSerializer)
    def post(self, request):
        data = LinkCodeInputSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        try:
            member = redeem_link_code(user=request.user, code=data.validated_data["code"])
        except DjangoValidationError as exc:
            return Response(
                {"detail": " ".join(exc.messages)}, status=status.HTTP_400_BAD_REQUEST
            )
        return Response(membership_data(request.user, member))


# ------------------------------------------------------------------ avec une fiche de membre


class ReaderSummaryView(APIView):
    """Mon statut dans cette bibliothèque, mes prêts en cours et en retard."""

    permission_classes = READER_LIBRARY

    @extend_schema(responses=OpenApiTypes.OBJECT)
    def get(self, request, member_id):
        member = request.member
        organization = request.organization
        active = queries.member_loans(member, "active")
        return Response({
            "member": {
                "id": member.pk,
                "member_number": member.member_number,
                "first_name": member.first_name,
                "last_name": member.last_name,
                "status": member.status,
                "joined_at": member.joined_at,
            },
            "organization": {"id": organization.pk, "name": organization.name},
            "loans": {
                "active": active.count(),
                "overdue": queries.member_loans(member, "overdue").count(),
            },
            "upcoming": ReaderLoanSerializer(list(active[:3]), many=True).data,
        })


class ReaderCategoryListView(generics.ListAPIView):
    """Catégories actives de la bibliothèque (pour filtrer le catalogue)."""

    permission_classes = READER_LIBRARY
    pagination_class = None
    serializer_class = ReaderCategorySerializer

    def get_queryset(self):
        if fake_schema_view(self):
            return Category.objects.none()
        return queries.catalog_categories(self.request.organization)


class ReaderBookListView(generics.ListAPIView):
    """Catalogue : recherche `q`, filtre `category`, `available=1`."""

    permission_classes = READER_LIBRARY
    pagination_class = ReaderPagination
    serializer_class = ReaderBookListSerializer

    def get_queryset(self):
        if fake_schema_view(self):
            return Book.objects.none()
        params = self.request.query_params
        category_id = None
        if params.get("category"):
            try:
                category_id = int(params["category"])
            except ValueError:
                return Book.objects.none()
        return queries.catalog_books(
            self.request.organization,
            params.get("q", "").strip(),
            category_id,
            params.get("available", "").lower() in TRUE_VALUES,
        )


class ReaderBookDetailView(APIView):
    """Détail d'un livre : disponibilité par emplacement et mes prêts de ce livre."""

    permission_classes = READER_LIBRARY

    @extend_schema(responses=ReaderBookDetailSerializer)
    def get(self, request, member_id, pk):
        try:
            book_id = int(pk)
        except ValueError:
            return not_found("Livre introuvable.")
        book = queries.book_for_reader(request.organization, book_id)
        if book is None:
            return not_found("Livre introuvable.")
        serializer = ReaderBookDetailSerializer(book, context={"member": request.member})
        return Response(serializer.data)


class ReaderLoanListView(generics.ListAPIView):
    """Mes prêts : `status=active` (défaut), `overdue` ou `history`."""

    permission_classes = READER_LIBRARY
    pagination_class = ReaderPagination
    serializer_class = ReaderLoanSerializer

    def get_queryset(self):
        if fake_schema_view(self):
            return Loan.objects.none()
        kind = self.request.query_params.get("status", "active").lower()
        if kind not in LOAN_KINDS:
            raise serializers.ValidationError(
                {"status": ["Valeurs possibles : active, overdue, history."]}
            )
        return queries.member_loans(self.request.member, kind)