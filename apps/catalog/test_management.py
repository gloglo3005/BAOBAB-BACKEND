from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase

from apps.audit.models import AuditLog
from apps.organizations.services import create_organization

from .models import Author, Book, BookCopy, Category, Location
from .services import (
    MAX_COPIES_PER_BATCH,
    add_copies,
    add_copy,
    archive_book,
    create_book,
    create_reference,
    get_or_create_authors,
    move_copy,
    rename_reference,
    restore_book,
    set_copy_status,
    set_reference_active,
    split_author_name,
    update_book,
    update_copy,
)

User = get_user_model()


class ManagementTestCase(TestCase):
    def setUp(self):
        self.org_a = create_organization(name="A", slug="a")
        self.org_b = create_organization(name="B", slug="b")
        self.user = User.objects.create_user(
            email="agent@example.com", password="abc12345",
            first_name="Ag", last_name="Ent",
        )
        self.book = create_book(organization=self.org_a, title="Livre", isbn="111")
        self.book_b = create_book(organization=self.org_b, title="Livre B", isbn="222")
        self.loc_a = create_reference(kind="location", organization=self.org_a, name="Salle 1")
        self.loc_a2 = create_reference(kind="location", organization=self.org_a, name="Salle 2")
        self.loc_b = create_reference(kind="location", organization=self.org_b, name="Salle B")
        self.cat_a = create_reference(kind="category", organization=self.org_a, name="Romans")
        self.cat_b = create_reference(kind="category", organization=self.org_b, name="Essais")

    def logs(self, action):
        return AuditLog.objects.filter(action=action)


class AuthorTests(ManagementTestCase):
    def test_decoupage_des_noms(self):
        self.assertEqual(split_author_name("Chinua Achebe"), ("Chinua", "Achebe"))
        self.assertEqual(split_author_name("Cheikh Anta Diop"), ("Cheikh Anta", "Diop"))
        self.assertEqual(split_author_name("Senghor"), ("", "Senghor"))

    def test_cree_les_auteurs_manquants_et_ignore_les_lignes_vides_et_doublons(self):
        authors = get_or_create_authors(
            self.org_a, "Chinua Achebe\n\n  Chinua   Achebe \nSenghor\n"
        )
        self.assertEqual([str(a) for a in authors], ["Chinua Achebe", "Senghor"])
        self.assertEqual(Author.objects.filter(organization=self.org_a).count(), 2)

    def test_retrouve_un_auteur_existant_sans_tenir_compte_de_la_casse(self):
        first = get_or_create_authors(self.org_a, "Chinua Achebe")[0]
        again = get_or_create_authors(self.org_a, "chinua ACHEBE")[0]
        self.assertEqual(first.pk, again.pk)

    def test_ne_reutilise_pas_l_auteur_d_une_autre_structure(self):
        other = get_or_create_authors(self.org_b, "Chinua Achebe")[0]
        mine = get_or_create_authors(self.org_a, "Chinua Achebe")[0]
        self.assertNotEqual(other.pk, mine.pk)
        self.assertEqual(mine.organization, self.org_a)


