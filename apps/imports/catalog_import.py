"""Import du catalogue : analyse d'un fichier, puis application du plan.

`analyze` ne modifie rien : elle lit le fichier et la base, et produit un plan
(livres à créer, livres déjà présents, exemplaires, références nouvelles) avec
la liste des erreurs. `apply_plan` écrit ce plan ; elle doit être appelée dans
une transaction (voir services.confirm_batch).
"""
import re
from dataclasses import dataclass, field

from django.utils import timezone

from apps.catalog.models import Author, Book, BookCopy, Category, Location
from apps.catalog.services import split_author_name

from .readers import normalize_header

COLUMNS = (
    "title", "inventory_code", "isbn", "author", "category",
    "publication_year", "publisher", "language", "location", "condition",
)
REQUIRED_COLUMNS = ("title",)

# En-têtes acceptés (après normalisation : sans accent, minuscules, « _ »).
_EXTRA_ALIASES = {
    "title": ("titre",),
    "inventory_code": (
        "code", "code_inventaire", "code_d_inventaire", "inventaire",
        "numero_inventaire", "n_inventaire",
    ),
    "author": ("auteur", "auteurs", "authors"),
    "category": ("categorie",),
    "publication_year": ("annee", "annee_publication", "annee_de_publication"),
    "publisher": ("editeur",),
    "language": ("langue",),
    "location": ("emplacement", "rayon", "localisation"),
    "condition": ("etat", "etat_physique"),
}
ALIASES = {name: name for name in COLUMNS}
for _canonical, _names in _EXTRA_ALIASES.items():
    for _name in _names:
        ALIASES[_name] = _canonical

CONDITIONS = {
    "new": BookCopy.Condition.NEW, "neuf": BookCopy.Condition.NEW,
    "good": BookCopy.Condition.GOOD, "bon": BookCopy.Condition.GOOD,
    "bon_etat": BookCopy.Condition.GOOD,
    "fair": BookCopy.Condition.FAIR, "passable": BookCopy.Condition.FAIR,
    "poor": BookCopy.Condition.POOR, "mauvais": BookCopy.Condition.POOR,
    "mauvais_etat": BookCopy.Condition.POOR,
}

MAX_LENGTHS = {
    "title": 255, "inventory_code": 50, "isbn": 20, "publisher": 150,
    "language": 50, "category": 100, "location": 100,
}
FIELD_LABELS = {
    "title": "titre", "inventory_code": "code d'inventaire", "isbn": "ISBN",
    "publisher": "éditeur", "language": "langue", "category": "catégorie",
    "location": "emplacement", "publication_year": "année", "author": "auteur",
}

# Au-delà, le détail n'est plus conservé dans l'aperçu (les compteurs restent exacts).
MAX_STORED_ERRORS = 500
MAX_STORED_WARNINGS = 200
MAX_STORED_MATCHES = 300

AUTHOR_SEPARATORS = re.compile(r"[;|\n]")


@dataclass
class PlannedCopy:
    line: int
    code: str
    location: str
    condition: str


@dataclass
class PlannedBook:
    title: str
    isbn: str
    year: int | None
    publisher: str
    language: str
    category: str
    authors: list  # [(prénom, nom)]
    first_line: int
    existing_id: int | None = None
    existing_title: str = ""
    matched_by: str = ""
    archived: bool = False
    copies: list = field(default_factory=list)


@dataclass
class Plan:
    row_count: int = 0
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    books: list = field(default_factory=list)
    new_categories: list = field(default_factory=list)
    new_locations: list = field(default_factory=list)
    new_authors: list = field(default_factory=list)
    ignored_columns: list = field(default_factory=list)

    @property
    def books_new(self):
        return sum(1 for book in self.books if book.existing_id is None)

    @property
    def books_existing(self):
        return sum(1 for book in self.books if book.existing_id is not None)

    @property
    def copies_total(self):
        return sum(len(book.copies) for book in self.books)


def _clean(text):
    return " ".join((text or "").split())


def _author_key(first, last):
    return (first.lower(), last.lower())


