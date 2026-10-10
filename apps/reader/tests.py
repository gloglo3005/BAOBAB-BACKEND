from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.catalog.models import Author, BookCopy, Category, Location
from apps.catalog.services import add_copy, archive_book, create_book
from apps.circulation.models import Loan
from apps.circulation.services import checkout_copy, return_copy
from apps.circulation.tests import CirculationTestCase
from apps.members.models import LinkCode, Member
from apps.members.services import (
    accept_member, create_link_code, register_member, reject_member, suspend_member,
)
from apps.organizations.models import Organization, OrganizationStaff
from apps.organizations.services import create_organization

from .forms import RegisterForm

User = get_user_model()

PASSWORD = "Zq7!fT2mQp91"


def make_reader(email="lecteur@example.com", first_name="Ama", last_name="Kofi"):
    return User.objects.create_user(
        email=email, password=PASSWORD, first_name=first_name, last_name=last_name
    )


class ReaderTestCase(CirculationTestCase):
    """Le lecteur est relié à la fiche member_a (structure A)."""

    def setUp(self):
        super().setUp()
        self.reader = make_reader()
        Member.objects.filter(pk=self.member_a.pk).update(user=self.reader)
        self.member_a.refresh_from_db()
        self.client.force_login(self.reader)

    def url(self, name, *args):
        return reverse(f"reader:{name}", args=args)

    def get(self, name, *args, **query):
        return self.client.get(self.url(name, *args), query)

    def library_url(self, name, member=None, *extra):
        return self.url(name, (member or self.member_a).pk, *extra)

    def loan(self, copy=None, member=None, days=14):
        loan = self.checkout(copy=copy, member=member)
        Loan.objects.filter(pk=loan.pk).update(
            due_date=timezone.localdate() + timedelta(days=days)
        )
        loan.refresh_from_db()
        return loan


# ------------------------------------------------------------------ inscription et connexion


