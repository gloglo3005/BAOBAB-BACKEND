from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from .login_limits import MAX_FAILURES, TOO_MANY_MESSAGE

User = get_user_model()

PASSWORD = "Zq7!fT2mQp91"


class WebLoginLimitTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            email="agent@example.com", password=PASSWORD,
            first_name="Ama", last_name="Kofi", is_staff=True,
        )

    def post_login(self, url_name, email="agent@example.com", password=PASSWORD):
        return self.client.post(reverse(url_name), {"username": email, "password": password})

    def assert_logged_in(self):
        self.assertIn("_auth_user_id", self.client.session)

    def assert_logged_out(self):
        self.assertNotIn("_auth_user_id", self.client.session)

    def lock(self, url_name):
        for _ in range(MAX_FAILURES):
            self.post_login(url_name, password="faux")

    def check_locks(self, url_name):
        self.lock(url_name)
        # Même le bon mot de passe est refusé tant que le compte est verrouillé.
        response = self.post_login(url_name)
        self.assertEqual(response.status_code, 200)
        self.assertIn(TOO_MANY_MESSAGE, response.context["form"].non_field_errors())
        self.assert_logged_out()

    def test_staff_login_se_verrouille(self):
        self.check_locks("staff:login")

    def test_reader_login_se_verrouille(self):
        self.check_locks("reader:login")

    def test_admin_global_se_verrouille(self):
        self.check_locks("admin:login")

    def test_connexion_normale_possible_sous_la_limite(self):
        for _ in range(MAX_FAILURES - 1):
            self.post_login("staff:login", password="faux")
        self.post_login("staff:login")
        self.assert_logged_in()

    def test_une_connexion_reussie_remet_le_compteur_a_zero(self):
        for _ in range(MAX_FAILURES - 1):
            self.post_login("staff:login", password="faux")
        self.post_login("staff:login")
        self.client.logout()
        # Sans remise à zéro, ces échecs feraient dépasser la limite.
        for _ in range(MAX_FAILURES - 1):
            self.post_login("staff:login", password="faux")
        self.post_login("staff:login")
        self.assert_logged_in()

    def test_la_limite_ne_touche_pas_les_autres_comptes(self):
        User.objects.create_user(
            email="autre@example.com", password=PASSWORD,
            first_name="Bob", last_name="Autre",
        )
        self.lock("staff:login")
        self.post_login("staff:login", email="autre@example.com")
        self.assert_logged_in()

    def test_majuscules_et_minuscules_comptent_ensemble(self):
        for _ in range(MAX_FAILURES):
            self.post_login("staff:login", email="AGENT@Example.com", password="faux")
        response = self.post_login("staff:login", email="agent@example.com")
        self.assertIn(TOO_MANY_MESSAGE, response.context["form"].non_field_errors())
        self.assert_logged_out()
