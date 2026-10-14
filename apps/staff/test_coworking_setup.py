"""Interface Staff coworking, livraison 1 : accès, menu, tableau de bord, configuration."""
from django.test import TestCase
from django.urls import reverse

from apps.api.test_staff_read import make_org, make_user
from apps.audit.models import AuditLog
from apps.coworking.models import CoworkingProfile, Rate, Resource, Zone
from apps.organizations.models import OrganizationStaff


class CoworkingStaffCase(TestCase):
    def setUp(self):
        self.org = make_org("Cowork A", library_module=False)
        self.org.coworking_module = True
        self.org.save()
        self.other = make_org("Cowork B", library_module=False)
        self.other.coworking_module = True
        self.other.save()
        self.staff = make_user("cw-staff")
        OrganizationStaff.objects.create(user=self.staff, organization=self.org, active=True)
        self.client.force_login(self.staff)

        self.zone = Zone.objects.create(organization=self.org, name="Open space")
        self.other_zone = Zone.objects.create(organization=self.other, name="Autre zone")

    def url(self, name, *args):
        return reverse(f"staff:{name}", args=args)

    def make_resource(self, code="D1", zone=None, **kwargs):
        return Resource.objects.create(
            organization=self.org, zone=zone or self.zone, code=code, name=f"Bureau {code}", **kwargs
        )


