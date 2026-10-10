"""Requêtes de l'interface Lecteur. Toutes partent d'une fiche Member du lecteur."""
from django.db.models import Count, F, Q
from django.utils import timezone

from apps.catalog.models import Book, BookCopy, Category
from apps.circulation.models import Loan
from apps.members.models import Member
from apps.staff.queries import book_matches, with_copy_counts


def my_memberships(user):
    """Fiches du lecteur, avec le nombre de prêts en cours et en retard."""
    today = timezone.localdate()
    active = Q(loans__status=Loan.Status.ACTIVE)
    return (
        Member.objects.filter(user=user)
        .select_related("organization")
        .annotate(
            active_loans=Count("loans", filter=active),
            overdue_loans=Count("loans", filter=active & Q(loans__due_date__lt=today)),
        )
        .order_by("organization__name")
    )


def catalog_books(organization, text="", category_id=None, only_available=False):
    books = (
        with_copy_counts(book_matches(organization, text))
        .filter(active=True)
        .prefetch_related("authors")
        .select_related("category")
        .order_by("title")
    )
    if category_id:
        books = books.filter(category_id=category_id)
    if only_available:
        books = books.filter(copies_available__gt=0)
    return books


def catalog_categories(organization):
    return Category.objects.filter(organization=organization, active=True)


def book_for_reader(organization, book_id):
    return (
        Book.objects.filter(organization=organization, active=True, pk=book_id)
        .select_related("category")
        .first()
    )


def available_by_location(book):
    """[(emplacement, nombre d'exemplaires disponibles)] — sans codes ni noms de membres."""
    rows = (
        BookCopy.objects.filter(
            organization=book.organization, book=book, status=BookCopy.Status.AVAILABLE
        )
        .values("location__name")
        .annotate(total=Count("pk"))
        .order_by("location__name")
    )
    return [(row["location__name"] or "", row["total"]) for row in rows]


def member_loans(member, kind):
    loans = Loan.objects.filter(
        organization=member.organization, member=member
    ).select_related("book_copy__book").prefetch_related("book_copy__book__authors")
    if kind == "overdue":
        return loans.overdue().order_by("due_date")
    if kind == "active":
        return loans.active().order_by("due_date")
    return loans.exclude(status=Loan.Status.ACTIVE).order_by(
        F("returned_at").desc(nulls_last=True), "-borrowed_at"
    )
