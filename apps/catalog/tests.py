from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from apps.organizations.services import create_organization

from .models import Author, Book, BookCopy, Category, Location
from .services import add_copy, create_book


class CatalogTestCase(TestCase):
    """Deux structures et un livre dans chacune, disponibles pour chaque test."""

    def setUp(self):
        self.org_a = create_organization(name="Bibliothèque A", slug="a")
        self.org_b = create_organization(name="Bibliothèque B", slug="b")
        self.book_a = create_book(organization=self.org_a, title="Livre A")
        self.book_b = create_book(organization=self.org_b, title="Livre B")


class CategoryLocationTests(CatalogTestCase):
    def test_categorie_en_double_refusee_sans_tenir_compte_de_la_casse(self):
        Category.objects.create(organization=self.org_a, name="Roman")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Category.objects.create(organization=self.org_a, name="roman")

    def test_meme_nom_autorise_dans_une_autre_structure(self):
        Category.objects.create(organization=self.org_a, name="Roman")
        Category.objects.create(organization=self.org_b, name="Roman")
        self.assertEqual(Category.objects.count(), 2)

    def test_emplacement_en_double_refuse(self):
        Location.objects.create(organization=self.org_a, name="Rayon A")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Location.objects.create(organization=self.org_a, name="rayon a")


class BookTests(CatalogTestCase):
    def test_un_livre_trois_exemplaires(self):
        book = create_book(organization=self.org_a, title="Le vieux nègre et la médaille")
        for code in ["BIB-001", "BIB-002", "BIB-003"]:
            add_copy(book=book, inventory_code=code)
        self.assertEqual(book.copies.count(), 3)
        self.assertEqual(Book.objects.filter(title=book.title).count(), 1)

    def test_isbn_unique_par_structure(self):
        create_book(organization=self.org_a, title="T1", isbn="978-2-1234")
        with self.assertRaises(ValidationError):
            create_book(organization=self.org_a, title="T2", isbn="978-2-1234")

    def test_plusieurs_livres_sans_isbn_autorises(self):
        create_book(organization=self.org_a, title="T1")
        create_book(organization=self.org_a, title="T2")
        # Livre A (setUp) + T1 + T2
        self.assertEqual(Book.objects.for_organization(self.org_a).count(), 3)

    def test_meme_isbn_autorise_dans_une_autre_structure(self):
        create_book(organization=self.org_a, title="T1", isbn="978-2-1234")
        create_book(organization=self.org_b, title="T1", isbn="978-2-1234")
        self.assertEqual(Book.objects.filter(isbn="978-2-1234").count(), 2)

    def test_livre_avec_auteurs(self):
        author = Author.objects.create(
            organization=self.org_a, first_name="Ferdinand", last_name="Oyono"
        )
        book = create_book(organization=self.org_a, title="Une vie de boy", authors=[author])
        self.assertEqual(book.authors.count(), 1)


class BookCopyTests(CatalogTestCase):
    def test_code_inventaire_unique_dans_une_structure(self):
        add_copy(book=self.book_a, inventory_code="BIB-001")
        with self.assertRaises(ValidationError):
            add_copy(book=self.book_a, inventory_code="BIB-001")

    def test_meme_code_autorise_dans_une_autre_structure(self):
        add_copy(book=self.book_a, inventory_code="BIB-001")
        add_copy(book=self.book_b, inventory_code="BIB-001")
        self.assertEqual(BookCopy.objects.count(), 2)

    def test_espaces_autour_du_code_retires(self):
        copy = add_copy(book=self.book_a, inventory_code="  BIB-009 ")
        self.assertEqual(copy.inventory_code, "BIB-009")

    def test_statut_emprunte_refuse_a_la_creation(self):
        with self.assertRaises(ValidationError):
            add_copy(
                book=self.book_a,
                inventory_code="BIB-001",
                status=BookCopy.Status.BORROWED,
            )

    def test_emplacement_d_une_autre_structure_refuse(self):
        location_b = Location.objects.create(organization=self.org_b, name="Rayon A")
        with self.assertRaises(ValidationError):
            add_copy(book=self.book_a, inventory_code="BIB-001", location=location_b)

    def test_exemplaire_reprend_la_structure_du_livre(self):
        copy = add_copy(book=self.book_a, inventory_code="BIB-001")
        self.assertEqual(copy.organization, self.org_a)


class IsolationTests(CatalogTestCase):
    def test_categorie_d_une_autre_structure_refusee(self):
        category_b = Category.objects.create(organization=self.org_b, name="Roman")
        with self.assertRaises(ValidationError):
            create_book(organization=self.org_a, title="X", category=category_b)

    def test_auteur_d_une_autre_structure_refuse(self):
        author_b = Author.objects.create(organization=self.org_b, last_name="Oyono")
        with self.assertRaises(ValidationError):
            create_book(organization=self.org_a, title="X", authors=[author_b])
        # Rien n'a été créé dans la structure A à part le livre de setUp.
        self.assertEqual(Book.objects.for_organization(self.org_a).count(), 1)

    def test_for_organization_ne_renvoie_que_ses_livres(self):
        titles = list(
            Book.objects.for_organization(self.org_a).values_list("title", flat=True)
        )
        self.assertEqual(titles, ["Livre A"])

    def test_exemplaire_lie_a_un_livre_d_une_autre_structure_refuse(self):
        copy = BookCopy(
            organization=self.org_b, book=self.book_a, inventory_code="ZZ-1"
        )
        with self.assertRaises(ValidationError):
            copy.full_clean()