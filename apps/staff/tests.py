from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.catalog.models import BookCopy
from apps.circulation.models import Loan
from apps.circulation.services import checkout_copy
from apps.circulation.tests import CirculationTestCase, make_staff
from apps.members.models import LinkCode, Member
from apps.organizations.models import Organization, OrganizationStaff

User = get_user_model()


class StaffTestCase(CirculationTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.staff)

    def get(self, name, **query):
        return self.client.get(reverse(f"staff:{name}"), query)

    def make_overdue(self, loan, days=3):
        Loan.objects.filter(pk=loan.pk).update(
            due_date=timezone.localdate() - timedelta(days=days)
        )


class AccessTests(StaffTestCase):
    def test_un_visiteur_est_renvoye_vers_la_connexion(self):
        self.client.logout()
        response = self.get("dashboard")
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("staff:login"), response["Location"])
        self.assertIn("next=", response["Location"])

    def test_la_page_de_connexion_s_affiche(self):
        self.client.logout()
        self.assertEqual(self.get("login").status_code, 200)

    def test_connexion_avec_email_et_mot_de_passe(self):
        self.client.logout()
        response = self.client.post(
            reverse("staff:login"),
            {"username": "staff@example.com", "password": "abc12345"},
        )
        self.assertRedirects(
            response, reverse("staff:dashboard"), fetch_redirect_response=False
        )

    def test_mauvais_mot_de_passe(self):
        self.client.logout()
        response = self.client.post(
            reverse("staff:login"),
            {"username": "staff@example.com", "password": "faux"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_le_staff_voit_son_tableau_de_bord(self):
        response = self.get("dashboard")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.wsgi_request.organization, self.org_a)

    def test_un_compte_sans_rattachement_est_refuse(self):
        reader = User.objects.create_user(
            email="lecteur@example.com", password="abc12345",
            first_name="Le", last_name="Teur",
        )
        self.client.force_login(reader)
        for name in ("dashboard", "search", "loan", "return", "members", "history"):
            self.assertEqual(self.get(name).status_code, 403, name)

    def test_un_admin_de_plateforme_sans_rattachement_est_refuse(self):
        admin = User.objects.create_superuser(
            email="admin@example.com", password="abc12345",
            first_name="Ad", last_name="Min",
        )
        self.client.force_login(admin)
        self.assertEqual(self.get("dashboard").status_code, 403)

    def test_structure_suspendue_ou_en_attente_refusee(self):
        for status in (Organization.Status.SUSPENDED, Organization.Status.PENDING):
            Organization.objects.filter(pk=self.org_a.pk).update(status=status)
            self.assertEqual(self.get("dashboard").status_code, 403, status)

    def test_staff_desactive_refuse(self):
        OrganizationStaff.objects.filter(user=self.staff).update(active=False)
        self.assertEqual(self.get("dashboard").status_code, 403)

    def test_module_bibliotheque_desactive(self):
        Organization.objects.filter(pk=self.org_a.pk).update(library_module=False)
        self.assertEqual(self.get("dashboard").status_code, 200)
        for name in ("search", "loan", "return", "overdue", "members", "catalog", "history"):
            self.assertEqual(self.get(name).status_code, 403, name)

    def test_mot_de_passe_temporaire_force_le_changement(self):
        User.objects.filter(pk=self.staff.pk).update(must_change_password=True)
        response = self.get("dashboard")
        self.assertRedirects(
            response, reverse("staff:password"), fetch_redirect_response=False
        )

        response = self.client.post(
            reverse("staff:password"),
            {
                "old_password": "abc12345",
                "new_password1": "Zq7!fT2mQp91",
                "new_password2": "Zq7!fT2mQp91",
            },
        )
        self.assertRedirects(
            response, reverse("staff:dashboard"), fetch_redirect_response=False
        )
        self.staff.refresh_from_db()
        self.assertFalse(self.staff.must_change_password)
        self.assertEqual(self.get("dashboard").status_code, 200)

    def test_deconnexion_seulement_en_post(self):
        self.assertEqual(self.client.get(reverse("staff:logout")).status_code, 405)
        response = self.client.post(reverse("staff:logout"))
        self.assertRedirects(
            response, reverse("staff:login"), fetch_redirect_response=False
        )
        self.assertEqual(self.get("dashboard").status_code, 302)

    def test_une_session_trafiquee_ne_donne_pas_une_autre_structure(self):
        self.client.force_login(self.staff_b)
        session = self.client.session
        session["staff_organization_id"] = self.org_a.pk
        session.save()
        response = self.get("dashboard")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.wsgi_request.organization, self.org_b)

    def test_staff_de_deux_structures_choisit_la_sienne(self):
        OrganizationStaff.objects.create(user=self.staff, organization=self.org_b)
        response = self.get("dashboard")
        self.assertRedirects(
            response, reverse("staff:organizations"), fetch_redirect_response=False
        )
        self.assertEqual(self.get("organizations").status_code, 200)

        response = self.client.post(
            reverse("staff:organizations"), {"organization": self.org_b.pk}
        )
        self.assertRedirects(
            response, reverse("staff:dashboard"), fetch_redirect_response=False
        )
        self.assertEqual(self.get("dashboard").wsgi_request.organization, self.org_b)

    def test_on_ne_choisit_pas_une_structure_qui_n_est_pas_la_sienne(self):
        OrganizationStaff.objects.create(user=self.staff, organization=self.org_b)
        third = Organization.objects.create(
            name="Autre", slug="autre", status=Organization.Status.ACTIVE
        )
        response = self.client.post(
            reverse("staff:organizations"), {"organization": third.pk}
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("staff_organization_id", self.client.session)

    def test_les_formulaires_exigent_le_jeton_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.staff)
        response = client.post(
            reverse("staff:loan"),
            {"member_id": self.member_a.pk, "copy_id": self.copy_a.pk},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(Loan.objects.count(), 0)


class DashboardTests(StaffTestCase):
    def test_les_chiffres_du_jour(self):
        stats = self.get("dashboard").context["stats"]
        self.assertEqual(stats["loans_today"], 0)
        self.assertEqual(stats["available"], 1)

        loan = self.checkout()
        stats = self.get("dashboard").context["stats"]
        self.assertEqual(stats["loans_today"], 1)
        self.assertEqual(stats["available"], 0)
        self.assertEqual(stats["borrowed"], 1)
        self.assertEqual(stats["overdue"], 0)

        self.make_overdue(loan)
        self.assertEqual(self.get("dashboard").context["stats"]["overdue"], 1)

    def test_les_retours_du_jour(self):
        loan = self.checkout()
        self.client.post(
            reverse("staff:return"), {"loan_id": loan.pk, "outcome": "GOOD"}
        )
        stats = self.get("dashboard").context["stats"]
        self.assertEqual(stats["returns_today"], 1)

    def test_les_chiffres_ne_comptent_que_la_structure(self):
        checkout_copy(
            organization=self.org_b, book_copy_id=self.copy_b.pk,
            member_id=self.member_b.pk, issued_by=self.staff_b,
        )
        stats = self.get("dashboard").context["stats"]
        self.assertEqual(stats["loans_today"], 0)
        self.assertEqual(stats["borrowed"], 0)


class SearchTests(StaffTestCase):
    def test_trouve_un_livre_un_exemplaire_et_un_membre(self):
        response = self.get("search", q="A")
        self.assertContains(response, "au moins 2 caractères")

        response = self.get("search", q="Livre")
        self.assertContains(response, "Livre A")

        response = self.get("search", q="A-00")
        self.assertContains(response, "A-001")

        response = self.get("search", q="Ama Kofi")
        self.assertContains(response, self.member_a.member_number)

        response = self.get("search", q=self.member_a.member_number)
        self.assertContains(response, "Kofi")

    def test_sans_resultat_le_message_aide(self):
        self.assertContains(self.get("search", q="introuvable"), "Aucun résultat")

    def test_la_recherche_ne_sort_pas_de_la_structure(self):
        for text in ("Livre B", "B-001", "Yao", "Mensah"):
            response = self.get("search", q=text)
            self.assertContains(response, "Aucun résultat", msg_prefix=text)

    def test_htmx_recoit_un_fragment_et_un_acces_direct_la_page(self):
        fragment = self.client.get(
            reverse("staff:search"), {"q": "Livre"}, headers={"HX-Request": "true"}
        )
        self.assertContains(fragment, "Livre A")
        self.assertNotContains(fragment, "<html")
        self.assertIn("HX-Request", fragment["Vary"])

        page = self.get("search", q="Livre")
        self.assertContains(page, "<html")

    def test_un_exemplaire_emprunte_montre_qui_l_a(self):
        self.checkout()
        response = self.get("search", q="A-001")
        self.assertContains(response, "Ama")
        self.assertContains(response, "Rendre")


class LoanFlowTests(StaffTestCase):
    def post_loan(self, **data):
        payload = {
            "member_id": self.member_a.pk,
            "copy_id": self.copy_a.pk,
            "member_q": self.member_a.member_number,
            "copy_q": "A-001",
        }
        payload.update(data)
        return self.client.post(reverse("staff:loan"), payload)

    def test_la_page_de_pret_s_affiche(self):
        self.assertEqual(self.get("loan").status_code, 200)

    def test_la_recherche_du_membre_propose_un_choix_unique(self):
        response = self.client.get(
            reverse("staff:loan_member_lookup"), {"member_q": "Ama"}
        )
        self.assertContains(response, 'name="member_id"')
        self.assertContains(response, f'value="{self.member_a.pk}"')
        self.assertContains(response, "Sélectionné")

    def test_la_recherche_de_l_exemplaire_propose_un_choix_unique(self):
        response = self.client.get(
            reverse("staff:loan_copy_lookup"), {"copy_q": "A-001"}
        )
        self.assertContains(response, 'name="copy_id"')
        self.assertContains(response, "Disponible")

    def test_un_membre_suspendu_ne_peut_pas_etre_choisi(self):
        Member.objects.filter(pk=self.member_a.pk).update(status=Member.Status.SUSPENDED)
        response = self.client.get(
            reverse("staff:loan_member_lookup"), {"member_q": "Ama"}
        )
        self.assertNotContains(response, 'type="hidden" name="member_id"')
        self.assertContains(response, "disabled")
        self.assertContains(response, "Prêt impossible")

    def test_un_exemplaire_emprunte_ne_peut_pas_etre_choisi(self):
        self.checkout()
        response = self.client.get(
            reverse("staff:loan_copy_lookup"), {"copy_q": "A-001"}
        )
        self.assertNotContains(response, 'type="hidden" name="copy_id"')
        self.assertContains(response, "disabled")
        self.assertContains(response, "retour prévu le")

    def test_les_lookups_ne_voient_pas_l_autre_structure(self):
        member = self.client.get(
            reverse("staff:loan_member_lookup"), {"member_q": "Yao"}
        )
        copy = self.client.get(reverse("staff:loan_copy_lookup"), {"copy_q": "B-001"})
        self.assertContains(member, "Aucun membre trouvé")
        self.assertContains(copy, "Aucun exemplaire trouvé")

    def test_le_pret_complet(self):
        response = self.post_loan()
        loan = Loan.objects.get()
        self.assertRedirects(
            response, reverse("staff:loan_receipt", args=[loan.pk]),
            fetch_redirect_response=False,
        )
        self.assertEqual(loan.issued_by, self.staff)
        self.copy_a.refresh_from_db()
        self.assertEqual(self.copy_a.status, BookCopy.Status.BORROWED)

        receipt = self.client.get(response["Location"])
        self.assertContains(receipt, "Prêt enregistré")
        self.assertContains(receipt, "A-001")
        entry = AuditLog.objects.get(action=AuditLog.Action.LOAN_CREATED)
        self.assertEqual(entry.user, self.staff)

    def test_un_deuxieme_pret_du_meme_exemplaire_est_refuse_avec_un_message(self):
        self.post_loan()
        response = self.post_loan()
        self.assertEqual(response.status_code, 302)
        page = self.client.get(response["Location"])
        self.assertContains(page, "déjà emprunté")
        self.assertEqual(Loan.objects.count(), 1)

    def test_sans_choix_on_demande_de_choisir(self):
        response = self.client.post(reverse("staff:loan"), {"notes": ""})
        page = self.client.get(response["Location"])
        self.assertContains(page, "Choisissez un membre et un exemplaire")
        self.assertEqual(Loan.objects.count(), 0)

    def test_un_membre_suspendu_est_refuse_meme_en_post_direct(self):
        Member.objects.filter(pk=self.member_a.pk).update(status=Member.Status.SUSPENDED)
        response = self.post_loan()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Loan.objects.count(), 0)

    def test_on_ne_prete_pas_l_exemplaire_ni_au_membre_d_une_autre_structure(self):
        self.post_loan(copy_id=self.copy_b.pk)
        self.post_loan(member_id=self.member_b.pk)
        self.assertEqual(Loan.objects.count(), 0)
        self.copy_b.refresh_from_db()
        self.assertEqual(self.copy_b.status, BookCopy.Status.AVAILABLE)

    def test_le_formulaire_est_prerempli_depuis_une_autre_page(self):
        response = self.get("loan", member_q="MEM-0001", copy_q="A-001")
        self.assertContains(response, 'value="MEM-0001"')
        self.assertContains(response, f'value="{self.copy_a.pk}"')

    def test_le_recu_d_une_autre_structure_est_introuvable(self):
        other = checkout_copy(
            organization=self.org_b, book_copy_id=self.copy_b.pk,
            member_id=self.member_b.pk, issued_by=self.staff_b,
        )
        response = self.client.get(reverse("staff:loan_receipt", args=[other.pk]))
        self.assertEqual(response.status_code, 404)


