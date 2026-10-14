"""Interface Staff coworking, livraison 2 : clients, accueil (arrivée/sortie), paiements."""
from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.api.test_staff_read import make_org, make_user
from apps.audit.models import AuditLog
from apps.coworking import services
from apps.coworking.models import (
    AccessSession,
    CoworkingProfile,
    Customer,
    Payment,
    Rate,
    Resource,
    Zone,
)
from apps.organizations.models import OrganizationStaff
from apps.staff import queries_coworking


class DeskCase(TestCase):
    def setUp(self):
        self.org = make_org("Cowork A", library_module=False)
        self.org.coworking_module = True
        self.org.save()
        self.other = make_org("Cowork B", library_module=False)
        self.other.coworking_module = True
        self.other.save()
        self.staff = make_user("desk-staff")
        OrganizationStaff.objects.create(user=self.staff, organization=self.org, active=True)
        self.client.force_login(self.staff)

        self.zone = Zone.objects.create(organization=self.org, name="Open space")
        self.desk = Resource.objects.create(
            organization=self.org, zone=self.zone, code="D1", name="Bureau 1"
        )
        self.desk2 = Resource.objects.create(
            organization=self.org, zone=self.zone, code="D2", name="Bureau 2"
        )
        Rate.objects.create(
            organization=self.org, resource_type=Resource.Type.DESK, unit=Rate.Unit.HOUR, amount=500
        )
        self.ama = services.create_customer(
            organization=self.org, first_name="Ama", last_name="Kodjo",
            phone="90112233", performed_by=self.staff,
        )
        self.other_zone = Zone.objects.create(organization=self.other, name="Autre")
        self.other_staff = make_user("other-staff")
        OrganizationStaff.objects.create(user=self.other_staff, organization=self.other, active=True)
        self.other_customer = services.create_customer(
            organization=self.other, first_name="Zoé", last_name="Autre",
            performed_by=self.other_staff,
        )

    def url(self, name, *args):
        return reverse(f"staff:{name}", args=args)

    def closed_session(self, customer=None, minutes=110, resource=None):
        """Session terminée : 110 min d'utilisation = 2 heures entamées = 1000 XOF (50 min = 500 XOF)."""
        session = services.check_in(
            organization=self.org, performed_by=self.staff,
            resource_id=(resource or self.desk).pk, customer_id=(customer or self.ama).pk,
        )
        AccessSession.objects.filter(pk=session.pk).update(
            checked_in_at=timezone.now() - timedelta(minutes=minutes)
        )
        return services.check_out(organization=self.org, performed_by=self.staff, session_id=session.pk)


