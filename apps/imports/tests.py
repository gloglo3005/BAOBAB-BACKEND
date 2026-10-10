import io
import shutil
import tempfile
from unittest import mock

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError
from django.test import SimpleTestCase, override_settings
from django.urls import reverse
from openpyxl import Workbook

from apps.audit.models import AuditLog
from apps.catalog.models import Author, Book, BookCopy, Category, Location
from apps.catalog.services import add_copy, archive_book, create_book, create_reference
from apps.staff.tests import StaffTestCase

from .catalog_import import ALIASES, REQUIRED_COLUMNS
from .models import ImportBatch
from .readers import MAX_BYTES, MAX_ROWS, ReadError, read_table
from .services import confirm_batch, create_batch

HEADER = (
    "title;inventory_code;isbn;author;category;publication_year;"
    "publisher;language;location;condition"
)


def oyono(code):
    return (
        f"Une vie de boy;{code};;Ferdinand Oyono;Roman;1956;"
        "Présence Africaine;Français;Rayon A;GOOD"
    )


def csv_file(*rows, header=HEADER, name="catalogue.csv"):
    content = "\n".join([header, *rows])
    return SimpleUploadedFile(name, content.encode("utf-8"), content_type="text/csv")


def xlsx_bytes(rows, extra_sheet=False):
    workbook = Workbook()
    sheet = workbook.active
    for row in rows:
        sheet.append(row)
    if extra_sheet:
        workbook.create_sheet("Autre").append(["title"])
        workbook["Autre"].append(["Ne doit pas être lu"])
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def read(data, name="f.csv"):
    return read_table(data, name, ALIASES, REQUIRED_COLUMNS)


# ------------------------------------------------------------------ lecture


class ReaderTests(SimpleTestCase):
    def test_csv_avec_virgule(self):
        table = read("title,inventory_code\nLivre,BIB-1\n".encode("utf-8"))
        self.assertEqual(table.columns, ["title", "inventory_code"])
        self.assertEqual(table.rows[0].values, {"title": "Livre", "inventory_code": "BIB-1"})

    def test_csv_avec_point_virgule_et_signature_utf8(self):
        data = "title;inventory_code\nÉmile Zola;BIB-1\n".encode("utf-8-sig")
        table = read(data)
        self.assertEqual(table.rows[0].values["title"], "Émile Zola")

    def test_en_tetes_francais_acceptes(self):
        data = (
            "Titre;Code d'inventaire;Auteur;Catégorie;Année de publication;"
            "Éditeur;Langue;Emplacement;État\nLivre;B1;A B;Roman;1956;Ed;Français;R1;Bon\n"
        ).encode("utf-8")
        table = read(data)
        self.assertEqual(
            table.columns,
            ["title", "inventory_code", "author", "category", "publication_year",
             "publisher", "language", "location", "condition"],
        )

    def test_colonnes_inconnues_ignorees_et_signalees(self):
        table = read("title;couleur\nLivre;rouge\n".encode("utf-8"))
        self.assertEqual(table.columns, ["title"])
        self.assertEqual(table.ignored_columns, ["couleur"])

    def test_numero_de_ligne_reel_et_lignes_vides_ignorees(self):
        table = read("title\nA\n\n\nB\n".encode("utf-8"))
        self.assertEqual([(r.line, r.values["title"]) for r in table.rows], [(2, "A"), (5, "B")])

    def test_xlsx_premiere_feuille_seulement(self):
        data = xlsx_bytes([["title", "inventory_code"], ["Livre", "BIB-1"]], extra_sheet=True)
        table = read(data, "f.xlsx")
        self.assertEqual([r.values["title"] for r in table.rows], ["Livre"])

    def test_xlsx_nombres_lus_comme_du_texte(self):
        data = xlsx_bytes([["title", "publication_year"], ["Livre", 1956], ["Autre", 1956.0]])
        table = read(data, "f.xlsx")
        self.assertEqual([r.values["publication_year"] for r in table.rows], ["1956", "1956"])

    def test_xlsx_numero_de_ligne_de_la_feuille(self):
        data = xlsx_bytes([["title"], ["A"], [None], ["B"]])
        table = read(data, "f.xlsx")
        self.assertEqual([r.line for r in table.rows], [2, 4])

    def test_xlsx_abime(self):
        with self.assertRaisesMessage(ReadError, "XLSX"):
            read(b"pas un zip", "f.xlsx")

    def test_refuse_un_csv_non_utf8(self):
        with self.assertRaisesMessage(ReadError, "UTF-8"):
            read("title\nÉmile".encode("cp1252"))

    def test_refuse_un_autre_format(self):
        with self.assertRaisesMessage(ReadError, ".csv ou .xlsx"):
            read(b"title\nA", "f.txt")

    def test_refuse_un_fichier_vide(self):
        with self.assertRaisesMessage(ReadError, "vide"):
            read(b"", "f.csv")
        with self.assertRaisesMessage(ReadError, "aucune ligne de données"):
            read(b"title\n", "f.csv")

    def test_refuse_sans_colonne_titre(self):
        with self.assertRaisesMessage(ReadError, "title"):
            read("auteur;code\nA;B\n".encode("utf-8"))

    def test_refuse_une_colonne_en_double(self):
        with self.assertRaisesMessage(ReadError, "deux fois"):
            read("title;titre\nA;B\n".encode("utf-8"))

    def test_limite_de_5000_lignes(self):
        ok = "title\n" + "\n".join(f"L{i}" for i in range(MAX_ROWS))
        self.assertEqual(len(read(ok.encode("utf-8")).rows), MAX_ROWS)
        too_many = ok + "\nL-de-trop"
        with self.assertRaisesMessage(ReadError, "5 000 lignes"):
            read(too_many.encode("utf-8"))

    def test_limite_de_5_mo(self):
        with self.assertRaisesMessage(ReadError, "5 Mo"):
            read(b"x" * (MAX_BYTES + 1))


