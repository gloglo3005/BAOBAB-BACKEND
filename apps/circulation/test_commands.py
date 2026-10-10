from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from .models import Loan
from .services import checkout_copy, return_copy
from .tests import CirculationTestCase


class ListLoansTests(CirculationTestCase):
    def run_command(self, *args, organization="a"):
        out = StringIO()
        call_command("list_loans", "--organization", organization, *args, stdout=out)
        return out.getvalue()

    def make_overdue(self, loan, days=3):
        Loan.objects.filter(pk=loan.pk).update(
            due_date=timezone.localdate() - timedelta(days=days)
        )

    def test_aucun_pret(self):
        self.assertIn("aucun", self.run_command())

    def test_liste_les_prets_en_cours(self):
        self.checkout()
        output = self.run_command()
        self.assertIn("A-001", output)
        self.assertIn(self.member_a.member_number, output)
        self.assertNotIn("EN RETARD", output)

    def test_overdue_ne_montre_que_les_retards(self):
        loan = self.checkout()
        self.assertIn("aucun", self.run_command("--overdue"))

        self.make_overdue(loan, days=3)
        output = self.run_command("--overdue")
        self.assertIn("A-001", output)
        self.assertIn("EN RETARD (3 j)", output)

    def test_les_prets_rendus_ne_sont_listes_qu_avec_all(self):
        loan = self.checkout()
        return_copy(organization=self.org_a, loan_id=loan.pk, received_by=self.staff)

        self.assertIn("aucun", self.run_command())
        self.assertIn("A-001", self.run_command("--all"))

    def test_ne_montre_pas_les_prets_d_une_autre_structure(self):
        checkout_copy(
            organization=self.org_b, book_copy_id=self.copy_b.pk,
            member_id=self.member_b.pk, issued_by=self.staff_b,
        )
        self.assertNotIn("B-001", self.run_command("--all", organization="a"))
        self.assertIn("B-001", self.run_command(organization="b"))

    def test_structure_inconnue(self):
        with self.assertRaises(CommandError):
            self.run_command(organization="nexiste-pas")