class AccessAndMenuTests(CoworkingStaffCase):
    def test_visiteur_renvoye_vers_la_connexion(self):
        self.client.logout()
        response = self.client.get(self.url("cw_setup"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("staff:login"), response["Location"])

    def test_structure_sans_module_coworking_refusee(self):
        library_only = make_org("Biblio seule")
        user = make_user("lib")
        OrganizationStaff.objects.create(user=user, organization=library_only, active=True)
        self.client.force_login(user)
        for name in ("cw_setup", "cw_zones", "cw_resources", "cw_rates", "cw_rules", "cw_zone_new"):
            self.assertEqual(self.client.get(self.url(name)).status_code, 403, name)

    def test_menu_coworking_visible_seulement_si_module_actif(self):
        self.assertContains(self.client.get(self.url("dashboard")), self.url("cw_setup"))
        library_only = make_org("Biblio seule")
        user = make_user("lib")
        OrganizationStaff.objects.create(user=user, organization=library_only, active=True)
        self.client.force_login(user)
        self.assertNotContains(self.client.get(self.url("dashboard")), self.url("cw_setup"))

    def test_tableau_de_bord_coworking_seul(self):
        response = self.client.get(self.url("dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Aucune ressource active")
        self.assertNotContains(response, "Prêter un livre")

    def test_tableau_de_bord_avec_ressources(self):
        self.make_resource("D1")
        self.make_resource("D2")
        response = self.client.get(self.url("dashboard"))
        self.assertContains(response, "Ressources libres")
        self.assertEqual(response.context["cw_stats"]["resources"]["total"], 2)

    def test_tableau_de_bord_des_deux_modules(self):
        both = make_org("Les deux")
        both.coworking_module = True
        both.save()
        user = make_user("both")
        OrganizationStaff.objects.create(user=user, organization=both, active=True)
        self.client.force_login(user)
        response = self.client.get(self.url("dashboard"))
        self.assertContains(response, "Prêter un livre")
        self.assertContains(response, "Aucune ressource active")
        self.assertContains(response, "Bibliothèque")

    def test_tableau_de_bord_sans_module(self):
        none = make_org("Sans module", library_module=False)
        user = make_user("none")
        OrganizationStaff.objects.create(user=user, organization=none, active=True)
        self.client.force_login(user)
        self.assertContains(self.client.get(self.url("dashboard")), "Aucun module de gestion")


class SetupHubTests(CoworkingStaffCase):
    def test_liste_de_depart_quand_rien_n_est_configure(self):
        Zone.objects.filter(organization=self.org).delete()
        response = self.client.get(self.url("cw_setup"))
        self.assertContains(response, "Créer au moins une zone")

    def test_signale_les_ressources_sans_tarif(self):
        self.make_resource("D1")
        Rate.objects.create(
            organization=self.org, resource_type=Resource.Type.ROOM, unit=Rate.Unit.HOUR, amount=1000
        )
        response = self.client.get(self.url("cw_setup"))
        self.assertEqual(response.context["without_rate"], 1)
        self.assertContains(response, "sans tarif")

    def test_un_tarif_de_type_couvre_les_ressources_du_type(self):
        self.make_resource("D1")
        Rate.objects.create(
            organization=self.org, resource_type=Resource.Type.DESK, unit=Rate.Unit.HOUR, amount=500
        )
        self.assertEqual(self.client.get(self.url("cw_setup")).context["without_rate"], 0)

    def test_les_donnees_d_une_autre_structure_ne_comptent_pas(self):
        Resource.objects.create(organization=self.other, zone=self.other_zone, code="X1", name="X")
        response = self.client.get(self.url("cw_setup"))
        self.assertEqual(response.context["resource_count"], 0)


class ZoneTests(CoworkingStaffCase):
    def test_creation_auditee(self):
        response = self.client.post(self.url("cw_zone_new"), {"name": "  Salles   de réunion ", "active": "on"})
        self.assertRedirects(response, self.url("cw_zones"), fetch_redirect_response=False)
        zone = Zone.objects.get(name="Salles de réunion")
        self.assertEqual(zone.organization, self.org)
        self.assertTrue(
            AuditLog.objects.filter(action=AuditLog.Action.ZONE_CREATED, organization=self.org).exists()
        )

    def test_nom_deja_pris_refuse_sans_tenir_compte_de_la_casse(self):
        response = self.client.post(self.url("cw_zone_new"), {"name": "OPEN SPACE", "active": "on"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Une zone porte déjà ce nom.")
        self.assertEqual(Zone.objects.filter(organization=self.org).count(), 1)

    def test_meme_nom_possible_dans_une_autre_structure(self):
        response = self.client.post(self.url("cw_zone_new"), {"name": "Autre zone", "active": "on"})
        self.assertEqual(response.status_code, 302)

    def test_modification_et_desactivation(self):
        response = self.client.post(self.url("cw_zone_edit", self.zone.pk), {"name": "Grand open space"})
        self.assertEqual(response.status_code, 302)
        self.zone.refresh_from_db()
        self.assertEqual(self.zone.name, "Grand open space")
        self.assertFalse(self.zone.active)  # case « active » décochée
        log = AuditLog.objects.filter(action=AuditLog.Action.ZONE_UPDATED).latest("pk")
        self.assertIn("name", log.metadata["changes"])

    def test_zone_d_une_autre_structure_introuvable(self):
        self.assertEqual(self.client.get(self.url("cw_zone_edit", self.other_zone.pk)).status_code, 404)
        response = self.client.post(self.url("cw_zone_edit", self.other_zone.pk), {"name": "Piratée"})
        self.assertEqual(response.status_code, 404)
        self.other_zone.refresh_from_db()
        self.assertEqual(self.other_zone.name, "Autre zone")

    def test_liste_affiche_le_nombre_de_ressources(self):
        self.make_resource("D1")
        self.make_resource("D2")
        response = self.client.get(self.url("cw_zones"))
        self.assertEqual(response.context["zones"][0].resource_count, 2)
        self.assertNotContains(response, "Autre zone")


class ResourceTests(CoworkingStaffCase):
    def payload(self, **overrides):
        data = {
            "zone": self.zone.pk, "code": "D1", "name": "Bureau 1",
            "resource_type": Resource.Type.DESK, "capacity": 1, "active": "on", "notes": "",
        }
        data.update(overrides)
        return data

    def test_creation_auditee(self):
        response = self.client.post(self.url("cw_resource_new"), self.payload())
        self.assertRedirects(response, self.url("cw_resources"), fetch_redirect_response=False)
        resource = Resource.objects.get(code="D1")
        self.assertEqual(resource.organization, self.org)
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.RESOURCE_CREATED).exists())

    def test_code_unique_dans_la_structure(self):
        self.make_resource("D1")
        response = self.client.post(self.url("cw_resource_new"), self.payload(code="d1"))
        self.assertContains(response, "Une ressource porte déjà ce code.")
        self.assertEqual(Resource.objects.filter(organization=self.org).count(), 1)

    def test_zone_d_une_autre_structure_refusee(self):
        response = self.client.post(self.url("cw_resource_new"), self.payload(zone=self.other_zone.pk))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Resource.objects.exists())

    def test_zone_desactivee_non_proposee(self):
        self.zone.active = False
        self.zone.save()
        response = self.client.post(self.url("cw_resource_new"), self.payload())
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Resource.objects.exists())

    def test_capacite_zero_refusee(self):
        response = self.client.post(self.url("cw_resource_new"), self.payload(capacity=0))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Resource.objects.exists())

    def test_modification_journalisee(self):
        resource = self.make_resource("D1")
        response = self.client.post(
            self.url("cw_resource_edit", resource.pk), self.payload(name="Bureau près de la fenêtre")
        )
        self.assertEqual(response.status_code, 302)
        resource.refresh_from_db()
        self.assertEqual(resource.name, "Bureau près de la fenêtre")
        log = AuditLog.objects.filter(action=AuditLog.Action.RESOURCE_UPDATED).latest("pk")
        self.assertEqual(log.metadata["changes"]["name"]["to"], "Bureau près de la fenêtre")

    def test_ressource_d_une_autre_structure_introuvable(self):
        foreign = Resource.objects.create(
            organization=self.other, zone=self.other_zone, code="X1", name="X"
        )
        self.assertEqual(self.client.get(self.url("cw_resource_edit", foreign.pk)).status_code, 404)

    def test_filtres_de_la_liste(self):
        self.make_resource("D1")
        self.make_resource("SALLE-A", resource_type=Resource.Type.ROOM)
        self.make_resource("OLD", active=False)
        codes = lambda response: sorted(r.code for r in response.context["page"].object_list)
        self.assertEqual(codes(self.client.get(self.url("cw_resources"))), ["D1", "OLD", "SALLE-A"])
        self.assertEqual(codes(self.client.get(self.url("cw_resources"), {"q": "salle"})), ["SALLE-A"])
        self.assertEqual(codes(self.client.get(self.url("cw_resources"), {"type": "DESK", "state": "active"})), ["D1"])
        self.assertEqual(codes(self.client.get(self.url("cw_resources"), {"state": "inactive"})), ["OLD"])
        self.assertEqual(codes(self.client.get(self.url("cw_resources"), {"zone": self.zone.pk})), ["D1", "OLD", "SALLE-A"])