# ------------------------------------------------------------------ import


class ImportTestCase(StaffTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        media = tempfile.mkdtemp()
        override = override_settings(MEDIA_ROOT=media)
        override.enable()
        cls.addClassCleanup(shutil.rmtree, media, True)
        cls.addClassCleanup(override.disable)

    def preview(self, *rows, organization=None, **kwargs):
        return create_batch(
            organization=organization or self.org_a,
            uploaded_file=csv_file(*rows, **kwargs),
            performed_by=self.staff,
        )

    def confirm(self, batch, organization=None):
        return confirm_batch(
            batch_id=batch.pk, organization=organization or self.org_a,
            performed_by=self.staff,
        )

    def run_import(self, *rows):
        batch = self.preview(*rows)
        self.assertEqual(batch.error_count, 0, batch.error_report)
        return self.confirm(batch)

    def messages_of(self, batch):
        return [item["message"] for item in batch.error_report]

    def counts(self):
        return (
            Book.objects.count(), BookCopy.objects.count(), Category.objects.count(),
            Location.objects.count(), Author.objects.count(),
        )


class CopiesRuleTests(ImportTestCase):
    def test_trois_lignes_un_livre_trois_exemplaires(self):
        batch = self.run_import(oyono("BIB-001"), oyono("BIB-002"), oyono("BIB-003"))

        books = Book.objects.filter(organization=self.org_a, title="Une vie de boy")
        self.assertEqual(books.count(), 1)
        copies = BookCopy.objects.filter(book=books.get())
        self.assertEqual(
            sorted(copies.values_list("inventory_code", flat=True)),
            ["BIB-001", "BIB-002", "BIB-003"],
        )
        self.assertEqual(batch.status, ImportBatch.Status.IMPORTED)
        self.assertEqual(batch.imported_count, 3)
        self.assertEqual(batch.summary["result"]["books_created"], 1)
        self.assertEqual(batch.summary["result"]["copies_created"], 3)

    def test_une_ligne_sans_code_cree_seulement_le_livre(self):
        self.run_import("Sans exemplaire;;;;;;;;;")
        book = Book.objects.get(organization=self.org_a, title="Sans exemplaire")
        self.assertEqual(book.copies.count(), 0)

    def test_champs_du_livre_et_de_l_exemplaire(self):
        self.run_import(
            "Une vie de boy;BIB-001;978-2-07 036822 8;Ferdinand Oyono;Roman;1956;"
            "Présence Africaine;Français;Rayon A;mauvais"
        )
        book = Book.objects.get(organization=self.org_a, title="Une vie de boy")
        self.assertEqual(book.isbn, "9782070368228")
        self.assertEqual(book.publication_year, 1956)
        self.assertEqual(book.publisher, "Présence Africaine")
        self.assertEqual(book.language, "Français")
        self.assertEqual(book.category.name, "Roman")
        self.assertEqual([(a.first_name, a.last_name) for a in book.authors.all()],
                         [("Ferdinand", "Oyono")])
        copy = book.copies.get()
        self.assertEqual(copy.inventory_code, "BIB-001")
        self.assertEqual(copy.location.name, "Rayon A")
        self.assertEqual(copy.condition, BookCopy.Condition.POOR)
        self.assertEqual(copy.status, BookCopy.Status.AVAILABLE)
        self.assertEqual(copy.organization, self.org_a)

    def test_etat_vide_vaut_bon(self):
        self.run_import("Livre;BIB-001;;;;;;;;")
        self.assertEqual(BookCopy.objects.get(inventory_code="BIB-001").condition,
                         BookCopy.Condition.GOOD)

    def test_plusieurs_auteurs_separes_par_un_point_virgule(self):
        self.run_import('Livre;;;"Ferdinand Oyono; Mongo Beti";;;;;;')
        book = Book.objects.get(organization=self.org_a, title="Livre")
        self.assertEqual(book.authors.count(), 2)

    def test_fichier_xlsx_avec_en_tetes_francais(self):
        data = xlsx_bytes([
            ["Titre", "Code", "Auteur", "Catégorie", "Année"],
            ["Une vie de boy", "BIB-001", "Ferdinand Oyono", "Roman", 1956],
            ["Une vie de boy", "BIB-002", "Ferdinand Oyono", "Roman", 1956],
        ])
        batch = create_batch(
            organization=self.org_a, performed_by=self.staff,
            uploaded_file=SimpleUploadedFile("inventaire.xlsx", data),
        )
        self.assertEqual(batch.error_count, 0)
        self.confirm(batch)
        book = Book.objects.get(organization=self.org_a, title="Une vie de boy")
        self.assertEqual(book.copies.count(), 2)
        self.assertEqual(book.publication_year, 1956)

    def test_aucune_ecriture_avant_la_confirmation(self):
        before = self.counts()
        batch = self.preview(oyono("BIB-001"))
        self.assertEqual(self.counts(), before)
        self.assertEqual(batch.status, ImportBatch.Status.PREVIEWED)
        self.assertTrue(batch.can_confirm)


class DetectionTests(ImportTestCase):
    def assert_blocked(self, batch, fragment):
        self.assertEqual(batch.status, ImportBatch.Status.PREVIEWED)
        self.assertGreaterEqual(batch.error_count, 1)
        self.assertFalse(batch.can_confirm)
        self.assertTrue(any(fragment in m for m in self.messages_of(batch)), batch.error_report)
        before = self.counts()
        with self.assertRaises(ValidationError):
            self.confirm(batch)
        self.assertEqual(self.counts(), before)

    def test_titre_manquant(self):
        self.assert_blocked(self.preview(";BIB-9;;;;;;;;"), "Titre manquant")

    def test_code_en_double_dans_le_fichier(self):
        batch = self.preview(oyono("BIB-001"), "Autre;bib-001;;;;;;;;")
        self.assert_blocked(batch, "en double dans le fichier")
        self.assertEqual(batch.error_report[0]["line"], 3)

    def test_code_deja_utilise_meme_en_minuscules(self):
        self.assert_blocked(self.preview("Livre;a-001;;;;;;;;"), "déjà utilisé")

    def test_etat_invalide(self):
        self.assert_blocked(self.preview("Livre;B1;;;;;;;;abîmé"), "État invalide")

    def test_annee_invalide(self):
        for value in ("abc", "3000", "0"):
            with self.subTest(value=value):
                self.assert_blocked(self.preview(f"Livre;;;;;{value};;;;"), "Année invalide")

    def test_champ_trop_long(self):
        self.assert_blocked(self.preview(f"Livre;{'X' * 51};;;;;;;;"), "trop long")

    def test_toutes_les_erreurs_sont_listees_avec_leur_ligne(self):
        batch = self.preview(";B1;;;;;;;;", "Livre;B2;;;;abc;;;;", "Livre;B2;;;;;;;;")
        self.assertEqual(
            sorted(item["line"] for item in batch.error_report), [2, 3, 4],
        )
        self.assertEqual(batch.error_count, 3)

    def test_un_import_avec_erreur_n_est_jamais_confirme(self):
        batch = self.preview(";B1;;;;;;;;")
        with self.assertRaisesMessage(ValidationError, "erreurs"):
            self.confirm(batch)

    def test_livre_deja_present_par_isbn(self):
        existing = create_book(
            organization=self.org_a, title="Ancien titre", isbn="9782070368228"
        )
        batch = self.preview("Titre différent;N-1;978-2-07-036822-8;;;;;;;")
        self.assertEqual(batch.summary["books_existing"], 1)
        self.assertEqual(batch.summary["books_new"], 0)
        match = batch.summary["matches"][0]
        self.assertEqual((match["existing_id"], match["matched_by"]), (existing.pk, "ISBN"))

        self.confirm(batch)
        self.assertEqual(Book.objects.filter(organization=self.org_a, isbn="9782070368228").count(), 1)
        self.assertEqual(existing.copies.get().inventory_code, "N-1")

    def test_livre_deja_present_par_titre_et_auteur(self):
        author = Author.objects.create(
            organization=self.org_a, first_name="Ferdinand", last_name="Oyono"
        )
        existing = create_book(
            organization=self.org_a, title="Une vie de boy", authors=[author]
        )
        batch = self.preview("UNE VIE DE BOY;N-1;;ferdinand oyono;;;;;;")
        self.assertEqual(batch.summary["books_existing"], 1)
        self.assertEqual(batch.summary["matches"][0]["matched_by"], "titre et auteur")

        self.confirm(batch)
        self.assertEqual(Book.objects.filter(organization=self.org_a, title__iexact="Une vie de boy").count(), 1)
        self.assertEqual(existing.copies.count(), 1)
        self.assertEqual(Author.objects.filter(organization=self.org_a).count(), 1)

    def test_meme_titre_autre_auteur_est_un_autre_livre(self):
        author = Author.objects.create(
            organization=self.org_a, first_name="Ferdinand", last_name="Oyono"
        )
        create_book(organization=self.org_a, title="Une vie de boy", authors=[author])
        batch = self.preview("Une vie de boy;N-1;;Autre Auteur;;;;;;")
        self.assertEqual(batch.summary["books_new"], 1)
        self.assertEqual(batch.summary["books_existing"], 0)

    def test_meme_isbn_dans_le_fichier_un_seul_livre(self):
        batch = self.preview(
            "Livre;N-1;9782070368228;;;;;;;", "Livre (2e ex.);N-2;9782070368228;;;;;;;"
        )
        self.assertEqual(batch.summary["books_new"], 1)
        self.assertEqual(batch.summary["copies"], 2)
        self.assertEqual(batch.summary["warnings_total"], 1)

    def test_isbn_differents_meme_titre_et_auteur_deux_livres(self):
        batch = self.preview(
            "Livre;N-1;9782070368228;A B;;;;;;", "Livre;N-2;9782070368235;A B;;;;;;"
        )
        self.assertEqual(batch.summary["books_new"], 2)

    def test_une_ligne_sans_code_sur_un_livre_present_ne_fait_rien(self):
        create_book(organization=self.org_a, title="Déjà là")
        batch = self.preview("Déjà là;;;;;;;;;")
        self.assertEqual(batch.error_count, 0)
        self.assertEqual(batch.summary["copies"], 0)
        self.assertEqual(batch.summary["warnings_total"], 1)

    def test_livre_archive_refuse_ses_exemplaires(self):
        archive_book(book=create_book(organization=self.org_a, title="Archivé"))
        self.assert_blocked(self.preview("Archivé;N-1;;;;;;;;"), "archivé")

    def test_valeurs_differentes_pour_un_meme_livre_sont_signalees(self):
        batch = self.preview("Livre;N-1;;;;1956;;;;", "Livre;N-2;;;;1960;;;;")
        self.assertEqual(batch.error_count, 0)
        self.assertEqual(batch.summary["warnings_total"], 1)
        self.confirm(batch)
        self.assertEqual(Book.objects.get(organization=self.org_a, title="Livre").publication_year, 1956)


class NewReferencesTests(ImportTestCase):
    def test_references_nouvelles_listees_avant_creation(self):
        before = self.counts()
        batch = self.preview(oyono("BIB-001"), oyono("BIB-002"))
        self.assertEqual(batch.summary["new_categories"], ["Roman"])
        self.assertEqual(batch.summary["new_locations"], ["Rayon A"])
        self.assertEqual(batch.summary["new_authors"], ["Ferdinand Oyono"])
        self.assertEqual(self.counts(), before)

        batch = self.confirm(batch)
        self.assertTrue(Category.objects.filter(organization=self.org_a, name="Roman").exists())
        self.assertTrue(Location.objects.filter(organization=self.org_a, name="Rayon A").exists())
        self.assertEqual(batch.summary["result"]["categories_created"], 1)

    def test_categorie_existante_n_est_pas_dupliquee(self):
        existing = create_reference(kind="category", organization=self.org_a, name="Roman")
        batch = self.preview("Livre;;;;ROMAN;;;;;")
        self.assertEqual(batch.summary["new_categories"], [])
        self.confirm(batch)
        self.assertEqual(Category.objects.filter(organization=self.org_a).count(), 1)
        self.assertEqual(Book.objects.get(organization=self.org_a, title="Livre").category, existing)

    def test_emplacement_existant_est_reutilise(self):
        existing = create_reference(kind="location", organization=self.org_a, name="Rayon A")
        self.run_import("Livre;N-1;;;;;;;rayon a;")
        self.assertEqual(Location.objects.filter(organization=self.org_a).count(), 1)
        self.assertEqual(BookCopy.objects.get(inventory_code="N-1").location, existing)

    def test_une_categorie_ne_sert_pas_si_le_livre_existe_deja(self):
        create_book(organization=self.org_a, title="Déjà là")
        batch = self.preview("Déjà là;N-1;;;Poésie;;;;;")
        self.assertEqual(batch.summary["new_categories"], [])


class IsolationTests(ImportTestCase):
    def test_les_donnees_d_une_autre_structure_ne_comptent_pas(self):
        other = create_book(organization=self.org_b, title="Livre", isbn="9782070368228")
        add_copy(book=other, inventory_code="SHARED-1")

        batch = self.preview("Livre;SHARED-1;9782070368228;;;;;;;")
        self.assertEqual(batch.error_count, 0)
        self.assertEqual(batch.summary["books_new"], 1)
        self.confirm(batch)

        mine = Book.objects.get(organization=self.org_a, isbn="9782070368228")
        self.assertEqual(mine.copies.get().inventory_code, "SHARED-1")
        self.assertEqual(other.copies.count(), 1)

    def test_l_import_ne_touche_que_la_structure_de_l_agent(self):
        before_b = Book.objects.filter(organization=self.org_b).count()
        self.run_import(oyono("BIB-001"))
        self.assertEqual(Book.objects.filter(organization=self.org_b).count(), before_b)
        self.assertFalse(Category.objects.filter(organization=self.org_b).exists())

    def test_un_import_d_une_autre_structure_est_introuvable(self):
        batch = self.preview(oyono("BIB-001"))
        with self.assertRaisesMessage(ValidationError, "introuvable"):
            self.confirm(batch, organization=self.org_b)
        batch.refresh_from_db()
        self.assertEqual(batch.status, ImportBatch.Status.PREVIEWED)

    def test_pages_d_un_import_d_une_autre_structure_en_404(self):
        batch = self.preview(oyono("BIB-001"))
        self.client.force_login(self.staff_b)
        self.assertEqual(
            self.client.get(reverse("imports:import_detail", args=[batch.pk])).status_code, 404
        )
        self.assertEqual(
            self.client.post(reverse("imports:import_confirm", args=[batch.pk])).status_code, 404
        )
        batch.refresh_from_db()
        self.assertEqual(batch.status, ImportBatch.Status.PREVIEWED)

    def test_l_historique_ne_liste_que_les_imports_de_la_structure(self):
        mine = self.preview(oyono("BIB-001"), name="mien.csv")
        create_batch(
            organization=self.org_b, performed_by=self.staff_b,
            uploaded_file=csv_file("Livre B;;;;;;;;;", name="autre.csv"),
        )
        response = self.client.get(reverse("imports:import_history"))
        self.assertContains(response, mine.file_name)
        self.assertNotContains(response, "autre.csv")


class AtomicityAndAuditTests(ImportTestCase):
    def count_logs(self, action):
        return AuditLog.objects.filter(action=action).count()

    def test_une_seule_ligne_d_audit_pour_tout_l_import(self):
        books_logs = self.count_logs(AuditLog.Action.BOOK_CREATED)
        copies_logs = self.count_logs(AuditLog.Action.COPY_CREATED)

        batch = self.run_import(oyono("BIB-001"), oyono("BIB-002"), oyono("BIB-003"))

        self.assertEqual(self.count_logs(AuditLog.Action.IMPORT_CONFIRMED), 1)
        self.assertEqual(self.count_logs(AuditLog.Action.BOOK_CREATED), books_logs)
        self.assertEqual(self.count_logs(AuditLog.Action.COPY_CREATED), copies_logs)
        log = AuditLog.objects.get(action=AuditLog.Action.IMPORT_CONFIRMED)
        self.assertEqual(log.organization, self.org_a)
        self.assertEqual(log.user, self.staff)
        self.assertEqual((log.entity_type, log.entity_id), ("ImportBatch", str(batch.pk)))
        self.assertEqual(log.metadata["books_created"], 1)
        self.assertEqual(log.metadata["copies_created"], 3)

    def test_un_double_clic_n_importe_qu_une_fois(self):
        batch = self.preview(oyono("BIB-001"), oyono("BIB-002"))
        self.confirm(batch)
        after_first = self.counts()

        with self.assertRaisesMessage(ValidationError, "déjà été effectué"):
            self.confirm(batch)

        self.assertEqual(self.counts(), after_first)
        self.assertEqual(self.count_logs(AuditLog.Action.IMPORT_CONFIRMED), 1)
        self.assertEqual(self.count_logs(AuditLog.Action.IMPORT_FAILED), 0)

    def test_un_echec_annule_tout(self):
        before = self.counts()
        batch = self.preview(oyono("BIB-001"), oyono("BIB-002"))

        with mock.patch(
            "apps.imports.catalog_import._create_copies", side_effect=RuntimeError("boom")
        ), self.assertLogs("apps.imports.services", level="ERROR"):
            with self.assertRaisesMessage(ValidationError, "rien n'a été importé"):
                self.confirm(batch)

        # Catégorie, emplacement, auteur et livre avaient déjà été écrits : tout est annulé.
        self.assertEqual(self.counts(), before)
        batch.refresh_from_db()
        self.assertEqual(batch.status, ImportBatch.Status.FAILED)
        self.assertEqual(self.count_logs(AuditLog.Action.IMPORT_FAILED), 1)
        self.assertEqual(self.count_logs(AuditLog.Action.IMPORT_CONFIRMED), 0)
        log = AuditLog.objects.get(action=AuditLog.Action.IMPORT_FAILED)
        self.assertEqual((log.organization, log.entity_id), (self.org_a, str(batch.pk)))

    def test_un_conflit_d_unicite_pendant_l_import_annule_tout(self):
        before = self.counts()
        batch = self.preview(oyono("BIB-001"))
        with mock.patch(
            "apps.imports.catalog_import._create_copies", side_effect=IntegrityError("dup")
        ):
            with self.assertRaisesMessage(ValidationError, "pendant l'import"):
                self.confirm(batch)
        self.assertEqual(self.counts(), before)
        batch.refresh_from_db()
        self.assertEqual(batch.status, ImportBatch.Status.FAILED)

    def test_catalogue_modifie_entre_l_apercu_et_la_confirmation(self):
        batch = self.preview(oyono("BIB-001"), oyono("BIB-002"))
        add_copy(book=self.book_a, inventory_code="BIB-002")  # code pris entre-temps
        before = self.counts()

        with self.assertRaisesMessage(ValidationError, "rien n'a été importé"):
            self.confirm(batch)

        self.assertEqual(self.counts(), before)
        batch.refresh_from_db()
        self.assertEqual(batch.status, ImportBatch.Status.FAILED)
        self.assertEqual(self.count_logs(AuditLog.Action.IMPORT_FAILED), 1)

    def test_un_import_echoue_ne_peut_plus_etre_confirme(self):
        batch = self.preview(oyono("BIB-001"))
        add_copy(book=self.book_a, inventory_code="BIB-001")
        with self.assertRaises(ValidationError):
            self.confirm(batch)
        with self.assertRaisesMessage(ValidationError, "ne peut plus être confirmé"):
            self.confirm(batch)
        self.assertEqual(self.count_logs(AuditLog.Action.IMPORT_FAILED), 1)

    def test_un_echec_ne_bloque_pas_l_import_suivant(self):
        batch = self.preview(oyono("BIB-001"))
        add_copy(book=self.book_a, inventory_code="BIB-001")
        with self.assertRaises(ValidationError):
            self.confirm(batch)
        self.run_import(oyono("BIB-010"))
        self.assertTrue(BookCopy.objects.filter(inventory_code="BIB-010").exists())


class ViewTests(ImportTestCase):
    def url(self, name, *args):
        return reverse(f"imports:{name}", args=args)

    def upload(self, file):
        return self.client.post(self.url("import_upload"), {"file": file})

    def test_la_page_d_envoi_s_affiche(self):
        response = self.client.get(self.url("import_upload"))
        self.assertContains(response, "Importer un catalogue")
        self.assertContains(response, self.url("import_template"))

    def test_le_lien_import_est_dans_la_navigation(self):
        response = self.client.get(reverse("staff:dashboard"))
        self.assertContains(response, self.url("import_upload"))

    def test_le_modele_csv_se_telecharge_et_s_importe_tel_quel(self):
        response = self.client.get(self.url("import_template"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response["Content-Type"])
        self.assertIn("attachment", response["Content-Disposition"])
        content = response.content
        self.assertTrue(content.startswith("\ufeff".encode("utf-8")))
        self.assertIn(b"inventory_code", content)

        batch = create_batch(
            organization=self.org_a, performed_by=self.staff,
            uploaded_file=SimpleUploadedFile("modele.csv", content),
        )
        self.assertEqual(batch.error_count, 0)
        self.assertEqual((batch.summary["books_new"], batch.summary["copies"]), (1, 3))

    def test_parcours_complet_envoi_apercu_confirmation_rapport(self):
        response = self.upload(csv_file(oyono("BIB-001"), oyono("BIB-002")))
        batch = ImportBatch.objects.get(organization=self.org_a)
        self.assertRedirects(
            response, self.url("import_detail", batch.pk), fetch_redirect_response=False
        )

        preview = self.client.get(self.url("import_detail", batch.pk))
        self.assertContains(preview, "Aperçu de l'import")
        self.assertContains(preview, "Confirmer l'import")
        self.assertContains(preview, "Rayon A")  # référence nouvelle listée

        response = self.client.post(self.url("import_confirm", batch.pk), follow=True)
        self.assertContains(response, "Rapport d'import")
        self.assertContains(response, "Import terminé.")
        self.assertNotContains(response, "Confirmer l'import")
        self.assertEqual(BookCopy.objects.filter(inventory_code__startswith="BIB-").count(), 2)

    def test_apercu_avec_erreurs_bloque_la_confirmation(self):
        self.upload(csv_file(";B1;;;;;;;;", oyono("BIB-002")))
        batch = ImportBatch.objects.get(organization=self.org_a)
        page = self.client.get(self.url("import_detail", batch.pk))
        self.assertContains(page, "Titre manquant")
        self.assertContains(page, "Renvoyer le fichier corrigé")
        self.assertNotContains(page, "Confirmer l'import")

        before = self.counts()
        response = self.client.post(self.url("import_confirm", batch.pk), follow=True)
        self.assertContains(response, "contient des erreurs")
        self.assertEqual(self.counts(), before)

    def test_mauvais_format_refuse(self):
        response = self.upload(SimpleUploadedFile("notes.txt", b"title\nA"))
        self.assertContains(response, "Format non pris en charge")
        self.assertFalse(ImportBatch.objects.exists())

    def test_csv_non_utf8_refuse_avec_une_aide(self):
        response = self.upload(SimpleUploadedFile("a.csv", "title\nÉmile".encode("cp1252")))
        self.assertContains(response, "UTF-8")
        self.assertFalse(ImportBatch.objects.exists())

    def test_fichier_de_plus_de_5_mo_refuse(self):
        response = self.upload(SimpleUploadedFile("gros.csv", b"x" * (MAX_BYTES + 1)))
        self.assertContains(response, "5 Mo")
        self.assertFalse(ImportBatch.objects.exists())

    def test_envoi_sans_fichier(self):
        response = self.client.post(self.url("import_upload"), {})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(ImportBatch.objects.exists())

    def test_la_confirmation_exige_post(self):
        batch = self.preview(oyono("BIB-001"))
        self.assertEqual(self.client.get(self.url("import_confirm", batch.pk)).status_code, 405)

    def test_historique_et_rapport_d_un_echec(self):
        batch = self.preview(oyono("BIB-001"))
        add_copy(book=self.book_a, inventory_code="BIB-001")
        with self.assertRaises(ValidationError):
            self.confirm(batch)
        self.assertContains(self.client.get(self.url("import_history")), "Échoué")
        self.assertContains(self.client.get(self.url("import_detail", batch.pk)), "Import échoué")

    def test_acces_reserve_au_staff_de_la_bibliotheque(self):
        batch = self.preview(oyono("BIB-001"))
        urls = [
            self.url("import_upload"), self.url("import_template"),
            self.url("import_history"), self.url("import_detail", batch.pk),
        ]

        self.client.logout()
        for url in urls:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302, url)
            self.assertIn(reverse("staff:login"), response["Location"])

        from django.contrib.auth import get_user_model
        reader = get_user_model().objects.create_user(
            email="lecteur@example.com", password="abc12345",
            first_name="Le", last_name="Teur",
        )
        self.client.force_login(reader)
        for url in urls:
            self.assertEqual(self.client.get(url).status_code, 403, url)

    def test_module_bibliotheque_desactive(self):
        from apps.organizations.models import Organization
        Organization.objects.filter(pk=self.org_a.pk).update(library_module=False)
        for name in ("import_upload", "import_template", "import_history"):
            self.assertEqual(self.client.get(self.url(name)).status_code, 403, name)