class ReturnFlowTests(StaffTestCase):
    def test_la_recherche_par_code_trouve_le_pret(self):
        loan = self.checkout()
        response = self.client.get(reverse("staff:return_lookup"), {"copy_q": "A-001"})
        self.assertContains(response, f'value="{loan.pk}"')
        self.assertContains(response, "Ama")

    def test_la_recherche_par_numero_de_membre_liste_ses_prets(self):
        other = self.new_copy("A-002")
        self.checkout()
        self.checkout(copy=other)
        response = self.client.get(
            reverse("staff:return_lookup"), {"copy_q": self.member_a.member_number}
        )
        self.assertContains(response, "A-001")
        self.assertContains(response, "A-002")

    def test_un_exemplaire_non_emprunte_n_a_pas_de_pret(self):
        response = self.client.get(reverse("staff:return_lookup"), {"copy_q": "A-001"})
        self.assertContains(response, "Aucun prêt en cours")

    def test_le_retour_en_bon_etat(self):
        loan = self.checkout()
        response = self.client.post(
            reverse("staff:return"),
            {"loan_id": loan.pk, "outcome": "GOOD", "copy_q": "A-001"},
        )
        self.assertRedirects(
            response, reverse("staff:return"), fetch_redirect_response=False
        )
        loan.refresh_from_db()
        self.copy_a.refresh_from_db()
        self.assertEqual(loan.status, Loan.Status.RETURNED)
        self.assertEqual(loan.received_by, self.staff)
        self.assertEqual(self.copy_a.status, BookCopy.Status.AVAILABLE)

        page = self.client.get(response["Location"])
        self.assertContains(page, "A-001 rendu par")

    def test_le_retour_d_un_exemplaire_endommage(self):
        loan = self.checkout()
        self.client.post(reverse("staff:return"), {"loan_id": loan.pk, "outcome": "DAMAGED"})
        self.copy_a.refresh_from_db()
        self.assertEqual(self.copy_a.status, BookCopy.Status.DAMAGED)

    def test_un_exemplaire_perdu(self):
        loan = self.checkout()
        response = self.client.post(
            reverse("staff:return"), {"loan_id": loan.pk, "outcome": "LOST"}
        )
        loan.refresh_from_db()
        self.assertEqual(loan.status, Loan.Status.LOST)
        self.assertContains(self.client.get(response["Location"]), "déclaré perdu")

    def test_un_etat_inconnu_est_refuse(self):
        loan = self.checkout()
        response = self.client.post(
            reverse("staff:return"), {"loan_id": loan.pk, "outcome": "NIMPORTEQUOI"}
        )
        self.assertEqual(response.status_code, 302)
        loan.refresh_from_db()
        self.assertEqual(loan.status, Loan.Status.ACTIVE)

    def test_un_retour_sans_choix_de_pret(self):
        response = self.client.post(reverse("staff:return"), {"outcome": "GOOD"})
        self.assertContains(self.client.get(response["Location"]), "Choisissez le prêt")

    def test_on_ne_clot_pas_le_pret_d_une_autre_structure(self):
        other = checkout_copy(
            organization=self.org_b, book_copy_id=self.copy_b.pk,
            member_id=self.member_b.pk, issued_by=self.staff_b,
        )
        self.client.post(reverse("staff:return"), {"loan_id": other.pk, "outcome": "GOOD"})
        other.refresh_from_db()
        self.assertEqual(other.status, Loan.Status.ACTIVE)
        self.assertEqual(
            self.client.get(
                reverse("staff:return_lookup"), {"copy_q": "B-001"}
            ).content.decode().count('name="loan_id"'),
            0,
        )

    def test_un_double_envoi_ne_clot_qu_une_fois(self):
        loan = self.checkout()
        data = {"loan_id": loan.pk, "outcome": "GOOD"}
        self.client.post(reverse("staff:return"), data)
        second = self.client.post(reverse("staff:return"), data)
        self.assertContains(self.client.get(second["Location"]), "déjà clôturé")
        self.assertEqual(AuditLog.objects.filter(action="LOAN_RETURNED").count(), 1)


