from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.urls import reverse
from rest_framework.test import APITestCase

from apps.members.models import Member
from apps.organizations.models import Organization, OrganizationStaff

User = get_user_model()

PASSWORD = "Zq7!fT2mQp91"
NOUVEAU = "Vb8#kL4wNs27"


def make_user(email="agent@example.com", **extra):
    return User.objects.create_user(
        email=email, password=PASSWORD, first_name="Ama", last_name="Kofi", **extra
    )


def make_org(name, slug, status=Organization.Status.ACTIVE):
    return Organization.objects.create(name=name, slug=slug, status=status)


class AuthTestCase(APITestCase):
    def setUp(self):
        cache.clear()  # remet à zéro la limite de tentatives
        self.user = make_user()

    def login(self, email="agent@example.com", password=PASSWORD):
        return self.client.post(
            reverse("api:login"), {"email": email, "password": password}, format="json"
        )

    def authenticate(self):
        token = self.login().json()["access"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")


class LoginTests(AuthTestCase):
    def test_connexion_reussie(self):
        reponse = self.login()
        self.assertEqual(reponse.status_code, 200)
        donnees = reponse.json()
        self.assertIn("access", donnees)
        self.assertIn("refresh", donnees)
        self.assertFalse(donnees["must_change_password"])

    def test_mauvais_mot_de_passe(self):
        self.assertEqual(self.login(password="faux").status_code, 401)

    def test_compte_inactif_refuse(self):
        User.objects.filter(pk=self.user.pk).update(is_active=False)
        self.assertEqual(self.login().status_code, 401)

    def test_indique_le_changement_obligatoire(self):
        User.objects.filter(pk=self.user.pk).update(must_change_password=True)
        self.assertTrue(self.login().json()["must_change_password"])

    def test_renouvellement_du_jeton(self):
        refresh = self.login().json()["refresh"]
        reponse = self.client.post(
            reverse("api:refresh"), {"refresh": refresh}, format="json"
        )
        self.assertEqual(reponse.status_code, 200)
        self.assertIn("access", reponse.json())

    def test_trop_de_tentatives(self):
        for _ in range(10):
            self.assertEqual(self.login(password="faux").status_code, 401)
        self.assertEqual(self.login(password="faux").status_code, 429)


class MeTests(AuthTestCase):
    def test_exige_un_jeton(self):
        self.assertEqual(self.client.get(reverse("api:me")).status_code, 401)

    def test_profil_sans_structure(self):
        self.authenticate()
        donnees = self.client.get(reverse("api:me")).json()
        self.assertEqual(donnees["email"], "agent@example.com")
        self.assertFalse(donnees["is_platform_admin"])
        self.assertEqual(donnees["staff_organizations"], [])
        self.assertEqual(donnees["memberships"], [])

    def test_structures_du_staff_actives_seulement(self):
        a = make_org("Biblio A", "biblio-a")
        b = make_org("Biblio B", "biblio-b", status=Organization.Status.SUSPENDED)
        c = make_org("Biblio C", "biblio-c")
        make_org("Biblio D", "biblio-d")  # aucun lien avec le compte
        OrganizationStaff.objects.create(user=self.user, organization=a)
        OrganizationStaff.objects.create(user=self.user, organization=b)
        OrganizationStaff.objects.create(user=self.user, organization=c, active=False)

        self.authenticate()
        donnees = self.client.get(reverse("api:me")).json()
        self.assertEqual([o["slug"] for o in donnees["staff_organizations"]], ["biblio-a"])

    def test_fiches_de_lecteur_avec_leur_statut(self):
        a = make_org("Biblio A", "biblio-a")
        b = make_org("Biblio B", "biblio-b")
        Member.objects.create(
            organization=a, user=self.user, member_number="MEM-0001",
            first_name="Ama", last_name="Kofi", status=Member.Status.PENDING,
        )
        Member.objects.create(
            organization=b, user=self.user, member_number="MEM-0007",
            first_name="Ama", last_name="Kofi", status=Member.Status.ACCEPTED,
        )
        Member.objects.create(  # fiche d'un autre lecteur : ne doit pas apparaître
            organization=a, user=None, member_number="MEM-0002",
            first_name="Autre", last_name="Lecteur", status=Member.Status.ACCEPTED,
        )

        self.authenticate()
        fiches = self.client.get(reverse("api:me")).json()["memberships"]
        self.assertEqual(
            [(f["member_number"], f["status"]) for f in fiches],
            [("MEM-0001", "PENDING"), ("MEM-0007", "ACCEPTED")],
        )

    def test_ne_montre_pas_les_donnees_d_un_autre_compte(self):
        autre = make_user("autre@example.com")
        org = make_org("Biblio Z", "biblio-z")
        OrganizationStaff.objects.create(user=autre, organization=org)

        self.authenticate()
        donnees = self.client.get(reverse("api:me")).json()
        self.assertEqual(donnees["staff_organizations"], [])


class PasswordTests(AuthTestCase):
    def changer(self, ancien=PASSWORD, nouveau=NOUVEAU):
        return self.client.post(
            reverse("api:password"),
            {"old_password": ancien, "new_password": nouveau},
            format="json",
        )

    def test_exige_un_jeton(self):
        self.assertEqual(self.changer().status_code, 401)

    def test_ancien_mot_de_passe_incorrect(self):
        self.authenticate()
        reponse = self.changer(ancien="faux")
        self.assertEqual(reponse.status_code, 400)
        self.assertIn("old_password", reponse.json())

    def test_mot_de_passe_trop_faible(self):
        self.authenticate()
        reponse = self.changer(nouveau="12345678")
        self.assertEqual(reponse.status_code, 400)
        self.assertIn("new_password", reponse.json())

    def test_refuse_le_meme_mot_de_passe(self):
        self.authenticate()
        reponse = self.changer(nouveau=PASSWORD)
        self.assertEqual(reponse.status_code, 400)
        self.assertIn("new_password", reponse.json())

    def test_changement_reussi(self):
        User.objects.filter(pk=self.user.pk).update(must_change_password=True)
        self.authenticate()
        reponse = self.changer()
        self.assertEqual(reponse.status_code, 200)
        self.assertFalse(reponse.json()["must_change_password"])

        self.user.refresh_from_db()
        self.assertFalse(self.user.must_change_password)
        self.assertEqual(self.login(password=NOUVEAU).status_code, 200)
        self.assertEqual(self.login(password=PASSWORD).status_code, 401)

        # Les nouveaux jetons fonctionnent tout de suite.
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {reponse.json()['access']}"
        )
        self.assertEqual(self.client.get(reverse("api:me")).status_code, 200)
