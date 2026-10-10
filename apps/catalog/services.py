from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models.functions import Lower

from apps.audit.models import AuditLog
from apps.audit.services import log_action

from .models import Author, Book, BookCopy, Category, Location


def _check_authors(organization, authors):
    wrong = [author for author in authors if author.organization_id != organization.id]
    if wrong:
        raise ValidationError(
            {"authors": "Tous les auteurs doivent appartenir à la même structure."}
        )


MAX_COPIES_PER_BATCH = 200

EDITABLE_BOOK_FIELDS = (
    "title", "subtitle", "isbn", "publication_year", "publisher",
    "language", "description", "category",
)
BOOK_FIELD_LABELS = {
    "title": "titre", "subtitle": "sous-titre", "isbn": "ISBN",
    "publication_year": "année", "publisher": "éditeur", "language": "langue",
    "description": "description", "category": "catégorie", "authors": "auteurs",
    "active": "statut",
}
EDITABLE_COPY_FIELDS = ("condition", "notes", "acquisition_date")

# Statuts qu'un agent peut poser à la main. « Emprunté » n'en fait pas partie :
# seuls le prêt et le retour le font évoluer.
MANUAL_COPY_STATUSES = (
    BookCopy.Status.AVAILABLE,
    BookCopy.Status.UNAVAILABLE,
    BookCopy.Status.DAMAGED,
    BookCopy.Status.LOST,
)


def _check_isbn_unique(organization, isbn, exclude_pk=None):
    if not isbn:
        return
    duplicate = (
        Book.objects.filter(organization=organization, isbn=isbn)
        .exclude(pk=exclude_pk)
        .first()
    )
    if duplicate:
        raise ValidationError(
            f"Ce numéro ISBN existe déjà dans le catalogue : « {duplicate.title} »."
        )


def _display(value):
    """Valeur lisible et sérialisable pour le journal d'audit."""
    if value is None or isinstance(value, (str, int, bool)):
        return value
    return str(value)


def record_book_created(book, performed_by=None):
    """Trace la création d'un livre (services et admin). Dans une transaction."""
    log_action(
        organization=book.organization,
        user=performed_by,
        action=AuditLog.Action.BOOK_CREATED,
        entity=book,
        description=f"Livre « {book.title} » créé.",
        metadata={"title": book.title},
    )


def record_copy_created(copy, performed_by=None):
    """Trace la création d'un exemplaire (services et admin). Dans une transaction."""
    log_action(
        organization=copy.organization,
        user=performed_by,
        action=AuditLog.Action.COPY_CREATED,
        entity=copy,
        description=(
            f"Exemplaire {copy.inventory_code} ajouté à « {copy.book.title} »."
        ),
        metadata={
            "inventory_code": copy.inventory_code,
            "book": copy.book.title,
            "status": copy.status,
        },
    )


@transaction.atomic
def create_book(
    *, organization, title, authors=(), category=None, performed_by=None, **extra_fields
):
    """Crée un livre après avoir vérifié la cohérence avec la structure."""
    _check_authors(organization, authors)
    _check_isbn_unique(organization, extra_fields.get("isbn", ""))

    book = Book(organization=organization, title=title, category=category, **extra_fields)
    book.full_clean()
    book.save()
    book.authors.set(authors)
    record_book_created(book, performed_by)
    return book


@transaction.atomic
def add_copy(*, book, inventory_code, location=None, performed_by=None, **extra_fields):
    """Ajoute un exemplaire à un livre. La structure est toujours celle du livre."""
    if extra_fields.get("status") == BookCopy.Status.BORROWED:
        raise ValidationError(
            {"status": "Le statut « Emprunté » est réservé aux prêts."}
        )

    copy = BookCopy(
        organization=book.organization,
        book=book,
        inventory_code=inventory_code.strip(),
        location=location,
        **extra_fields,
    )
    copy.full_clean()
    copy.save()
    record_copy_created(copy, performed_by)
    return copy


# ------------------------------------------------------------------ auteurs


def split_author_name(line):
    """« Chinua Achebe » devient ("Chinua", "Achebe") ; un seul mot est un nom."""
    parts = line.split()
    if len(parts) == 1:
        return "", parts[0]
    return " ".join(parts[:-1]), parts[-1]


@transaction.atomic
def get_or_create_authors(organization, text):
    """Auteurs d'après un texte « un auteur par ligne » ; crée ceux qui manquent."""
    authors, seen = [], set()
    for raw_line in (text or "").splitlines():
        line = " ".join(raw_line.split())
        if not line:
            continue
        first_name, last_name = split_author_name(line)
        key = (first_name.lower(), last_name.lower())
        if key in seen:
            continue
        seen.add(key)

        author = Author.objects.filter(
            organization=organization,
            first_name__iexact=first_name,
            last_name__iexact=last_name,
        ).first()
        if author is None:
            author = Author(
                organization=organization, first_name=first_name, last_name=last_name
            )
            author.full_clean()
            author.save()
        authors.append(author)
    return authors


