from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.audit.models import AuditLog
from apps.catalog.models import Book, BookCopy
from apps.circulation.models import Loan
from apps.circulation.services import checkout_copy
from apps.members.models import Member
from apps.organizations.models import LoanPolicy, OrganizationStaff

from .test_staff_read import make_org, make_user


class StaffCirculationWriteTests(TestCase):
    def setUp(self):
        self.org_a = make_org("Biblio A")
        self.org_b = make_org("Biblio B")
        self.staff_a = make_user("staff-a")
        self.staff_b = make_user("staff-b")
        self.membership_a = OrganizationStaff.objects.create(
            user=self.staff_a, organization=self.org_a, active=True
        )
        OrganizationStaff.objects.create(user=self.staff_b, organization=self.org_b, active=True)

        book = Book.objects.create(organization=self.org_a, title="Livre A")
        self.copy1 = BookCopy.objects.create(
            organization=self.org_a, book=book, inventory_code="A-001"
        )
        self.copy2 = BookCopy.objects.create(
            organization=self.org_a, book=book, inventory_code="A-002"
        )
        self.member = Member.objects.create(
            organization=self.org_a, member_number="MEM-0001",
            first_name="Ada", last_name="Test", status=Member.Status.ACCEPTED,
        )

        book_b = Book.objects.create(organization=self.org_b, title="Livre B")
        self.copy_b = BookCopy.objects.create(
            organization=self.org_b, book=book_b, inventory_code="B-001"
        )
        self.member_b = Member.objects.create(
            organization=self.org_b, member_number="MEM-0001",
            first_name="Bob", last_name="Autre", status=Member.Status.ACCEPTED,
        )

        self.client = APIClient()
        self.client.force_authenticate(self.staff_a)

    # ---------------------------------------------------------------- outils

    def loans_url(self, org=None):
        org = org or self.org_a
        return reverse("api:staff-loans", kwargs={"organization_id": org.pk})

    def action_url(self, name, loan, org=None):
        org = org or self.org_a
        return reverse(f"api:{name}", kwargs={"organization_id": org.pk, "pk": loan.pk})

    def post_loan(self, copy, member, org=None):
        return self.client.post(
            self.loans_url(org),
            {"book_copy": str(copy.pk), "member": str(member.pk)},
            format="json",
        )

    def make_loan(self, copy=None):
        return checkout_copy(
            organization=self.org_a,
            book_copy_id=(copy or self.copy1).pk,
            member_id=self.member.pk,
            issued_by=self.staff_a,
        )

    # ---------------------------------------------------------------- prêt

    def test_checkout_ok(self):
        response = self.post_loan(self.copy1, self.member)
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertEqual(data["status"], "ACTIVE")
        self.assertEqual(data["inventory_code"], "A-001")

        self.copy1.refresh_from_db()
        self.assertEqual(self.copy1.status, BookCopy.Status.BORROWED)

        policy = LoanPolicy.objects.get(organization=self.org_a)
        expected = timezone.localdate() + timedelta(days=policy.loan_duration_days)
        self.assertEqual(data["due_date"], expected.isoformat())
        self.assertTrue(
            AuditLog.objects.filter(
                organization=self.org_a, action=AuditLog.Action.LOAN_CREATED
            ).exists()
        )

    def test_checkout_already_borrowed(self):
        self.assertEqual(self.post_loan(self.copy1, self.member).status_code, 201)
        self.assertEqual(self.post_loan(self.copy1, self.member).status_code, 409)
        self.assertEqual(Loan.objects.filter(organization=self.org_a).count(), 1)

    def test_checkout_copy_of_other_organization_is_404(self):
        self.assertEqual(self.post_loan(self.copy_b, self.member).status_code, 404)
        self.copy_b.refresh_from_db()
        self.assertEqual(self.copy_b.status, BookCopy.Status.AVAILABLE)

    def test_checkout_member_of_other_organization_is_404(self):
        self.assertEqual(self.post_loan(self.copy1, self.member_b).status_code, 404)

    def test_checkout_member_not_accepted(self):
        self.member.status = Member.Status.PENDING
        self.member.save()
        self.assertEqual(self.post_loan(self.copy1, self.member).status_code, 409)

    def test_checkout_limit_reached(self):
        LoanPolicy.objects.update_or_create(
            organization=self.org_a, defaults={"max_active_loans": 1}
        )
        self.assertEqual(self.post_loan(self.copy1, self.member).status_code, 201)
        self.assertEqual(self.post_loan(self.copy2, self.member).status_code, 409)

    def test_checkout_missing_fields(self):
        response = self.client.post(self.loans_url(), {}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_checkout_invalid_identifier_does_not_crash(self):
        response = self.client.post(
            self.loans_url(), {"book_copy": "abc", "member": "abc"}, format="json"
        )
        self.assertIn(response.status_code, (400, 404))

    def test_other_organization_staff_refused(self):
        self.client.force_authenticate(self.staff_b)
        self.assertEqual(self.post_loan(self.copy1, self.member).status_code, 403)
        self.assertEqual(Loan.objects.count(), 0)

    def test_inactive_staff_refused(self):
        self.membership_a.active = False
        self.membership_a.save()
        self.assertEqual(self.post_loan(self.copy1, self.member).status_code, 403)

    def test_anonymous_refused(self):
        self.client.force_authenticate(None)
        self.assertEqual(self.post_loan(self.copy1, self.member).status_code, 401)

    # ---------------------------------------------------------------- retour

    def test_return_good(self):
        loan = self.make_loan()
        response = self.client.post(
            self.action_url("staff-loan-return", loan), {"outcome": "GOOD"}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "RETURNED")
        self.copy1.refresh_from_db()
        self.assertEqual(self.copy1.status, BookCopy.Status.AVAILABLE)
        self.assertTrue(
            AuditLog.objects.filter(
                organization=self.org_a, action=AuditLog.Action.LOAN_RETURNED
            ).exists()
        )

    def test_return_default_outcome_is_good(self):
        loan = self.make_loan()
        response = self.client.post(self.action_url("staff-loan-return", loan), {}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "RETURNED")

    def test_return_damaged(self):
        loan = self.make_loan()
        response = self.client.post(
            self.action_url("staff-loan-return", loan), {"outcome": "DAMAGED"}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        self.copy1.refresh_from_db()
        self.assertEqual(self.copy1.status, BookCopy.Status.DAMAGED)

    def test_return_twice_is_conflict(self):
        loan = self.make_loan()
        url = self.action_url("staff-loan-return", loan)
        self.assertEqual(self.client.post(url, {}, format="json").status_code, 200)
        self.assertEqual(self.client.post(url, {}, format="json").status_code, 409)

    def test_return_invalid_outcome(self):
        loan = self.make_loan()
        response = self.client.post(
            self.action_url("staff-loan-return", loan), {"outcome": "XXX"}, format="json"
        )
        self.assertEqual(response.status_code, 400)

    def test_return_loan_of_other_organization_is_404(self):
        loan_b = checkout_copy(
            organization=self.org_b, book_copy_id=self.copy_b.pk,
            member_id=self.member_b.pk, issued_by=self.staff_b,
        )
        response = self.client.post(
            reverse(
                "api:staff-loan-return",
                kwargs={"organization_id": self.org_a.pk, "pk": loan_b.pk},
            ),
            {},
            format="json",
        )
        self.assertEqual(response.status_code, 404)
        loan_b.refresh_from_db()
        self.assertEqual(loan_b.status, Loan.Status.ACTIVE)

    # ---------------------------------------------------------------- perte

    def test_lost_shortcut(self):
        loan = self.make_loan()
        response = self.client.post(self.action_url("staff-loan-lost", loan), {}, format="json")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "LOST")
        self.assertIsNone(data["returned_at"])
        self.copy1.refresh_from_db()
        self.assertEqual(self.copy1.status, BookCopy.Status.LOST)
        self.assertTrue(
            AuditLog.objects.filter(
                organization=self.org_a, action=AuditLog.Action.COPY_MARKED_LOST
            ).exists()
        )

    def test_return_with_outcome_lost(self):
        loan = self.make_loan()
        response = self.client.post(
            self.action_url("staff-loan-return", loan), {"outcome": "LOST"}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "LOST")