class RegistrationTests(ReaderTestCase):
    def setUp(self):
        super().setUp()
        self.client = Client()  # visiteur

    def payload(self, **overrides):
        data = {
            "first_name": "Yao", "last_name": "Mensah", "email": "Yao@Example.com",
            "phone": "90 00 00 00", "password1": PASSWORD, "password2": PASSWORD,
        }
        data.update(overrides)
        return data

    def test_le_formulaire_ne_propose_aucun_role(self):
        names = set(RegisterForm().fields)
        self.assertEqual(
            names,
            {"first_name", "last_name", "email", "phone", "password1", "password2"},
        )
        page = self.client.get(self.url("register"))
        self.assertEqual(page.status_code, 200)
        for word in ("role", "staff", "admin", "is_platform_admin", "is_superuser"):
            self.assertNotContains(page, f'name="{word}"')

    def test_inscription_cree_un_compte_sans_privilege_et_connecte(self):
        response = self.client.post(self.url("register"), self.payload())
        self.assertRedirects(response, self.url("join"), fetch_redirect_response=False)

        user = User.objects.get(email="yao@example.com")
        self.assertEqual((user.first_name, user.last_name), ("Yao", "Mensah"))
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertFalse(user.is_platform_admin)
        self.assertFalse(OrganizationStaff.objects.filter(user=user).exists())
        self.assertTrue(user.check_password(PASSWORD))
        self.assertEqual(self.client.session["_auth_user_id"], str(user.pk))

    def test_un_role_envoye_a_la_main_est_ignore(self):
        self.client.post(
            self.url("register"),
            self.payload(
                role="PLATFORM_ADMIN", is_platform_admin="on", is_staff="on",
                is_superuser="on", organization="1", must_change_password="on",
            ),
        )
        user = User.objects.get(email="yao@example.com")
        self.assertFalse(user.is_staff or user.is_superuser or user.is_platform_admin)
        self.assertFalse(user.must_change_password)
        self.assertFalse(OrganizationStaff.objects.filter(user=user).exists())

    def test_le_compte_cree_n_ouvre_ni_le_staff_ni_l_admin_globale(self):
        self.client.post(self.url("register"), self.payload(role="PLATFORM_ADMIN", is_staff="on"))
        for name in ("dashboard", "members", "catalog", "loan"):
            self.assertEqual(self.client.get(reverse(f"staff:{name}")).status_code, 403, name)
        self.assertEqual(self.client.get(reverse("imports:import_upload")).status_code, 403)
        response = self.client.get("/global/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])

    def test_email_deja_utilise_meme_avec_une_autre_casse(self):
        before = User.objects.count()
        response = self.client.post(self.url("register"), self.payload(email="LECTEUR@example.com"))
        self.assertContains(response, "Un compte existe déjà")
        self.assertEqual(User.objects.count(), before)

    def test_mots_de_passe_differents(self):
        before = User.objects.count()
        response = self.client.post(self.url("register"), self.payload(password2="autre"))
        self.assertContains(response, "ne correspondent pas")
        self.assertEqual(User.objects.count(), before)

    def test_mot_de_passe_trop_faible(self):
        before = User.objects.count()
        response = self.client.post(
            self.url("register"), self.payload(password1="12345678", password2="12345678")
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(User.objects.count(), before)

    def test_noms_trop_longs_pour_la_fiche_membre(self):
        before = User.objects.count()
        response = self.client.post(self.url("register"), self.payload(first_name="A" * 101))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(User.objects.count(), before)

    def test_un_connecte_est_renvoye_a_l_accueil(self):
        self.client.force_login(self.reader)
        self.assertRedirects(
            self.client.get(self.url("register")), self.url("home"), fetch_redirect_response=False
        )


class LoginTests(ReaderTestCase):
    def setUp(self):
        super().setUp()
        self.client = Client()

    def test_la_page_de_connexion_s_affiche(self):
        self.assertEqual(self.client.get(self.url("login")).status_code, 200)

    def test_connexion_puis_accueil_lecteur(self):
        response = self.client.post(
            self.url("login"), {"username": "lecteur@example.com", "password": PASSWORD}
        )
        self.assertRedirects(response, self.url("home"), fetch_redirect_response=False)
        self.assertContains(self.client.get(self.url("home")), "Bonjour Ama")

    def test_connexion_insensible_a_la_casse_de_l_email(self):
        response = self.client.post(
            self.url("login"), {"username": "LECTEUR@Example.com", "password": PASSWORD}
        )
        self.assertRedirects(response, self.url("home"), fetch_redirect_response=False)

    def test_mauvais_mot_de_passe(self):
        response = self.client.post(
            self.url("login"), {"username": "lecteur@example.com", "password": "faux"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_compte_desactive_refuse(self):
        User.objects.filter(pk=self.reader.pk).update(is_active=False)
        response = self.client.post(
            self.url("login"), {"username": "lecteur@example.com", "password": PASSWORD}
        )
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertEqual(response.status_code, 200)

    def test_redirection_next_interne_acceptee_externe_refusee(self):
        target = self.library_url("catalog")
        response = self.client.post(
            self.url("login"),
            {"username": "lecteur@example.com", "password": PASSWORD, "next": target},
        )
        self.assertRedirects(response, target, fetch_redirect_response=False)

        self.client.logout()
        response = self.client.post(
            self.url("login"),
            {"username": "lecteur@example.com", "password": PASSWORD,
             "next": "https://evil.example/"},
        )
        self.assertRedirects(response, self.url("home"), fetch_redirect_response=False)

    def test_un_visiteur_est_renvoye_vers_la_connexion(self):
        pages = [
            self.url("home"), self.url("profile"), self.url("join"),
            self.library_url("library"), self.library_url("catalog"),
            self.library_url("loans"), self.library_url("overdue"),
            self.library_url("history"),
        ]
        for url in pages:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302, url)
            self.assertIn(self.url("login"), response["Location"], url)
            self.assertIn("next=", response["Location"], url)

    def test_la_deconnexion_exige_post(self):
        self.client.force_login(self.reader)
        self.assertEqual(self.client.get(self.url("logout")).status_code, 405)
        response = self.client.post(self.url("logout"))
        self.assertRedirects(response, self.url("login"), fetch_redirect_response=False)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_changement_de_mot_de_passe(self):
        self.client.force_login(self.reader)
        new = "Nv8#kP3wLx52"
        response = self.client.post(
            self.url("password"),
            {"old_password": PASSWORD, "new_password1": new, "new_password2": new},
        )
        self.assertRedirects(response, self.url("profile"), fetch_redirect_response=False)
        self.reader.refresh_from_db()
        self.assertTrue(self.reader.check_password(new))


# ------------------------------------------------------------------ séparation des interfaces


class InterfaceSeparationTests(ReaderTestCase):
    def test_un_lecteur_ne_peut_pas_appeler_une_route_staff(self):
        for name in ("dashboard", "search", "loan", "return", "members", "catalog",
                     "overdue", "history"):
            self.assertEqual(
                self.client.get(reverse(f"staff:{name}")).status_code, 403, name
            )
        self.assertEqual(self.client.get(reverse("imports:import_upload")).status_code, 403)
        self.assertEqual(self.client.get(reverse("imports:import_history")).status_code, 403)

    def test_un_lecteur_n_ouvre_pas_l_admin_globale(self):
        response = self.client.get("/global/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])

    def test_un_lecteur_membre_d_une_structure_n_est_pas_staff(self):
        # Avoir une fiche Member ne donne aucun droit Staff.
        self.assertTrue(Member.objects.filter(user=self.reader).exists())
        self.assertEqual(self.client.get(reverse("staff:members")).status_code, 403)

    def test_un_staff_ne_peut_pas_appeler_une_route_globale(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(reverse("staff:dashboard")).status_code, 200)
        response = self.client.get("/global/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])

    def test_un_staff_peut_aussi_se_servir_de_l_espace_lecteur(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.get("home").status_code, 200)


# ------------------------------------------------------------------ rejoindre une bibliothèque


class JoinTests(ReaderTestCase):
    def setUp(self):
        super().setUp()
        self.newcomer = make_reader("nouveau@example.com", "Yao", "Mensah")
        self.client.force_login(self.newcomer)

    def test_accueil_sans_bibliotheque(self):
        response = self.get("home")
        self.assertContains(response, "Rejoindre une bibliothèque")
        self.assertNotContains(response, self.member_a.member_number)

    def test_la_page_liste_les_structures_ouvertes(self):
        pending = create_organization(name="En attente", slug="attente")
        Organization.objects.filter(pk=pending.pk).update(status=Organization.Status.PENDING)
        no_library = create_organization(name="Coworking seul", slug="cowork")
        Organization.objects.filter(pk=no_library.pk).update(library_module=False)
        page = self.get("join")
        self.assertContains(page, "Structure A")
        self.assertContains(page, "Structure B")
        self.assertNotContains(page, "En attente</span>")
        self.assertNotContains(page, "Coworking seul")

    def test_demande_a_rejoindre(self):
        response = self.client.post(self.url("join_request", self.org_a.pk))
        member = Member.objects.get(organization=self.org_a, user=self.newcomer)
        self.assertRedirects(
            response, self.url("library", member.pk), fetch_redirect_response=False
        )
        self.assertEqual(member.status, Member.Status.PENDING)
        self.assertEqual((member.first_name, member.last_name), ("Yao", "Mensah"))
        self.assertTrue(
            AuditLog.objects.filter(
                action=AuditLog.Action.MEMBER_CREATED, entity_id=str(member.pk)
            ).exists()
        )

    def test_demande_en_double_refusee(self):
        self.client.post(self.url("join_request", self.org_a.pk))
        response = self.client.post(self.url("join_request", self.org_a.pk), follow=True)
        self.assertContains(response, "déjà")
        self.assertEqual(
            Member.objects.filter(organization=self.org_a, user=self.newcomer).count(), 1
        )

    def test_demande_exige_post(self):
        self.assertEqual(self.get("join_request", self.org_a.pk).status_code, 405)

    def test_demande_vers_une_structure_inactive_ou_sans_module(self):
        Organization.objects.filter(pk=self.org_b.pk).update(status=Organization.Status.SUSPENDED)
        self.assertEqual(self.client.post(self.url("join_request", self.org_b.pk)).status_code, 404)
        Organization.objects.filter(pk=self.org_a.pk).update(library_module=False)
        self.assertEqual(self.client.post(self.url("join_request", self.org_a.pk)).status_code, 404)
        self.assertFalse(Member.objects.filter(user=self.newcomer).exists())

    def test_rattachement_par_code(self):
        counter = register_member(organization=self.org_b, first_name="Yao", last_name="Mensah")
        code = create_link_code(member=counter, created_by=self.staff_b).code
        response = self.client.post(self.url("join"), {"code": code.lower()})
        self.assertRedirects(
            response, self.url("library", counter.pk), fetch_redirect_response=False
        )
        counter.refresh_from_db()
        self.assertEqual(counter.user, self.newcomer)
        self.assertEqual(counter.status, Member.Status.ACCEPTED)
        self.assertTrue(LinkCode.objects.get(code=code).used_at)

    def test_code_invalide_ou_deja_utilise(self):
        page = self.client.post(self.url("join"), {"code": "FAUX1234"})
        self.assertContains(page, "Code invalide ou expiré")

        counter = register_member(organization=self.org_b, first_name="Yao", last_name="Mensah")
        code = create_link_code(member=counter).code
        self.client.post(self.url("join"), {"code": code})
        self.client.force_login(make_reader("autre@example.com"))
        page = self.client.post(self.url("join"), {"code": code})
        self.assertContains(page, "Code invalide ou expiré")
        counter.refresh_from_db()
        self.assertEqual(counter.user, self.newcomer)

    def test_la_demande_valide_au_comptoir_donne_acces_au_catalogue(self):
        self.client.post(self.url("join_request", self.org_a.pk))
        member = Member.objects.get(organization=self.org_a, user=self.newcomer)
        accept_member(member, performed_by=self.staff)
        page = self.client.get(self.url("library", member.pk))
        self.assertContains(page, "Membre")
        self.assertNotContains(page, "en attente de validation par la bibliothèque")


# ------------------------------------------------------------------ accueil et profil


class HomeAndProfileTests(ReaderTestCase):
    def test_accueil_liste_mes_bibliotheques_avec_les_compteurs(self):
        self.loan(days=5)
        self.loan(copy=self.new_copy("A-002"), days=-3)
        page = self.get("home")
        self.assertContains(page, "Structure A")
        self.assertContains(page, "2 emprunts en cours")
        self.assertContains(page, "1 en retard")
        self.assertContains(page, self.member_a.member_number)

    def test_l_accueil_ne_montre_que_mes_fiches(self):
        # Les deux structures numérotent à partir de MEM-0001 : on distingue la fiche de B.
        Member.objects.filter(pk=self.member_b.pk).update(member_number="B-777")
        self.member_b.refresh_from_db()
        page = self.get("home")
        self.assertNotContains(page, "Structure B")
        self.assertNotContains(page, self.member_b.member_number)

    def test_un_lecteur_membre_de_deux_structures_les_voit_toutes_les_deux(self):
        Member.objects.filter(pk=self.member_b.pk).update(user=self.reader)
        page = self.get("home")
        self.assertContains(page, "Structure A")
        self.assertContains(page, "Structure B")

    def test_structure_suspendue_ou_sans_module_non_accessible(self):
        Organization.objects.filter(pk=self.org_a.pk).update(status=Organization.Status.SUSPENDED)
        self.assertContains(self.get("home"), "pas disponible pour le moment")
        self.assertEqual(self.get("library", self.member_a.pk).status_code, 403)
        Organization.objects.filter(pk=self.org_a.pk).update(
            status=Organization.Status.ACTIVE, library_module=False
        )
        self.assertEqual(self.get("catalog", self.member_a.pk).status_code, 403)

    def test_profil(self):
        page = self.get("profile")
        self.assertContains(page, "lecteur@example.com")
        self.assertContains(page, "Structure A")
        self.assertContains(page, "Se déconnecter")


# ------------------------------------------------------------------ catalogue


class CatalogTests(ReaderTestCase):
    def setUp(self):
        super().setUp()
        author = Author.objects.create(
            organization=self.org_a, first_name="Ferdinand", last_name="Oyono"
        )
        self.roman = Category.objects.create(organization=self.org_a, name="Roman")
        self.shelf = Location.objects.create(organization=self.org_a, name="Rayon A")
        self.boy = create_book(
            organization=self.org_a, title="Une vie de boy", authors=[author],
            category=self.roman, publisher="Présence Africaine", publication_year=1956,
        )
        add_copy(book=self.boy, inventory_code="A-010", location=self.shelf)
        add_copy(book=self.boy, inventory_code="A-011", location=self.shelf)

    def test_le_catalogue_liste_les_livres_actifs_de_ma_bibliotheque(self):
        page = self.get("catalog", self.member_a.pk)
        self.assertContains(page, "Une vie de boy")
        self.assertContains(page, "Livre A")
        self.assertContains(page, "Disponible (2 sur 2)")
        self.assertNotContains(page, "Livre B")

    def test_recherche_par_titre_auteur_et_isbn(self):
        create_book(organization=self.org_a, title="Autre", isbn="9782070368228")
        for query, expected in (("boy", "Une vie de boy"), ("oyono", "Une vie de boy"),
                                ("978-2", None), ("9782070368228", "Autre")):
            page = self.get("catalog", self.member_a.pk, q=query)
            if expected:
                self.assertContains(page, expected, msg_prefix=query)
        self.assertNotContains(self.get("catalog", self.member_a.pk, q="oyono"), "Livre A")
        self.assertContains(self.get("catalog", self.member_a.pk, q="zzzz"), "Aucun livre ne correspond")

    def test_filtre_par_categorie_et_disponibilite(self):
        page = self.get("catalog", self.member_a.pk, category=self.roman.pk)
        self.assertContains(page, "Une vie de boy")
        self.assertNotContains(page, "Livre A")
        self.loan(copy=self.copy_a)
        page = self.get("catalog", self.member_a.pk, available="1")
        self.assertNotContains(page, ">Livre A<")
        self.assertContains(page, "Une vie de boy")
        self.assertContains(self.get("catalog", self.member_a.pk), "Tout est emprunté")

    def test_categorie_invalide_ne_fait_pas_d_erreur(self):
        self.assertEqual(self.get("catalog", self.member_a.pk, category="abc").status_code, 200)

    def test_les_livres_archives_sont_caches(self):
        archive_book(book=self.boy)
        self.assertNotContains(self.get("catalog", self.member_a.pk), "Une vie de boy")
        self.assertEqual(self.get("book_detail", self.member_a.pk, self.boy.pk).status_code, 404)

    def test_livre_sans_exemplaire(self):
        create_book(organization=self.org_a, title="Sans exemplaire")
        self.assertContains(self.get("catalog", self.member_a.pk, q="Sans"), "Aucun exemplaire")

    def test_detail_d_un_livre(self):
        page = self.get("book_detail", self.member_a.pk, self.boy.pk)
        self.assertContains(page, "Une vie de boy")
        self.assertContains(page, "Ferdinand Oyono")
        self.assertContains(page, "2 exemplaires disponibles")
        self.assertContains(page, "Rayon A : 2")
        self.assertContains(page, "Présence Africaine")
        self.assertContains(page, "1956")
        # Un lecteur n'a pas besoin des codes d'inventaire.
        self.assertNotContains(page, "A-010")

    def test_detail_indique_qu_on_a_deja_le_livre(self):
        self.loan(copy=self.copy_a, days=5)
        page = self.get("book_detail", self.member_a.pk, self.book_a.pk)
        self.assertContains(page, "Vous avez déjà ce livre")
        self.assertContains(page, "Aucun exemplaire disponible")

    def test_detail_ne_revele_pas_qui_a_emprunte(self):
        other = register_member(organization=self.org_a, first_name="Secret", last_name="Personne")
        self.loan(copy=self.copy_a, member=other, days=5)
        page = self.get("book_detail", self.member_a.pk, self.book_a.pk)
        self.assertNotContains(page, "Secret")
        self.assertNotContains(page, "Personne")

    def test_catalogue_pagine(self):
        for i in range(25):
            create_book(organization=self.org_a, title=f"Titre {i:02d}")
        page = self.get("catalog", self.member_a.pk)
        self.assertContains(page, "Page 1 sur 2")
        self.assertContains(self.get("catalog", self.member_a.pk, page=2), "Page 2 sur 2")


# ------------------------------------------------------------------ emprunts, retards, historique


class LoanPagesTests(ReaderTestCase):
    def test_mes_emprunts(self):
        self.loan(days=10)
        page = self.get("loans", self.member_a.pk)
        self.assertContains(page, "Livre A")
        self.assertContains(page, "10 jours restants")
        self.assertContains(page, (timezone.localdate() + timedelta(days=10)).strftime("%d/%m/%Y"))

    def test_echeances_proches(self):
        self.loan(days=0)
        self.assertContains(self.get("loans", self.member_a.pk), "À rendre aujourd'hui")
        Loan.objects.update(due_date=timezone.localdate() + timedelta(days=2))
        self.assertContains(self.get("loans", self.member_a.pk), "Plus que 2 jours")

    def test_mes_retards(self):
        self.loan(days=-4)
        ok = self.new_copy("A-002")
        self.loan(copy=ok, days=6)
        page = self.get("overdue", self.member_a.pk)
        self.assertContains(page, "En retard de 4 jours")
        self.assertContains(page, "Livre A")
        self.assertEqual(len(page.context["page"].object_list), 1)
        self.assertEqual(self.get("loans", self.member_a.pk).context["page"].paginator.count, 2)

    def test_aucun_retard(self):
        self.assertContains(self.get("overdue", self.member_a.pk), "Aucun retard")

    def test_le_compteur_de_retards_est_dans_la_navigation(self):
        self.loan(days=-2)
        self.assertContains(self.get("loans", self.member_a.pk), '<span class="count">1</span>')

    def test_historique_des_livres_rendus(self):
        loan = self.loan(days=5)
        return_copy(organization=self.org_a, loan_id=loan.pk, received_by=self.staff)
        self.assertContains(self.get("history", self.member_a.pk), "rendu le")
        self.assertNotContains(self.get("loans", self.member_a.pk), "Livre A")
        self.assertEqual(
            self.get("history", self.member_a.pk).context["page"].paginator.count, 1
        )
        self.assertEqual(loan.pk, self.get("history", self.member_a.pk).context["page"].object_list[0].pk)

    def test_historique_vide(self):
        self.assertContains(self.get("history", self.member_a.pk), "pas encore rendu")

    def test_un_livre_perdu_apparait_dans_l_historique(self):
        loan = self.loan(days=5)
        Loan.objects.filter(pk=loan.pk).update(status=Loan.Status.LOST)
        self.assertContains(self.get("history", self.member_a.pk), "Déclaré perdu")

    def test_je_ne_vois_pas_les_prets_des_autres_membres(self):
        other = register_member(organization=self.org_a, first_name="Eve", last_name="Dia")
        copy = self.new_copy("A-002")
        create_book(organization=self.org_a, title="Livre d'Eve")
        self.loan(copy=copy, member=other, days=3)
        for name in ("loans", "overdue", "history"):
            page = self.get(name, self.member_a.pk)
            self.assertEqual(page.context["page"].paginator.count, 0, name)
        self.assertEqual(self.get("home").context["memberships"][0].active_loans, 0)

    def test_adhesion_suspendue(self):
        suspend_member(self.member_a, performed_by=self.staff)
        self.assertContains(self.get("library", self.member_a.pk), "adhésion est suspendue")
        self.assertEqual(self.get("catalog", self.member_a.pk).status_code, 200)
        self.assertEqual(self.get("loans", self.member_a.pk).status_code, 200)

    def test_les_pages_se_lisent_en_get_seulement(self):
        for name in ("library", "catalog", "loans", "overdue", "history"):
            response = self.client.get(self.url(name, self.member_a.pk))
            self.assertEqual(response.status_code, 200, name)


# ------------------------------------------------------------------ statut d'adhésion


class MembershipStatusTests(ReaderTestCase):
    def setUp(self):
        super().setUp()
        self.newcomer = make_reader("nouveau@example.com", "Yao", "Mensah")
        self.client.force_login(self.newcomer)
        self.client.post(self.url("join_request", self.org_a.pk))
        self.pending = Member.objects.get(organization=self.org_a, user=self.newcomer)

    def test_en_attente_consulte_le_catalogue_et_voit_un_bandeau(self):
        page = self.get("library", self.pending.pk)
        self.assertContains(page, "en attente de validation par la bibliothèque")
        self.assertEqual(self.get("catalog", self.pending.pk).status_code, 200)
        self.assertContains(self.get("loans", self.pending.pk), "aucun livre emprunté")

    def test_demande_refusee(self):
        reject_member(self.pending, performed_by=self.staff)
        self.assertEqual(self.get("library", self.pending.pk).status_code, 403)
        self.assertEqual(self.get("catalog", self.pending.pk).status_code, 403)
        self.assertContains(self.get("home"), "refusée")


# ------------------------------------------------------------------ isolation entre structures


class IsolationTests(ReaderTestCase):
    def test_la_fiche_d_un_autre_lecteur_est_introuvable(self):
        other = make_reader("autre@example.com", "Yao", "Mensah")
        Member.objects.filter(pk=self.member_b.pk).update(user=other)
        for name in ("library", "catalog", "loans", "overdue", "history"):
            self.assertEqual(self.get(name, self.member_b.pk).status_code, 404, name)

    def test_une_fiche_sans_compte_est_introuvable(self):
        for name in ("library", "catalog", "loans"):
            self.assertEqual(self.get(name, self.member_b.pk).status_code, 404, name)

    def test_identifiants_devines(self):
        for pk in (0, 99999):
            self.assertEqual(self.get("library", pk).status_code, 404)

    def test_un_livre_d_une_autre_structure_est_introuvable(self):
        for name in ("book_detail",):
            response = self.get(name, self.member_a.pk, self.book_b.pk)
            self.assertEqual(response.status_code, 404)
            self.assertNotContains(response, "Livre B", status_code=404)

    def test_le_catalogue_ne_contient_que_ma_structure(self):
        create_book(organization=self.org_b, title="Secret de B")
        page = self.get("catalog", self.member_a.pk)
        self.assertNotContains(page, "Secret de B")
        self.assertNotContains(page, "Livre B")
        self.assertNotContains(self.get("catalog", self.member_a.pk, q="Secret"), "Secret de B")

    def test_les_categories_proposees_sont_celles_de_ma_structure(self):
        Category.objects.create(organization=self.org_b, name="Catégorie de B")
        Category.objects.create(organization=self.org_a, name="Catégorie de A")
        page = self.get("catalog", self.member_a.pk)
        self.assertContains(page, "Catégorie de A")
        self.assertNotContains(page, "Catégorie de B")

    def test_une_categorie_d_une_autre_structure_ne_filtre_rien_d_utile(self):
        other = Category.objects.create(organization=self.org_b, name="Catégorie de B")
        page = self.get("catalog", self.member_a.pk, category=other.pk)
        self.assertEqual(page.context["page"].paginator.count, 0)

    def test_deux_structures_un_seul_compte_chaque_fiche_reste_cloisonnee(self):
        Member.objects.filter(pk=self.member_b.pk).update(user=self.reader)
        self.loan(copy=self.copy_a, days=5)
        copy_b_loan = checkout_copy(
            organization=self.org_b, book_copy_id=self.copy_b.pk,
            member_id=self.member_b.pk, issued_by=self.staff_b,
        )
        a = self.get("loans", self.member_a.pk)
        b = self.get("loans", self.member_b.pk)
        self.assertContains(a, "Livre A")
        self.assertNotContains(a, "Livre B")
        self.assertContains(b, "Livre B")
        self.assertNotContains(b, "Livre A")
        self.assertEqual(copy_b_loan.organization, self.org_b)

    def test_les_pages_n_exposent_ni_codes_ni_agents(self):
        self.loan(copy=self.copy_a, days=5)
        for name in ("library", "catalog", "loans", "history"):
            page = self.get(name, self.member_a.pk)
            self.assertNotContains(page, "A-001", msg_prefix=name)
            self.assertNotContains(page, "staff@example.com", msg_prefix=name)