class OverdueTests(StaffTestCase):
    def test_liste_les_retards_de_la_structure_seulement(self):
        loan = self.checkout()
        other = checkout_copy(
            organization=self.org_b, book_copy_id=self.copy_b.pk,
            member_id=self.member_b.pk, issued_by=self.staff_b,
        )
        self.assertContains(self.get("overdue"), "Aucun prêt en retard")

        self.make_overdue(loan, days=4)
        self.make_overdue(other, days=9)
        response = self.get("overdue")
        self.assertContains(response, "A-001")
        self.assertContains(response, "4 jours")
        self.assertNotContains(response, "B-001")


class MembersTests(StaffTestCase):
    def test_la_liste_ne_montre_que_les_membres_de_la_structure(self):
        response = self.get("members")
        self.assertContains(response, "Kofi")
        self.assertNotContains(response, "Mensah")

    def test_recherche_et_filtre_de_statut(self):
        self.assertContains(self.get("members", q="Ama"), "Kofi")
        self.assertContains(self.get("members", q="zzz"), "Aucun membre ne correspond")
        self.assertContains(self.get("members", status="SUSPENDED"), "Aucun membre ne correspond")
        self.assertContains(self.get("members", status="ACCEPTED"), "Kofi")

    def test_inscription_au_comptoir(self):
        response = self.client.post(
            reverse("staff:member_new"),
            {"first_name": "Efua", "last_name": "Dosso", "phone": "90 00 00 00"},
        )
        member = Member.objects.get(last_name="Dosso")
        self.assertRedirects(
            response, reverse("staff:member_detail", args=[member.pk]),
            fetch_redirect_response=False,
        )
        self.assertEqual(member.organization, self.org_a)
        self.assertEqual(member.status, Member.Status.ACCEPTED)
        entry = AuditLog.objects.get(
            action=AuditLog.Action.MEMBER_CREATED, entity_id=str(member.pk)
        )
        self.assertEqual(entry.user, self.staff)

    def test_inscription_invalide(self):
        response = self.client.post(reverse("staff:member_new"), {"first_name": "", "last_name": ""})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Member.objects.filter(organization=self.org_a).count(), 1)

    def test_numero_deja_pris(self):
        response = self.client.post(
            reverse("staff:member_new"),
            {"first_name": "Efua", "last_name": "Dosso", "member_number": "MEM-0001"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Member.objects.filter(organization=self.org_a).count(), 1)

    def test_la_fiche_montre_prets_et_historique(self):
        self.checkout()
        response = self.client.get(reverse("staff:member_detail", args=[self.member_a.pk]))
        self.assertContains(response, "A-001")
        self.assertContains(response, "Prêts en cours")

    def test_la_fiche_d_un_membre_d_une_autre_structure_est_introuvable(self):
        response = self.client.get(reverse("staff:member_detail", args=[self.member_b.pk]))
        self.assertEqual(response.status_code, 404)

    def test_suspendre_puis_reactiver(self):
        url = reverse("staff:member_action", args=[self.member_a.pk])
        self.client.post(url, {"action": "suspend"})
        self.member_a.refresh_from_db()
        self.assertEqual(self.member_a.status, Member.Status.SUSPENDED)
        entry = AuditLog.objects.get(action=AuditLog.Action.MEMBER_SUSPENDED)
        self.assertEqual(entry.user, self.staff)

        self.client.post(url, {"action": "reactivate"})
        self.member_a.refresh_from_db()
        self.assertEqual(self.member_a.status, Member.Status.ACCEPTED)

    def test_les_actions_exigent_post_et_une_action_connue(self):
        url = reverse("staff:member_action", args=[self.member_a.pk])
        self.assertEqual(self.client.get(url).status_code, 405)
        response = self.client.post(url, {"action": "supprimer"})
        self.assertContains(self.client.get(response["Location"]), "Action inconnue")

    def test_on_n_agit_pas_sur_un_membre_d_une_autre_structure(self):
        url = reverse("staff:member_action", args=[self.member_b.pk])
        self.assertEqual(self.client.post(url, {"action": "suspend"}).status_code, 404)
        self.member_b.refresh_from_db()
        self.assertEqual(self.member_b.status, Member.Status.ACCEPTED)

    def test_code_de_rattachement(self):
        response = self.client.post(
            reverse("staff:member_action", args=[self.member_a.pk]),
            {"action": "link_code"},
        )
        code = LinkCode.objects.get(member=self.member_a)
        self.assertContains(self.client.get(response["Location"]), code.code)


class CatalogAndHistoryTests(StaffTestCase):
    def test_le_catalogue_ne_montre_que_les_livres_de_la_structure(self):
        response = self.get("catalog")
        self.assertContains(response, "Livre A")
        self.assertNotContains(response, "Livre B")
        self.assertContains(self.get("catalog", q="zzz"), "Aucun livre ne correspond")

    def test_la_fiche_livre_montre_les_exemplaires_et_l_emprunteur(self):
        self.checkout()
        response = self.client.get(reverse("staff:book_detail", args=[self.book_a.pk]))
        self.assertContains(response, "A-001")
        self.assertContains(response, "Ama")

    def test_la_fiche_d_un_livre_d_une_autre_structure_est_introuvable(self):
        response = self.client.get(reverse("staff:book_detail", args=[self.book_b.pk]))
        self.assertEqual(response.status_code, 404)

    def test_l_historique_filtre_par_statut_et_reste_dans_la_structure(self):
        loan = self.checkout()
        checkout_copy(
            organization=self.org_b, book_copy_id=self.copy_b.pk,
            member_id=self.member_b.pk, issued_by=self.staff_b,
        )
        response = self.get("history")
        self.assertContains(response, "A-001")
        self.assertNotContains(response, "B-001")

        self.assertContains(self.get("history", status="RETURNED"), "Aucun prêt ne correspond")
        self.client.post(reverse("staff:return"), {"loan_id": loan.pk, "outcome": "GOOD"})
        self.assertContains(self.get("history", status="RETURNED"), "A-001")
        self.assertContains(self.get("history", q="Kofi"), "A-001")
