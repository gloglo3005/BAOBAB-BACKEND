import html

from django.urls import reverse

from apps.audit.models import AuditLog
from apps.catalog.models import Author, Book, BookCopy, Category, Location
from apps.catalog.services import create_reference

from .tests import StaffTestCase


class CatalogManagementTestCase(StaffTestCase):
    def setUp(self):
        super().setUp()
        self.cat = create_reference(kind="category", organization=self.org_a, name="Romans")
        self.cat_b = create_reference(kind="category", organization=self.org_b, name="Essais")
        self.loc = create_reference(kind="location", organization=self.org_a, name="Salle 1")
        self.loc2 = create_reference(kind="location", organization=self.org_a, name="Salle 2")
        self.loc_b = create_reference(kind="location", organization=self.org_b, name="Salle B")

    def url(self, name, *args):
        return reverse(f"staff:{name}", args=args)

    def new_book_data(self, **extra):
        data = {
            "title": "Things Fall Apart",
            "authors_text": "Chinua Achebe",
            "category": self.cat.pk,
            "isbn": "",
            "codes": "",
            "condition": "GOOD",
        }
        data.update(extra)
        return data


class AccessTests(CatalogManagementTestCase):
    def test_les_pages_de_gestion_sont_fermees_aux_comptes_sans_rattachement(self):
        from django.contrib.auth import get_user_model

        reader = get_user_model().objects.create_user(
            email="lecteur@example.com", password="abc12345",
            first_name="Le", last_name="Teur",
        )
        self.client.force_login(reader)
        urls = [
            self.url("book_new"), self.url("catalog_settings"),
            self.url("book_edit", self.book_a.pk), self.url("copies_add", self.book_a.pk),
            self.url("copy_edit", self.copy_a.pk),
        ]
        for url in urls:
            self.assertEqual(self.client.get(url).status_code, 403, url)

    def test_les_actions_de_modification_exigent_post(self):
        for url in (
            self.url("book_archive", self.book_a.pk),
            self.url("copy_status", self.copy_a.pk),
            self.url("catalog_settings_action"),
        ):
            self.assertEqual(self.client.get(url).status_code, 405, url)


