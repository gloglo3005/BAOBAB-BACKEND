import shutil
import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from apps.audit.models import AuditLog
from apps.catalog.models import Book, BookCopy
from apps.imports.models import ImportBatch
from apps.organizations.models import OrganizationStaff

from .test_staff_read import make_org, make_user

HEADER = "title;inventory_code;isbn;author;category;publication_year;publisher;language;location;condition"
GOOD = [
    "Une vie de boy;BIB-001;;Ferdinand Oyono;Roman;1956;Présence Africaine;Français;Rayon A;GOOD",
    "Une vie de boy;BIB-002;;Ferdinand Oyono;Roman;1956;Présence Africaine;Français;Rayon A;GOOD",
]


def csv_file(*rows, name="catalogue.csv"):
    return SimpleUploadedFile(
        name, "\n".join([HEADER, *rows]).encode("utf-8"), content_type="text/csv"
    )


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix="baobab-test-media-"))
class StaffImportsApiTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        from django.conf import settings

        shutil.rmtree(settings.MEDIA_ROOT, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        self.org_a = make_org("Biblio A")
        self.org_b = make_org("Biblio B")
        self.agent_a = make_user("agent-a")
        self.agent_b = make_user("agent-b")
        OrganizationStaff.objects.create(user=self.agent_a, organization=self.org_a)
        OrganizationStaff.objects.create(user=self.agent_b, organization=self.org_b)
        self.client = APIClient()
        self.client.force_authenticate(self.agent_a)

    def url(self, name, org=None, **kwargs):
        return reverse(f"api:{name}", kwargs={"organization_id": (org or self.org_a).pk, **kwargs})

    def upload(self, *rows, **kwargs):
        return self.client.post(
            self.url("staff-imports"), {"file": csv_file(*rows, **kwargs)}, format="multipart"
        )

    def test_apercu_puis_confirmation(self):
        response = self.upload(*GOOD)
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.data["status"], "PREVIEWED")
        self.assertEqual(response.data["summary"]["books_new"], 1)
        self.assertEqual(response.data["summary"]["copies"], 2)
        self.assertTrue(response.data["can_confirm"])
        self.assertFalse(Book.objects.exists())  # l'aperçu n'écrit rien dans le catalogue

        confirm = self.client.post(
            self.url("staff-import-confirm", pk=response.data["id"]), format="json"
        )
        self.assertEqual(confirm.status_code, 200, confirm.content)
        self.assertEqual(confirm.data["status"], "IMPORTED")
        self.assertEqual(confirm.data["result"]["copies_created"], 2)
        self.assertEqual(Book.objects.filter(organization=self.org_a).count(), 1)
        self.assertEqual(BookCopy.objects.filter(organization=self.org_a).count(), 2)
        self.assertTrue(AuditLog.objects.filter(action="IMPORT_CONFIRMED", user=self.agent_a).exists())

        again = self.client.post(self.url("staff-import-confirm", pk=response.data["id"]), format="json")
        self.assertEqual(again.status_code, 409)

    def test_fichier_avec_erreurs_ne_peut_pas_etre_confirme(self):
        response = self.upload(";BIB-001;;;;;;;;GOOD")  # titre manquant
        self.assertEqual(response.status_code, 201)
        self.assertGreater(response.data["error_count"], 0)
        self.assertFalse(response.data["can_confirm"])
        confirm = self.client.post(self.url("staff-import-confirm", pk=response.data["id"]), format="json")
        self.assertEqual(confirm.status_code, 409)
        self.assertFalse(Book.objects.exists())

    def test_fichiers_refuses(self):
        bad_ext = SimpleUploadedFile("notes.txt", b"abc")
        response = self.client.post(self.url("staff-imports"), {"file": bad_ext}, format="multipart")
        self.assertEqual(response.status_code, 400)
        self.assertIn("csv", response.data["detail"])
        no_title = SimpleUploadedFile("x.csv", b"isbn;author\n1;2", content_type="text/csv")
        response = self.client.post(self.url("staff-imports"), {"file": no_title}, format="multipart")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.post(self.url("staff-imports"), {}, format="multipart").status_code, 400)

    def test_historique_et_isolation_entre_structures(self):
        batch_id = self.upload(*GOOD).data["id"]
        history = self.client.get(self.url("staff-imports"))
        self.assertEqual(history.data["count"], 1)
        other = APIClient()
        other.force_authenticate(self.agent_b)
        self.assertEqual(other.get(self.url("staff-imports", org=self.org_b)).data["count"], 0)
        self.assertEqual(other.get(self.url("staff-import-detail", org=self.org_b, pk=batch_id)).status_code, 404)
        self.assertEqual(other.post(self.url("staff-import-confirm", org=self.org_b, pk=batch_id), format="json").status_code, 404)
        self.assertEqual(other.get(self.url("staff-imports", org=self.org_a)).status_code, 403)
        self.assertEqual(self.client.get(self.url("staff-import-detail", pk="abc")).status_code, 404)

    def test_acces_reserve_au_staff(self):
        anonymous = APIClient()
        self.assertEqual(anonymous.get(self.url("staff-imports")).status_code, 401)
        reader = APIClient()
        reader.force_authenticate(make_user("lecteur"))
        self.assertEqual(reader.get(self.url("staff-imports")).status_code, 403)

    def test_modele_csv(self):
        response = self.client.get(self.url("staff-import-template"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response["Content-Type"])
        self.assertIn("title;inventory_code", response.content.decode("utf-8-sig"))
        self.assertEqual(ImportBatch.objects.count(), 0)