class CustomerTests(DeskCase):
    def test_creation_numerotee_et_auditee(self):
        response = self.client.post(
            self.url("cw_customer_new"),
            {"first_name": "  Kofi ", "last_name": "Mensah", "phone": " 91000000 ", "email": ""},
        )
        customer = Customer.objects.get(first_name="Kofi")
        self.assertRedirects(response, self.url("cw_customer_detail", customer.pk), fetch_redirect_response=False)
        self.assertEqual((customer.organization, customer.phone), (self.org, "91000000"))
        self.assertTrue(customer.customer_number)
        self.assertTrue(
            AuditLog.objects.filter(action=AuditLog.Action.CUSTOMER_CREATED, organization=self.org).exists()
        )

    def test_creation_sans_nom_refusee(self):
        response = self.client.post(self.url("cw_customer_new"), {"first_name": "", "last_name": ""})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Customer.objects.filter(organization=self.org).count(), 1)

    def test_liste_et_recherche(self):
        services.create_customer(
            organization=self.org, first_name="Yao", last_name="Dosseh", performed_by=self.staff
        )
        names = lambda response: sorted(c.first_name for c in response.context["page"].object_list)
        self.assertEqual(names(self.client.get(self.url("cw_customers"))), ["Ama", "Yao"])
        self.assertEqual(names(self.client.get(self.url("cw_customers"), {"q": "9011"})), ["Ama"])
        self.assertEqual(names(self.client.get(self.url("cw_customers"), {"q": "dosseh"})), ["Yao"])
        self.assertNotIn("Zoé", names(self.client.get(self.url("cw_customers"), {"q": "Zoé"})))

    def test_fiche_affiche_le_reste_a_payer(self):
        self.closed_session()
        response = self.client.get(self.url("cw_customer_detail", self.ama.pk))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["unpaid_total"], 1000)
        self.assertContains(response, "Reste à payer")

    def test_fiche_d_un_client_d_une_autre_structure_introuvable(self):
        self.assertEqual(self.client.get(self.url("cw_customer_detail", self.other_customer.pk)).status_code, 404)
        self.assertEqual(self.client.get(self.url("cw_customer_edit", self.other_customer.pk)).status_code, 404)
        response = self.client.post(
            self.url("cw_customer_action", self.other_customer.pk), {"action": "suspend"}
        )
        self.assertEqual(response.status_code, 404)

    def test_modification_journalisee(self):
        response = self.client.post(
            self.url("cw_customer_edit", self.ama.pk),
            {"first_name": "Ama", "last_name": "Kodjo", "phone": "90999999", "email": ""},
        )
        self.assertRedirects(response, self.url("cw_customer_detail", self.ama.pk), fetch_redirect_response=False)
        self.ama.refresh_from_db()
        self.assertEqual(self.ama.phone, "90999999")
        log = AuditLog.objects.filter(action=AuditLog.Action.CUSTOMER_UPDATED).latest("pk")
        self.assertEqual(log.metadata["changes"]["phone"]["to"], "90999999")

    def test_suspension_et_reactivation(self):
        self.client.post(self.url("cw_customer_action", self.ama.pk), {"action": "suspend"})
        self.ama.refresh_from_db()
        self.assertEqual(self.ama.status, Customer.Status.SUSPENDED)
        log = AuditLog.objects.filter(action=AuditLog.Action.CUSTOMER_UPDATED).latest("pk")
        self.assertEqual(log.metadata["changes"]["status"]["to"], "SUSPENDED")
        self.client.post(self.url("cw_customer_action", self.ama.pk), {"action": "reactivate"})
        self.ama.refresh_from_db()
        self.assertEqual(self.ama.status, Customer.Status.ACTIVE)

    def test_action_en_get_refusee(self):
        self.assertEqual(self.client.get(self.url("cw_customer_action", self.ama.pk)).status_code, 405)


