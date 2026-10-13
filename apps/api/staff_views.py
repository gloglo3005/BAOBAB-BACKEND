"""API staff en lecture seule : /api/v1/staff/<organization_id>/...

Chaque requête est limitée à la structure du staff connecté
(request.organization, posée par IsStaffOfOrganization).
"""
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Count, Q
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import generics
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.catalog.models import Book, BookCopy
from apps.circulation.models import Loan
from apps.members.models import Member

from .permissions import STAFF_LIBRARY
from .staff_serializers import (
    BookDetailSerializer,
    BookListSerializer,
    CopySerializer,
    LoanSerializer,
    MemberSerializer,
)

TRUE_VALUES = {"1", "true", "yes", "oui"}


def safe_filter(qs, **lookup):
    """Un identifiant invalide dans l'URL donne une liste vide, pas une erreur 500."""
    try:
        return qs.filter(**lookup)
    except (ValueError, TypeError, DjangoValidationError):
        return qs.none()


class StaffPagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = "page_size"
    max_page_size = 100


class StaffReadMixin:
    permission_classes = STAFF_LIBRARY
    pagination_class = StaffPagination
    model = None

    def base_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return self.model.objects.none()
        return self.model.objects.filter(organization=self.request.organization)


def annotated_books(qs):
    return qs.select_related("category").prefetch_related("authors").annotate(
        copies_total=Count("copies", distinct=True),
        copies_available=Count(
            "copies",
            filter=Q(copies__status=BookCopy.Status.AVAILABLE),
            distinct=True,
        ),
    )


class BookListView(StaffReadMixin, generics.ListAPIView):
    model = Book
    serializer_class = BookListSerializer

    def get_queryset(self):
        qs = annotated_books(self.base_queryset())
        params = self.request.query_params
        q = params.get("q", "").strip()
        if q:
            matching = self.base_queryset().filter(
                Q(title__icontains=q)
                | Q(subtitle__icontains=q)
                | Q(isbn__icontains=q)
                | Q(authors__first_name__icontains=q)
                | Q(authors__last_name__icontains=q)
                | Q(copies__inventory_code__icontains=q)
            ).values("pk")
            qs = qs.filter(pk__in=matching)
        if params.get("category"):
            qs = safe_filter(qs, category_id=params["category"])
        if "active" in params:
            qs = qs.filter(active=params["active"].lower() in TRUE_VALUES)
        return qs.order_by("title", "pk")


class BookDetailView(StaffReadMixin, generics.RetrieveAPIView):
    model = Book
    serializer_class = BookDetailSerializer

    def get_queryset(self):
        return annotated_books(self.base_queryset()).prefetch_related("copies__location")


class CopyListView(StaffReadMixin, generics.ListAPIView):
    model = BookCopy
    serializer_class = CopySerializer

    def get_queryset(self):
        qs = self.base_queryset().select_related("book", "location")
        params = self.request.query_params
        q = params.get("q", "").strip()
        if q:
            qs = qs.filter(
                Q(inventory_code__icontains=q)
                | Q(book__title__icontains=q)
                | Q(book__isbn__icontains=q)
            )
        if params.get("status"):
            qs = qs.filter(status=params["status"].upper())
        if params.get("location"):
            qs = safe_filter(qs, location_id=params["location"])
        if params.get("book"):
            qs = safe_filter(qs, book_id=params["book"])
        return qs


class CopyDetailView(StaffReadMixin, generics.RetrieveAPIView):
    model = BookCopy
    serializer_class = CopySerializer

    def get_queryset(self):
        return self.base_queryset().select_related("book", "location")


def annotated_members(qs):
    return qs.annotate(
        active_loans=Count("loans", filter=Q(loans__status=Loan.Status.ACTIVE))
    )


class MemberListView(StaffReadMixin, generics.ListAPIView):
    model = Member
    serializer_class = MemberSerializer

    def get_queryset(self):
        qs = annotated_members(self.base_queryset())
        params = self.request.query_params
        q = params.get("q", "").strip()
        if q:
            qs = qs.filter(
                Q(member_number__icontains=q)
                | Q(first_name__icontains=q)
                | Q(last_name__icontains=q)
                | Q(phone__icontains=q)
            )
        if params.get("status"):
            qs = qs.filter(status=params["status"].upper())
        return qs.order_by("last_name", "first_name", "pk")


class MemberDetailView(StaffReadMixin, generics.RetrieveAPIView):
    model = Member
    serializer_class = MemberSerializer

    def get_queryset(self):
        return annotated_members(self.base_queryset())


class LoanListView(StaffReadMixin, generics.ListAPIView):
    model = Loan
    serializer_class = LoanSerializer

    def get_queryset(self):
        qs = self.base_queryset().select_related("book_copy__book", "member")
        params = self.request.query_params
        if params.get("status"):
            qs = qs.filter(status=params["status"].upper())
        if params.get("overdue", "").lower() in TRUE_VALUES:
            qs = qs.overdue()
        if params.get("member"):
            qs = safe_filter(qs, member_id=params["member"])
        if params.get("book_copy"):
            qs = safe_filter(qs, book_copy_id=params["book_copy"])
        return qs.order_by("-borrowed_at", "-pk")


class LoanDetailView(StaffReadMixin, generics.RetrieveAPIView):
    model = Loan
    serializer_class = LoanSerializer

    def get_queryset(self):
        return self.base_queryset().select_related("book_copy__book", "member")


class DashboardView(APIView):
    permission_classes = STAFF_LIBRARY

    @extend_schema(responses=OpenApiTypes.OBJECT)
    def get(self, request, organization_id):
        organization = request.organization

        def counts_by_status(model):
            counts = dict.fromkeys(model.Status.values, 0)
            rows = (
                model.objects.filter(organization=organization)
                .values("status")
                .annotate(n=Count("id"))
                .order_by()
            )
            for row in rows:
                counts[row["status"]] = row["n"]
            return counts

        copies = counts_by_status(BookCopy)
        members = counts_by_status(Member)
        loans = Loan.objects.filter(organization=organization)
        return Response({
            "books": {
                "active": Book.objects.filter(organization=organization, active=True).count()
            },
            "copies": {"total": sum(copies.values()), "by_status": copies},
            "members": {"total": sum(members.values()), "by_status": members},
            "loans": {"active": loans.active().count(), "overdue": loans.overdue().count()},
        })