class RateTests(CoworkingStaffCase):
    def payload(self, **overrides):
        data = {"resource": "", "resource_type": "DESK", "unit": "HOUR", "amount": 500, "active": "on"}
        data.update(overrides)
        return data

    def test_tarif_par_type(self):
        response = self.client.post(self.url("cw_rate_new"), self.payload())
        self.assertRedirects(response, self.url("cw_rates"), fetch_redirect_response=False)
        rate = Rate.objects.get()
        self.assertEqual((rate.organization, rate.resource_type, rate.amount), (self.org, "DESK", 500))
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.RATE_CREATED).exists())

    def test_tarif_par_ressource(self):
        resource = self.make_resource("D1")
        response = self.client.post(
            self.url("cw_rate_new"), self.payload(resource=resource.pk, resource_type="")
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Rate.objects.get().resource, resource)

    def test_ressource_et_type_ensemble_refuses(self):
        resource = self.make_resource("D1")
        response = self.client.post(self.url("cw_rate_new"), self.payload(resource=resource.pk))
        self.assertContains(response, "pas les deux")
        self.assertFalse(Rate.objects.exists())

    def test_ni_ressource_ni_type_refuses(self):
        response = self.client.post(self.url("cw_rate_new"), self.payload(resource_type=""))
        self.assertContains(response, "Choisissez une ressource ou un type")
        self.assertFalse(Rate.objects.exists())

    def test_periode_incoherente_refusee_sans_erreur_serveur(self):
        response = self.client.post(
            self.url("cw_rate_new"), self.payload(valid_from="2026-12-01", valid_until="2026-11-01")
        )
        self.assertContains(response, "La date de fin précède la date de début.")
        self.assertFalse(Rate.objects.exists())

    def test_ressource_d_une_autre_structure_refusee(self):
        foreign = Resource.objects.create(
            organization=self.other, zone=self.other_zone, code="X1", name="X"
        )
        response = self.client.post(
            self.url("cw_rate_new"), self.payload(resource=foreign.pk, resource_type="")
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Rate.objects.exists())

    def test_modification_n_ouvre_pas_le_tarif_d_une_autre_structure(self):
        foreign = Rate.objects.create(
            organization=self.other, resource_type="DESK", unit="HOUR", amount=1
        )
        self.assertEqual(self.client.get(self.url("cw_rate_edit", foreign.pk)).status_code, 404)

    def test_modification_et_desactivation(self):
        rate = Rate.objects.create(organization=self.org, resource_type="DESK", unit="HOUR", amount=500)
        response = self.client.post(
            self.url("cw_rate_edit", rate.pk), self.payload(amount=700, active="")
        )
        self.assertEqual(response.status_code, 302)
        rate.refresh_from_db()
        self.assertEqual(rate.amount, 700)
        self.assertFalse(rate.active)
        log = AuditLog.objects.filter(action=AuditLog.Action.RATE_UPDATED).latest("pk")
        self.assertEqual(log.metadata["changes"]["amount"], {"from": 500, "to": 700})

    def test_liste_formate_les_montants(self):
        Rate.objects.create(organization=self.org, resource_type="DESK", unit="HOUR", amount=15000)
        self.assertContains(self.client.get(self.url("cw_rates")), "XOF")


