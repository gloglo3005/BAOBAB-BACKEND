from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.catalog.models import Book, Category
from apps.circulation.models import Loan
from apps.members.models import Member
from apps.organizations.models import Organization
from apps.reader import queries


class OrganizationMiniSerializer(serializers.ModelSerializer):
    class Meta:
        model = Organization
        fields = ["id", "name"]


class OrganizationPublicSerializer(serializers.ModelSerializer):
    my_membership = serializers.SerializerMethodField()

    class Meta:
        model = Organization
        fields = ["id", "name", "description", "city", "country", "my_membership"]

    @extend_schema_field(OpenApiTypes.OBJECT)
    def get_my_membership(self, obj):
        member = self.context.get("mine", {}).get(obj.pk)
        if member is None:
            return None
        return {"id": member.pk, "status": member.status}


class ReaderMembershipSerializer(serializers.ModelSerializer):
    organization = OrganizationMiniSerializer(read_only=True)
    active_loans = serializers.IntegerField(read_only=True)
    overdue_loans = serializers.IntegerField(read_only=True)

    class Meta:
        model = Member
        fields = [
            "id", "organization", "member_number", "first_name", "last_name",
            "status", "joined_at", "active_loans", "overdue_loans",
        ]


class LinkCodeInputSerializer(serializers.Serializer):
    code = serializers.CharField(max_length=20)


class MembershipRequestSerializer(serializers.Serializer):
    organization = serializers.CharField(help_text="Identifiant de la structure.")


class ReaderCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = ["id", "name"]


class ReaderLoanSerializer(serializers.ModelSerializer):
    """Un prêt vu par son lecteur : jamais de code d'inventaire ni de note du staff."""

    book = serializers.SerializerMethodField()
    authors = serializers.SerializerMethodField()
    is_overdue = serializers.BooleanField(read_only=True)
    days_overdue = serializers.IntegerField(read_only=True)
    days_left = serializers.SerializerMethodField()

    class Meta:
        model = Loan
        fields = [
            "id", "book", "authors", "borrowed_at", "due_date", "returned_at",
            "status", "renewal_count", "is_overdue", "days_overdue", "days_left",
        ]

    @extend_schema_field(OpenApiTypes.OBJECT)
    def get_book(self, obj):
        book = obj.book_copy.book
        return {"id": book.pk, "title": book.title}

    @extend_schema_field(serializers.ListField(child=serializers.CharField()))
    def get_authors(self, obj):
        return [str(author) for author in obj.book_copy.book.authors.all()]

    @extend_schema_field(OpenApiTypes.INT)
    def get_days_left(self, obj):
        if obj.status != Loan.Status.ACTIVE:
            return None
        return (obj.due_date - timezone.localdate()).days


class ReaderBookListSerializer(serializers.ModelSerializer):
    category = ReaderCategorySerializer(read_only=True)
    authors = serializers.SerializerMethodField()
    copies_available = serializers.IntegerField(read_only=True)

    class Meta:
        model = Book
        fields = [
            "id", "title", "subtitle", "language", "publication_year",
            "category", "authors", "copies_available",
        ]

    @extend_schema_field(serializers.ListField(child=serializers.CharField()))
    def get_authors(self, obj):
        return [str(author) for author in obj.authors.all()]


class ReaderBookDetailSerializer(serializers.ModelSerializer):
    """Détail d'un livre : disponibilité par emplacement, sans codes d'inventaire."""

    category = ReaderCategorySerializer(read_only=True)
    authors = serializers.SerializerMethodField()
    availability = serializers.SerializerMethodField()
    available_total = serializers.SerializerMethodField()
    my_loans = serializers.SerializerMethodField()

    class Meta:
        model = Book
        fields = [
            "id", "title", "subtitle", "description", "language", "publication_year",
            "publisher", "isbn", "category", "authors",
            "availability", "available_total", "my_loans",
        ]

    def _availability(self, obj):
        if not hasattr(obj, "_reader_availability"):
            obj._reader_availability = queries.available_by_location(obj)
        return obj._reader_availability

    @extend_schema_field(serializers.ListField(child=serializers.CharField()))
    def get_authors(self, obj):
        return [str(author) for author in obj.authors.all()]

    @extend_schema_field(serializers.ListField(child=serializers.DictField()))
    def get_availability(self, obj):
        return [
            {"location": name, "available": count}
            for name, count in self._availability(obj)
        ]

    @extend_schema_field(OpenApiTypes.INT)
    def get_available_total(self, obj):
        return sum(count for _, count in self._availability(obj))

    @extend_schema_field(serializers.ListField(child=serializers.DictField()))
    def get_my_loans(self, obj):
        member = self.context["member"]
        loans = queries.member_loans(member, "active").filter(book_copy__book=obj)
        return ReaderLoanSerializer(loans, many=True, context=self.context).data