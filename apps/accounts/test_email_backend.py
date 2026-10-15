from django.contrib.auth import authenticate, get_user_model
from django.test import TestCase


class CaseInsensitiveEmailTests(TestCase):
    def setUp(self):
        User = get_user_model()
        User.objects.create_user(email="Jean@Mail.com", password="Secret-pass-123")
        User.objects.create_user(email="paul@mail.com", password="Secret-pass-123")

    def test_majuscules_acceptees(self):
        self.assertIsNotNone(authenticate(email="PAUL@MAIL.COM", password="Secret-pass-123"))

    def test_casse_enregistree_acceptee(self):
        self.assertIsNotNone(authenticate(email="Jean@Mail.com", password="Secret-pass-123"))
        self.assertIsNotNone(authenticate(email="jean@mail.com", password="Secret-pass-123"))

    def test_mauvais_mot_de_passe_refuse(self):
        self.assertIsNone(authenticate(email="paul@mail.com", password="mauvais"))

    def test_compte_inconnu_refuse(self):
        self.assertIsNone(authenticate(email="inconnu@mail.com", password="Secret-pass-123"))

    def test_compte_desactive_refuse(self):
        get_user_model().objects.filter(email="paul@mail.com").update(is_active=False)
        self.assertIsNone(authenticate(email="paul@mail.com", password="Secret-pass-123"))
