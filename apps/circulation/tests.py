import threading
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase, TransactionTestCase, skipUnlessDBFeature
from django.utils import timezone

from apps.catalog.models import BookCopy
from apps.catalog.services import add_copy, create_book
from apps.members.models import Member
from apps.members.services import register_member, suspend_member
from apps.organizations.models import Organization, OrganizationStaff
from apps.organizations.services import create_organization

from .models import Loan
from .services import ReturnOutcome, checkout_copy, return_copy

User = get_user_model()


def make_staff(organization=None, email="staff@example.com"):
    """Crée un agent ; rattaché à la structure donnée s'il y en a une."""
    user = User.objects.create_user(
        email=email, password="abc12345", first_name="Kossi", last_name="Amah"
    )
    if organization is not None:
        OrganizationStaff.objects.create(user=user, organization=organization)
    return user


class CirculationTestCase(TestCase):
    """Deux structures, chacune avec un livre, une copie, un membre accepté
    et un agent Staff rattaché (self.staff pour A, self.staff_b pour B)."""

    def setUp(self):
        self.org_a = create_organization(name="Structure A", slug="a")
        self.book_a = create_book(organization=self.org_a, title="Livre A")
        self.copy_a = add_copy(book=self.book_a, inventory_code="A-001")
        self.member_a = register_member(
            organization=self.org_a, first_name="Ama", last_name="Kofi"
        )

        self.org_b = create_organization(name="Structure B", slug="b")
        self.book_b = create_book(organization=self.org_b, title="Livre B")
        self.copy_b = add_copy(book=self.book_b, inventory_code="B-001")
        self.member_b = register_member(
            organization=self.org_b, first_name="Yao", last_name="Mensah"
        )

        self.staff = make_staff(self.org_a)
        self.staff_b = make_staff(self.org_b, email="staff-b@example.com")

    def checkout(self, copy=None, member=None, organization=None):
        return checkout_copy(
            organization=organization or self.org_a,
            book_copy_id=(copy or self.copy_a).pk,
            member_id=(member or self.member_a).pk,
            issued_by=self.staff,
        )

    def new_copy(self, code):
        return add_copy(book=self.book_a, inventory_code=code)