class UpdateBookTests(ManagementTestCase):
    def test_modifier_le_titre_trace_l_ancienne_et_la_nouvelle_valeur(self):
        update_book(book=self.book, performed_by=self.user, title="Nouveau titre")
        self.book.refresh_from_db()
        self.assertEqual(self.book.title, "Nouveau titre")
        entry = self.logs(AuditLog.Action.BOOK_UPDATED).get()
        self.assertEqual(entry.user, self.user)
        self.assertEqual(
            entry.metadata["changes"]["title"], {"from": "Livre", "to": "Nouveau titre"}
        )

    def test_aucun_changement_aucune_ligne_de_journal(self):
        update_book(book=self.book, performed_by=self.user, title="Livre", isbn="111")
        self.assertEqual(self.logs(AuditLog.Action.BOOK_UPDATED).count(), 0)

    def test_isbn_deja_utilise_message_clair(self):
        other = create_book(organization=self.org_a, title="Autre", isbn="999")
        with self.assertRaisesMessage(ValidationError, "existe déjà dans le catalogue"):
            update_book(book=other, isbn="111")
        other.refresh_from_db()
        self.assertEqual(other.isbn, "999")

    def test_isbn_deja_utilise_a_la_creation(self):
        with self.assertRaisesMessage(ValidationError, "« Livre »"):
            create_book(organization=self.org_a, title="Doublon", isbn="111")

    def test_meme_isbn_autorise_dans_une_autre_structure(self):
        update_book(book=self.book_b, isbn="111")
        self.book_b.refresh_from_db()
        self.assertEqual(self.book_b.isbn, "111")

    def test_categorie_d_une_autre_structure_refusee(self):
        with self.assertRaises(ValidationError):
            update_book(book=self.book, category=self.cat_b)
        self.book.refresh_from_db()
        self.assertIsNone(self.book.category)

    def test_changer_de_categorie_et_d_auteurs(self):
        authors = get_or_create_authors(self.org_a, "Chinua Achebe")
        update_book(
            book=self.book, performed_by=self.user, category=self.cat_a, authors=authors
        )
        self.book.refresh_from_db()
        self.assertEqual(self.book.category, self.cat_a)
        self.assertEqual([str(a) for a in self.book.authors.all()], ["Chinua Achebe"])
        changes = self.logs(AuditLog.Action.BOOK_UPDATED).get().metadata["changes"]
        self.assertEqual(changes["category"], {"from": None, "to": "Romans"})
        self.assertEqual(changes["authors"]["to"], ["Chinua Achebe"])

    def test_auteur_d_une_autre_structure_refuse(self):
        foreign = get_or_create_authors(self.org_b, "Quelqu'un Dailleurs")
        with self.assertRaises(ValidationError):
            update_book(book=self.book, authors=foreign)
        self.assertEqual(self.book.authors.count(), 0)

    def test_champ_inconnu(self):
        with self.assertRaises(ValueError):
            update_book(book=self.book, active=False)

    def test_titre_vide_refuse(self):
        with self.assertRaises(ValidationError):
            update_book(book=self.book, title="")
        self.book.refresh_from_db()
        self.assertEqual(self.book.title, "Livre")


class ArchiveTests(ManagementTestCase):
    def test_archiver_puis_restaurer(self):
        archive_book(book=self.book, performed_by=self.user)
        self.book.refresh_from_db()
        self.assertFalse(self.book.active)
        self.assertEqual(self.logs(AuditLog.Action.BOOK_ARCHIVED).get().user, self.user)

        restore_book(book=self.book, performed_by=self.user)
        self.book.refresh_from_db()
        self.assertTrue(self.book.active)
        entry = self.logs(AuditLog.Action.BOOK_UPDATED).get()
        self.assertEqual(entry.metadata["changes"]["active"], {"from": False, "to": True})

    def test_refuse_si_un_exemplaire_est_emprunte(self):
        copy = add_copy(book=self.book, inventory_code="A-001")
        BookCopy.objects.filter(pk=copy.pk).update(status=BookCopy.Status.BORROWED)
        with self.assertRaisesMessage(ValidationError, "encore emprunté"):
            archive_book(book=self.book)
        self.book.refresh_from_db()
        self.assertTrue(self.book.active)
        self.assertEqual(self.logs(AuditLog.Action.BOOK_ARCHIVED).count(), 0)

    def test_double_archivage_et_double_restauration(self):
        archive_book(book=self.book)
        with self.assertRaisesMessage(ValidationError, "déjà archivé"):
            archive_book(book=self.book)
        restore_book(book=self.book)
        with self.assertRaisesMessage(ValidationError, "n'est pas archivé"):
            restore_book(book=self.book)

    def test_on_n_ajoute_pas_d_exemplaire_a_un_livre_archive(self):
        archive_book(book=self.book)
        with self.assertRaisesMessage(ValidationError, "archivé"):
            add_copies(book=self.book, codes=["A-001"])
        self.assertEqual(BookCopy.objects.count(), 0)


