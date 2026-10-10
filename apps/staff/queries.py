"""Requêtes de l'interface Staff. Toutes sont filtrées par structure."""
from django.db.models import Count, Q
from django.utils import timezone

from apps.catalog.models import Book, BookCopy
from apps.circulation.models import Loan
from apps.members.models import Member

MIN_QUERY_LENGTH = 2


def tokens(text):
    return [part for part in (text or "").split() if part]


def member_matches(organization, text):
    """Membres dont le numéro, le nom, le prénom ou le téléphone contient chaque mot."""
    queryset = Member.objects.filter(organization=organization)
    for token in tokens(text):
        queryset = queryset.filter(
            Q(member_number__icontains=token)
            | Q(first_name__icontains=token)
            | Q(last_name__icontains=token)
            | Q(phone__icontains=token)
        )
    return queryset


def book_matches(organization, text):
    """Livres dont le titre, l'ISBN ou un auteur contient chaque mot."""
    queryset = Book.objects.filter(organization=organization)
    for token in tokens(text):
        queryset = queryset.filter(
            Q(title__icontains=token)
            | Q(subtitle__icontains=token)
            | Q(isbn__icontains=token)
            | Q(authors__first_name__icontains=token)
            | Q(authors__last_name__icontains=token)
        )
    ids = queryset.values("pk")
    return Book.objects.filter(pk__in=ids)


def with_copy_counts(books):
    return books.annotate(
        copies_total=Count("copies", distinct=True),
        copies_available=Count(
            "copies",
            filter=Q(copies__status=BookCopy.Status.AVAILABLE),
            distinct=True,
        ),
    )


def current_loans_by_copy(organization, copies):
    """{id exemplaire: prêt en cours} pour les exemplaires donnés."""
    loans = (
        Loan.objects.for_organization(organization)
        .active()
        .filter(book_copy__in=[copy.pk for copy in copies])
        .select_related("member")
    )
    return {loan.book_copy_id: loan for loan in loans}


def search_all(organization, text, limit=10):
    """Recherche unique : livres, exemplaires et membres."""
    text = (text or "").strip()
    if len(text) < MIN_QUERY_LENGTH:
        return None

    books = list(with_copy_counts(book_matches(organization, text)).order_by("title")[:limit])

    copies = list(
        BookCopy.objects.filter(
            organization=organization, inventory_code__icontains=text
        )
        .select_related("book")
        .order_by("inventory_code")[:limit]
    )
    loans = current_loans_by_copy(organization, copies)
    for copy in copies:
        copy.current_loan = loans.get(copy.pk)

    members = list(
        member_matches(organization, text)
        .annotate(
            active_loans=Count(
                "loans", filter=Q(loans__status=Loan.Status.ACTIVE), distinct=True
            )
        )
        .order_by("last_name", "first_name")[:limit]
    )
    return {"books": books, "copies": copies, "members": members}


def dashboard_stats(organization):
    today = timezone.localdate()
    loans = Loan.objects.for_organization(organization)
    copies = BookCopy.objects.filter(organization=organization)
    return {
        "loans_today": loans.filter(borrowed_at__date=today).count(),
        "returns_today": loans.filter(returned_at__date=today).count(),
        "overdue": loans.overdue().count(),
        "available": copies.filter(status=BookCopy.Status.AVAILABLE).count(),
        "borrowed": copies.filter(status=BookCopy.Status.BORROWED).count(),
        "lost": copies.filter(status=BookCopy.Status.LOST).count(),
        "damaged": copies.filter(status=BookCopy.Status.DAMAGED).count(),
        "recent_loans": list(
            loans.select_related("member", "book_copy__book").order_by("-updated_at")[:6]
        ),
    }
