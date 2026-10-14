"""Interface Staff coworking, livraison 3 : planning, réservations, historique des sessions, corrections."""
from datetime import timedelta

from django.contrib.messages import get_messages
from django.utils import timezone

from apps.api.test_staff_read import make_org, make_user
from apps.audit.models import AuditLog
from apps.coworking import services
from apps.coworking.models import AccessSession, CoworkingProfile, Customer, Payment, Reservation, Resource, Zone
from apps.organizations.models import OrganizationStaff
from apps.staff.test_coworking_desk import DeskCase


class BookingCase(DeskCase):
    """Même décor que le comptoir (poste D1 et D2 à 500 XOF / heure, client Ama) + outils de réservation."""

    def slot(self, days=1, hour=10, minutes=60):
        """(début, fin) aware, `days` jours plus tard à `hour` h locale."""
        start = timezone.localtime().replace(hour=hour, minute=0, second=0, microsecond=0) + timedelta(days=days)
        return start, start + timedelta(minutes=minutes)

    def book(self, resource=None, customer=None, days=1, hour=10, minutes=60):
        start, end = self.slot(days, hour, minutes)
        return services.create_reservation(
            organization=self.org, customer_id=(customer or self.ama).pk,
            resource_id=(resource or self.desk).pk, starts_at=start, ends_at=end, created_by=self.staff,
        )

    def soon(self, starts_in=30, minutes=60, resource=None, customer=None):
        start = timezone.now() + timedelta(minutes=starts_in)
        return services.create_reservation(
            organization=self.org, customer_id=(customer or self.ama).pk,
            resource_id=(resource or self.desk).pk, starts_at=start,
            ends_at=start + timedelta(minutes=minutes), created_by=self.staff,
        )

    def move(self, reservation, start_minutes_ago, minutes=60):
        """Place une réservation dans le passé (pour tester absence et délais)."""
        start = timezone.now() - timedelta(minutes=start_minutes_ago)
        Reservation.objects.filter(pk=reservation.pk).update(starts_at=start, ends_at=start + timedelta(minutes=minutes))
        reservation.refresh_from_db()
        return reservation

    def form_data(self, start, end, **extra):
        local = timezone.localtime(start)
        data = {
            "customer_id": self.ama.pk,
            "resource_id": self.desk.pk,
            "date": local.strftime("%Y-%m-%d"),
            "start": local.strftime("%H:%M"),
            "end": timezone.localtime(end).strftime("%H:%M"),
            "resource_type": "",
            "notes": "",
        }
        data.update(extra)
        return data

    def texts(self, response):
        return [str(message) for message in get_messages(response.wsgi_request)]


