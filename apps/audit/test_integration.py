from unittest.mock import patch

from django.core.exceptions import ValidationError

from apps.catalog.models import BookCopy
from apps.circulation.models import Loan
from apps.circulation.services import ReturnOutcome, checkout_copy, return_copy
from apps.circulation.tests import CirculationTestCase
from apps.members.models import Member
from apps.members.services import (
    register_member,
    request_membership,
    suspend_member,
)

from .models import AuditLog


class LoanAuditTests(CirculationTestCase):
    def logs(self, action, organization=None):
        return AuditLog.objects.filter(
            action=action, organization=organization or self.org_a
        )

    def test_le_pret_est_trace_avec_son_auteur(self):
        loan = self.checkout()
        entry = self.logs(AuditLog.Action.LOAN_CREATED).get()
        self.assertEqual(entry.user, self.staff)
        self.assertEqual(entry.entity_type, "Loan")
        self.assertEqual(entry.entity_id, str(loan.pk))
        self.assertEqual(entry.metadata["book_copy"], "A-001")
        self.assertEqual(entry.metadata["member"], self.member_a.member_number)
        self.assertEqual(entry.metadata["due_date"], loan.due_date.isoformat())
        self.assertEqual(
            entry.metadata["copy_status"], {"from": "AVAILABLE", "to": "BORROWED"}
        )

    def test_un_pret_refuse_ne_laisse_aucune_trace(self):
        self.member_a.status = Member.Status.SUSPENDED
        self.member_a.save()
        with self.assertRaises(ValidationError):
            self.checkout()
        self.assertEqual(self.logs(AuditLog.Action.LOAN_CREATED).count(), 0)

    def test_le_pret_est_annule_si_le_journal_echoue(self):
        with patch("apps.circulation.services.log_action", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.checkout()
        self.assertEqual(Loan.objects.count(), 0)
        self.copy_a.refresh_from_db()
        self.assertEqual(self.copy_a.status, BookCopy.Status.AVAILABLE)

    def test_le_retour_est_trace(self):
        loan = self.checkout()
        return_copy(organization=self.org_a, loan_id=loan.pk, received_by=self.staff)
        entry = self.logs(AuditLog.Action.LOAN_RETURNED).get()
        self.assertEqual(entry.user, self.staff)
        self.assertEqual(entry.entity_id, str(loan.pk))
        self.assertEqual(entry.metadata["outcome"], "GOOD")
        self.assertEqual(entry.metadata["days_overdue"], 0)
        self.assertEqual(
            entry.metadata["copy_status"], {"from": "BORROWED", "to": "AVAILABLE"}
        )

    def test_un_retour_endommage_est_trace_comme_retour(self):
        loan = self.checkout()
        return_copy(
            organization=self.org_a, loan_id=loan.pk, received_by=self.staff,
            outcome=ReturnOutcome.DAMAGED,
        )
        entry = self.logs(AuditLog.Action.LOAN_RETURNED).get()
        self.assertEqual(entry.metadata["outcome"], "DAMAGED")
        self.assertIn("endommagé", entry.description)

    def test_un_exemplaire_perdu_est_trace_comme_perte_et_pas_comme_retour(self):
        loan = self.checkout()
        return_copy(
            organization=self.org_a, loan_id=loan.pk, received_by=self.staff,
            outcome=ReturnOutcome.LOST,
        )
        entry = self.logs(AuditLog.Action.COPY_MARKED_LOST).get()
        self.assertEqual(entry.entity_type, "BookCopy")
        self.assertEqual(entry.entity_id, str(self.copy_a.pk))
        self.assertEqual(entry.metadata["loan_id"], loan.pk)
        self.assertEqual(self.logs(AuditLog.Action.LOAN_RETURNED).count(), 0)

    def test_le_retard_est_note_au_retour(self):
        from datetime import timedelta
        from django.utils import timezone

        loan = self.checkout()
        Loan.objects.filter(pk=loan.pk).update(
            due_date=timezone.localdate() - timedelta(days=5)
        )
        return_copy(organization=self.org_a, loan_id=loan.pk, received_by=self.staff)
        entry = self.logs(AuditLog.Action.LOAN_RETURNED).get()
        self.assertEqual(entry.metadata["days_overdue"], 5)

    def test_un_retour_refuse_ne_laisse_aucune_trace(self):
        loan = self.checkout()
        with self.assertRaises(ValidationError):
            return_copy(
                organization=self.org_a, loan_id=loan.pk, received_by=self.staff_b
            )
        self.assertEqual(self.logs(AuditLog.Action.LOAN_RETURNED).count(), 0)

    def test_chaque_structure_ne_voit_que_son_journal(self):
        self.checkout()
        checkout_copy(
            organization=self.org_b, book_copy_id=self.copy_b.pk,
            member_id=self.member_b.pk, issued_by=self.staff_b,
        )
        self.assertEqual(self.logs(AuditLog.Action.LOAN_CREATED).count(), 1)
        self.assertEqual(
            self.logs(AuditLog.Action.LOAN_CREATED, self.org_b).get().user, self.staff_b
        )


class MemberAuditTests(CirculationTestCase):
    def test_l_inscription_au_comptoir_est_tracee(self):
        member = register_member(
            organization=self.org_a, first_name="Efua", last_name="Dosso",
            performed_by=self.staff,
        )
        entry = AuditLog.objects.get(
            action=AuditLog.Action.MEMBER_CREATED, entity_id=str(member.pk)
        )
        self.assertEqual(entry.user, self.staff)
        self.assertEqual(entry.metadata["origin"], "counter")
        self.assertEqual(entry.metadata["member_number"], member.member_number)

    def test_la_demande_d_un_lecteur_est_tracee_a_son_nom(self):
        reader = type(self.staff).objects.create_user(
            email="lecteur@example.com", password="abc12345",
            first_name="Lea", last_name="Tete",
        )
        member = request_membership(user=reader, organization=self.org_a)
        entry = AuditLog.objects.get(
            action=AuditLog.Action.MEMBER_CREATED, entity_id=str(member.pk)
        )
        self.assertEqual(entry.user, reader)
        self.assertEqual(entry.metadata["origin"], "request")
        self.assertEqual(entry.metadata["status"], "PENDING")

    def test_la_suspension_est_tracee(self):
        suspend_member(self.member_a, performed_by=self.staff)
        entry = AuditLog.objects.get(action=AuditLog.Action.MEMBER_SUSPENDED)
        self.assertEqual(entry.user, self.staff)
        self.assertEqual(entry.entity_id, str(self.member_a.pk))
        self.assertEqual(
            entry.metadata["status"], {"from": "ACCEPTED", "to": "SUSPENDED"}
        )

    def test_une_suspension_refusee_ne_laisse_aucune_trace(self):
        suspend_member(self.member_a)
        with self.assertRaises(ValidationError):
            suspend_member(self.member_a)
        self.assertEqual(
            AuditLog.objects.filter(action=AuditLog.Action.MEMBER_SUSPENDED).count(), 1
        )

    def test_les_autres_changements_de_statut_ne_sont_pas_journalises_en_suspension(self):
        suspend_member(self.member_a)
        from apps.members.services import reactivate_member
        reactivate_member(self.member_a)
        self.assertEqual(
            AuditLog.objects.filter(action=AuditLog.Action.MEMBER_SUSPENDED).count(), 1
        )