class CheckoutTests(CirculationTestCase):
    def test_prete_une_copie_disponible(self):
        loan = self.checkout()

        self.assertEqual(loan.status, Loan.Status.ACTIVE)
        self.assertEqual(loan.organization, self.org_a)
        self.assertEqual(loan.book_copy, self.copy_a)
        self.assertEqual(loan.member, self.member_a)
        self.assertEqual(loan.issued_by, self.staff)
        self.assertIsNone(loan.returned_at)
        self.copy_a.refresh_from_db()
        self.assertEqual(self.copy_a.status, BookCopy.Status.BORROWED)

    def test_calcule_l_echeance_avec_la_politique_par_defaut(self):
        loan = self.checkout()
        self.assertEqual(loan.due_date, timezone.localdate() + timedelta(days=14))

    def test_calcule_l_echeance_avec_la_politique_de_la_structure(self):
        policy = self.org_a.loan_policy
        policy.loan_duration_days = 7
        policy.save()

        loan = self.checkout()
        self.assertEqual(loan.due_date, timezone.localdate() + timedelta(days=7))

    def test_refuse_une_copie_deja_empruntee(self):
        other = register_member(organization=self.org_a, first_name="Eve", last_name="Dia")
        self.checkout()

        with self.assertRaisesMessage(ValidationError, "déjà emprunté"):
            self.checkout(member=other)
        self.assertEqual(Loan.objects.active().count(), 1)

    def test_refuse_une_copie_qui_n_est_pas_disponible(self):
        for status in (
            BookCopy.Status.LOST,
            BookCopy.Status.DAMAGED,
            BookCopy.Status.UNAVAILABLE,
        ):
            with self.subTest(status=status):
                copy = self.new_copy(f"X-{status}")
                copy.status = status
                copy.save()
                with self.assertRaises(ValidationError):
                    self.checkout(copy=copy)

    def test_refuse_un_membre_suspendu(self):
        suspend_member(self.member_a)
        with self.assertRaisesMessage(ValidationError, "suspendu"):
            self.checkout()

    def test_refuse_un_membre_non_accepte(self):
        pending = Member.objects.create(
            organization=self.org_a, member_number="P-1",
            first_name="Attente", last_name="Test",
        )
        with self.assertRaisesMessage(ValidationError, "pas été accepté"):
            self.checkout(member=pending)

    def test_refuse_au_dela_de_la_limite_de_prets(self):
        policy = self.org_a.loan_policy
        policy.max_active_loans = 2
        policy.save()
        self.checkout()
        self.checkout(copy=self.new_copy("A-002"))

        with self.assertRaisesMessage(ValidationError, "Limite atteinte"):
            self.checkout(copy=self.new_copy("A-003"))
        self.assertEqual(Loan.objects.active().count(), 2)

    def test_un_retour_libere_une_place_dans_la_limite(self):
        policy = self.org_a.loan_policy
        policy.max_active_loans = 1
        policy.save()
        loan = self.checkout()
        return_copy(organization=self.org_a, loan_id=loan.pk, received_by=self.staff)

        self.checkout(copy=self.new_copy("A-002"))  # ne doit pas lever d'erreur

    def test_refuse_si_la_structure_n_est_pas_active(self):
        self.org_a.status = Organization.Status.SUSPENDED
        self.org_a.save()
        with self.assertRaisesMessage(ValidationError, "pas active"):
            self.checkout()

    def test_refuse_si_le_module_bibliotheque_est_desactive(self):
        self.org_a.library_module = False
        self.org_a.save()
        with self.assertRaisesMessage(ValidationError, "module bibliothèque"):
            self.checkout()

    def test_un_echec_ne_modifie_rien(self):
        suspend_member(self.member_a)
        with self.assertRaises(ValidationError):
            self.checkout()

        self.assertEqual(Loan.objects.count(), 0)
        self.copy_a.refresh_from_db()
        self.assertEqual(self.copy_a.status, BookCopy.Status.AVAILABLE)


class CheckoutIsolationTests(CirculationTestCase):
    def test_staff_a_ne_prete_pas_une_copie_de_la_structure_b(self):
        with self.assertRaisesMessage(ValidationError, "introuvable"):
            self.checkout(copy=self.copy_b)  # organization=A par défaut
        self.assertEqual(Loan.objects.count(), 0)
        self.copy_b.refresh_from_db()
        self.assertEqual(self.copy_b.status, BookCopy.Status.AVAILABLE)

    def test_staff_a_ne_prete_pas_a_un_membre_de_la_structure_b(self):
        with self.assertRaisesMessage(ValidationError, "introuvable"):
            self.checkout(member=self.member_b)
        self.assertEqual(Loan.objects.count(), 0)
        self.copy_a.refresh_from_db()
        self.assertEqual(self.copy_a.status, BookCopy.Status.AVAILABLE)

    def test_un_identifiant_inexistant_donne_le_meme_message_qu_une_autre_structure(self):
        with self.assertRaises(ValidationError) as other_org:
            self.checkout(copy=self.copy_b)
        with self.assertRaises(ValidationError) as missing:
            checkout_copy(
                organization=self.org_a, book_copy_id=999999,
                member_id=self.member_a.pk, issued_by=self.staff,
            )
        self.assertEqual(other_org.exception.messages, missing.exception.messages)