class PlanningTests(BookingCase):
    def test_planning_liste_les_ressources_et_les_reservations_du_jour(self):
        reservation = self.book()
        day = timezone.localtime(reservation.starts_at).strftime("%Y-%m-%d")
        response = self.client.get(self.url("cw_planning"), {"date": day})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["resource"].code for row in response.context["rows"]], ["D1", "D2"])
        items = response.context["rows"][0]["items"]
        self.assertEqual([(item["kind"], item["pk"]) for item in items], [("reservation", reservation.pk)])
        self.assertContains(response, "Ama Kodjo")
        self.assertEqual(response.context["rows"][1]["items"], [])

    def test_date_invalide_retombe_sur_aujourd_hui(self):
        response = self.client.get(self.url("cw_planning"), {"date": "pas-une-date"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["day"], timezone.localdate())

    def test_reservation_annulee_absente_du_planning(self):
        reservation = self.book()
        services.cancel_reservation(
            organization=self.org, reservation_id=reservation.pk, performed_by=self.staff, reason="Test"
        )
        day = timezone.localtime(reservation.starts_at).strftime("%Y-%m-%d")
        response = self.client.get(self.url("cw_planning"), {"date": day})
        self.assertEqual(response.context["rows"][0]["items"], [])

    def test_reservation_honoree_remplacee_par_la_presence(self):
        reservation = self.soon(starts_in=5)
        services.check_in(
            organization=self.org, performed_by=self.staff, reservation_id=reservation.pk
        )
        response = self.client.get(self.url("cw_planning"))
        items = response.context["rows"][0]["items"]
        self.assertEqual([item["kind"] for item in items], ["session"])
        self.assertTrue(items[0]["open"])

    def test_filtre_par_type_et_zone(self):
        room = Resource.objects.create(
            organization=self.org, zone=self.zone, code="S1", name="Salle", resource_type=Resource.Type.ROOM
        )
        codes = lambda **params: [r["resource"].code for r in self.client.get(self.url("cw_planning"), params).context["rows"]]
        self.assertEqual(codes(type="ROOM"), ["S1"])
        self.assertEqual(codes(type="DESK"), ["D1", "D2"])
        self.assertEqual(codes(zone=room.zone_id), ["D1", "D2", "S1"])
        self.assertEqual(codes(zone=self.other_zone.pk), [])

    def test_ressource_inactive_et_autre_structure_absentes(self):
        Resource.objects.filter(pk=self.desk2.pk).update(active=False)
        Resource.objects.create(organization=self.other, zone=self.other_zone, code="X1", name="Autre poste")
        response = self.client.get(self.url("cw_planning"))
        self.assertEqual([r["resource"].code for r in response.context["rows"]], ["D1"])

    def test_horaires_d_ouverture_dessinent_la_fenetre(self):
        profile = services.get_profile(self.org)
        today = timezone.localdate()
        keys = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        profile.opening_hours = {keys[today.weekday()]: ["08:00", "18:00"]}
        profile.save()
        response = self.client.get(self.url("cw_planning"))
        self.assertEqual([h["label"] for h in response.context["hours"]], [f"{h:02d}h" for h in range(8, 18)])
        self.assertFalse(response.context["closed"])
        tomorrow = (today + timedelta(days=1)).isoformat()
        self.assertTrue(self.client.get(self.url("cw_planning"), {"date": tomorrow}).context["closed"])

    def test_signale_les_absences_a_decider(self):
        self.move(self.soon(), start_minutes_ago=120)
        response = self.client.get(self.url("cw_planning"))
        self.assertEqual(response.context["to_decide"], 1)
        self.assertContains(response, "state=TO_DECIDE")


class ReservationListTests(BookingCase):
    def test_liste_filtres_et_isolation(self):
        first = self.book(days=1)
        second = self.book(resource=self.desk2, days=2)
        services.cancel_reservation(
            organization=self.org, reservation_id=second.pk, performed_by=self.staff, reason="Client indisponible"
        )
        other = services.create_reservation(
            organization=self.other, customer_id=self.other_customer.pk,
            resource_id=Resource.objects.create(organization=self.other, zone=self.other_zone, code="X1", name="X").pk,
            starts_at=self.slot()[0], ends_at=self.slot()[1], created_by=self.other_staff,
        )
        ids = lambda **params: [r.pk for r in self.client.get(self.url("cw_reservations"), params).context["page"].object_list]
        self.assertEqual(sorted(ids()), sorted([first.pk, second.pk]))
        self.assertNotIn(other.pk, ids())
        self.assertEqual(ids(state="CANCELLED"), [second.pk])
        self.assertEqual(ids(state="CONFIRMED"), [first.pk])
        self.assertEqual(ids(q="kodjo"), sorted([first.pk, second.pk], reverse=True))
        self.assertEqual(ids(q="D2"), [second.pk])
        self.assertEqual(ids(q="Zoé"), [])
        day = timezone.localtime(first.starts_at).strftime("%Y-%m-%d")
        self.assertEqual(ids(**{"from": day, "to": day}), [first.pk])

    def test_absences_a_decider(self):
        waiting = self.move(self.soon(), start_minutes_ago=120)
        self.book(resource=self.desk2)
        response = self.client.get(self.url("cw_reservations"), {"state": "TO_DECIDE"})
        self.assertEqual([r.pk for r in response.context["page"].object_list], [waiting.pk])
        self.assertContains(response, "Absence à décider")

    def test_reservation_avec_arrivee_n_est_pas_une_absence_a_decider(self):
        reservation = self.soon(starts_in=5)
        services.check_in(organization=self.org, performed_by=self.staff, reservation_id=reservation.pk)
        self.move(reservation, start_minutes_ago=120)
        response = self.client.get(self.url("cw_reservations"), {"state": "TO_DECIDE"})
        self.assertEqual(list(response.context["page"].object_list), [])


class NewReservationTests(BookingCase):
    def test_page_et_prefill_depuis_le_planning(self):
        response = self.client.get(self.url("cw_reservation_new"), {"resource": self.desk.pk, "customer": self.ama.pk})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["customer"], self.ama)
        self.assertContains(response, 'type="hidden" name="customer_id"')

    def test_prefill_ignore_un_client_d_une_autre_structure(self):
        response = self.client.get(self.url("cw_reservation_new"), {"customer": self.other_customer.pk})
        self.assertIsNone(response.context["customer"])
        self.assertNotContains(response, "Zoé")

    def test_ressources_libres_et_montant_estime(self):
        start, end = self.slot(minutes=90)
        self.book(resource=self.desk)
        response = self.client.get(
            self.url("cw_reservation_availability"),
            {"date": timezone.localtime(start).strftime("%Y-%m-%d"), "start": "10:30", "end": "12:00"},
        )
        rows = response.context["panel"]["rows"]
        self.assertEqual([row["resource"].code for row in rows], ["D2"])
        self.assertEqual(rows[0]["amount"], 1000)  # 90 min = 2 heures entamées x 500
        self.assertContains(response, "1\xa0000 XOF")

    def test_availability_periode_incomplete_ou_invalide(self):
        response = self.client.get(self.url("cw_reservation_availability"), {"date": "2030-01-01", "start": "10:00"})
        self.assertContains(response, "Choisissez le jour")
        day = self.slot()[0].strftime("%Y-%m-%d")
        response = self.client.get(self.url("cw_reservation_availability"), {"date": day, "start": "11:00", "end": "10:00"})
        self.assertIn("La fin doit être postérieure au début.", response.context["panel"]["problem"])

    def test_availability_refuse_comme_le_service(self):
        day = self.slot()[0].strftime("%Y-%m-%d")
        get = lambda **p: self.client.get(self.url("cw_reservation_availability"), {"date": day, **p}).context["panel"]["problem"]
        self.assertEqual(get(start="10:00", end="10:10"), "Durée minimale : 30 minutes.")
        self.assertEqual(get(start="06:00", end="20:00"), "Durée maximale : 480 minutes.")
        yesterday = (timezone.localdate() - timedelta(days=1)).isoformat()
        past = self.client.get(
            self.url("cw_reservation_availability"), {"date": yesterday, "start": "10:00", "end": "11:00"}
        ).context["panel"]["problem"]
        self.assertIn("Le début est déjà passé", past)
        profile = services.get_profile(self.org)
        profile.opening_hours = {key: ["08:00", "12:00"] for key in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}
        profile.save()
        self.assertEqual(get(start="11:00", end="13:00"), "La période est en dehors des horaires d'ouverture.")

    def test_creation_complete(self):
        start, end = self.slot()
        response = self.client.post(self.url("cw_reservation_new"), self.form_data(start, end, notes="Réunion"))
        reservation = Reservation.objects.get()
        self.assertRedirects(response, self.url("cw_reservation_detail", reservation.pk), fetch_redirect_response=False)
        self.assertEqual((reservation.organization, reservation.customer, reservation.resource), (self.org, self.ama, self.desk))
        self.assertEqual((reservation.starts_at, reservation.ends_at), (start, end))
        self.assertEqual((reservation.quoted_amount, reservation.created_by, reservation.notes), (500, self.staff, "Réunion"))
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.RESERVATION_CREATED, organization=self.org).exists())
        self.assertIn("500 XOF", " ".join(self.texts(response)).replace(" ", " ").replace("\xa0", " "))

    def test_conflit_refuse_avec_le_message_du_service(self):
        self.book()
        start, end = self.slot()
        response = self.client.post(self.url("cw_reservation_new"), self.form_data(start, end))
        self.assertEqual(response.status_code, 200)
        self.assertIn("Cette ressource est déjà réservée sur cette période.", response.context["form"].non_field_errors())
        self.assertEqual(Reservation.objects.count(), 1)

    def test_client_ou_ressource_manquants(self):
        start, end = self.slot()
        data = self.form_data(start, end)
        del data["customer_id"], data["resource_id"]
        response = self.client.post(self.url("cw_reservation_new"), data)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form"].errors["customer_id"], ["Choisissez un client."])
        self.assertEqual(response.context["form"].errors["resource_id"], ["Choisissez une ressource libre."])
        self.assertEqual(Reservation.objects.count(), 0)

    def test_fin_avant_debut_refusee(self):
        start, _ = self.slot()
        response = self.client.post(self.url("cw_reservation_new"), self.form_data(start, start - timedelta(minutes=30)))
        self.assertIn("La fin doit être postérieure au début.", response.context["form"].non_field_errors())

    def test_client_suspendu_refuse(self):
        self.ama.status = Customer.Status.SUSPENDED
        self.ama.save()
        start, end = self.slot()
        response = self.client.post(self.url("cw_reservation_new"), self.form_data(start, end))
        self.assertIn("Ce client est suspendu.", response.context["form"].non_field_errors())

    def test_client_ou_ressource_d_une_autre_structure_introuvables(self):
        foreign = Resource.objects.create(organization=self.other, zone=self.other_zone, code="X1", name="X")
        start, end = self.slot()
        response = self.client.post(self.url("cw_reservation_new"), self.form_data(start, end, customer_id=self.other_customer.pk))
        self.assertIn("Client introuvable.", response.context["form"].non_field_errors())
        response = self.client.post(self.url("cw_reservation_new"), self.form_data(start, end, resource_id=foreign.pk))
        self.assertIn("Ressource introuvable.", response.context["form"].non_field_errors())
        self.assertEqual(Reservation.objects.count(), 0)

    def test_reservations_desactivees(self):
        profile = services.get_profile(self.org)
        profile.booking_enabled = False
        profile.save()
        response = self.client.get(self.url("cw_reservation_new"))
        self.assertRedirects(response, self.url("cw_planning"), fetch_redirect_response=False)
        self.assertIn("désactivées", self.texts(response)[0])
        start, end = self.slot()
        self.client.post(self.url("cw_reservation_new"), self.form_data(start, end))
        self.assertEqual(Reservation.objects.count(), 0)

    def test_recherche_client_masque_les_autres_structures(self):
        response = self.client.get(self.url("cw_reservation_customer_lookup"), {"customer_q": "kodjo"})
        self.assertContains(response, "Ama Kodjo")
        response = self.client.get(self.url("cw_reservation_customer_lookup"), {"customer_q": "Autre"})
        self.assertContains(response, "Aucun client trouvé.")


