from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.catalog.models import Book, BookCopy, Category, Location
from apps.circulation.models import Loan
from apps.members.models import Member


class CategoryMiniSerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = ["id", "name"]


class LocationMiniSerializer(serializers.ModelSerializer):
    class Meta:
        model = Location
        fields = ["id", "name"]


class BookMiniSerializer(serializers.ModelSerializer):
    class Meta:
        model = Book
        fields = ["id", "title"]


class CopyInlineSerializer(serializers.ModelSerializer):
    location = LocationMiniSerializer(read_only=True)

    class Meta:
        model = BookCopy
        fields = [
            "id", "inventory_code", "location", "status", "condition",
            "acquisition_date", "notes",
        ]


class CopySerializer(CopyInlineSerializer):
    book = BookMiniSerializer(read_only=True)

    class Meta(CopyInlineSerializer.Meta):
        fields = ["id", "book"] + CopyInlineSerializer.Meta.fields[1:]


class BookListSerializer(serializers.ModelSerializer):
    category = CategoryMiniSerializer(read_only=True)
    authors = serializers.SerializerMethodField()
    copies_total = serializers.IntegerField(read_only=True)
    copies_available = serializers.IntegerField(read_only=True)

    class Meta:
        model = Book
        fields = [
            "id", "title", "subtitle", "isbn", "publication_year", "publisher",
            "language", "category", "authors", "active",
            "copies_total", "copies_available",
        ]

    @extend_schema_field(serializers.ListField(child=serializers.CharField()))
    def get_authors(self, obj):
        return [str(author) for author in obj.authors.all()]


class BookDetailSerializer(BookListSerializer):
    copies = CopyInlineSerializer(many=True, read_only=True)

    class Meta(BookListSerializer.Meta):
        fields = BookListSerializer.Meta.fields + ["description", "copies"]


class MemberSerializer(serializers.ModelSerializer):
    active_loans = serializers.IntegerField(read_only=True)

    class Meta:
        model = Member
        fields = [
            "id", "member_number", "first_name", "last_name", "phone",
            "status", "joined_at", "active_loans",
        ]


class LoanSerializer(serializers.ModelSerializer):
    book_copy = serializers.PrimaryKeyRelatedField(read_only=True)
    member = serializers.PrimaryKeyRelatedField(read_only=True)
    inventory_code = serializers.CharField(source="book_copy.inventory_code", read_only=True)
    book_title = serializers.CharField(source="book_copy.book.title", read_only=True)
    member_number = serializers.CharField(source="member.member_number", read_only=True)
    member_name = serializers.SerializerMethodField()
    is_overdue = serializers.BooleanField(read_only=True)
    days_overdue = serializers.IntegerField(read_only=True)

    class Meta:
        model = Loan
        fields = [
            "id", "book_copy", "inventory_code", "book_title", "member",
            "member_number", "member_name", "borrowed_at", "due_date",
            "returned_at", "status", "renewal_count", "is_overdue",
            "days_overdue", "notes",
        ]

    def get_member_name(self, obj) -> str:
        return f"{obj.member.first_name} {obj.member.last_name}".strip()