class NewBookTests(CatalogManagementTestCase):
    def test_la_page_s_affiche(self):
        self.assertContains(self.client.get(self.url("book_new")), "Nouveau livre")

    def test_creer_un_livre_avec_ses_exemplaires(self):
        response = self.client.post(
            self.url("book_new"),
            self.new_book_data(codes="A-100\nA-101", location=self.loc.pk),
        )
        book = Book.objects.get(title="Things Fall Apart")
        self.assertRedirects(
            response, self.url("book_detail", book.pk), fetch_redirect_response=False
        )
        self.assertEqual(book.organization, self.org_a)
        self.assertEqual(book.category, self.cat)
        self.assertEqual([str(a) for a in book.authors.all()], ["Chinua Achebe"])
        copies = BookCopy.objects.filter(book=book)
        self.assertEqual(sorted(c.inventory_code for c in copies), ["A-100", "A-101"])
        self.assertTrue(all(c.location == self.loc for c in copies))

        self.assertEqual(
            AuditLog.objects.get(action=AuditLog.Action.BOOK_CREATED, entity_id=str(book.pk)).user,
            self.staff,
        )
        self.assertEqual(
            AuditLog.objects.filter(
                action=AuditLog.Action.COPY_CREATED, organization=self.org_a, entity_id__in=[
                    str(c.pk) for c in copies
                ]
            ).count(),
            2,
        )
        page = self.client.get(response["Location"])
        self.assertContains(page, "avec 2 exemplaires")

    def test_creer_un_livre_sans_exemplaire(self):
        self.client.post(self.url("book_new"), self.new_book_data())
        book = Book.objects.get(title="Things Fall Apart")
        self.assertEqual(book.copies.count(), 0)

    def test_l_isbn_est_normalise_et_les_doublons_refuses(self):
        self.client.post(self.url("book_new"), self.new_book_data(isbn="978-2-07 036822-8"))
        self.assertEqual(Book.objects.get(title="Things Fall Apart").isbn, "9782070368228")

        response = self.client.post(
            self.url("book_new"),
            self.new_book_data(title="Autre", isbn="9782070368228", authors_text="Nouvel Auteur"),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "existe déjà dans le catalogue")
        self.assertFalse(Book.objects.filter(title="Autre").exists())
        self.assertFalse(Author.objects.filter(last_name="Auteur").exists())

    def test_un_code_en_double_annule_aussi_la_creation_du_livre_et_des_auteurs(self):
        response = self.client.post(
            self.url("book_new"),
            self.new_book_data(
                title="Nouveau", authors_text="Un Nouvel", codes="A-001\nB-777"
            ),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "déjà utilisés : A-001")
        self.assertFalse(Book.objects.filter(title="Nouveau").exists())
        self.assertFalse(BookCopy.objects.filter(inventory_code="B-777").exists())
        self.assertFalse(Author.objects.filter(last_name="Nouvel").exists())

    def test_categorie_et_emplacement_d_une_autre_structure_refuses(self):
        response = self.client.post(
            self.url("book_new"), self.new_book_data(category=self.cat_b.pk)
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Book.objects.filter(title="Things Fall Apart").exists())

        response = self.client.post(
            self.url("book_new"),
            self.new_book_data(codes="Z-1", location=self.loc_b.pk),
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(BookCopy.objects.filter(inventory_code="Z-1").exists())

    def test_titre_obligatoire(self):
        response = self.client.post(self.url("book_new"), self.new_book_data(title=""))
        self.assertEqual(response.status_code, 200)

    def test_une_categorie_desactivee_n_est_plus_proposee(self):
        Category.objects.filter(pk=self.cat.pk).update(active=False)
        response = self.client.get(self.url("book_new"))
        self.assertNotContains(response, ">Romans<")


class EditBookTests(CatalogManagementTestCase):
    def test_la_page_est_prerempli(self):
        response = self.client.get(self.url("book_edit", self.book_a.pk))
        self.assertContains(response, self.book_a.title)

    def test_modifier_un_livre_trace_les_changements(self):
        response = self.client.post(
            self.url("book_edit", self.book_a.pk),
            {
                "title": "Livre A (2e édition)",
                "authors_text": "Chinua Achebe\nSenghor",
                "category": self.cat.pk,
                "isbn": "",
            },
        )
        self.assertRedirects(
            response, self.url("book_detail", self.book_a.pk), fetch_redirect_response=False
        )
        self.book_a.refresh_from_db()
        self.assertEqual(self.book_a.title, "Livre A (2e édition)")
        self.assertEqual(self.book_a.category, self.cat)
        self.assertEqual(self.book_a.authors.count(), 2)
        entry = AuditLog.objects.get(action=AuditLog.Action.BOOK_UPDATED)
        self.assertEqual(entry.user, self.staff)
        self.assertEqual(entry.metadata["changes"]["title"]["from"], "Livre A")

    def test_un_livre_d_une_autre_structure_est_introuvable(self):
        self.assertEqual(
            self.client.get(self.url("book_edit", self.book_b.pk)).status_code, 404
        )
        self.client.post(self.url("book_edit", self.book_b.pk), {"title": "Piraté"})
        self.book_b.refresh_from_db()
        self.assertEqual(self.book_b.title, "Livre B")

    def test_isbn_deja_pris_laisse_le_livre_inchange(self):
        other = Book.objects.create(organization=self.org_a, title="Autre", isbn="555")
        response = self.client.post(
            self.url("book_edit", self.book_a.pk),
            {"title": "Livre A", "isbn": "555", "authors_text": ""},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "« Autre »")
        self.book_a.refresh_from_db()
        self.assertEqual(self.book_a.isbn, "")
        self.assertEqual(other.isbn, "555")


class ArchiveTests(CatalogManagementTestCase):
    def test_archiver_puis_restaurer(self):
        url = self.url("book_archive", self.book_a.pk)
        response = self.client.post(url, {"action": "archive"})
        self.assertRedirects(
            response, self.url("book_detail", self.book_a.pk), fetch_redirect_response=False
        )
        self.book_a.refresh_from_db()
        self.assertFalse(self.book_a.active)
        self.assertEqual(
            AuditLog.objects.get(action=AuditLog.Action.BOOK_ARCHIVED).user, self.staff
        )
        self.assertContains(self.client.get(response["Location"]), "est archivé")

        self.client.post(url, {"action": "restore"})
        self.book_a.refresh_from_db()
        self.assertTrue(self.book_a.active)

    def test_refuse_tant_qu_un_exemplaire_est_emprunte(self):
        self.checkout()
        response = self.client.post(
            self.url("book_archive", self.book_a.pk), {"action": "archive"}
        )
        self.assertContains(self.client.get(response["Location"]), "encore emprunté")
        self.book_a.refresh_from_db()
        self.assertTrue(self.book_a.active)

    def test_un_livre_d_une_autre_structure_ne_s_archive_pas(self):
        response = self.client.post(
            self.url("book_archive", self.book_b.pk), {"action": "archive"}
        )
        self.assertEqual(response.status_code, 404)
        self.book_b.refresh_from_db()
        self.assertTrue(self.book_b.active)

    def test_un_exemplaire_d_un_livre_archive_n_est_plus_pretable(self):
        self.client.post(self.url("book_archive", self.book_a.pk), {"action": "archive"})
        lookup = self.client.get(
            reverse("staff:loan_copy_lookup"), {"copy_q": "A-001"}
        )
        self.assertContains(lookup, "Livre archivé")
        self.assertNotContains(lookup, 'type="hidden" name="copy_id"')

        self.client.post(
            reverse("staff:loan"),
            {"member_id": self.member_a.pk, "copy_id": self.copy_a.pk},
        )
        self.assertEqual(self.copy_a.loans.count(), 0)

    def test_les_filtres_du_catalogue(self):
        self.client.post(self.url("book_archive", self.book_a.pk), {"action": "archive"})
        link = f'href="{self.url("book_detail", self.book_a.pk)}"'
        self.assertNotContains(self.client.get(self.url("catalog")), link)
        self.assertContains(self.client.get(self.url("catalog"), {"state": "archived"}), link)
        self.assertContains(self.client.get(self.url("catalog"), {"state": "all"}), link)

    def test_filtre_par_categorie(self):
        Book.objects.filter(pk=self.book_a.pk).update(category=self.cat)
        Book.objects.create(organization=self.org_a, title="Sans catégorie")
        response = self.client.get(self.url("catalog"), {"category": self.cat.pk})
        self.assertContains(response, "Livre A")
        self.assertNotContains(response, "Sans catégorie")


class AddCopiesTests(CatalogManagementTestCase):
    def test_ajouter_plusieurs_exemplaires(self):
        response = self.client.post(
            self.url("copies_add", self.book_a.pk),
            {"codes": "A-200\nA-201\n\nA-202", "location": self.loc.pk, "condition": "NEW"},
        )
        self.assertRedirects(
            response, self.url("book_detail", self.book_a.pk), fetch_redirect_response=False
        )
        self.assertEqual(self.book_a.copies.count(), 4)
        copy = BookCopy.objects.get(inventory_code="A-200")
        self.assertEqual((copy.location, copy.condition), (self.loc, "NEW"))
        self.assertContains(self.client.get(response["Location"]), "3 exemplaires ajoutés")

    def test_un_code_existant_n_ajoute_rien(self):
        response = self.client.post(
            self.url("copies_add", self.book_a.pk),
            {"codes": "A-300\na-001", "condition": "GOOD"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "déjà utilisés")
        self.assertFalse(BookCopy.objects.filter(inventory_code="A-300").exists())

    def test_livre_d_une_autre_structure_ou_archive(self):
        self.assertEqual(
            self.client.get(self.url("copies_add", self.book_b.pk)).status_code, 404
        )
        self.client.post(self.url("book_archive", self.book_a.pk), {"action": "archive"})
        response = self.client.post(
            self.url("copies_add", self.book_a.pk), {"codes": "A-400", "condition": "GOOD"}
        )
        self.assertContains(response, "est archivé")
        self.assertFalse(BookCopy.objects.filter(inventory_code="A-400").exists())


class EditCopyTests(CatalogManagementTestCase):
    def test_modifier_et_deplacer(self):
        response = self.client.post(
            self.url("copy_edit", self.copy_a.pk),
            {
                "condition": "POOR",
                "location": self.loc2.pk,
                "notes": "Dos abîmé",
                "acquisition_date": "2026-09-01",
            },
        )
        self.assertRedirects(
            response, self.url("book_detail", self.book_a.pk), fetch_redirect_response=False
        )
        self.copy_a.refresh_from_db()
        self.assertEqual(self.copy_a.condition, "POOR")
        self.assertEqual(self.copy_a.location, self.loc2)
        self.assertEqual(self.copy_a.notes, "Dos abîmé")
        entry = AuditLog.objects.get(action=AuditLog.Action.COPY_MOVED)
        self.assertEqual(entry.user, self.staff)
        self.assertEqual(entry.metadata["location"]["to"], "Salle 2")

    def test_un_emplacement_d_une_autre_structure_est_refuse(self):
        response = self.client.post(
            self.url("copy_edit", self.copy_a.pk),
            {"condition": "GOOD", "location": self.loc_b.pk},
        )
        self.assertEqual(response.status_code, 200)
        self.copy_a.refresh_from_db()
        self.assertIsNone(self.copy_a.location)

    def test_un_exemplaire_d_une_autre_structure_est_introuvable(self):
        self.assertEqual(
            self.client.get(self.url("copy_edit", self.copy_b.pk)).status_code, 404
        )
        self.assertEqual(
            self.client.post(
                self.url("copy_status", self.copy_b.pk), {"status": "UNAVAILABLE"}
            ).status_code,
            404,
        )

    def test_un_exemplaire_emprunte_montre_le_retour_et_pas_le_changement_de_statut(self):
        self.checkout()
        response = self.client.get(self.url("copy_edit", self.copy_a.pk))
        self.assertContains(response, "Enregistrer son retour")
        self.assertNotContains(response, "Changer le statut")

    def test_changer_le_statut(self):
        response = self.client.post(
            self.url("copy_status", self.copy_a.pk), {"status": "UNAVAILABLE"}
        )
        self.assertRedirects(
            response, self.url("copy_edit", self.copy_a.pk), fetch_redirect_response=False
        )
        self.copy_a.refresh_from_db()
        self.assertEqual(self.copy_a.status, BookCopy.Status.UNAVAILABLE)
        self.assertEqual(
            AuditLog.objects.get(action=AuditLog.Action.COPY_STATUS_CHANGED).user, self.staff
        )

    def test_statut_emprunte_et_statut_inconnu_refuses(self):
        for status in ("BORROWED", "N_IMPORTE_QUOI"):
            self.client.post(self.url("copy_status", self.copy_a.pk), {"status": status})
        self.copy_a.refresh_from_db()
        self.assertEqual(self.copy_a.status, BookCopy.Status.AVAILABLE)

    def test_un_emprunt_en_cours_protege_le_statut_meme_en_post_direct(self):
        self.checkout()
        response = self.client.post(
            self.url("copy_status", self.copy_a.pk), {"status": "UNAVAILABLE"}
        )
        page = self.client.get(response["Location"]).content.decode()
        self.assertIn("enregistrez d'abord", html.unescape(page))
        self.copy_a.refresh_from_db()
        self.assertEqual(self.copy_a.status, BookCopy.Status.BORROWED)


class CatalogSettingsTests(CatalogManagementTestCase):
    def post(self, **data):
        return self.client.post(self.url("catalog_settings_action"), data)

    def test_la_page_ne_montre_que_les_references_de_la_structure(self):
        response = self.client.get(self.url("catalog_settings"))
        self.assertContains(response, "Romans")
        self.assertContains(response, "Salle 1")
        self.assertNotContains(response, "Essais")
        self.assertNotContains(response, "Salle B")

    def test_ajouter_une_categorie_et_un_emplacement(self):
        self.post(kind="category", action="add", **{"category-name": "Poésie"})
        self.post(kind="location", action="add", **{"location-name": "Réserve"})
        self.assertTrue(Category.objects.filter(organization=self.org_a, name="Poésie").exists())
        self.assertTrue(Location.objects.filter(organization=self.org_a, name="Réserve").exists())

    def test_doublon_refuse_avec_un_message(self):
        response = self.post(kind="category", action="add", **{"category-name": "romans"})
        page = self.client.get(response["Location"])
        self.assertContains(page, "existe déjà")
        self.assertEqual(Category.objects.filter(organization=self.org_a).count(), 1)

    def test_le_meme_nom_est_permis_dans_une_autre_structure(self):
        self.post(kind="category", action="add", **{"category-name": "Essais"})
        self.assertEqual(Category.objects.filter(name="Essais").count(), 2)

    def test_renommer_desactiver_reactiver(self):
        self.post(kind="location", action="rename", id=self.loc.pk, name="Grande salle")
        self.loc.refresh_from_db()
        self.assertEqual(self.loc.name, "Grande salle")

        self.post(kind="location", action="deactivate", id=self.loc.pk)
        self.loc.refresh_from_db()
        self.assertFalse(self.loc.active)
        self.assertNotContains(self.client.get(self.url("book_new")), ">Grande salle<")

        self.post(kind="location", action="activate", id=self.loc.pk)
        self.loc.refresh_from_db()
        self.assertTrue(self.loc.active)

    def test_on_ne_touche_pas_aux_references_d_une_autre_structure(self):
        response = self.post(kind="category", action="rename", id=self.cat_b.pk, name="Piraté")
        self.assertEqual(response.status_code, 404)
        self.cat_b.refresh_from_db()
        self.assertEqual(self.cat_b.name, "Essais")
        self.assertEqual(
            self.post(kind="location", action="deactivate", id=self.loc_b.pk).status_code, 404
        )

    def test_type_ou_action_inconnus(self):
        self.assertContains(
            self.client.get(self.post(kind="auteur", action="add")["Location"]),
            "Action inconnue",
        )
        self.assertContains(
            self.client.get(self.post(kind="category", action="supprimer")["Location"]),
            "Action inconnue",
        )