class ReceptionTests(DeskCase):
    def test_page_accueil(self):
        response = self.client.get(self.url("cw_reception"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(sorted(r.code for r in response.context["walk_in_resources"]), ["D1", "D2"])
        self.assertContains(response, "Personne n'est en session")

    def test_accueil_sans_reservation_desactive(self):
        profile = services.get_profile(self.org)
        profile.walk_in_enabled = False
        profile.save()
        response = self.client.get(self.url("cw_reception"))
        self.assertContains(response, "désactivé")
        self.assertNotContains(response, 'id="checkin-form"')

    def test_recherche_client_trop_courte(self):
        response = self.client.get(self.url("cw_customer_lookup"), {"customer_q": "a"})
        self.assertContains(response, "au moins 2 caractères")

    def test_recherche_client_trouve_et_masque_les_autres_structures(self):
        response = self.client.get(self.url("cw_customer_lookup"), {"customer_q": "kodjo"})
        self.assertContains(response, "Ama Kodjo")
        self.assertContains(response, f'value="{self.ama.pk}"')
        response = self.client.get(self.url("cw_customer_lookup"), {"customer_q": "Autre"})
        self.assertNotContains(response, "Zoé")
        self.assertContains(response, "Aucun client trouvé.")

    def test_numero_exact_selectionne_d_office(self):
        response = self.client.get(self.url("cw_customer_lookup"), {"customer_q": self.ama.customer_number})
        self.assertContains(response, 'type="hidden" name="customer_id"')
        self.assertContains(response, "Sélectionné")

    def test_client_suspendu_ou_deja_en_session_non_selectionnable(self):
        self.ama.status = Customer.Status.SUSPENDED
        self.ama.save()
        response = self.client.get(self.url("cw_customer_lookup"), {"customer_q": "kodjo"})
        self.assertContains(response, "Client suspendu")
        self.assertContains(response, "disabled")
        self.ama.status = Customer.Status.ACTIVE
        self.ama.save()
        services.check_in(
            organization=self.org, performed_by=self.staff, resource_id=self.desk.pk, customer_id=self.ama.pk
        )
        response = self.client.get(self.url("cw_customer_lookup"), {"customer_q": "kodjo"})
        self.assertContains(response, "Déjà en session")

    def test_arrivee_sans_reservation(self):
        response = self.client.post(
            self.url("cw_check_in"), {"customer_id": self.ama.pk, "resource_id": self.desk.pk}
        )
        self.assertRedirects(response, self.url("cw_reception"), fetch_redirect_response=False)
        session = AccessSession.objects.get()
        self.assertEqual((session.organization, session.customer, session.resource), (self.org, self.ama, self.desk))
        self.assertEqual(session.status, AccessSession.Status.OPEN)
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.SESSION_CHECKED_IN).exists())
        page = self.client.get(self.url("cw_reception"))
        self.assertContains(page, "Ama Kodjo")
        self.assertEqual([r.code for r in page.context["walk_in_resources"]], ["D2"])

    def test_arrivee_sans_choix_refusee(self):
        response = self.client.post(self.url("cw_check_in"), {"customer_id": self.ama.pk}, follow=True)
        self.assertContains(response, "Choisissez un client et une ressource")
        self.assertFalse(AccessSession.objects.exists())

    def test_arrivee_sur_ressource_occupee_refusee(self):
        services.check_in(
            organization=self.org, performed_by=self.staff, resource_id=self.desk.pk, customer_id=self.ama.pk
        )
        other = services.create_customer(
            organization=self.org, first_name="Yao", last_name="Dosseh", performed_by=self.staff
        )
        self.client.post(self.url("cw_check_in"), {"customer_id": other.pk, "resource_id": self.desk.pk})
        self.assertEqual(AccessSession.objects.count(), 1)

    def test_arrivee_client_suspendu_refusee(self):
        self.ama.status = Customer.Status.SUSPENDED
        self.ama.save()
        self.client.post(self.url("cw_check_in"), {"customer_id": self.ama.pk, "resource_id": self.desk.pk})
        self.assertFalse(AccessSession.objects.exists())

    def test_arrivee_avec_ressource_ou_client_d_une_autre_structure_refusee(self):
        self.client.post(self.url("cw_check_in"), {"customer_id": self.other_customer.pk, "resource_id": self.desk.pk})
        foreign = Resource.objects.create(organization=self.other, zone=self.other_zone, code="X1", name="X")
        self.client.post(self.url("cw_check_in"), {"customer_id": self.ama.pk, "resource_id": foreign.pk})
        self.assertFalse(AccessSession.objects.exists())

    def test_arrivee_avec_reservation(self):
        start = timezone.now() + timedelta(minutes=10)
        reservation = services.create_reservation(
            organization=self.org, customer_id=self.ama.pk, resource_id=self.desk.pk,
            starts_at=start, ends_at=start + timedelta(hours=2), created_by=self.staff,
        )
        page = self.client.get(self.url("cw_reception"))
        self.assertEqual([r.pk for r in page.context["to_welcome"]], [reservation.pk])
        response = self.client.post(self.url("cw_check_in"), {"reservation_id": reservation.pk})
        self.assertRedirects(response, self.url("cw_reception"), fetch_redirect_response=False)
        session = AccessSession.objects.get()
        self.assertEqual(session.reservation, reservation)
        self.assertEqual(self.client.get(self.url("cw_reception")).context["to_welcome"].count(), 0)

    def test_reservation_trop_tot_non_proposee(self):
        start = timezone.now() + timedelta(hours=3)
        services.create_reservation(
            organization=self.org, customer_id=self.ama.pk, resource_id=self.desk.pk,
            starts_at=start, ends_at=start + timedelta(hours=1), created_by=self.staff,
        )
        self.assertEqual(self.client.get(self.url("cw_reception")).context["to_welcome"].count(), 0)

    def test_sortie_calcule_le_montant_et_renvoie_vers_la_session(self):
        session = services.check_in(
            organization=self.org, performed_by=self.staff, resource_id=self.desk.pk, customer_id=self.ama.pk
        )
        AccessSession.objects.filter(pk=session.pk).update(checked_in_at=timezone.now() - timedelta(minutes=90))
        page = self.client.get(self.url("cw_reception"))
        self.assertEqual(page.context["open_sessions"][0].estimate, 1000)
        response = self.client.post(self.url("cw_check_out", session.pk))
        self.assertRedirects(response, self.url("cw_session_detail", session.pk), fetch_redirect_response=False)
        session.refresh_from_db()
        self.assertEqual((session.status, session.amount), (AccessSession.Status.CLOSED, 1000))
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.SESSION_CHECKED_OUT).exists())
        detail = self.client.get(self.url("cw_session_detail", session.pk))
        self.assertContains(detail, "Enregistrer le paiement")
        self.assertEqual(detail.context["session"].balance, 1000)

    def test_double_sortie_signalee(self):
        session = self.closed_session()
        response = self.client.post(self.url("cw_check_out", session.pk), follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Payment.objects.count(), 0)

    def test_sortie_d_une_session_d_une_autre_structure_introuvable(self):
        session = AccessSession.objects.create(
            organization=self.other, customer=self.other_customer,
            resource=Resource.objects.create(organization=self.other, zone=self.other_zone, code="X1", name="X"),
            checked_in_by=self.other_staff,
        )
        self.assertEqual(self.client.post(self.url("cw_check_out", session.pk)).status_code, 404)
        self.assertEqual(self.client.get(self.url("cw_session_detail", session.pk)).status_code, 404)

    def test_sortie_en_get_refusee(self):
        session = services.check_in(
            organization=self.org, performed_by=self.staff, resource_id=self.desk.pk, customer_id=self.ama.pk
        )
        self.assertEqual(self.client.get(self.url("cw_check_out", session.pk)).status_code, 405)


