from datetime import date
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, TransactionTestCase

from apps.catalog.services import add_copy, create_book
from apps.organizations.services import create_organization

from .models import AuditLog, AuditLogImmutableError
from .services import log_action

User = get_user_model()


class LogActionTests(TestCase):
    def setUp(self):
        self.org = create_organization(name="A", slug="a")
        self.book = create_book(organization=self.org, title="Livre")
        self.user = User.objects.create_user(
            email="u@example.com", password="abc12345", first_name="U", last_name="V"
        )

    def test_ecrit_une_ligne_complete(self):
        entry = log_action(
            organization=self.org,
            user=self.user,
            action=AuditLog.Action.BOOK_UPDATED,
            entity=self.book,
            description="Titre modifié.",
            metadata={"title": {"from": "Livre", "to": "Livre 2"}},
        )
        entry.refresh_from_db()
        self.assertEqual(entry.organization, self.org)
        self.assertEqual(entry.user, self.user)
        self.assertEqual(entry.action, "BOOK_UPDATED")
        self.assertEqual(entry.entity_type, "Book")
        self.assertEqual(entry.entity_id, str(self.book.pk))
        self.assertEqual(entry.metadata["title"]["to"], "Livre 2")
        self.assertIsNotNone(entry.created_at)

    def test_l_auteur_peut_etre_vide(self):
        entry = log_action(
            organization=self.org, action=AuditLog.Action.BOOK_UPDATED, entity=self.book
        )
        self.assertIsNone(entry.user)

    def test_les_dates_passent_dans_les_details(self):
        entry = log_action(
            organization=self.org,
            action=AuditLog.Action.BOOK_UPDATED,
            entity=self.book,
            metadata={"due_date": date(2026, 10, 15)},
        )
        entry.refresh_from_db()
        self.assertEqual(entry.metadata["due_date"], "2026-10-15")

    def test_refuse_une_action_inconnue(self):
        with self.assertRaises(ValueError):
            log_action(organization=self.org, action="N_IMPORTE_QUOI", entity=self.book)


class LogActionOutsideTransactionTests(TransactionTestCase):
    def test_refuse_hors_transaction(self):
        org = create_organization(name="A", slug="a")
        book = create_book(organization=org, title="Livre")
        with self.assertRaises(RuntimeError):
            log_action(
                organization=org, action=AuditLog.Action.BOOK_UPDATED, entity=book
            )


class ImmutabilityTests(TestCase):
    def setUp(self):
        self.org = create_organization(name="A", slug="a")
        self.book = create_book(organization=self.org, title="Livre")
        self.entry = AuditLog.objects.get(action=AuditLog.Action.BOOK_CREATED)

    def test_une_ligne_ne_se_modifie_pas(self):
        self.entry.description = "Truqué"
        with self.assertRaises(AuditLogImmutableError):
            self.entry.save()

    def test_une_ligne_ne_se_supprime_pas(self):
        with self.assertRaises(AuditLogImmutableError):
            self.entry.delete()

    def test_pas_de_modification_ni_suppression_en_masse(self):
        with self.assertRaises(AuditLogImmutableError):
            AuditLog.objects.update(description="Truqué")
        with self.assertRaises(AuditLogImmutableError):
            AuditLog.objects.all().delete()
        self.assertTrue(AuditLog.objects.filter(pk=self.entry.pk).exists())

    def test_le_catalogue_trace_la_creation_du_livre_et_de_l_exemplaire(self):
        add_copy(book=self.book, inventory_code="A-001")
        actions = list(AuditLog.objects.values_list("action", flat=True))
        self.assertIn("BOOK_CREATED", actions)
        self.assertIn("COPY_CREATED", actions)

    def test_creation_annulee_si_le_livre_est_invalide(self):
        before = AuditLog.objects.count()
        with patch("apps.catalog.services.log_action", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                create_book(organization=self.org, title="Autre livre")
        self.assertEqual(AuditLog.objects.count(), before)
        from apps.catalog.models import Book
        self.assertFalse(Book.objects.filter(title="Autre livre").exists())