def _authors_key(authors):
    return tuple(sorted(_author_key(first, last) for first, last in authors))


def _parse_authors(text):
    authors, seen = [], set()
    for part in AUTHOR_SEPARATORS.split(text or ""):
        name = _clean(part)
        if not name:
            continue
        first, last = split_author_name(name)
        key = _author_key(first, last)
        if key not in seen:
            seen.add(key)
            authors.append((first, last))
    return authors


class _Existing:
    """Ce que la structure possède déjà (une seule lecture de la base)."""

    def __init__(self, organization):
        self.codes = {
            code.lower(): code
            for code in BookCopy.objects.filter(organization=organization)
            .values_list("inventory_code", flat=True)
        }
        self.by_isbn, self.by_title = {}, {}
        books = (
            Book.objects.filter(organization=organization)
            .prefetch_related("authors").order_by("pk")
        )
        for book in books:
            if book.isbn:
                self.by_isbn.setdefault(book.isbn, book)
            key = (book.title.lower(), _authors_key(
                (a.first_name, a.last_name) for a in book.authors.all()
            ))
            self.by_title.setdefault(key, book)
        self.categories = {
            name.lower(): name
            for name in Category.objects.filter(organization=organization)
            .values_list("name", flat=True)
        }
        self.locations = {
            name.lower(): name
            for name in Location.objects.filter(organization=organization)
            .values_list("name", flat=True)
        }
        self.authors = {
            _author_key(first, last)
            for first, last in Author.objects.filter(organization=organization)
            .values_list("first_name", "last_name")
        }

    def find_book(self, isbn, title_key):
        """Livre déjà présent : même ISBN, ou même titre et même auteur."""
        if isbn and isbn in self.by_isbn:
            return self.by_isbn[isbn], "ISBN"
        book = self.by_title.get(title_key)
        if book and not (book.isbn and isbn and book.isbn != isbn):
            return book, "titre et auteur"
        return None, ""


def _parse_row(row, max_year):
    """Valeurs nettoyées d'une ligne et liste de ses erreurs."""
    raw = row.values
    errors = []
    data = {}

    data["title"] = _clean(raw.get("title"))
    if not data["title"]:
        errors.append("Titre manquant.")

    for name in ("inventory_code", "publisher", "language", "category", "location"):
        data[name] = _clean(raw.get(name))
    data["isbn"] = re.sub(r"[\s-]", "", raw.get("isbn", "")).upper()
    for name, limit in MAX_LENGTHS.items():
        if len(data.get(name, "")) > limit:
            errors.append(
                f"{FIELD_LABELS[name].capitalize()} trop long ({limit} caractères au plus)."
            )

    year_text = _clean(raw.get("publication_year"))
    data["year"] = None
    if year_text:
        match = re.fullmatch(r"(\d{1,4})(?:[.,]0+)?", year_text)
        if match and 1 <= int(match.group(1)) <= max_year:
            data["year"] = int(match.group(1))
        else:
            errors.append(f"Année invalide : « {year_text} ».")

    condition_text = _clean(raw.get("condition"))
    data["condition"] = BookCopy.Condition.GOOD
    if condition_text:
        condition = CONDITIONS.get(normalize_header(condition_text))
        if condition is None:
            errors.append(
                f"État invalide : « {condition_text} » "
                "(attendu : neuf, bon, passable, mauvais, ou NEW, GOOD, FAIR, POOR)."
            )
        else:
            data["condition"] = condition

    data["authors"] = _parse_authors(raw.get("author"))
    for first, last in data["authors"]:
        if len(first) > 100 or len(last) > 100:
            errors.append("Nom d'auteur trop long (100 caractères au plus).")
            break
    return data, errors


