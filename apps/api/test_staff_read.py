from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from rest_framework.test import APIClient

from apps.catalog.models import Book, BookCopy
from apps.circulation.models import Loan
from apps.members.models import Member
from apps.organizations.models import Organization, OrganizationStaff

User = get_user_model()


def make_user(name):
    return User.objects.create_user(
        **{User.USERNAME_FIELD: f"{name}@test.com", "password": "Mdp-test-123"}
    )


def make_org(name, library_module=True):
    return Organization.objects.create(
        name=name,
        slug=slugify(name),
        library_module=library_module,
        status=Organization.Status.ACTIVE,
    )


class StaffReadApiTests(TestCase):
    def setUp(self):
        self.org_a = make_org("Biblio A")
        self.org_b = make_org("Biblio B")
        self.org_c = make_org("Sans module", library_module=False)
        self.staff_a = make_user("staff-a")
        self.staff_b = make_user("staff-b")
        self.staff_c = make_user("staff-c")
        OrganizationStaff.objects.create(user=self.staff_a, organization=self.org_a, active=True)
        OrganizationStaff.objects.create(user=self.staff_b, organization=self.org_b, active=True)
        OrganizationStaff.objects.create(user=self.staff_c, organization=self.org_c, active=True)

        self.book_a = Book.objects.create(organization=self.org_a, title="Les Misérables")
        self.copy_a1 = BookCopy.objects.create(
            organization=self.org_a, book=self.book_a, inventory_code="A-001"
        )
        self.copy_a2 = BookCopy.objects.create(
            organization=self.org_a, book=self.book_a, inventory_code="A-002",
            status=BookCopy.Status.BORROWED,
        )
        self.member_a = Member.objects.create(
            organization=self.org_a, member_number="MEM-0001",
            first_name="Ada", last_name="Test", status=Member.Status.ACCEPTED,
        )
        Member.objects.create(
            organization=self.org_a, member_number="MEM-0002",
            first_name="Bob", last_name="Attente", status=Member.Status.PENDING,
        )
        today = timezone.localdate()
        self.loan_overdue = Loan.objects.create(
            organization=self.org_a, book_copy=self.copy_a2, member=self.member_a,
            due_date=today - timedelta(days=5), issued_by=self.staff_a,
        )
        Loan.objects.create(
            organization=self.org_a, book_copy=self.copy_a1, member=self.member_a,
            due_date=today - timedelta(days=20), issued_by=self.staff_a,
            status=Loan.Status.RETURNED, returned_at=timezone.now(),
        )

        self.book_b = Book.objects.create(organization=self.org_b, title="Autre livre")
        BookCopy.objects.create(organization=self.org_b, book=self.book_b, inventory_code="B-001")

        self.client = APIClient()
        self.client.force_authenticate(self.staff_a)

    def url(self, name, org=None, **kwargs):
        org = org or self.org_a
        return reverse(f"api:{name}", kwargs={"organization_id": org.pk, **kwargs})

    def test_anonymous_refused(self):
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(self.url("staff-books")).status_code, 401)

    def test_other_organization_refused(self):
        self.assertEqual(self.client.get(self.url("staff-books", org=self.org_b)).status_code, 403)

    def test_module_disabled_refused(self):
        self.client.force_authenticate(self.staff_c)
        self.assertEqual(self.client.get(self.url("staff-books", org=self.org_c)).status_code, 403)

    def test_books_isolated(self):
        data = self.client.get(self.url("staff-books")).json()
        self.assertEqual(data["count"], 1)
        book = data["results"][0]
        self.assertEqual(book["title"], "Les Misérables")
        self.assertEqual(book["copies_total"], 2)
        self.assertEqual(book["copies_available"], 1)

    def test_books_search(self):
        found = self.client.get(self.url("staff-books"), {"q": "A-002"}).json()
        self.assertEqual(found["count"], 1)
        none = self.client.get(self.url("staff-books"), {"q": "zzzz"}).json()
        self.assertEqual(none["count"], 0)

    def test_book_detail_other_organization_is_404(self):
        response = self.client.get(self.url("staff-book-detail", pk=self.book_b.pk))
        self.assertEqual(response.status_code, 404)

    def test_book_detail_lists_copies(self):
        data = self.client.get(self.url("staff-book-detail", pk=self.book_a.pk)).json()
        self.assertEqual(len(data["copies"]), 2)

    def test_copies_filter_status(self):
        data = self.client.get(self.url("staff-copies"), {"status": "borrowed"}).json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["results"][0]["inventory_code"], "A-002")

    def test_members_filter_status(self):
        self.assertEqual(self.client.get(self.url("staff-members")).json()["count"], 2)
        data = self.client.get(self.url("staff-members"), {"status": "PENDING"}).json()
        self.assertEqual(data["count"], 1)

    def test_member_active_loans(self):
        data = self.client.get(self.url("staff-member-detail", pk=self.member_a.pk)).json()
        self.assertEqual(data["active_loans"], 1)

    def test_loans_filters(self):
        self.assertEqual(self.client.get(self.url("staff-loans")).json()["count"], 2)
        overdue = self.client.get(self.url("staff-loans"), {"overdue": "true"}).json()
        self.assertEqual(overdue["count"], 1)
        self.assertEqual(overdue["results"][0]["days_overdue"], 5)
        returned = self.client.get(self.url("staff-loans"), {"status": "RETURNED"}).json()
        self.assertEqual(returned["count"], 1)

    def test_invalid_filter_does_not_crash(self):
        response = self.client.get(self.url("staff-books"), {"category": "abc"})
        self.assertEqual(response.status_code, 200)

    def test_dashboard(self):
        data = self.client.get(self.url("staff-dashboard")).json()
        self.assertEqual(data["books"]["active"], 1)
        self.assertEqual(data["copies"]["total"], 2)
        self.assertEqual(data["copies"]["by_status"]["AVAILABLE"], 1)
        self.assertEqual(data["members"]["by_status"]["PENDING"], 1)
        self.assertEqual(data["loans"], {"active": 1, "overdue": 1})

    def test_unsupported_methods_are_refused(self):
        response = self.client.delete(self.url("staff-books"))
        self.assertEqual(response.status_code, 405)
        response = self.client.post(self.url("staff-dashboard"), {})
        self.assertEqual(response.status_code, 405)