class PaymentTests(DeskCase):
    def test_choix_du_client_sans_parametre(self):
        response = self.client.get(self.url("cw_payment_new"))
        self.assertContains(response, "Cherchez le client")

    def test_recherche_client_pour_paiement(self):
        response = self.client.get(self.url("cw_payment_customer_lookup"), {"customer_q": "kodjo"})
        self.assertContains(response, f"?customer={self.ama.pk}")
        self.assertNotContains(self.client.get(self.url("cw_payment_customer_lookup"), {"customer_q": "Autre"}), "Zoé")

    def test_paiement_complet_d_une_session(self):
        session = self.closed_session()
        page = self.client.get(self.url("cw_payment_new"), {"session": session.pk})
        self.assertEqual(page.context["form"].initial["amount"], 1000)
        response = self.client.post(
            f"{self.url('cw_payment_new')}?session={session.pk}",
            {"amount": 1000, "method": "CASH", "session": session.pk, "note": ""},
        )
        payment = Payment.objects.get()
        self.assertRedirects(response, self.url("cw_payment_receipt", payment.pk), fetch_redirect_response=False)
        self.assertEqual((payment.amount, payment.session, payment.customer), (1000, session, self.ama))
        self.assertTrue(payment.receipt_number)
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.PAYMENT_RECORDED).exists())
        self.assertEqual(queries_coworking.unpaid_summary(self.org), {"count": 0, "amount": 0})

    def test_paiement_partiel_puis_solde(self):
        session = self.closed_session()
        url = f"{self.url('cw_payment_new')}?session={session.pk}"
        self.client.post(url, {"amount": 400, "method": "MOBILE_MONEY", "session": session.pk})
        self.assertEqual(queries_coworking.unpaid_summary(self.org), {"count": 1, "amount": 600})
        page = self.client.get(url)
        self.assertEqual(page.context["form"].initial["amount"], 600)
        self.client.post(url, {"amount": 600, "method": "CASH", "session": session.pk})
        self.assertEqual(queries_coworking.unpaid_summary(self.org)["count"], 0)

    def test_paiement_superieur_au_solde_refuse(self):
        session = self.closed_session()
        response = self.client.post(
            f"{self.url('cw_payment_new')}?session={session.pk}",
            {"amount": 1500, "method": "CASH", "session": session.pk},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Payment.objects.exists())

    def test_montant_invalide_refuse(self):
        session = self.closed_session()
        for bad in ("0", "-5", "abc", ""):
            response = self.client.post(
                f"{self.url('cw_payment_new')}?session={session.pk}",
                {"amount": bad, "method": "CASH", "session": session.pk},
            )
            self.assertEqual(response.status_code, 200, bad)
        self.assertFalse(Payment.objects.exists())

    def test_session_en_cours_renvoie_vers_l_accueil(self):
        session = services.check_in(
            organization=self.org, performed_by=self.staff, resource_id=self.desk.pk, customer_id=self.ama.pk
        )
        response = self.client.get(self.url("cw_payment_new"), {"session": session.pk})
        self.assertRedirects(response, self.url("cw_reception"), fetch_redirect_response=False)

    def test_session_deja_soldee_renvoie_vers_la_session(self):
        session = self.closed_session()
        services.record_payment(
            organization=self.org, performed_by=self.staff, customer_id=self.ama.pk,
            amount=1000, session_id=session.pk,
        )
        response = self.client.get(self.url("cw_payment_new"), {"session": session.pk})
        self.assertRedirects(response, self.url("cw_session_detail", session.pk), fetch_redirect_response=False)

    def test_session_d_une_autre_structure_refusee(self):
        foreign = AccessSession.objects.create(
            organization=self.other, customer=self.other_customer,
            resource=Resource.objects.create(organization=self.other, zone=self.other_zone, code="X1", name="X"),
            checked_in_by=self.other_staff, status=AccessSession.Status.CLOSED, amount=500,
            checked_out_at=timezone.now() + timedelta(minutes=1), checked_out_by=self.other_staff,
        )
        self.assertEqual(self.client.get(self.url("cw_payment_new"), {"session": foreign.pk}).status_code, 404)
        self.assertEqual(self.client.get(self.url("cw_payment_new"), {"customer": self.other_customer.pk}).status_code, 404)

    def test_session_d_un_autre_client_refusee_dans_le_formulaire(self):
        other = services.create_customer(
            organization=self.org, first_name="Yao", last_name="Dosseh", performed_by=self.staff
        )
        yao_session = self.closed_session(customer=other, resource=self.desk2)
        self.closed_session()
        response = self.client.post(
            f"{self.url('cw_payment_new')}?customer={self.ama.pk}",
            {"amount": 500, "method": "CASH", "session": yao_session.pk},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Payment.objects.exists())

    def test_acompte_libre_sans_session(self):
        response = self.client.post(
            f"{self.url('cw_payment_new')}?customer={self.ama.pk}",
            {"amount": 2000, "method": "BANK", "session": "", "note": "Abonnement"},
        )
        payment = Payment.objects.get()
        self.assertRedirects(response, self.url("cw_payment_receipt", payment.pk), fetch_redirect_response=False)
        self.assertIsNone(payment.session)

    def test_acompte_sur_reservation_compte_pour_la_session(self):
        start = timezone.now() + timedelta(minutes=5)
        reservation = services.create_reservation(
            organization=self.org, customer_id=self.ama.pk, resource_id=self.desk.pk,
            starts_at=start, ends_at=start + timedelta(hours=2), created_by=self.staff,
        )
        services.record_payment(
            organization=self.org, performed_by=self.staff, customer_id=self.ama.pk,
            amount=1000, reservation_id=reservation.pk,
        )
        session = services.check_in(
            organization=self.org, performed_by=self.staff, reservation_id=reservation.pk
        )
        services.check_out(organization=self.org, performed_by=self.staff, session_id=session.pk)
        detail = self.client.get(self.url("cw_session_detail", session.pk))
        self.assertEqual(detail.context["session"].balance, 0)
        self.assertEqual(queries_coworking.unpaid_summary(self.org)["count"], 0)

    def test_recu_imprimable(self):
        session = self.closed_session()
        self.client.post(
            f"{self.url('cw_payment_new')}?session={session.pk}",
            {"amount": 400, "method": "CASH", "session": session.pk},
        )
        payment = Payment.objects.get()
        response = self.client.get(self.url("cw_payment_receipt", payment.pk))
        self.assertContains(response, payment.receipt_number)
        self.assertContains(response, "Ama Kodjo")
        self.assertEqual(response.context["target_balance"], 600)

    def test_annulation_avec_motif_restaure_le_solde(self):
        session = self.closed_session()
        self.client.post(
            f"{self.url('cw_payment_new')}?session={session.pk}",
            {"amount": 1000, "method": "CASH", "session": session.pk},
        )
        payment = Payment.objects.get()
        response = self.client.post(self.url("cw_payment_void", payment.pk), {"reason": "Erreur de saisie"})
        self.assertRedirects(response, self.url("cw_payment_receipt", payment.pk), fetch_redirect_response=False)
        payment.refresh_from_db()
        self.assertEqual((payment.status, payment.void_reason), (Payment.Status.VOIDED, "Erreur de saisie"))
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.PAYMENT_VOIDED).exists())
        self.assertEqual(queries_coworking.unpaid_summary(self.org), {"count": 1, "amount": 1000})
        self.assertContains(self.client.get(self.url("cw_payment_receipt", payment.pk)), "PAIEMENT ANNULÉ")

    def test_annulation_sans_motif_refusee(self):
        session = self.closed_session()
        self.client.post(
            f"{self.url('cw_payment_new')}?session={session.pk}",
            {"amount": 1000, "method": "CASH", "session": session.pk},
        )
        payment = Payment.objects.get()
        self.client.post(self.url("cw_payment_void", payment.pk), {"reason": "   "})
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.RECORDED)

    def test_paiement_d_une_autre_structure_introuvable(self):
        foreign = Payment.objects.create(
            organization=self.other, customer=self.other_customer, amount=100,
            receipt_number="X-1", recorded_by=self.other_staff,
        )
        self.assertEqual(self.client.get(self.url("cw_payment_receipt", foreign.pk)).status_code, 404)
        self.assertEqual(self.client.post(self.url("cw_payment_void", foreign.pk), {"reason": "x"}).status_code, 404)
        self.assertNotContains(self.client.get(self.url("cw_payments")), "X-1")

    def test_liste_et_filtres(self):
        first = self.closed_session()
        second = self.closed_session(resource=self.desk2)
        self.client.post(
            f"{self.url('cw_payment_new')}?session={first.pk}",
            {"amount": 1000, "method": "CASH", "session": first.pk},
        )
        self.client.post(
            f"{self.url('cw_payment_new')}?session={second.pk}",
            {"amount": 500, "method": "MOBILE_MONEY", "session": second.pk},
        )
        listed = lambda **params: len(self.client.get(self.url("cw_payments"), params).context["page"].object_list)
        self.assertEqual(listed(), 2)
        self.assertEqual(listed(method="MOBILE_MONEY"), 1)
        self.assertEqual(listed(state="VOIDED"), 0)
        self.assertEqual(listed(q="kodjo"), 2)
        self.assertEqual(listed(q="inconnu"), 0)

    def test_page_a_encaisser_et_tableau_de_bord(self):
        self.closed_session()
        self.closed_session(resource=self.desk2, minutes=50)
        page = self.client.get(self.url("cw_unpaid"))
        self.assertEqual(page.context["summary"], {"count": 2, "amount": 1500})
        self.assertContains(page, "Encaisser")
        dashboard = self.client.get(self.url("dashboard"))
        self.assertEqual(dashboard.context["cw_unpaid"], {"count": 2, "amount": 1500})
        self.assertContains(dashboard, "À encaisser")

    def test_page_a_encaisser_vide(self):
        self.assertContains(self.client.get(self.url("cw_unpaid")), "Tout est encaissé")

    def test_acces_refuse_sans_module(self):
        library_only = make_org("Biblio seule")
        user = make_user("lib2")
        OrganizationStaff.objects.create(user=user, organization=library_only, active=True)
        self.client.force_login(user)
        for name in ("cw_reception", "cw_customers", "cw_payments", "cw_unpaid", "cw_payment_new", "cw_customer_new"):
            self.assertEqual(self.client.get(self.url(name)).status_code, 403, name)
        self.assertEqual(self.client.post(self.url("cw_check_in"), {}).status_code, 403)