class RulesTests(CoworkingStaffCase):
    def payload(self, **overrides):
        data = {
            "public_description": "", "booking_enabled": "on", "walk_in_enabled": "on",
            "min_booking_minutes": 30, "max_booking_minutes": 480,
            "cancellation_deadline_hours": 2, "no_show_grace_minutes": 30,
            "walk_in_buffer_minutes": 30, "check_in_early_minutes": 15,
        }
        data.update(overrides)
        return data

    def test_la_page_s_affiche_et_cree_la_fiche(self):
        response = self.client.get(self.url("cw_rules"))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(CoworkingProfile.objects.filter(organization=self.org).exists())

    def test_horaires_enregistres_jour_par_jour(self):
        data = self.payload(mon_open="08:00", mon_close="18:00", sat_open="09:00", sat_close="13:00")
        response = self.client.post(self.url("cw_rules"), data)
        self.assertRedirects(response, self.url("cw_rules"), fetch_redirect_response=False)
        profile = CoworkingProfile.objects.get(organization=self.org)
        self.assertEqual(profile.opening_hours, {"mon": ["08:00", "18:00"], "sat": ["09:00", "13:00"]})
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.COWORKING_PROFILE_UPDATED).exists())

    def test_les_horaires_enregistres_reviennent_dans_le_formulaire(self):
        self.client.post(self.url("cw_rules"), self.payload(tue_open="07:30", tue_close="17:45"))
        response = self.client.get(self.url("cw_rules"))
        self.assertContains(response, 'value="07:30"')
        self.assertContains(response, 'value="17:45"')

    def test_tout_vide_veut_dire_aucune_restriction(self):
        self.client.post(self.url("cw_rules"), self.payload(mon_open="08:00", mon_close="18:00"))
        self.client.post(self.url("cw_rules"), self.payload())
        self.assertEqual(CoworkingProfile.objects.get(organization=self.org).opening_hours, {})

    def test_jour_incomplet_refuse(self):
        response = self.client.post(self.url("cw_rules"), self.payload(mon_open="08:00"))
        self.assertContains(response, "Renseignez l&#x27;ouverture et la fermeture")
        self.assertEqual(CoworkingProfile.objects.get(organization=self.org).opening_hours, {})

    def test_ouverture_apres_fermeture_refusee(self):
        response = self.client.post(self.url("cw_rules"), self.payload(mon_open="18:00", mon_close="08:00"))
        self.assertContains(response, "L&#x27;ouverture doit précéder la fermeture.")

    def test_duree_minimale_superieure_a_la_maximale_refusee(self):
        response = self.client.post(
            self.url("cw_rules"), self.payload(min_booking_minutes=600, max_booking_minutes=60)
        )
        self.assertContains(response, "La durée minimale dépasse la durée maximale.")
        self.assertEqual(CoworkingProfile.objects.get(organization=self.org).min_booking_minutes, 30)

    def test_la_fiche_d_une_autre_structure_n_est_pas_touchee(self):
        self.client.post(self.url("cw_rules"), self.payload(min_booking_minutes=45))
        other_profile, _ = CoworkingProfile.objects.get_or_create(organization=self.other)
        self.assertEqual(other_profile.min_booking_minutes, 30)
