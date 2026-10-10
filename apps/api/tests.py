from django.test import TestCase
from django.urls import reverse


class ApiBaseTests(TestCase):
    def test_ping_est_public(self):
        reponse = self.client.get(reverse("api:ping"))
        self.assertEqual(reponse.status_code, 200)
        self.assertEqual(reponse.json()["status"], "ok")

    def test_le_schema_se_genere(self):
        reponse = self.client.get(reverse("api:schema"))
        self.assertEqual(reponse.status_code, 200)