class StaffAuthorizationTests(CirculationTestCase):
    """L'agent qui prête ou reçoit un livre doit être Staff actif de la structure."""

    def checkout_as(self, user):
        return checkout_copy(
            organization=self.org_a, book_copy_id=self.copy_a.pk,
            member_id=self.member_a.pk, issued_by=user,
        )

    def assert_rien_n_a_change_au_pret(self):
        self.assertEqual(Loan.objects.count(), 0)
        self.copy_a.refresh_from_db()
        self.assertEqual(self.copy_a.status, BookCopy.Status.AVAILABLE)

    def test_refuse_un_staff_d_une_autre_structure_au_pret(self):
        with self.assertRaisesMessage(ValidationError, "pas autorisé"):
            self.checkout_as(self.staff_b)
        self.assert_rien_n_a_change_au_pret()

    def test_refuse_un_compte_sans_rattachement_au_pret(self):
        outsider = make_staff(email="outsider@example.com")
        with self.assertRaisesMessage(ValidationError, "pas autorisé"):
            self.checkout_as(outsider)
        self.assert_rien_n_a_change_au_pret()

    def test_refuse_un_staff_desactive_au_pret(self):
        OrganizationStaff.objects.filter(
            user=self.staff, organization=self.org_a
        ).update(active=False)
        with self.assertRaisesMessage(ValidationError, "pas autorisé"):
            self.checkout_as(self.staff)
        self.assert_rien_n_a_change_au_pret()

    def test_refuse_un_compte_utilisateur_inactif_au_pret(self):
        self.staff.is_active = False
        self.staff.save()
        with self.assertRaisesMessage(ValidationError, "pas autorisé"):
            self.checkout_as(self.staff)
        self.assert_rien_n_a_change_au_pret()

    def test_refuse_un_staff_d_une_autre_structure_au_retour(self):
        loan = self.checkout()
        with self.assertRaisesMessage(ValidationError, "pas autorisé"):
            return_copy(
                organization=self.org_a, loan_id=loan.pk, received_by=self.staff_b
            )
        loan.refresh_from_db()
        self.assertEqual(loan.status, Loan.Status.ACTIVE)
        self.assertIsNone(loan.received_by)
        self.copy_a.refresh_from_db()
        self.assertEqual(self.copy_a.status, BookCopy.Status.BORROWED)

    def test_refuse_un_staff_desactive_au_retour(self):
        loan = self.checkout()
        OrganizationStaff.objects.filter(
            user=self.staff, organization=self.org_a
        ).update(active=False)
        with self.assertRaisesMessage(ValidationError, "pas autorisé"):
            return_copy(
                organization=self.org_a, loan_id=loan.pk, received_by=self.staff
            )
        loan.refresh_from_db()
        self.assertEqual(loan.status, Loan.Status.ACTIVE)

    def test_un_staff_rattache_aux_deux_structures_travaille_dans_chacune(self):
        OrganizationStaff.objects.create(user=self.staff, organization=self.org_b)
        loan_a = self.checkout()
        loan_b = checkout_copy(
            organization=self.org_b, book_copy_id=self.copy_b.pk,
            member_id=self.member_b.pk, issued_by=self.staff,
        )
        self.assertEqual(loan_a.organization, self.org_a)
        self.assertEqual(loan_b.organization, self.org_b)