class ReservationDetailTests(BookingCase):
    def test_fiche_et_isolation(self):
        reservation = self.book()
        response = self.client.get(self.url("cw_reservation_detail", reservation.pk))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ama Kodjo")
        self.assertTrue(response.context["can_cancel"])
        self.assertFalse(response.context["late"])
        foreign = services.create_reservation(
            organization=self.other, customer_id=self.other_customer.pk,
            resource_id=Resource.objects.create(organization=self.other, zone=self.other_zone, code="X1", name="X").pk,
            starts_at=self.slot()[0], ends_at=self.slot()[1], created_by=self.other_staff,
        )
        self.assertEqual(self.client.get(self.url("cw_reservation_detail", foreign.pk)).status_code, 404)
        self.assertEqual(self.client.post(self.url("cw_reservation_cancel", foreign.pk), {"reason": "x"}).status_code, 404)
        self.assertEqual(self.client.post(self.url("cw_reservation_no_show", foreign.pk)).status_code, 404)
        foreign.refresh_from_db()
        self.assertEqual(foreign.status, Reservation.Status.CONFIRMED)

    def test_annulation_avec_motif(self):
        reservation = self.book()
        response = self.client.post(self.url("cw_reservation_cancel", reservation.pk), {"reason": "  Client malade "})
        self.assertRedirects(response, self.url("cw_reservation_detail", reservation.pk), fetch_redirect_response=False)
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.CANCELLED)
        self.assertEqual((reservation.cancel_reason, reservation.cancelled_by), ("Client malade", self.staff))
        log = AuditLog.objects.filter(action=AuditLog.Action.RESERVATION_CANCELLED).get()
        self.assertEqual((log.metadata["reason"], log.metadata["late"]), ("Client malade", False))
        page = self.client.get(self.url("cw_reservation_detail", reservation.pk))
        self.assertContains(page, "RÉSERVATION ANNULÉE")
        self.assertFalse(page.context["can_cancel"])
        # La ressource est de nouveau libre : rien ne s'est supprimé, tout s'est annulé.
        self.assertEqual(Reservation.objects.count(), 1)
        self.book()

    def test_annulation_sans_motif_refusee(self):
        reservation = self.book()
        response = self.client.post(self.url("cw_reservation_cancel", reservation.pk), {"reason": "   "})
        self.assertEqual(self.texts(response), ["Le motif de l'annulation est obligatoire."])
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.CONFIRMED)

    def test_annulation_tardive_signalee_et_journalisee(self):
        reservation = self.soon(starts_in=30)
        page = self.client.get(self.url("cw_reservation_detail", reservation.pk))
        self.assertTrue(page.context["late"])
        self.assertContains(page, "tardive")
        response = self.client.post(self.url("cw_reservation_cancel", reservation.pk), {"reason": "Imprévu"})
        self.assertEqual(len(self.texts(response)), 2)
        self.assertIn("Annulation tardive", self.texts(response)[1])
        log = AuditLog.objects.filter(action=AuditLog.Action.RESERVATION_CANCELLED).get()
        self.assertTrue(log.metadata["late"])

    def test_annulation_deja_annulee_ou_avec_arrivee_refusee_par_le_service(self):
        reservation = self.book()
        url = self.url("cw_reservation_cancel", reservation.pk)
        self.client.post(url, {"reason": "Une fois"})
        response = self.client.post(url, {"reason": "Deux fois"})
        self.assertEqual(self.texts(response)[-1], "Seule une réservation confirmée peut être annulée.")
        honored = self.soon(starts_in=5, resource=self.desk2)
        services.check_in(organization=self.org, performed_by=self.staff, reservation_id=honored.pk)
        response = self.client.post(self.url("cw_reservation_cancel", honored.pk), {"reason": "Trop tard"})
        self.assertEqual(self.texts(response)[-1], "Une arrivée est déjà enregistrée sur cette réservation.")

    def test_actions_en_get_refusees(self):
        reservation = self.book()
        self.assertEqual(self.client.get(self.url("cw_reservation_cancel", reservation.pk)).status_code, 405)
        self.assertEqual(self.client.get(self.url("cw_reservation_no_show", reservation.pk)).status_code, 405)

    def test_absence_avant_la_fin_du_delai_de_grace_refusee(self):
        reservation = self.move(self.soon(), start_minutes_ago=10)
        page = self.client.get(self.url("cw_reservation_detail", reservation.pk))
        self.assertFalse(page.context["can_no_show"])
        self.assertTrue(page.context["waiting_grace"])
        response = self.client.post(self.url("cw_reservation_no_show", reservation.pk))
        self.assertIn("délai de grâce", self.texts(response)[0])
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.CONFIRMED)

    def test_absence_apres_le_delai_de_grace(self):
        reservation = self.move(self.soon(), start_minutes_ago=45)
        page = self.client.get(self.url("cw_reservation_detail", reservation.pk))
        self.assertTrue(page.context["can_no_show"])
        response = self.client.post(self.url("cw_reservation_no_show", reservation.pk))
        self.assertRedirects(response, self.url("cw_reservation_detail", reservation.pk), fetch_redirect_response=False)
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.NO_SHOW)
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.RESERVATION_NO_SHOW).exists())
        self.assertContains(self.client.get(self.url("cw_reservation_detail", reservation.pk)), "Absence")

    def test_arrivee_proposee_dans_la_fenetre(self):
        reservation = self.soon(starts_in=5)
        page = self.client.get(self.url("cw_reservation_detail", reservation.pk))
        self.assertTrue(page.context["can_check_in"])
        self.assertContains(page, "Enregistrer l'arrivée")
        far = self.book(resource=self.desk2, days=3)
        self.assertFalse(self.client.get(self.url("cw_reservation_detail", far.pk)).context["can_check_in"])