class AddCopiesTests(ManagementTestCase):
    def test_ajoute_plusieurs_exemplaires_et_les_trace(self):
        copies = add_copies(
            book=self.book, codes=["A-001", " A-002 ", "", "A-003"],
            location=self.loc_a, performed_by=self.user,
        )
        self.assertEqual([c.inventory_code for c in copies], ["A-001", "A-002", "A-003"])
        self.assertTrue(all(c.location == self.loc_a for c in copies))
        self.assertEqual(self.logs(AuditLog.Action.COPY_CREATED).count(), 3)

    def test_un_code_saisi_deux_fois_ne_cree_rien(self):
        with self.assertRaisesMessage(ValidationError, "plusieurs fois : a-001"):
            add_copies(book=self.book, codes=["A-001", "a-001", "A-002"])
        self.assertEqual(BookCopy.objects.count(), 0)

    def test_un_code_deja_pris_ne_cree_rien_meme_en_minuscules(self):
        add_copy(book=self.book, inventory_code="A-001")
        with self.assertRaisesMessage(ValidationError, "déjà utilisés : A-001"):
            add_copies(book=self.book, codes=["a-001", "A-002"])
        self.assertEqual(BookCopy.objects.count(), 1)

    def test_le_meme_code_est_possible_dans_une_autre_structure(self):
        add_copy(book=self.book, inventory_code="X-001")
        add_copies(book=self.book_b, codes=["X-001"])
        self.assertEqual(BookCopy.objects.filter(inventory_code="X-001").count(), 2)

    def test_liste_vide_et_trop_longue(self):
        with self.assertRaisesMessage(ValidationError, "au moins un code"):
            add_copies(book=self.book, codes=["", "  "])
        codes = [f"C-{n}" for n in range(MAX_COPIES_PER_BATCH + 1)]
        with self.assertRaisesMessage(ValidationError, "Au plus"):
            add_copies(book=self.book, codes=codes)

    def test_un_emplacement_d_une_autre_structure_annule_tout(self):
        with self.assertRaises(ValidationError):
            add_copies(book=self.book, codes=["A-001", "A-002"], location=self.loc_b)
        self.assertEqual(BookCopy.objects.count(), 0)
        self.assertEqual(self.logs(AuditLog.Action.COPY_CREATED).count(), 0)


class UpdateAndMoveCopyTests(ManagementTestCase):
    def setUp(self):
        super().setUp()
        self.copy = add_copy(book=self.book, inventory_code="A-001", location=self.loc_a)

    def test_modifier_l_etat_et_les_notes(self):
        update_copy(copy=self.copy, condition=BookCopy.Condition.POOR, notes="Dos abîmé")
        self.copy.refresh_from_db()
        self.assertEqual(self.copy.condition, BookCopy.Condition.POOR)
        self.assertEqual(self.copy.notes, "Dos abîmé")

    def test_champ_non_modifiable(self):
        with self.assertRaises(ValueError):
            update_copy(copy=self.copy, status=BookCopy.Status.LOST)

    def test_deplacer_trace_l_ancien_et_le_nouvel_emplacement(self):
        move_copy(copy=self.copy, location=self.loc_a2, performed_by=self.user)
        self.copy.refresh_from_db()
        self.assertEqual(self.copy.location, self.loc_a2)
        entry = self.logs(AuditLog.Action.COPY_MOVED).get()
        self.assertEqual(entry.user, self.user)
        self.assertEqual(entry.metadata["location"], {"from": "Salle 1", "to": "Salle 2"})

    def test_deplacer_au_meme_endroit_ne_trace_rien(self):
        move_copy(copy=self.copy, location=self.loc_a)
        self.assertEqual(self.logs(AuditLog.Action.COPY_MOVED).count(), 0)

    def test_retirer_l_emplacement(self):
        move_copy(copy=self.copy, location=None)
        self.copy.refresh_from_db()
        self.assertIsNone(self.copy.location)

    def test_emplacement_d_une_autre_structure_refuse(self):
        with self.assertRaisesMessage(ValidationError, "n'existe pas dans cette structure"):
            move_copy(copy=self.copy, location=self.loc_b)
        self.copy.refresh_from_db()
        self.assertEqual(self.copy.location, self.loc_a)