class ReturnTests(CirculationTestCase):
    def setUp(self):
        super().setUp()
        self.loan = self.checkout()

    def give_back(self, **kwargs):
        return return_copy(
            organization=self.org_a, loan_id=self.loan.pk,
            received_by=self.staff, **kwargs,
        )

    def test_retour_en_bon_etat(self):
        loan = self.give_back()

        self.assertEqual(loan.status, Loan.Status.RETURNED)
        self.assertIsNotNone(loan.returned_at)
        self.assertEqual(loan.received_by, self.staff)
        self.copy_a.refresh_from_db()
        self.assertEqual(self.copy_a.status, BookCopy.Status.AVAILABLE)

    def test_le_pret_renvoye_porte_une_copie_a_jour(self):
        loan = self.give_back()
        self.assertEqual(loan.book_copy.status, BookCopy.Status.AVAILABLE)

    def test_retour_d_une_copie_endommagee(self):
        loan = self.give_back(outcome=ReturnOutcome.DAMAGED)

        self.assertEqual(loan.status, Loan.Status.RETURNED)
        self.copy_a.refresh_from_db()
        self.assertEqual(self.copy_a.status, BookCopy.Status.DAMAGED)

    def test_copie_perdue(self):
        loan = self.give_back(outcome=ReturnOutcome.LOST, notes="Déclaré perdu par le membre")

        self.assertEqual(loan.status, Loan.Status.LOST)
        self.assertIsNone(loan.returned_at)
        self.assertEqual(loan.received_by, self.staff)
        self.assertIn("perdu", loan.notes)
        self.copy_a.refresh_from_db()
        self.assertEqual(self.copy_a.status, BookCopy.Status.LOST)

    def test_une_copie_perdue_ne_peut_plus_etre_pretee(self):
        self.give_back(outcome=ReturnOutcome.LOST)
        with self.assertRaisesMessage(ValidationError, "perdu"):
            self.checkout()

    def test_refuse_un_pret_deja_cloture(self):
        self.give_back()
        with self.assertRaisesMessage(ValidationError, "déjà clôturé"):
            self.give_back()

    def test_refuse_un_constat_inconnu(self):
        with self.assertRaises(ValueError):
            self.give_back(outcome="N_IMPORTE_QUOI")
        self.loan.refresh_from_db()
        self.assertEqual(self.loan.status, Loan.Status.ACTIVE)

    def test_staff_b_ne_cloture_pas_un_pret_de_la_structure_a(self):
        with self.assertRaisesMessage(ValidationError, "introuvable"):
            return_copy(
                organization=self.org_b, loan_id=self.loan.pk, received_by=self.staff_b
            )
        self.loan.refresh_from_db()
        self.assertEqual(self.loan.status, Loan.Status.ACTIVE)

    def test_la_copie_rendue_peut_etre_repretee_et_l_historique_est_conserve(self):
        self.give_back()
        second = self.checkout()

        self.assertNotEqual(second.pk, self.loan.pk)
        self.assertEqual(self.copy_a.loans.count(), 2)
        self.loan.refresh_from_db()
        self.assertEqual(self.loan.status, Loan.Status.RETURNED)


class OverdueTests(CirculationTestCase):
    def test_detecte_un_pret_en_retard(self):
        loan = self.checkout()
        Loan.objects.filter(pk=loan.pk).update(
            due_date=timezone.localdate() - timedelta(days=3)
        )
        loan.refresh_from_db()

        self.assertTrue(loan.is_overdue)
        self.assertEqual(loan.days_overdue, 3)
        self.assertIn(loan, Loan.objects.overdue())

    def test_un_pret_dans_les_delais_n_est_pas_en_retard(self):
        loan = self.checkout()
        self.assertFalse(loan.is_overdue)
        self.assertNotIn(loan, Loan.objects.overdue())

    def test_un_pret_rendu_n_est_plus_en_retard(self):
        loan = self.checkout()
        Loan.objects.filter(pk=loan.pk).update(
            due_date=timezone.localdate() - timedelta(days=3)
        )
        loan = return_copy(
            organization=self.org_a, loan_id=loan.pk, received_by=self.staff
        )

        self.assertFalse(loan.is_overdue)
        self.assertNotIn(loan, Loan.objects.overdue())


@skipUnlessDBFeature("has_select_for_update")
class ConcurrentCheckoutTests(TransactionTestCase):
    """Deux demandes simultanées sur la même copie : un seul prêt actif.

    Ce test n'a de sens que sur PostgreSQL (SQLite ne verrouille pas les
    lignes), il est donc ignoré automatiquement ailleurs.
    """

    def test_un_seul_pret_actif_pour_deux_demandes_simultanees(self):
        organization = create_organization(name="A", slug="a")
        staff = make_staff(organization)
        book = create_book(organization=organization, title="Livre")
        copy = add_copy(book=book, inventory_code="A-001")
        members = [
            register_member(organization=organization, first_name=name, last_name="Test")
            for name in ("Ama", "Yao")
        ]

        barrier = threading.Barrier(2)
        results = []

        def worker(member):
            try:
                barrier.wait()  # les deux fils démarrent exactement ensemble
                checkout_copy(
                    organization=organization, book_copy_id=copy.pk,
                    member_id=member.pk, issued_by=staff,
                )
                results.append("ok")
            except ValidationError:
                results.append("refusé")
            finally:
                connection.close()  # chaque fil a sa propre connexion

        threads = [threading.Thread(target=worker, args=(m,)) for m in members]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(sorted(results), ["ok", "refusé"])
        self.assertEqual(Loan.objects.active().filter(book_copy=copy).count(), 1)