# ------------------------------------------------------------------ livres


@transaction.atomic
def update_book(*, book, authors=None, performed_by=None, **fields):
    """Modifie un livre et trace les anciennes et nouvelles valeurs.

    `authors` : liste d'auteurs de la structure, ou None pour ne pas y toucher.
    Rien n'est écrit (ni journal) s'il n'y a aucun changement.
    """
    unknown = set(fields) - set(EDITABLE_BOOK_FIELDS)
    if unknown:
        raise ValueError(f"Champs non modifiables : {', '.join(sorted(unknown))}")

    book = Book.objects.select_for_update().get(pk=book.pk)

    changes = {}
    for name, new_value in fields.items():
        old_value = getattr(book, name)
        if old_value != new_value:
            changes[name] = {"from": _display(old_value), "to": _display(new_value)}
            setattr(book, name, new_value)

    if authors is not None:
        _check_authors(book.organization, authors)
        old_names = sorted(str(author) for author in book.authors.all())
        new_names = sorted(str(author) for author in authors)
        if old_names != new_names:
            changes["authors"] = {"from": old_names, "to": new_names}

    if not changes:
        return book

    if "isbn" in changes:
        _check_isbn_unique(book.organization, book.isbn, exclude_pk=book.pk)
    book.full_clean()
    book.save()
    if "authors" in changes:
        book.authors.set(authors)

    labels = ", ".join(BOOK_FIELD_LABELS[name] for name in changes)
    log_action(
        organization=book.organization,
        user=performed_by,
        action=AuditLog.Action.BOOK_UPDATED,
        entity=book,
        description=f"Livre « {book.title} » modifié ({labels}).",
        metadata={"changes": changes},
    )
    return book


@transaction.atomic
def archive_book(*, book, performed_by=None):
    """Archive un livre (RG14 : on n'efface pas). Refusé si un exemplaire est emprunté."""
    book = Book.objects.select_for_update().get(pk=book.pk)
    if not book.active:
        raise ValidationError("Ce livre est déjà archivé.")

    borrowed = book.copies.filter(status=BookCopy.Status.BORROWED).count()
    if borrowed:
        raise ValidationError(
            f"Impossible d'archiver « {book.title} » : {borrowed} exemplaire(s) "
            "encore emprunté(s). Enregistrez d'abord les retours."
        )

    book.active = False
    book.save(update_fields=["active", "updated_at"])
    log_action(
        organization=book.organization,
        user=performed_by,
        action=AuditLog.Action.BOOK_ARCHIVED,
        entity=book,
        description=f"Livre « {book.title} » archivé.",
        metadata={"title": book.title},
    )
    return book


@transaction.atomic
def restore_book(*, book, performed_by=None):
    """Remet un livre archivé dans le catalogue."""
    book = Book.objects.select_for_update().get(pk=book.pk)
    if book.active:
        raise ValidationError("Ce livre n'est pas archivé.")

    book.active = True
    book.save(update_fields=["active", "updated_at"])
    log_action(
        organization=book.organization,
        user=performed_by,
        action=AuditLog.Action.BOOK_UPDATED,
        entity=book,
        description=f"Livre « {book.title} » restauré.",
        metadata={"changes": {"active": {"from": False, "to": True}}},
    )
    return book


# ------------------------------------------------------------------ exemplaires


@transaction.atomic
def add_copies(*, book, codes, location=None, condition=BookCopy.Condition.GOOD,
               performed_by=None):
    """Ajoute plusieurs exemplaires d'un coup : tout ou rien.

    Si un code est saisi deux fois ou existe déjà (majuscules et minuscules
    comptent pour pareil), aucun exemplaire n'est créé et le message liste
    les codes en cause.
    """
    book = Book.objects.select_for_update().get(pk=book.pk)
    if not book.active:
        raise ValidationError(
            "Ce livre est archivé : restaurez-le avant d'ajouter des exemplaires."
        )

    cleaned, seen, repeated = [], set(), []
    for raw in codes:
        code = (raw or "").strip()
        if not code:
            continue
        if code.lower() in seen:
            repeated.append(code)
            continue
        seen.add(code.lower())
        cleaned.append(code)

    if not cleaned:
        raise ValidationError("Saisissez au moins un code d'inventaire.")
    if len(cleaned) > MAX_COPIES_PER_BATCH:
        raise ValidationError(
            f"Au plus {MAX_COPIES_PER_BATCH} exemplaires à la fois."
        )

    existing = list(
        BookCopy.objects.filter(organization=book.organization)
        .annotate(lowered=Lower("inventory_code"))
        .filter(lowered__in=[code.lower() for code in cleaned])
        .values_list("inventory_code", flat=True)
    )

    problems = []
    if repeated:
        problems.append("saisis plusieurs fois : " + ", ".join(sorted(set(repeated))))
    if existing:
        problems.append("déjà utilisés : " + ", ".join(sorted(existing)))
    if problems:
        raise ValidationError("Aucun exemplaire créé. Codes " + " ; ".join(problems) + ".")

    return [
        add_copy(
            book=book, inventory_code=code, location=location,
            condition=condition, performed_by=performed_by,
        )
        for code in cleaned
    ]


