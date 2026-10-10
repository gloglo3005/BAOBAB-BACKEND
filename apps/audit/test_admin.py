from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.catalog.models import Book, BookCopy
from apps.members.models import Member
from apps.members.services import register_member
from apps.organizations.services import create_organization

from .models import AuditLog

User = get_user_model()


class AuditAdminTests(TestCase):
    def setUp(self):
        self.admin_user = User.objects.create_superuser(
            email="admin@example.com", password="abc12345",
            first_name="Ad", last_name="Min",
        )
        self.client.force_login(self.admin_user)
        self.org = create_organization(name="UL", slug="ul")

    def test_un_livre_cree_dans_l_admin_est_trace(self):
        response = self.client.post(
            reverse("admin:catalog_book_add"),
            {"organization": self.org.pk, "title": "Admin Book", "active": "on"},
        )
        self.assertEqual(response.status_code, 302)
        book = Book.objects.get(title="Admin Book")
        entry = AuditLog.objects.get(action=AuditLog.Action.BOOK_CREATED)
        self.assertEqual(entry.user, self.admin_user)
        self.assertEqual(entry.entity_id, str(book.pk))

    def test_un_exemplaire_ajoute_dans_l_admin_est_trace(self):
        response = self.client.post(
            reverse("admin:catalog_bookcopy_add"),
            {
                "organization": self.org.pk,
                "book": self._book().pk,
                "inventory_code": "UL-001",
                "status": "AVAILABLE",
                "condition": "GOOD",
            },
        )
        self.assertEqual(response.status_code, 302)
        copy = BookCopy.objects.get(inventory_code="UL-001")
        entry = AuditLog.objects.get(action=AuditLog.Action.COPY_CREATED)
        self.assertEqual(entry.user, self.admin_user)
        self.assertEqual(entry.entity_id, str(copy.pk))

    def test_un_exemplaire_ajoute_dans_la_page_du_livre_est_trace(self):
        book = self._book()
        response = self.client.post(
            reverse("admin:catalog_book_change", args=[book.pk]),
            {
                "title": "Livre", "active": "on",
                "copies-TOTAL_FORMS": "1", "copies-INITIAL_FORMS": "0",
                "copies-MIN_NUM_FORMS": "0", "copies-MAX_NUM_FORMS": "1000",
                "copies-0-inventory_code": "UL-007",
                "copies-0-status": "AVAILABLE", "copies-0-condition": "GOOD",
            },
        )
        self.assertEqual(response.status_code, 302, response.content[:500])
        copy = BookCopy.objects.get(inventory_code="UL-007")
        entry = AuditLog.objects.get(
            action=AuditLog.Action.COPY_CREATED, entity_id=str(copy.pk)
        )
        self.assertEqual(entry.user, self.admin_user)

    def test_la_suspension_depuis_l_admin_est_tracee_au_nom_de_l_admin(self):
        member = register_member(organization=self.org, first_name="A", last_name="B")
        response = self.client.post(
            reverse("admin:members_member_changelist"),
            {"action": "suspend", "_selected_action": [member.pk]},
        )
        self.assertEqual(response.status_code, 302)
        member.refresh_from_db()
        self.assertEqual(member.status, Member.Status.SUSPENDED)
        entry = AuditLog.objects.get(action=AuditLog.Action.MEMBER_SUSPENDED)
        self.assertEqual(entry.user, self.admin_user)

    def test_le_journal_se_consulte_mais_ne_se_modifie_pas(self):
        self._book()
        self.assertEqual(
            self.client.get(reverse("admin:audit_auditlog_changelist")).status_code, 200
        )
        entry = AuditLog.objects.get()
        self.assertEqual(
            self.client.get(
                reverse("admin:audit_auditlog_change", args=[entry.pk])
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.get(reverse("admin:audit_auditlog_add")).status_code, 403
        )
        self.assertEqual(
            self.client.post(
                reverse("admin:audit_auditlog_delete", args=[entry.pk]), {"post": "yes"}
            ).status_code,
            403,
        )
        self.assertTrue(AuditLog.objects.filter(pk=entry.pk).exists())

    def _book(self):
        from apps.catalog.services import create_book
        return create_book(organization=self.org, title="Livre")