def analyze(table, organization):
    """Analyse sans rien écrire. Renvoie un `Plan`."""
    existing = _Existing(organization)
    plan = Plan(row_count=len(table.rows), ignored_columns=list(table.ignored_columns))
    max_year = timezone.localdate().year + 1

    seen_codes = {}  # code en minuscules -> première ligne du fichier
    groups_by_isbn, groups_by_title = {}, {}

    def error(line, message):
        plan.errors.append({"line": line, "message": message})

    def warning(line, message):
        plan.warnings.append({"line": line, "message": message})

    for row in table.rows:
        data, row_errors = _parse_row(row, max_year)
        code = data["inventory_code"]

        if code:
            lowered = code.lower()
            if lowered in seen_codes:
                row_errors.append(
                    f"Code d'inventaire « {code} » en double dans le fichier "
                    f"(déjà ligne {seen_codes[lowered]})."
                )
            else:
                seen_codes[lowered] = row.line
                if lowered in existing.codes:
                    row_errors.append(
                        f"Code d'inventaire « {code} » déjà utilisé dans cette structure."
                    )

        if row_errors:
            for message in row_errors:
                error(row.line, message)
            continue

        title_key = (data["title"].lower(), _authors_key(data["authors"]))
        isbn = data["isbn"]

        group = groups_by_isbn.get(isbn) if isbn else None
        if group is not None and group.title.lower() != data["title"].lower():
            warning(
                row.line,
                f"Même ISBN que la ligne {group.first_line} mais un titre différent : "
                f"regroupé avec « {group.title} ».",
            )
        if group is None:
            candidate = groups_by_title.get(title_key)
            if candidate and not (candidate.isbn and isbn and candidate.isbn != isbn):
                group = candidate

        if group is None:
            group = PlannedBook(
                title=data["title"], isbn=isbn, year=data["year"],
                publisher=data["publisher"], language=data["language"],
                category=data["category"], authors=data["authors"],
                first_line=row.line,
            )
            found, matched_by = existing.find_book(isbn, title_key)
            if found is not None:
                group.existing_id = found.pk
                group.existing_title = found.title
                group.matched_by = matched_by
                group.archived = not found.active
            plan.books.append(group)
            groups_by_title[title_key] = group
            if isbn:
                groups_by_isbn[isbn] = group
        else:
            _merge_metadata(group, data, row.line, warning)
            if isbn and not group.isbn:
                group.isbn = isbn
                groups_by_isbn[isbn] = group
                if group.existing_id is None:
                    found, matched_by = existing.find_book(isbn, title_key)
                    if found is not None:
                        group.existing_id = found.pk
                        group.existing_title = found.title
                        group.matched_by = matched_by
                        group.archived = not found.active

        if code:
            if group.existing_id is not None and group.archived:
                error(
                    row.line,
                    f"Le livre « {group.existing_title} » est archivé : "
                    "restaurez-le avant d'importer ses exemplaires.",
                )
                continue
            group.copies.append(
                PlannedCopy(
                    line=row.line, code=code, location=data["location"],
                    condition=data["condition"],
                )
            )
        elif group.existing_id is not None:
            warning(
                row.line,
                f"Livre déjà présent (« {group.existing_title} ») et aucun code "
                "d'inventaire : rien à ajouter pour cette ligne.",
            )

    _collect_new_references(plan, existing)
    return plan


def _merge_metadata(group, data, line, warning):
    """Les lignes d'un même livre complètent les champs vides ; un désaccord est signalé."""
    differing = []
    for attribute, key in (("year", "year"), ("publisher", "publisher"),
                           ("language", "language"), ("category", "category")):
        current, new = getattr(group, attribute), data[key]
        if not new:
            continue
        if not current:
            setattr(group, attribute, new)
        elif str(current).lower() != str(new).lower():
            differing.append({"year": "année"}.get(key, FIELD_LABELS.get(key, key)))
    if differing:
        warning(
            line,
            f"Valeur différente de la ligne {group.first_line} pour : "
            + ", ".join(differing) + ". La première valeur est conservée.",
        )