@transaction.atomic
def update_copy(*, copy, **fields):
    """Modifie l'état physique, les notes ou la date d'acquisition d'un exemplaire."""
    unknown = set(fields) - set(EDITABLE_COPY_FIELDS)
    if unknown:
        raise ValueError(f"Champs non modifiables : {', '.join(sorted(unknown))}")

    copy = BookCopy.objects.select_for_update().get(pk=copy.pk)
    changed = []
    for name, new_value in fields.items():
        if getattr(copy, name) != new_value:
            setattr(copy, name, new_value)
            changed.append(name)
    if changed:
        copy.full_clean()
        copy.save(update_fields=changed + ["updated_at"])
    return copy


@transaction.atomic
def move_copy(*, copy, location, performed_by=None):
    """Change l'emplacement d'un exemplaire (ou le retire avec location=None)."""
    copy = BookCopy.objects.select_for_update().get(pk=copy.pk)
    if location is not None and location.organization_id != copy.organization_id:
        raise ValidationError("Cet emplacement n'existe pas dans cette structure.")

    new_id = location.pk if location else None
    if copy.location_id == new_id:
        return copy

    old_location = copy.location
    copy.location = location
    copy.save(update_fields=["location", "updated_at"])
    log_action(
        organization=copy.organization,
        user=performed_by,
        action=AuditLog.Action.COPY_MOVED,
        entity=copy,
        description=f"Exemplaire {copy.inventory_code} déplacé.",
        metadata={
            "inventory_code": copy.inventory_code,
            "location": {
                "from": _display(old_location),
                "to": _display(location),
            },
        },
    )
    return copy


@transaction.atomic
def set_copy_status(*, copy, new_status, performed_by=None):
    """Change à la main le statut d'un exemplaire (hors prêt et retour).

    Un exemplaire emprunté ne se modifie pas : on enregistre d'abord son retour.
    """
    if new_status not in MANUAL_COPY_STATUSES:
        raise ValidationError("Ce statut ne peut pas être choisi à la main.")

    copy = BookCopy.objects.select_for_update().get(pk=copy.pk)
    if copy.status == BookCopy.Status.BORROWED:
        raise ValidationError(
            "Cet exemplaire est emprunté : enregistrez d'abord son retour."
        )
    if copy.status == new_status:
        raise ValidationError("L'exemplaire a déjà ce statut.")

    previous = copy.status
    copy.status = new_status
    copy.save(update_fields=["status", "updated_at"])

    lost = new_status == BookCopy.Status.LOST
    log_action(
        organization=copy.organization,
        user=performed_by,
        action=(
            AuditLog.Action.COPY_MARKED_LOST if lost
            else AuditLog.Action.COPY_STATUS_CHANGED
        ),
        entity=copy,
        description=(
            f"Exemplaire {copy.inventory_code} déclaré perdu." if lost
            else f"Statut de l'exemplaire {copy.inventory_code} modifié."
        ),
        metadata={
            "inventory_code": copy.inventory_code,
            "copy_status": {"from": previous, "to": new_status},
        },
    )
    return copy


# ------------------------------------------------------------------ catégories et emplacements

REFERENCE_MODELS = {"category": (Category, "catégorie"), "location": (Location, "emplacement")}


def _check_reference_name(model, label, organization, name, exclude_pk=None):
    if not name:
        raise ValidationError("Le nom est obligatoire.")
    duplicate = (
        model.objects.filter(organization=organization, name__iexact=name)
        .exclude(pk=exclude_pk)
        .exists()
    )
    if duplicate:
        raise ValidationError(f"Cette {label} existe déjà." if label == "catégorie"
                              else f"Cet {label} existe déjà.")


@transaction.atomic
def create_reference(*, kind, organization, name, description=""):
    """Crée une catégorie (kind="category") ou un emplacement (kind="location")."""
    model, label = REFERENCE_MODELS[kind]
    name = " ".join((name or "").split())
    _check_reference_name(model, label, organization, name)
    item = model(organization=organization, name=name, description=(description or "").strip())
    item.full_clean()
    item.save()
    return item


@transaction.atomic
def rename_reference(*, item, name):
    model = type(item)
    label = "catégorie" if model is Category else "emplacement"
    name = " ".join((name or "").split())
    item = model.objects.select_for_update().get(pk=item.pk)
    _check_reference_name(model, label, item.organization, name, exclude_pk=item.pk)
    item.name = name
    item.full_clean()
    item.save(update_fields=["name", "updated_at"])
    return item


@transaction.atomic
def set_reference_active(*, item, active):
    """Désactive ou réactive : une catégorie ou un emplacement ne se supprime pas."""
    item = type(item).objects.select_for_update().get(pk=item.pk)
    item.active = bool(active)
    item.save(update_fields=["active", "updated_at"])
    return item
