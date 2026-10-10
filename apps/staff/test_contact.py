from django.contrib.auth import get_user_model
from django.urls import reverse

from .tests import StaffTestCase

User = get_user_model()


class MemberContactTests(StaffTestCase):
    def link_reader(self):
        reader = User.objects.create_user(
            email="lecteur@example.com", password="abc12345", first_name="Ama", last_name="Kofi"
        )
        self.member_a.user = reader
        self.member_a.save(update_fields=["user"])
        return reader

    def test_fiche_membre_affiche_l_email_du_compte_relie(self):
        self.link_reader()
        response = self.client.get(reverse("staff:member_detail", args=[self.member_a.pk]))
        self.assertContains(response, "lecteur@example.com")

    def test_fiche_membre_sans_compte(self):
        response = self.client.get(reverse("staff:member_detail", args=[self.member_a.pk]))
        self.assertContains(response, "pas de compte relié")

    def test_liste_des_membres_affiche_l_email_du_compte(self):
        self.link_reader()
        self.assertContains(self.get("members"), "lecteur@example.com")

    def test_liste_des_membres_sans_requete_par_ligne(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from apps.members.services import register_member

        def queries():
            with CaptureQueriesContext(connection) as context:
                self.get("members")
            return len(context)

        self.link_reader()
        self.get("members")  # réchauffe les caches de session et de structure
        avant = queries()
        for index in range(3):
            member = register_member(
                organization=self.org_a, first_name=f"M{index}", last_name=f"N{index}"
            )
            member.user = User.objects.create_user(
                email=f"m{index}@example.com", password="abc12345",
                first_name=f"M{index}", last_name=f"N{index}",
            )
            member.save(update_fields=["user"])
        self.assertEqual(queries(), avant)

    def test_email_d_une_autre_structure_jamais_affiche(self):
        reader = User.objects.create_user(
            email="autre@example.com", password="abc12345", first_name="Yao", last_name="Mensah"
        )
        self.member_b.user = reader
        self.member_b.save(update_fields=["user"])
        self.assertNotContains(self.get("members"), "autre@example.com")
        response = self.client.get(reverse("staff:member_detail", args=[self.member_b.pk]))
        self.assertEqual(response.status_code, 404)