def _collect_new_references(plan, existing):
    categories, locations, authors = {}, {}, {}
    for book in plan.books:
        if book.existing_id is None:
            if book.category and book.category.lower() not in existing.categories:
                categories.setdefault(book.category.lower(), book.category)
            for first, last in book.authors:
                if _author_key(first, last) not in existing.authors:
                    authors.setdefault(
                        _author_key(first, last), f"{first} {last}".strip()
                    )
        for copy in book.copies:
            if copy.location and copy.location.lower() not in existing.locations:
                locations.setdefault(copy.location.lower(), copy.location)
    plan.new_categories = list(categories.values())
    plan.new_locations = list(locations.values())
    plan.new_authors = list(authors.values())


def plan_summary(plan):
    """Résumé enregistré dans ImportBatch.summary (affiché dans l'aperçu)."""
    matches = [
        {
            "line": book.first_line,
            "title": book.title,
            "existing_id": book.existing_id,
            "existing_title": book.existing_title,
            "matched_by": book.matched_by,
            "copies": len(book.copies),
        }
        for book in plan.books if book.existing_id is not None
    ]
    return {
        "books_new": plan.books_new,
        "books_existing": plan.books_existing,
        "copies": plan.copies_total,
        "new_categories": plan.new_categories,
        "new_locations": plan.new_locations,
        "new_authors": plan.new_authors,
        "matches": matches[:MAX_STORED_MATCHES],
        "matches_total": len(matches),
        "warnings": plan.warnings[:MAX_STORED_WARNINGS],
        "warnings_total": len(plan.warnings),
        "ignored_columns": plan.ignored_columns,
    }


def _create_references(model, organization, names):
    created = []
    for name in names:
        created.append(model.objects.create(organization=organization, name=name))
    return created


def _create_books(organization, plan, categories, authors):
    to_create = [book for book in plan.books if book.existing_id is None]
    objects = [
        Book(
            organization=organization, title=book.title, isbn=book.isbn,
            publication_year=book.year, publisher=book.publisher,
            language=book.language,
            category=categories.get(book.category.lower()) if book.category else None,
        )
        for book in to_create
    ]
    Book.objects.bulk_create(objects)
    links = []
    through = Book.authors.through
    for book, obj in zip(to_create, objects):
        for first, last in book.authors:
            links.append(
                through(book_id=obj.pk, author_id=authors[_author_key(first, last)].pk)
            )
    through.objects.bulk_create(links)
    return {id(book): obj for book, obj in zip(to_create, objects)}


def _create_copies(organization, plan, created_books, locations):
    copies = []
    for book in plan.books:
        book_id = book.existing_id if book.existing_id is not None else created_books[id(book)].pk
        for copy in book.copies:
            copies.append(
                BookCopy(
                    organization=organization, book_id=book_id,
                    inventory_code=copy.code,
                    location=locations.get(copy.location.lower()) if copy.location else None,
                    condition=copy.condition,
                )
            )
    BookCopy.objects.bulk_create(copies)
    return len(copies)


def apply_plan(plan, organization):
    """Écrit le plan. À appeler dans une transaction : tout ou rien."""
    new_categories = _create_references(Category, organization, plan.new_categories)
    new_locations = _create_references(Location, organization, plan.new_locations)

    categories = {c.name.lower(): c for c in Category.objects.filter(organization=organization)}
    locations = {l.name.lower(): l for l in Location.objects.filter(organization=organization)}

    known_authors = {
        _author_key(a.first_name, a.last_name): a
        for a in Author.objects.filter(organization=organization)
    }
    missing = {}
    for book in plan.books:
        if book.existing_id is not None:
            continue
        for first, last in book.authors:
            key = _author_key(first, last)
            if key not in known_authors and key not in missing:
                missing[key] = Author(organization=organization, first_name=first, last_name=last)
    Author.objects.bulk_create(list(missing.values()))
    known_authors.update(missing)

    created_books = _create_books(organization, plan, categories, known_authors)
    copies_created = _create_copies(organization, plan, created_books, locations)

    return {
        "books_created": len(created_books),
        "books_matched": plan.books_existing,
        "copies_created": copies_created,
        "categories_created": len(new_categories),
        "locations_created": len(new_locations),
        "authors_created": len(missing),
    }
