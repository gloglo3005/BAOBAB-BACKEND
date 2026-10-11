"""API staff en écriture, catalogue : livres, exemplaires, catégories, emplacements.

Chaque route appelle les services de apps.catalog : règles métier, verrous et
audit sont les mêmes que dans l'interface /staff/.
"""
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from drf_spectacular.utils import extend_schema
from rest_framework import generics, serializers, status
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.catalog.models import Book, BookCopy, Category, Location
from apps.catalog.services import (
    MANUAL_COPY_STATUSES,
    add_copies,
    archive_book,
    create_book,
    create_reference,
    get_or_create_authors,
    move_copy,
    rename_reference,
    restore_book,
    set_copy_status,
    set_reference_active,
    update_book,
    update_copy,
)

from .permissions import STAFF_LIBRARY
from .staff_members_views import refuse_validation
from .staff_serializers import BookDetailSerializer, CopySerializer
from .staff_views import (
    TRUE_VALUES,
    BookDetailView,
    BookListView,
    CopyDetailView,
    StaffReadMixin,
    annotated_books,
)

EDITABLE_BOOK_FIELDS = (
    "title", "subtitle", "isbn", "publication_year", "publisher", "language", "description",
)


# ------------------------------------------------------------------ sérialiseurs


class CategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = ["id", "name", "description", "active"]


class LocationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Location
        fields = ["id", "name", "description", "active"]


class BookWriteSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=255)
    subtitle = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")
    description = serializers.CharField(required=False, allow_blank=True, default="")
    isbn = serializers.CharField(max_length=20, required=False, allow_blank=True, default="")
    publication_year = serializers.IntegerField(
        required=False, allow_null=True, default=None, min_value=1, max_value=9999
    )
    publisher = serializers.CharField(max_length=150, required=False, allow_blank=True, default="")
    language = serializers.CharField(max_length=50, required=False, allow_blank=True, default="")
    category = serializers.CharField(
        required=False, allow_null=True, allow_blank=True, default=None,
        help_text="Identifiant d'une catégorie de la structure, ou null.",
    )
    authors = serializers.ListField(
        child=serializers.CharField(max_length=201), required=False, default=list,
        help_text="Un nom complet par élément, par exemple « Chinua Achebe ».",
    )


class CopiesCreateSerializer(serializers.Serializer):
    codes = serializers.ListField(
        child=serializers.CharField(max_length=50), allow_empty=False,
        help_text="Codes d'inventaire des exemplaires à ajouter (200 au plus).",
    )
    location = serializers.CharField(required=False, allow_null=True, default=None)
    condition = serializers.ChoiceField(
        choices=BookCopy.Condition.choices, required=False, default=BookCopy.Condition.GOOD
    )


class CopyUpdateSerializer(serializers.Serializer):
    condition = serializers.ChoiceField(choices=BookCopy.Condition.choices, required=False)
    notes = serializers.CharField(required=False, allow_blank=True)
    acquisition_date = serializers.DateField(required=False, allow_null=True)


class CopyMoveSerializer(serializers.Serializer):
    location = serializers.CharField(
        allow_null=True, help_text="Identifiant de l'emplacement, ou null pour le retirer."
    )


class CopyStatusSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=[(s.value, s.label) for s in MANUAL_COPY_STATUSES])


class ReferenceCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=100)
    description = serializers.CharField(required=False, allow_blank=True, default="")


class ReferenceUpdateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=100, required=False)
    active = serializers.BooleanField(required=False)


# ------------------------------------------------------------------ outils


class FieldRefused(Exception):
    """Un identifiant du corps de la requête ne désigne rien dans cette structure."""

    def __init__(self, field, message):
        super().__init__(message)
        self.field = field
        self.message = message


def field_refused(exc):
    return Response({exc.field: [exc.message]}, status=status.HTTP_400_BAD_REQUEST)


def not_found(message):
    return Response({"detail": message}, status=status.HTTP_404_NOT_FOUND)


def get_object_or_none(model, request, pk):
    """L'objet, uniquement s'il appartient à la structure du staff connecté."""
    try:
        return model.objects.filter(pk=pk, organization=request.organization).first()
    except (ValueError, TypeError, DjangoValidationError):
        return None


def lookup(model, request, value, field, label):
    obj = get_object_or_none(model, request, value)
    if obj is None:
        raise FieldRefused(field, f"{label} introuvable.")
    return obj


def optional_lookup(model, request, value, field, label):
    if value in (None, ""):
        return None
    return lookup(model, request, value, field, label)


