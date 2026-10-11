from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.audit.models import AuditLog
from apps.catalog.models import Author, Book, BookCopy, Category, Location
from apps.circulation.models import Loan
from apps.members.models import Member
from apps.members.services import create_link_code
from apps.organizations.models import Organization, OrganizationStaff

from .test_staff_read import make_org, make_user


class ReaderApiTests(TestCase):
    def setUp(self):
        self.org_a = make_org("Biblio A")
        self.org_b = make_org("Biblio B")
        self.org_nolib = make_org("Sans module", library_module=False)
        self.org_suspended = make_org("Suspendue")
        Organization.objects.filter(pk=self.org_suspended.pk).update(
            status=Organization.Status.SUSPENDED
        )

        self.staff = make_user("staff-a")
        OrganizationStaff.objects.create(user=self.staff, organization=self.org_a, active=True)
        self.reader1 = self.make_reader("lecteur1")
        self.reader2 = self.make_reader("lecteur2")

        self.member_a1 = self.make_member("MEM-0001", Member.Status.ACCEPTED, user=self.reader1)
        self.member_a2 = self.make_member("MEM-0002", Member.Status.PENDING, user=self.reader2)
        self.member_a3 = self.make_member("MEM-0003", Member.Status.ACCEPTED)

        self.cat_a = Category.objects.create(organization=self.org_a, name="Romans")
        self.loc1 = Location.objects.create(organization=self.org_a, name="Rayon 1")
        author = Author.objects.create(
            organization=self.org_a, first_name="Chinua", last_name="Achebe"
        )
        self.book_a = Book.objects.create(
            organization=self.org_a, title="Things Fall Apart", category=self.cat_a
        )
        self.book_a.authors.add(author)
        self.c1, self.c2, self.c3, self.c4, self.c5 = [
            BookCopy.objects.create(
                organization=self.org_a, book=self.book_a,
                inventory_code=f"A-00{i}", location=self.loc1,
            )
            for i in range(1, 6)
        ]
        BookCopy.objects.filter(pk__in=[self.c1.pk, self.c2.pk, self.c5.pk]).update(
            status=BookCopy.Status.BORROWED
        )

        self.book_c = Book.objects.create(organization=self.org_a, title="Tout emprunté")
        c6 = BookCopy.objects.create(
            organization=self.org_a, book=self.book_c, inventory_code="C-001"
        )
        BookCopy.objects.filter(pk=c6.pk).update(status=BookCopy.Status.BORROWED)

        self.book_arch = Book.objects.create(
            organization=self.org_a, title="Archivé", active=False
        )
        self.book_b = Book.objects.create(organization=self.org_b, title="Livre B")

        today = timezone.localdate()
        self.loan1 = Loan.objects.create(
            organization=self.org_a, book_copy=self.c1, member=self.member_a1,
            due_date=today + timedelta(days=5), issued_by=self.staff,
        )
        self.loan_overdue = Loan.objects.create(
            organization=self.org_a, book_copy=self.c2, member=self.member_a1,
            due_date=today - timedelta(days=3), issued_by=self.staff,
        )
        self.loan_returned = Loan.objects.create(
            organization=self.org_a, book_copy=self.c3, member=self.member_a1,
            due_date=today - timedelta(days=10), issued_by=self.staff,
            status=Loan.Status.RETURNED, returned_at=timezone.now(),
        )
        self.loan_other = Loan.objects.create(
            organization=self.org_a, book_copy=self.c5, member=self.member_a3,
            due_date=today + timedelta(days=2), issued_by=self.staff,
        )

        self.client = APIClient()
        self.client.force_authenticate(self.reader1)

    # ---------------------------------------------------------------- outils

    def make_reader(self, name):
        user = make_user(name)
        user.first_name = "Awa"
        user.last_name = name
        user.save()
        return user

    def make_member(self, number, member_status, org=None, **extra):
        return Member.objects.create(
            organization=org or self.org_a, member_number=number,
            first_name="Prénom", last_name=f"Nom{number}", status=member_status, **extra,
        )

    def url(self, name, **kwargs):
        return reverse(f"api:{name}", kwargs=kwargs)

    def get(self, name, params=None, **kwargs):
        return self.client.get(self.url(name, **kwargs), params or {})

    def post(self, name, data=None, **kwargs):
        return self.client.post(self.url(name, **kwargs), data or {}, format="json")

    # ---------------------------------------------------------------- sans fiche

    def test_organizations_list(self):
        data = self.get("reader-organizations").json()
        self.assertEqual([o["name"] for o in data["results"]], ["Biblio A", "Biblio B"])
        mine = {o["name"]: o["my_membership"] for o in data["results"]}
        self.assertEqual(mine["Biblio A"]["status"], "ACCEPTED")
        self.assertIsNone(mine["Biblio B"])

    def test_organizations_search(self):
        data = self.get("reader-organizations", {"q": "Biblio B"}).json()
        self.assertEqual([o["name"] for o in data["results"]], ["Biblio B"])

    def test_organizations_anonymous_refused(self):
        self.client.force_authenticate(None)
        self.assertEqual(self.get("reader-organizations").status_code, 401)

    def test_account_must_change_password_refused(self):
        self.reader1.must_change_password = True
        self.reader1.save()
        self.client.force_authenticate(self.reader1)
        self.assertEqual(self.get("reader-organizations").status_code, 403)

    def test_memberships_list(self):
        data = self.get("reader-memberships").json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["organization"]["name"], "Biblio A")
        self.assertEqual(data[0]["active_loans"], 2)
        self.assertEqual(data[0]["overdue_loans"], 1)

    def test_request_membership_ok(self):
        response = self.post("reader-memberships", {"organization": str(self.org_b.pk)})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["status"], "PENDING")
        self.assertTrue(
            Member.objects.filter(organization=self.org_b, user=self.reader1).exists()
        )
        self.assertTrue(
            AuditLog.objects.filter(
                organization=self.org_b, action=AuditLog.Action.MEMBER_CREATED
            ).exists()
        )

    def test_request_membership_twice_is_409(self):
        response = self.post("reader-memberships", {"organization": str(self.org_a.pk)})
        self.assertEqual(response.status_code, 409)

    def test_request_membership_unavailable_organization_is_404(self):
        for org in (self.org_nolib, self.org_suspended):
            response = self.post("reader-memberships", {"organization": str(org.pk)})
            self.assertEqual(response.status_code, 404)

    def test_request_membership_missing_field_is_400(self):
        self.assertEqual(self.post("reader-memberships", {}).status_code, 400)

    def test_request_membership_anonymous_refused(self):
        self.client.force_authenticate(None)
        response = self.post("reader-memberships", {"organization": str(self.org_b.pk)})
        self.assertEqual(response.status_code, 401)

    def test_link_code(self):
        counter_member = self.make_member(
            "MEM-0001", Member.Status.ACCEPTED, org=self.org_b
        )
        link = create_link_code(member=counter_member, created_by=self.staff)

        self.client.force_authenticate(self.reader2)
        response = self.post("reader-link", {"code": link.code})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["id"], counter_member.pk)
        counter_member.refresh_from_db()
        self.assertEqual(counter_member.user_id, self.reader2.pk)

        # Un code est à usage unique.
        self.client.force_authenticate(self.reader1)
        self.assertEqual(self.post("reader-link", {"code": link.code}).status_code, 400)

    def test_link_code_wrong_code_is_400(self):
        self.assertEqual(self.post("reader-link", {"code": "ZZZZZZZZ"}).status_code, 400)

    # ---------------------------------------------------------------- avec une fiche

    def test_summary(self):
        data = self.get("reader-summary", member_id=self.member_a1.pk).json()
        self.assertEqual(data["member"]["member_number"], "MEM-0001")
        self.assertEqual(data["organization"]["name"], "Biblio A")
        self.assertEqual(data["loans"], {"active": 2, "overdue": 1})
        self.assertEqual(len(data["upcoming"]), 2)
        self.assertEqual(data["upcoming"][0]["id"], self.loan_overdue.pk)

    def test_summary_of_another_reader_is_refused(self):
        self.client.force_authenticate(self.reader2)
        response = self.get("reader-summary", member_id=self.member_a1.pk)
        self.assertEqual(response.status_code, 403)

    def test_summary_of_member_without_account_is_refused(self):
        response = self.get("reader-summary", member_id=self.member_a3.pk)
        self.assertEqual(response.status_code, 403)

    def test_summary_anonymous_refused(self):
        self.client.force_authenticate(None)
        self.assertEqual(self.get("reader-summary", member_id=self.member_a1.pk).status_code, 401)

    def test_malformed_ids_are_refused_not_crashing(self):
        self.assertEqual(self.get("reader-summary", member_id="abc").status_code, 403)
        self.client.force_authenticate(self.staff)
        response = self.client.get(self.url("staff-books", organization_id="abc"))
        self.assertEqual(response.status_code, 403)

    def test_rejected_member_is_refused(self):
        Member.objects.filter(pk=self.member_a2.pk).update(status=Member.Status.REJECTED)
        self.client.force_authenticate(self.reader2)
        self.assertEqual(self.get("reader-summary", member_id=self.member_a2.pk).status_code, 403)

    def test_module_disabled_is_refused(self):
        member = self.make_member(
            "MEM-0001", Member.Status.ACCEPTED, org=self.org_nolib, user=self.reader1
        )
        self.assertEqual(self.get("reader-summary", member_id=member.pk).status_code, 403)

    def test_pending_member_can_browse(self):
        self.client.force_authenticate(self.reader2)
        self.assertEqual(self.get("reader-books", member_id=self.member_a2.pk).status_code, 200)
        data = self.get("reader-loans", member_id=self.member_a2.pk).json()
        self.assertEqual(data["count"], 0)

    # ---------------------------------------------------------------- catalogue

    def test_books_list(self):
        data = self.get("reader-books", member_id=self.member_a1.pk).json()
        titles = sorted(book["title"] for book in data["results"])
        self.assertEqual(titles, ["Things Fall Apart", "Tout emprunté"])
        book = next(b for b in data["results"] if b["title"] == "Things Fall Apart")
        self.assertEqual(book["copies_available"], 2)
        self.assertEqual(book["authors"], ["Chinua Achebe"])
        self.assertEqual(book["category"]["name"], "Romans")
        self.assertNotIn("inventory_code", str(data))

    def test_books_search_category_available(self):
        member_id = self.member_a1.pk
        found = self.get("reader-books", {"q": "things"}, member_id=member_id).json()
        self.assertEqual(found["count"], 1)
        available = self.get("reader-books", {"available": "1"}, member_id=member_id).json()
        self.assertEqual([b["title"] for b in available["results"]], ["Things Fall Apart"])
        by_category = self.get(
            "reader-books", {"category": str(self.cat_a.pk)}, member_id=member_id
        ).json()
        self.assertEqual(by_category["count"], 1)
        invalid = self.get("reader-books", {"category": "abc"}, member_id=member_id)
        self.assertEqual(invalid.status_code, 200)
        self.assertEqual(invalid.json()["count"], 0)

    def test_categories(self):
        data = self.get("reader-categories", member_id=self.member_a1.pk).json()
        self.assertEqual([c["name"] for c in data], ["Romans"])

    def test_book_detail(self):
        data = self.get(
            "reader-book-detail", member_id=self.member_a1.pk, pk=self.book_a.pk
        ).json()
        self.assertEqual(data["title"], "Things Fall Apart")
        self.assertEqual(data["availability"], [{"location": "Rayon 1", "available": 2}])
        self.assertEqual(data["available_total"], 2)
        self.assertEqual(len(data["my_loans"]), 2)
        self.assertNotIn("inventory_code", str(data))

    def test_book_detail_hides_other_readers_loans(self):
        self.client.force_authenticate(self.reader2)
        data = self.get(
            "reader-book-detail", member_id=self.member_a2.pk, pk=self.book_a.pk
        ).json()
        self.assertEqual(data["my_loans"], [])

    def test_book_detail_not_found_cases(self):
        member_id = self.member_a1.pk
        for pk in (self.book_arch.pk, self.book_b.pk, "abc"):
            response = self.get("reader-book-detail", member_id=member_id, pk=pk)
            self.assertEqual(response.status_code, 404)

    # ---------------------------------------------------------------- prêts

    def test_loans_default_is_active_and_isolated(self):
        data = self.get("reader-loans", member_id=self.member_a1.pk).json()
        self.assertEqual(data["count"], 2)
        ids = [loan["id"] for loan in data["results"]]
        self.assertNotIn(self.loan_other.pk, ids)
        self.assertNotIn("inventory_code", str(data))
        first = data["results"][0]
        self.assertEqual(first["book"]["title"], "Things Fall Apart")
        self.assertEqual(first["authors"], ["Chinua Achebe"])

    def test_loans_overdue(self):
        data = self.get("reader-loans", {"status": "overdue"}, member_id=self.member_a1.pk).json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["results"][0]["days_overdue"], 3)
        self.assertTrue(data["results"][0]["is_overdue"])

    def test_loans_history(self):
        data = self.get("reader-loans", {"status": "history"}, member_id=self.member_a1.pk).json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["results"][0]["status"], "RETURNED")
        self.assertIsNone(data["results"][0]["days_left"])

    def test_loans_days_left(self):
        data = self.get("reader-loans", member_id=self.member_a1.pk).json()
        by_id = {loan["id"]: loan for loan in data["results"]}
        self.assertEqual(by_id[self.loan1.pk]["days_left"], 5)
        self.assertEqual(by_id[self.loan_overdue.pk]["days_left"], -3)

    def test_loans_invalid_status_is_400(self):
        response = self.get("reader-loans", {"status": "xxx"}, member_id=self.member_a1.pk)
        self.assertEqual(response.status_code, 400)

    def test_loans_of_another_reader_are_refused(self):
        self.client.force_authenticate(self.reader2)
        self.assertEqual(self.get("reader-loans", member_id=self.member_a1.pk).status_code, 403)