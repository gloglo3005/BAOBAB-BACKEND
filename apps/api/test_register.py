from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.urls import reverse
from rest_framework.test import APITestCase

from apps.organizations.models import OrganizationStaff

User = get_user_model()

PASSWORD = "Zq7!fT2mQp91"


class RegisterApiTests(APITestCase):
    def setUp(self):
        cache.clear()  # remet à zéro la limite de créations

    def payload(self, **extra):
        data = {
            "first_name": "Yao", "last_name": "Mensah", "email": "yao@example.com",
            "phone": "90000000", "password1": PASSWORD, "password2": PASSWORD,
        }
        data.update(extra)
        return data

    def register(self, **extra):
        return self.client.post(reverse("api:register"), self.payload(**extra), format="json")

    def test_cree_un_compte_lecteur_sans_privilege_et_renvoie_les_jetons(self):
        response = self.register()
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertTrue(body["access"] and body["refresh"])
        self.assertFalse(body["must_change_password"])

        user = User.objects.get(email="yao@example.com")
        self.assertEqual((user.first_name, user.last_name), ("Yao", "Mensah"))
        self.assertFalse(user.is_staff or user.is_superuser or user.is_platform_admin)
        self.assertFalse(OrganizationStaff.objects.filter(user=user).exists())
        self.assertTrue(user.check_password(PASSWORD))

    def test_les_jetons_ouvrent_l_api(self):
        token = self.register().json()["access"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        me = self.client.get(reverse("api:me")).json()
        self.assertEqual(me["email"], "yao@example.com")
        self.assertEqual(me["staff_organizations"], [])
        self.assertFalse(me["is_platform_admin"])

    def test_un_role_envoye_a_la_main_est_ignore(self):
        self.register(
            is_staff=True, is_superuser=True, is_platform_admin=True, must_change_password=True
        )
        user = User.objects.get(email="yao@example.com")
        self.assertFalse(user.is_staff or user.is_superuser or user.is_platform_admin)
        self.assertFalse(user.must_change_password)

    def test_email_deja_utilise_meme_avec_une_autre_casse(self):
        User.objects.create_user(email="yao@example.com", password=PASSWORD)
        response = self.register(email="YAO@example.com")
        self.assertEqual(response.status_code, 400)
        self.assertIn("email", response.json())
        self.assertEqual(User.objects.count(), 1)

    def test_mots_de_passe_differents(self):
        response = self.register(password2="autre chose 123")
        self.assertEqual(response.status_code, 400)
        self.assertIn("password2", response.json())
        self.assertFalse(User.objects.exists())

    def test_mot_de_passe_trop_faible(self):
        response = self.register(password1="12345678", password2="12345678")
        self.assertEqual(response.status_code, 400)
        self.assertIn("password1", response.json())

    def test_champs_obligatoires(self):
        response = self.client.post(reverse("api:register"), {}, format="json")
        self.assertEqual(response.status_code, 400)
        for field in ("first_name", "last_name", "email", "password1", "password2"):
            self.assertIn(field, response.json())

    def test_un_jeton_invalide_n_empeche_pas_l_inscription(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer jeton-perime")
        self.assertEqual(self.register().status_code, 201)

    def test_limite_de_creations(self):
        for i in range(10):
            self.assertEqual(self.register(email=f"u{i}@example.com").status_code, 201)
        self.assertEqual(self.register(email="trop@example.com").status_code, 429)