class CopyStatusTests(ManagementTestCase):
    def setUp(self):
        super().setUp()
        self.copy = add_copy(book=self.book, inventory_code="A-001")

    def test_rendre_indisponible_puis_disponible(self):
        set_copy_status(
            copy=self.copy, new_status=BookCopy.Status.UNAVAILABLE, performed_by=self.user
        )
        self.copy.refresh_from_db()
        self.assertEqual(self.copy.status, BookCopy.Status.UNAVAILABLE)
        entry = self.logs(AuditLog.Action.COPY_STATUS_CHANGED).get()
        self.assertEqual(entry.user, self.user)
        self.assertEqual(
            entry.metadata["copy_status"], {"from": "AVAILABLE", "to": "UNAVAILABLE"}
        )
        set_copy_status(copy=self.copy, new_status=BookCopy.Status.AVAILABLE)
        self.assertEqual(self.logs(AuditLog.Action.COPY_STATUS_CHANGED).count(), 2)

    def test_declarer_perdu_utilise_l_action_perte(self):
        set_copy_status(copy=self.copy, new_status=BookCopy.Status.LOST)
        self.assertEqual(self.logs(AuditLog.Action.COPY_MARKED_LOST).count(), 1)
        self.assertEqual(self.logs(AuditLog.Action.COPY_STATUS_CHANGED).count(), 0)

    def test_perdu_puis_retrouve(self):
        set_copy_status(copy=self.copy, new_status=BookCopy.Status.LOST)
        set_copy_status(copy=self.copy, new_status=BookCopy.Status.AVAILABLE)
        self.copy.refresh_from_db()
        self.assertEqual(self.copy.status, BookCopy.Status.AVAILABLE)

    def test_un_exemplaire_emprunte_ne_se_modifie_pas(self):
        BookCopy.objects.filter(pk=self.copy.pk).update(status=BookCopy.Status.BORROWED)
        with self.assertRaisesMessage(ValidationError, "enregistrez d'abord son retour"):
            set_copy_status(copy=self.copy, new_status=BookCopy.Status.UNAVAILABLE)
        self.copy.refresh_from_db()
        self.assertEqual(self.copy.status, BookCopy.Status.BORROWED)

    def test_on_ne_pose_pas_emprunte_a_la_main(self):
        with self.assertRaisesMessage(ValidationError, "pas être choisi"):
            set_copy_status(copy=self.copy, new_status=BookCopy.Status.BORROWED)

    def test_meme_statut_refuse(self):
        with self.assertRaisesMessage(ValidationError, "déjà ce statut"):
            set_copy_status(copy=self.copy, new_status=BookCopy.Status.AVAILABLE)


class ReferenceTests(ManagementTestCase):
    def test_creer_une_categorie_et_un_emplacement(self):
        self.assertEqual(self.cat_a.organization, self.org_a)
        self.assertTrue(self.loc_a.active)

    def test_doublon_sans_tenir_compte_de_la_casse(self):
        with self.assertRaisesMessage(ValidationError, "existe déjà"):
            create_reference(kind="category", organization=self.org_a, name="romans")
        with self.assertRaisesMessage(ValidationError, "existe déjà"):
            create_reference(kind="location", organization=self.org_a, name=" SALLE  1 ")

    def test_meme_nom_possible_dans_une_autre_structure(self):
        create_reference(kind="category", organization=self.org_b, name="Romans")
        self.assertEqual(Category.objects.filter(name="Romans").count(), 2)

    def test_nom_vide_refuse(self):
        with self.assertRaisesMessage(ValidationError, "obligatoire"):
            create_reference(kind="category", organization=self.org_a, name="   ")

    def test_renommer_et_refuser_un_nom_deja_pris(self):
        rename_reference(item=self.loc_a, name="Grande salle")
        self.loc_a.refresh_from_db()
        self.assertEqual(self.loc_a.name, "Grande salle")
        with self.assertRaisesMessage(ValidationError, "existe déjà"):
            rename_reference(item=self.loc_a, name="salle 2")
        rename_reference(item=self.loc_a, name="GRANDE SALLE")

    def test_desactiver_et_reactiver(self):
        set_reference_active(item=self.cat_a, active=False)
        self.cat_a.refresh_from_db()
        self.assertFalse(self.cat_a.active)
        set_reference_active(item=self.cat_a, active=True)
        self.cat_a.refresh_from_db()
        self.assertTrue(self.cat_a.active)