class SessionHistoryTests(BookingCase):
    def test_liste_filtres_et_isolation(self):
        paid = self.closed_session()
        open_one = services.check_in(
            organization=self.org, performed_by=self.staff, resource_id=self.desk2.pk,
            customer_id=services.create_customer(
                organization=self.org, first_name="Yao", last_name="Dosseh", performed_by=self.staff
            ).pk,
        )
        services.record_payment(
            organization=self.org, customer_id=self.ama.pk, amount=paid.amount,
            performed_by=self.staff, session_id=paid.pk,
        )
        foreign_resource = Resource.objects.create(organization=self.other, zone=self.other_zone, code="X1", name="X")
        foreign = services.check_in(
            organization=self.other, performed_by=self.other_staff,
            resource_id=foreign_resource.pk, customer_id=self.other_customer.pk,
        )
        ids = lambda **params: sorted(s.pk for s in self.client.get(self.url("cw_sessions"), params).context["page"].object_list)
        self.assertEqual(ids(), sorted([paid.pk, open_one.pk]))
        self.assertNotIn(foreign.pk, ids())
        self.assertEqual(ids(state="OPEN"), [open_one.pk])
        self.assertEqual(ids(state="CLOSED"), [paid.pk])
        self.assertEqual(ids(q="dosseh"), [open_one.pk])
        self.assertEqual(ids(q="D1"), [paid.pk])
        self.assertEqual(ids(unpaid="1"), [])
        self.assertEqual(ids(**{"from": (timezone.localdate() + timedelta(days=1)).isoformat()}), [])

    def test_reste_a_payer_et_corrigees(self):
        session = self.closed_session()
        self.assertEqual([s.pk for s in self.client.get(self.url("cw_sessions"), {"unpaid": "1"}).context["page"].object_list], [session.pk])
        self.assertEqual(list(self.client.get(self.url("cw_sessions"), {"corrected": "1"}).context["page"].object_list), [])
        services.correct_session(
            organization=self.org, session_id=session.pk, performed_by=self.staff, reason="Erreur de saisie", amount=800
        )
        response = self.client.get(self.url("cw_sessions"), {"corrected": "1"})
        self.assertEqual([s.pk for s in response.context["page"].object_list], [session.pk])
        self.assertContains(response, "Corrigée")