def book_data(book):
    qs = annotated_books(Book.objects.filter(pk=book.pk)).prefetch_related("copies__location")
    return BookDetailSerializer(qs.get()).data


def copy_data(copy):
    return CopySerializer(
        BookCopy.objects.select_related("book", "location").get(pk=copy.pk)
    ).data


# ------------------------------------------------------------------ livres


class BookListCreateView(BookListView):
    """GET : liste des livres (lecture). POST : crée un livre."""

    @extend_schema(request=BookWriteSerializer, responses={201: BookDetailSerializer})
    def post(self, request, organization_id):
        data = BookWriteSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        values = dict(data.validated_data)
        category_id = values.pop("category", None)
        author_names = values.pop("authors", [])
        title = values.pop("title")
        try:
            with transaction.atomic():
                category = optional_lookup(
                    Category, request, category_id, "category", "Catégorie"
                )
                authors = get_or_create_authors(request.organization, "\n".join(author_names))
                book = create_book(
                    organization=request.organization,
                    title=title,
                    authors=authors,
                    category=category,
                    performed_by=request.user,
                    **values,
                )
        except FieldRefused as exc:
            return field_refused(exc)
        except DjangoValidationError as exc:
            return refuse_validation(exc)
        return Response(book_data(book), status=status.HTTP_201_CREATED)


class BookDetailUpdateView(BookDetailView):
    """GET : détail d'un livre (lecture). PATCH : modifie les champs envoyés."""

    @extend_schema(request=BookWriteSerializer, responses=BookDetailSerializer)
    def patch(self, request, organization_id, pk):
        book = get_object_or_none(Book, request, pk)
        if book is None:
            return not_found("Livre introuvable.")
        data = BookWriteSerializer(data=request.data, partial=True)
        data.is_valid(raise_exception=True)
        values = dict(data.validated_data)
        fields = {name: values[name] for name in EDITABLE_BOOK_FIELDS if name in values}
        try:
            with transaction.atomic():
                if "category" in values:
                    fields["category"] = optional_lookup(
                        Category, request, values["category"], "category", "Catégorie"
                    )
                authors = None
                if "authors" in values:
                    authors = get_or_create_authors(
                        request.organization, "\n".join(values["authors"])
                    )
                book = update_book(
                    book=book, authors=authors, performed_by=request.user, **fields
                )
        except FieldRefused as exc:
            return field_refused(exc)
        except DjangoValidationError as exc:
            return refuse_validation(exc)
        return Response(book_data(book))


class BookStateView(APIView):
    """Base : archive ou restaure un livre via un service."""

    permission_classes = STAFF_LIBRARY
    service = None

    @extend_schema(request=None, responses=BookDetailSerializer)
    def post(self, request, organization_id, pk):
        book = get_object_or_none(Book, request, pk)
        if book is None:
            return not_found("Livre introuvable.")
        try:
            book = self.service(book=book, performed_by=request.user)
        except DjangoValidationError as exc:
            return refuse_validation(exc)
        return Response(book_data(book))


class BookArchiveView(BookStateView):
    """Archive un livre (refusé si un exemplaire est encore emprunté)."""

    service = staticmethod(archive_book)


class BookRestoreView(BookStateView):
    """Remet un livre archivé dans le catalogue."""

    service = staticmethod(restore_book)


# ------------------------------------------------------------------ exemplaires


class BookCopiesCreateView(APIView):
    """Ajoute un ou plusieurs exemplaires à un livre (tout ou rien)."""

    permission_classes = STAFF_LIBRARY

    @extend_schema(
        request=CopiesCreateSerializer, responses={201: CopySerializer(many=True)}
    )
    def post(self, request, organization_id, pk):
        book = get_object_or_none(Book, request, pk)
        if book is None:
            return not_found("Livre introuvable.")
        data = CopiesCreateSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        try:
            with transaction.atomic():
                location = optional_lookup(
                    Location, request, data.validated_data["location"],
                    "location", "Emplacement",
                )
                copies = add_copies(
                    book=book,
                    codes=data.validated_data["codes"],
                    location=location,
                    condition=data.validated_data["condition"],
                    performed_by=request.user,
                )
        except FieldRefused as exc:
            return field_refused(exc)
        except DjangoValidationError as exc:
            return refuse_validation(exc)
        return Response(
            [copy_data(copy) for copy in copies], status=status.HTTP_201_CREATED
        )


