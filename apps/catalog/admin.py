from django import forms
from django.contrib import admin

from .models import Author, Book, BookCopy, Category, Location
from .services import record_book_created, record_copy_created


class OrganizationOwnedAdmin(admin.ModelAdmin):
    """Base commune : filtre par structure, et la structure ne change plus après création."""

    list_filter = ("organization",)
    autocomplete_fields = ("organization",)

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))
        if obj:
            fields.append("organization")
        return fields


@admin.register(Author)
class AuthorAdmin(OrganizationOwnedAdmin):
    list_display = ("__str__", "organization")
    search_fields = ("first_name", "last_name")


@admin.register(Category)
class CategoryAdmin(OrganizationOwnedAdmin):
    list_display = ("name", "organization", "active")
    list_filter = ("organization", "active")
    search_fields = ("name",)


@admin.register(Location)
class LocationAdmin(OrganizationOwnedAdmin):
    list_display = ("name", "organization", "active")
    list_filter = ("organization", "active")
    search_fields = ("name",)


class BookAdminForm(forms.ModelForm):
    class Meta:
        model = Book
        fields = "__all__"

    def clean(self):
        cleaned = super().clean()
        organization = cleaned.get("organization")
        organization_id = organization.id if organization else self.instance.organization_id
        authors = cleaned.get("authors")
        if organization_id and authors:
            if any(author.organization_id != organization_id for author in authors):
                self.add_error(
                    "authors",
                    "Tous les auteurs doivent appartenir à la même structure.",
                )
        return cleaned


class BookCopyInline(admin.TabularInline):
    model = BookCopy
    exclude = ("organization",)
    autocomplete_fields = ("location",)
    can_delete = False
    extra = 1


@admin.register(Book)
class BookAdmin(OrganizationOwnedAdmin):
    form = BookAdminForm
    list_display = ("title", "organization", "category", "active")
    list_filter = ("organization", "active")
    search_fields = ("title", "isbn")
    autocomplete_fields = ("organization", "category", "authors")
    list_select_related = ("organization", "category")

    def get_inlines(self, request, obj=None):
        # Les exemplaires s'ajoutent une fois le livre créé.
        return [BookCopyInline] if obj else []

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if not change:
            record_book_created(obj, request.user)

    def save_formset(self, request, form, formset, change):
        if formset.model is not BookCopy:
            return super().save_formset(request, form, formset, change)
        for copy in formset.save(commit=False):
            is_new = copy.pk is None
            copy.save()
            if is_new:
                record_copy_created(copy, request.user)
        formset.save_m2m()


@admin.register(BookCopy)
class BookCopyAdmin(OrganizationOwnedAdmin):
    list_display = ("inventory_code", "book", "organization", "status", "condition")
    list_filter = ("organization", "status", "condition")
    search_fields = ("inventory_code", "book__title")
    autocomplete_fields = ("organization", "book", "location")
    list_select_related = ("organization", "book")

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if not change:
            record_copy_created(obj, request.user)