class SessionCorrectionTests(BookingCase):
    def test_motif_obligatoire(self):
        session = self.closed_session()
        response = self.client.post(self.url("cw_session_correct", session.pk), {"amount": 700, "reason": "  "})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form"].errors["reason"], ["Le motif de la correction est obligatoire."])
        session.refresh_from_db()
        self.assertEqual(session.amount, 1000)
        self.assertFalse(AuditLog.objects.filter(action=AuditLog.Action.SESSION_CORRECTED).exists())

    def test_rien_a_corriger(self):
        session = self.closed_session()
        page = self.client.get(self.url("cw_session_correct", session.pk))
        initial = page.context["form"].initial
        response = self.client.post(
            self.url("cw_session_correct", session.pk),
            {
                "checked_in_at": timezone.localtime(initial["checked_in_at"]).strftime("%Y-%m-%dT%H:%M"),
                "checked_out_at": timezone.localtime(initial["checked_out_at"]).strftime("%Y-%m-%dT%H:%M"),
                "amount": session.amount,
                "reason": "Rien",
            },
        )
        self.assertIn("Rien à corriger.", response.context["form"].non_field_errors())

    def test_correction_du_montant_journalisee(self):
        session = self.closed_session()
        response = self.client.post(self.url("cw_session_correct", session.pk), {"amount": 700, "reason": "Remise accordée"})
        self.assertRedirects(response, self.url("cw_session_detail", session.pk), fetch_redirect_response=False)
        session.refresh_from_db()
        self.assertEqual(session.amount, 700)
        log = AuditLog.objects.filter(action=AuditLog.Action.SESSION_CORRECTED).get()
        self.assertEqual(log.metadata["reason"], "Remise accordée")
        self.assertEqual((log.metadata["before"]["amount"], log.metadata["after"]["amount"]), (1000, 700))
        self.assertEqual(log.user, self.staff)

    def test_correction_de_l_heure_recalcule_le_montant(self):
        session = self.closed_session(minutes=110)  # 2 h entamées = 1000
        new_in = timezone.localtime(session.checked_out_at - timedelta(minutes=50))
        response = self.client.post(
            self.url("cw_session_correct", session.pk),
            {"checked_in_at": new_in.strftime("%Y-%m-%dT%H:%M"), "amount": session.amount, "reason": "Arrivée mal saisie"},
        )
        self.assertRedirects(response, self.url("cw_session_detail", session.pk), fetch_redirect_response=False)
        session.refresh_from_db()
        self.assertEqual(session.amount, 500)
        self.assertEqual(timezone.localtime(session.checked_in_at).strftime("%H:%M"), new_in.strftime("%H:%M"))

    def test_heure_future_et_sortie_avant_arrivee_refusees(self):
        session = self.closed_session()
        future = (timezone.localtime() + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M")
        response = self.client.post(
            self.url("cw_session_correct", session.pk), {"checked_out_at": future, "reason": "Test"}
        )
        self.assertIn("Une heure corrigée ne peut pas être dans le futur.", response.context["form"].non_field_errors())
        early = (timezone.localtime(session.checked_in_at) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")
        response = self.client.post(
            self.url("cw_session_correct", session.pk), {"checked_out_at": early, "reason": "Test"}
        )
        self.assertIn("La sortie doit suivre l'arrivée.", response.context["form"].non_field_errors())

    def test_session_en_cours_ne_propose_que_l_arrivee(self):
        session = services.check_in(
            organization=self.org, performed_by=self.staff, resource_id=self.desk.pk, customer_id=self.ama.pk
        )
        page = self.client.get(self.url("cw_session_correct", session.pk))
        self.assertEqual(list(page.context["form"].fields), ["checked_in_at", "reason"])
        new_in = (timezone.localtime() - timedelta(minutes=40)).strftime("%Y-%m-%dT%H:%M")
        self.client.post(self.url("cw_session_correct", session.pk), {"checked_in_at": new_in, "reason": "Arrivée oubliée"})
        session.refresh_from_db()
        self.assertEqual(session.status, AccessSession.Status.OPEN)
        self.assertEqual(timezone.localtime(session.checked_in_at).strftime("%H:%M"), new_in[-5:])

    def test_historique_des_corrections_sur_la_fiche_session(self):
        session = self.closed_session()
        self.client.post(self.url("cw_session_correct", session.pk), {"amount": 700, "reason": "Remise accordée"})
        response = self.client.get(self.url("cw_session_detail", session.pk))
        corrections = response.context["corrections"]
        self.assertEqual(len(corrections), 1)
        self.assertEqual((corrections[0]["reason"], corrections[0]["before"]["amount"], corrections[0]["after"]["amount"]), ("Remise accordée", 1000, 700))
        self.assertContains(response, "Remise accordée")
        self.assertContains(response, "Corrections")

    def test_session_d_une_autre_structure_introuvable(self):
        foreign_resource = Resource.objects.create(organization=self.other, zone=self.other_zone, code="X1", name="X")
        foreign = services.check_in(
            organization=self.other, performed_by=self.other_staff,
            resource_id=foreign_resource.pk, customer_id=self.other_customer.pk,
        )
        self.assertEqual(self.client.get(self.url("cw_session_correct", foreign.pk)).status_code, 404)
        self.assertEqual(self.client.post(self.url("cw_session_correct", foreign.pk), {"amount": 1, "reason": "x"}).status_code, 404)


class AccessTests(BookingCase):
    def test_module_coworking_obligatoire(self):
        library_only = make_org("Biblio seule")
        user = make_user("lib3")
        OrganizationStaff.objects.create(user=user, organization=library_only, active=True)
        self.client.force_login(user)
        for name in ("cw_planning", "cw_reservations", "cw_reservation_new", "cw_sessions", "cw_reservation_availability"):
            self.assertEqual(self.client.get(self.url(name)).status_code, 403, name)
        self.assertEqual(self.client.post(self.url("cw_reservation_cancel", 1), {"reason": "x"}).status_code, 403)

    def test_connexion_obligatoire(self):
        self.client.logout()
        for name in ("cw_planning", "cw_reservations", "cw_sessions"):
            self.assertEqual(self.client.get(self.url(name)).status_code, 302, name)

    def test_navigation_expose_les_nouvelles_pages(self):
        response = self.client.get(self.url("cw_planning"))
        for name in ("cw_planning", "cw_reservations", "cw_sessions"):
            self.assertContains(response, self.url(name))
