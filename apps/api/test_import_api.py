import shutil
import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.api.test_platform_api import as_user, staff_user
from apps.api.test_staff_read import make_org, make_user
from apps.audit.models import AuditLog
from apps.catalog.models import Book, BookCopy

HEADER = "title,inventory_code,author,category"
MEDIA = tempfile.mkdtemp()


def csv_file(*rows, name="catalogue.csv"):
    content = "\n".join([HEADER, *rows]) + "\n"
    return SimpleUploadedFile(name, content.encode("utf-8"), content_type="text/csv")


@override_settings(MEDIA_ROOT=MEDIA)
class ImportApiTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA, ignore_errors=True)

    def setUp(self):
        self.org = make_org("Biblio Import")
        self.other = make_org("Autre Biblio")
        self.agent = staff_user(self.org, "agent-imp")
        self.foreign = staff_user(self.other, "etranger-imp")
        self.client = as_user(self.agent)

    def url(self, path=""):
        return f"/api/v1/staff/{self.org.pk}/imports/{path}"

    def upload(self, *rows, client=None, name="catalogue.csv"):
        return (client or self.client).post(self.url(), {"file": csv_file(*rows, name=name)}, format="multipart")

    # ------------------------------------------------------------------ accès

    def test_access_rules(self):
        self.assertIn(APIClient().get(self.url()).status_code, (401, 403))
        self.assertEqual(as_user(self.foreign).get(self.url()).status_code, 403)
        no_library = make_org("Cowork seul", library_module=False)
        agent = staff_user(no_library, "cowork-imp")
        self.assertEqual(as_user(agent).get(f"/api/v1/staff/{no_library.pk}/imports/").status_code, 403)

    # ------------------------------------------------------------------ envoi et aperçu

    def test_upload_gives_a_preview_and_writes_nothing(self):
        response = self.upload("Une vie de boy,BIB-001,Ferdinand Oyono,Roman", "Une vie de boy,BIB-002,Ferdinand Oyono,Roman")
        self.assertEqual(response.status_code, 201, response.content)
        data = response.data
        self.assertEqual((data["status"], data["row_count"], data["error_count"]), ("PREVIEWED", 2, 0))
        self.assertTrue(data["can_confirm"])
        self.assertEqual((data["summary"]["books_new"], data["summary"]["copies"]), (1, 2))
        self.assertEqual(Book.objects.filter(organization=self.org).count(), 0)

    def test_bad_files_are_refused(self):
        wrong = self.client.post(self.url(), {"file": SimpleUploadedFile("x.txt", b"abc")}, format="multipart")
        self.assertEqual(wrong.status_code, 400)
        missing = self.client.post(self.url(), {}, format="multipart")
        self.assertEqual(missing.status_code, 400)
        no_title = self.client.post(
            self.url(), {"file": SimpleUploadedFile("a.csv", b"inventory_code,author\nBIB-1,Moi\n")}, format="multipart"
        )
        self.assertEqual(no_title.status_code, 400)

    def test_row_errors_block_confirmation(self):
        response = self.upload("Bon livre,BIB-001,Auteur,Roman", ",BIB-002,Auteur,Roman")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.data["error_count"], 1)
        self.assertFalse(response.data["can_confirm"])
        self.assertEqual(response.data["errors"][0]["message"], "Titre manquant.")
        refused = self.client.post(self.url(f"{response.data['id']}/confirm/"))
        self.assertEqual(refused.status_code, 409)
        self.assertEqual(Book.objects.filter(organization=self.org).count(), 0)

    # ------------------------------------------------------------------ confirmation

    def test_confirm_imports_everything_once(self):
        preview = self.upload("Une vie de boy,BIB-001,Ferdinand Oyono,Roman", "Une vie de boy,BIB-002,Ferdinand Oyono,Roman")
        url = self.url(f"{preview.data['id']}/confirm/")
        done = self.client.post(url)
        self.assertEqual(done.status_code, 200, done.content)
        self.assertEqual(done.data["status"], "IMPORTED")
        self.assertEqual(done.data["result"]["books_created"], 1)
        self.assertEqual(Book.objects.filter(organization=self.org).count(), 1)
        self.assertEqual(BookCopy.objects.filter(organization=self.org).count(), 2)
        self.assertTrue(AuditLog.objects.filter(organization=self.org, action="IMPORT_CONFIRMED", user=self.agent).exists())
        self.assertEqual(self.client.post(url).status_code, 409)
        self.assertEqual(BookCopy.objects.filter(organization=self.org).count(), 2)

    def test_second_upload_recognises_existing_book(self):
        first = self.upload("Une vie de boy,BIB-001,Ferdinand Oyono,Roman")
        self.client.post(self.url(f"{first.data['id']}/confirm/"))
        second = self.upload("Une vie de boy,BIB-009,Ferdinand Oyono,Roman")
        self.assertEqual(second.data["summary"]["books_existing"], 1)
        self.assertEqual(second.data["matches_total"] if "matches_total" in second.data else len(second.data["matches"]), 1)

    # ------------------------------------------------------------------ historique et isolation

    def test_history_detail_and_isolation(self):
        first = self.upload("Livre A,A-1,Auteur,Roman")
        listing = self.client.get(self.url()).data
        self.assertEqual(listing["count"], 1)
        self.assertEqual(listing["results"][0]["user_email"], self.agent.email)
        detail = self.client.get(self.url(f"{first.data['id']}/"))
        self.assertEqual(detail.status_code, 200)
        other_client = as_user(self.foreign)
        self.assertEqual(other_client.get(f"/api/v1/staff/{self.other.pk}/imports/{first.data['id']}/").status_code, 404)
        self.assertEqual(other_client.post(f"/api/v1/staff/{self.other.pk}/imports/{first.data['id']}/confirm/").status_code, 404)
        self.assertEqual(self.client.get(self.url("abc/")).status_code, 404)
        self.assertEqual(self.client.post(self.url("abc/confirm/")).status_code, 404)

    def test_template(self):
        data = self.client.get(self.url("template/")).data
        self.assertEqual(data["columns"][0], "title")
        self.assertEqual(data["required"], ["title"])
        self.assertTrue(data["rows"])