class CopyDetailUpdateView(CopyDetailView):
    """GET : détail d'un exemplaire (lecture). PATCH : état, notes, date d'acquisition."""

    @extend_schema(request=CopyUpdateSerializer, responses=CopySerializer)
    def patch(self, request, organization_id, pk):
        copy = get_object_or_none(BookCopy, request, pk)
        if copy is None:
            return not_found("Exemplaire introuvable.")
        data = CopyUpdateSerializer(data=request.data, partial=True)
        data.is_valid(raise_exception=True)
        try:
            copy = update_copy(copy=copy, **data.validated_data)
        except DjangoValidationError as exc:
            return refuse_validation(exc)
        return Response(copy_data(copy))


class CopyMoveView(APIView):
    """Change l'emplacement d'un exemplaire (location null pour le retirer)."""

    permission_classes = STAFF_LIBRARY

    @extend_schema(request=CopyMoveSerializer, responses=CopySerializer)
    def post(self, request, organization_id, pk):
        copy = get_object_or_none(BookCopy, request, pk)
        if copy is None:
            return not_found("Exemplaire introuvable.")
        data = CopyMoveSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        try:
            location = optional_lookup(
                Location, request, data.validated_data["location"], "location", "Emplacement"
            )
            copy = move_copy(copy=copy, location=location, performed_by=request.user)
        except FieldRefused as exc:
            return field_refused(exc)
        except DjangoValidationError as exc:
            return refuse_validation(exc)
        return Response(copy_data(copy))


class CopyStatusView(APIView):
    """Change à la main le statut d'un exemplaire (hors prêt et retour)."""

    permission_classes = STAFF_LIBRARY

    @extend_schema(request=CopyStatusSerializer, responses=CopySerializer)
    def post(self, request, organization_id, pk):
        copy = get_object_or_none(BookCopy, request, pk)
        if copy is None:
            return not_found("Exemplaire introuvable.")
        data = CopyStatusSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        try:
            copy = set_copy_status(
                copy=copy, new_status=data.validated_data["status"], performed_by=request.user
            )
        except DjangoValidationError as exc:
            return refuse_validation(exc)
        return Response(copy_data(copy))


# ------------------------------------------------------------------ catégories et emplacements


class ReferencePagination(PageNumberPagination):
    page_size = 100
    page_size_query_param = "page_size"
    max_page_size = 200


class ReferenceListCreateView(StaffReadMixin, generics.ListAPIView):
    """GET : liste (filtres q, active). POST : crée une entrée."""

    pagination_class = ReferencePagination
    kind = None

    def get_queryset(self):
        qs = self.base_queryset()
        params = self.request.query_params
        if "active" in params:
            qs = qs.filter(active=params["active"].lower() in TRUE_VALUES)
        q = params.get("q", "").strip()
        if q:
            qs = qs.filter(name__icontains=q)
        return qs

    @extend_schema(request=ReferenceCreateSerializer)
    def post(self, request, organization_id):
        data = ReferenceCreateSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        try:
            item = create_reference(
                kind=self.kind,
                organization=request.organization,
                name=data.validated_data["name"],
                description=data.validated_data["description"],
            )
        except DjangoValidationError as exc:
            return refuse_validation(exc)
        return Response(self.get_serializer(item).data, status=status.HTTP_201_CREATED)


class ReferenceDetailView(StaffReadMixin, generics.RetrieveAPIView):
    """GET : détail. PATCH : renomme (name) et/ou (dés)active (active)."""

    @extend_schema(request=ReferenceUpdateSerializer)
    def patch(self, request, organization_id, pk):
        item = get_object_or_none(self.model, request, pk)
        if item is None:
            return not_found("Élément introuvable.")
        data = ReferenceUpdateSerializer(data=request.data, partial=True)
        data.is_valid(raise_exception=True)
        try:
            with transaction.atomic():
                if "name" in data.validated_data:
                    item = rename_reference(item=item, name=data.validated_data["name"])
                if "active" in data.validated_data:
                    item = set_reference_active(item=item, active=data.validated_data["active"])
        except DjangoValidationError as exc:
            return refuse_validation(exc)
        return Response(self.get_serializer(item).data)


class CategoryListCreateView(ReferenceListCreateView):
    model = Category
    kind = "category"
    serializer_class = CategorySerializer


class CategoryDetailView(ReferenceDetailView):
    model = Category
    serializer_class = CategorySerializer


class LocationListCreateView(ReferenceListCreateView):
    model = Location
    kind = "location"
    serializer_class = LocationSerializer


class LocationDetailView(ReferenceDetailView):
    model = Location
    serializer_class = LocationSerializer