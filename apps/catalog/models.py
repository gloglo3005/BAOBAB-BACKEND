from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.functions import Lower

from apps.organizations.base import OrganizationOwnedModel, check_same_organization


class Author(OrganizationOwnedModel):
    first_name = models.CharField("prénom", max_length=100, blank=True)
    last_name = models.CharField("nom", max_length=100)
    biography = models.TextField("biographie", blank=True)

    class Meta:
        verbose_name = "auteur"
        verbose_name_plural = "auteurs"
        ordering = ["last_name", "first_name"]

    def __str__(self):
        return f"{self.first_name} {self.last_name}".strip()


class Category(OrganizationOwnedModel):
    name = models.CharField("nom", max_length=100)
    description = models.TextField(blank=True)
    active = models.BooleanField("active", default=True)

    class Meta:
        verbose_name = "catégorie"
        verbose_name_plural = "catégories"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                Lower("name"), "organization", name="unique_category_name_per_organization"
            ),
        ]

    def __str__(self):
        return self.name


class Location(OrganizationOwnedModel):
    name = models.CharField("nom", max_length=100)
    description = models.TextField(blank=True)
    active = models.BooleanField("active", default=True)

    class Meta:
        verbose_name = "emplacement"
        verbose_name_plural = "emplacements"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                Lower("name"), "organization", name="unique_location_name_per_organization"
            ),
        ]

    def __str__(self):
        return self.name


class Book(OrganizationOwnedModel):
    title = models.CharField("titre", max_length=255)
    subtitle = models.CharField("sous-titre", max_length=255, blank=True)
    description = models.TextField(blank=True)
    isbn = models.CharField("ISBN", max_length=20, blank=True)
    publication_year = models.PositiveSmallIntegerField(
        "année de publication", null=True, blank=True
    )
    publisher = models.CharField("éditeur", max_length=150, blank=True)
    language = models.CharField("langue", max_length=50, blank=True)
    category = models.ForeignKey(
        Category, on_delete=models.PROTECT, null=True, blank=True,
        related_name="books",
    )
    authors = models.ManyToManyField(Author, blank=True, related_name="books")
    active = models.BooleanField("actif", default=True)

    class Meta:
        verbose_name = "livre"
        verbose_name_plural = "livres"
        ordering = ["title"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "isbn"],
                condition=~models.Q(isbn=""),
                name="unique_isbn_per_organization",
            ),
        ]

    def __str__(self):
        return self.title

    def clean(self):
        super().clean()
        check_same_organization(self, category=self.category)


class BookCopy(OrganizationOwnedModel):
    class Status(models.TextChoices):
        AVAILABLE = "AVAILABLE", "Disponible"
        BORROWED = "BORROWED", "Emprunté"
        LOST = "LOST", "Perdu"
        DAMAGED = "DAMAGED", "Endommagé"
        UNAVAILABLE = "UNAVAILABLE", "Indisponible"

    class Condition(models.TextChoices):
        NEW = "NEW", "Neuf"
        GOOD = "GOOD", "Bon"
        FAIR = "FAIR", "Passable"
        POOR = "POOR", "Mauvais"

    book = models.ForeignKey(
        Book, on_delete=models.PROTECT, related_name="copies"
    )
    inventory_code = models.CharField("code d'inventaire", max_length=50)
    location = models.ForeignKey(
        Location, on_delete=models.PROTECT, null=True, blank=True,
        related_name="copies",
    )
    status = models.CharField(
        "statut", max_length=20, choices=Status.choices, default=Status.AVAILABLE
    )
    condition = models.CharField(
        "état", max_length=20, choices=Condition.choices, default=Condition.GOOD
    )
    acquisition_date = models.DateField("date d'acquisition", null=True, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        verbose_name = "exemplaire"
        verbose_name_plural = "exemplaires"
        ordering = ["inventory_code"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "inventory_code"],
                name="unique_inventory_code_per_organization",
            ),
        ]

    def __str__(self):
        return f"{self.inventory_code} - {self.book}"

    def clean(self):
        super().clean()
        # Si la structure n'est pas renseignée, on reprend celle du livre.
        if self.organization_id is None and self.book_id:
            self.organization_id = self.book.organization_id

        check_same_organization(
            self,
            book=self.book if self.book_id else None,
            location=self.location,
        )

        if self.organization_id and self.inventory_code:
            duplicates = BookCopy.objects.filter(
                organization_id=self.organization_id, inventory_code=self.inventory_code
            ).exclude(pk=self.pk)
            if duplicates.exists():
                raise ValidationError(
                    {"inventory_code": "Ce code existe déjà dans cette structure."}
                )