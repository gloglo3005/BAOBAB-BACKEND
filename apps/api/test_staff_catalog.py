from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from apps.audit.models import AuditLog
from apps.catalog.models import Book, BookCopy, Category, Location
from apps.organizations.models import OrganizationStaff

from .test_staff_read import make_org, make_user


class StaffCatalogWriteTests(TestCase):
    def setUp(self):
        self.org_a = make_org("Biblio A")
        self.org_b = make_org("Biblio B")
        self.staff_a = make_user("staff-a")
        self.staff_b = make_user("staff-b")
        OrganizationStaff.objects.create(user=self.staff_a, organization=self.org_a, active=True)
        OrganizationStaff.objects.create(user=self.staff_b, organization=self.org_b, active=True)

        self.cat_a = Category.objects.create(organization=self.org_a, name="Romans")
        self.cat_b = Category.objects.create(organization=self.org_b, name="Sciences")
        self.loc_a1 = Location.objects.create(organization=self.org_a, name="Rayon 1")
        self.loc_a2 = Location.objects.create(organization=self.org_a, name="Rayon 2")
        self.loc_b = Location.objects.create(organization=self.org_b, name="Rayon B")

        self.book = Book.objects.create(
            organization=self.org_a, title="Livre A", category=self.cat_a
        )
        self.copy = BookCopy.objects.create(
            organization=self.org_a, book=self.book, inventory_code="A-001",
            location=self.loc_a1,
        )
        self.book_b = Book.objects.create(organization=self.org_b, title="Livre B")
        self.copy_b = BookCopy.objects.create(
            organization=self.org_b, book=self.book_b, inventory_code="B-001"
        )

        self.client = APIClient()
        self.client.force_authenticate(self.staff_a)

    # ---------------------------------------------------------------- outils

    def url(self, name, org=None, **kwargs):
        return reverse(
            f"api:{name}", kwargs={"organization_id": (org or self.org_a).pk, **kwargs}
        )

    def post(self, name, data=None, **kwargs):
        return self.client.post(self.url(name, **kwargs), data or {}, format="json")

    def patch(self, name, data, **kwargs):
        return self.client.patch(self.url(name, **kwargs), data, format="json")

    def audit(self, action):
        return AuditLog.objects.filter(organization=self.org_a, action=action)

    # ---------------------------------------------------------------- livres

    def test_create_book_ok(self):
        response = self.post("staff-books", {
            "title": "Things Fall Apart", "authors": ["Chinua Achebe"],
            "category": str(self.cat_a.pk), "isbn": "978-1",
        })
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertEqual(data["title"], "Things Fall Apart")
        self.assertEqual(data["authors"], ["Chinua Achebe"])
        self.assertEqual(str(data["category"]["id"]), str(self.cat_a.pk))
        self.assertTrue(self.audit(AuditLog.Action.BOOK_CREATED).exists())

    def test_create_book_duplicate_isbn_is_409(self):
        payload = {"title": "Premier", "isbn": "978-2"}
        self.assertEqual(self.post("staff-books", payload).status_code, 201)
        self.assertEqual(
            self.post("staff-books", {"title": "Second", "isbn": "978-2"}).status_code, 409
        )

    def test_create_book_missing_title_is_400(self):
        self.assertEqual(self.post("staff-books", {"subtitle": "x"}).status_code, 400)

    def test_create_book_category_of_other_organization_is_400(self):
        response = self.post(
            "staff-books", {"title": "Autre", "category": str(self.cat_b.pk)}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("category", response.json())
        self.assertEqual(Book.objects.filter(organization=self.org_a).count(), 1)

    def test_create_book_other_organization_staff_refused(self):
        self.client.force_authenticate(self.staff_b)
        self.assertEqual(self.post("staff-books", {"title": "X"}).status_code, 403)

    def test_create_book_anonymous_refused(self):
        self.client.force_authenticate(None)
        self.assertEqual(self.post("staff-books", {"title": "X"}).status_code, 401)

    def test_update_book(self):
        response = self.patch(
            "staff-book-detail",
            {"title": "Nouveau titre", "authors": ["Ada Lovelace"], "category": None},
            pk=self.book.pk,
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["title"], "Nouveau titre")
        self.assertEqual(data["authors"], ["Ada Lovelace"])
        self.assertIsNone(data["category"])
        self.assertTrue(self.audit(AuditLog.Action.BOOK_UPDATED).exists())

    def test_update_book_without_change(self):
        response = self.patch("staff-book-detail", {"title": "Livre A"}, pk=self.book.pk)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.audit(AuditLog.Action.BOOK_UPDATED).exists())

    def test_update_book_of_other_organization_is_404(self):
        response = self.patch("staff-book-detail", {"title": "Piraté"}, pk=self.book_b.pk)
        self.assertEqual(response.status_code, 404)
        self.book_b.refresh_from_db()
        self.assertEqual(self.book_b.title, "Livre B")

    def test_archive_and_restore(self):
        response = self.post("staff-book-archive", pk=self.book.pk)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["active"])
        self.assertTrue(self.audit(AuditLog.Action.BOOK_ARCHIVED).exists())

        self.assertEqual(self.post("staff-book-archive", pk=self.book.pk).status_code, 409)

        response = self.post("staff-book-restore", pk=self.book.pk)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["active"])
        self.assertEqual(self.post("staff-book-restore", pk=self.book.pk).status_code, 409)

    def test_archive_refused_when_a_copy_is_borrowed(self):
        BookCopy.objects.filter(pk=self.copy.pk).update(status=BookCopy.Status.BORROWED)
        self.assertEqual(self.post("staff-book-archive", pk=self.book.pk).status_code, 409)
        self.book.refresh_from_db()
        self.assertTrue(self.book.active)

    def test_archive_book_of_other_organization_is_404(self):
        self.assertEqual(self.post("staff-book-archive", pk=self.book_b.pk).status_code, 404)

    # ---------------------------------------------------------------- ajout d'exemplaires

    def test_add_copies_ok(self):
        response = self.post(
            "staff-book-copies",
            {"codes": ["N-1", "N-2"], "location": str(self.loc_a1.pk)},
            pk=self.book.pk,
        )
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertEqual(len(data), 2)
        self.assertEqual(data[0]["location"]["name"], "Rayon 1")
        self.assertEqual(self.audit(AuditLog.Action.COPY_CREATED).count(), 2)

    def test_add_copies_existing_code_is_409_and_nothing_created(self):
        response = self.post("staff-book-copies", {"codes": ["N-1", "A-001"]}, pk=self.book.pk)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(BookCopy.objects.filter(organization=self.org_a).count(), 1)

    def test_add_copies_repeated_code_is_409(self):
        response = self.post("staff-book-copies", {"codes": ["X-1", "x-1"]}, pk=self.book.pk)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(BookCopy.objects.filter(organization=self.org_a).count(), 1)

    def test_add_copies_to_archived_book_is_409(self):
        Book.objects.filter(pk=self.book.pk).update(active=False)
        response = self.post("staff-book-copies", {"codes": ["N-1"]}, pk=self.book.pk)
        self.assertEqual(response.status_code, 409)

    def test_add_copies_empty_list_is_400(self):
        self.assertEqual(
            self.post("staff-book-copies", {"codes": []}, pk=self.book.pk).status_code, 400
        )

    def test_add_copies_to_book_of_other_organization_is_404(self):
        response = self.post("staff-book-copies", {"codes": ["N-1"]}, pk=self.book_b.pk)
        self.assertEqual(response.status_code, 404)

    def test_add_copies_location_of_other_organization_is_400(self):
        response = self.post(
            "staff-book-copies",
            {"codes": ["N-1"], "location": str(self.loc_b.pk)},
            pk=self.book.pk,
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(BookCopy.objects.filter(organization=self.org_a).count(), 1)

    # ---------------------------------------------------------------- exemplaires

    def test_update_copy(self):
        response = self.patch(
            "staff-copy-detail",
            {"condition": "POOR", "notes": "Dos abîmé"},
            pk=self.copy.pk,
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["condition"], "POOR")
        self.assertEqual(data["notes"], "Dos abîmé")

    def test_update_copy_invalid_condition_is_400(self):
        response = self.patch("staff-copy-detail", {"condition": "XXX"}, pk=self.copy.pk)
        self.assertEqual(response.status_code, 400)

    def test_update_copy_of_other_organization_is_404(self):
        response = self.patch("staff-copy-detail", {"notes": "x"}, pk=self.copy_b.pk)
        self.assertEqual(response.status_code, 404)

    def test_move_copy(self):
        response = self.post(
            "staff-copy-move", {"location": str(self.loc_a2.pk)}, pk=self.copy.pk
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["location"]["name"], "Rayon 2")
        self.assertTrue(self.audit(AuditLog.Action.COPY_MOVED).exists())

    def test_move_copy_remove_location(self):
        response = self.post("staff-copy-move", {"location": None}, pk=self.copy.pk)
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["location"])

    def test_move_copy_same_location_does_not_log(self):
        response = self.post(
            "staff-copy-move", {"location": str(self.loc_a1.pk)}, pk=self.copy.pk
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.audit(AuditLog.Action.COPY_MOVED).exists())

    def test_move_copy_location_of_other_organization_is_400(self):
        response = self.post(
            "staff-copy-move", {"location": str(self.loc_b.pk)}, pk=self.copy.pk
        )
        self.assertEqual(response.status_code, 400)
        self.copy.refresh_from_db()
        self.assertEqual(self.copy.location_id, self.loc_a1.pk)

    def test_move_copy_missing_field_is_400(self):
        self.assertEqual(self.post("staff-copy-move", {}, pk=self.copy.pk).status_code, 400)

    def test_move_copy_of_other_organization_is_404(self):
        response = self.post("staff-copy-move", {"location": None}, pk=self.copy_b.pk)
        self.assertEqual(response.status_code, 404)

    def test_status_lost(self):
        response = self.post("staff-copy-status", {"status": "LOST"}, pk=self.copy.pk)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "LOST")
        self.assertTrue(self.audit(AuditLog.Action.COPY_MARKED_LOST).exists())

    def test_status_damaged(self):
        response = self.post("staff-copy-status", {"status": "DAMAGED"}, pk=self.copy.pk)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.audit(AuditLog.Action.COPY_STATUS_CHANGED).exists())

    def test_status_borrowed_cannot_be_chosen(self):
        response = self.post("staff-copy-status", {"status": "BORROWED"}, pk=self.copy.pk)
        self.assertEqual(response.status_code, 400)

    def test_status_of_borrowed_copy_is_409(self):
        BookCopy.objects.filter(pk=self.copy.pk).update(status=BookCopy.Status.BORROWED)
        response = self.post("staff-copy-status", {"status": "AVAILABLE"}, pk=self.copy.pk)
        self.assertEqual(response.status_code, 409)

    def test_status_same_status_is_409(self):
        response = self.post("staff-copy-status", {"status": "AVAILABLE"}, pk=self.copy.pk)
        self.assertEqual(response.status_code, 409)

    # ---------------------------------------------------------------- catégories et emplacements

    def test_list_categories_is_isolated(self):
        data = self.client.get(self.url("staff-categories")).json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["results"][0]["name"], "Romans")

    def test_create_category(self):
        response = self.post("staff-categories", {"name": "Poésie"})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["name"], "Poésie")
        self.assertTrue(response.json()["active"])

    def test_create_category_duplicate_name_is_409(self):
        self.assertEqual(self.post("staff-categories", {"name": "romans"}).status_code, 409)

    def test_create_category_same_name_in_other_organization_is_allowed(self):
        self.assertEqual(self.post("staff-categories", {"name": "Sciences"}).status_code, 201)

    def test_rename_and_deactivate_category(self):
        response = self.patch(
            "staff-category-detail", {"name": "Fiction", "active": False}, pk=self.cat_a.pk
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["name"], "Fiction")
        self.assertFalse(response.json()["active"])

    def test_patch_category_of_other_organization_is_404(self):
        response = self.patch("staff-category-detail", {"name": "Piraté"}, pk=self.cat_b.pk)
        self.assertEqual(response.status_code, 404)
        self.cat_b.refresh_from_db()
        self.assertEqual(self.cat_b.name, "Sciences")

    def test_create_and_patch_location(self):
        response = self.post("staff-locations", {"name": "Réserve"})
        self.assertEqual(response.status_code, 201)
        location_id = response.json()["id"]
        response = self.patch("staff-location-detail", {"active": False}, pk=location_id)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["active"])

    def test_list_locations_filter_active(self):
        Location.objects.filter(pk=self.loc_a2.pk).update(active=False)
        data = self.client.get(self.url("staff-locations"), {"active": "true"}).json()
        self.assertEqual(data["count"], 1)