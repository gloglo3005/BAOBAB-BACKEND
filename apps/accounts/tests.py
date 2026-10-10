from django.contrib.auth import get_user_model
from django.test import TestCase

User = get_user_model()


class UserManagerTests(TestCase):
    def test_utilisateur_normal_n_est_pas_admin(self):
        user = User.objects.create_user(
            email="lecteur@example.com", password="abc12345",
            first_name="Ama", last_name="Kofi",
        )
        self.assertFalse(user.is_platform_admin)
        self.assertFalse(user.is_staff)
        self.assertTrue(user.check_password("abc12345"))

    def test_superuser_est_platform_admin(self):
        user = User.objects.create_superuser(
            email="admin@example.com", password="abc12345",
            first_name="Root", last_name="Admin",
        )
        self.assertTrue(user.is_platform_admin)
        self.assertTrue(user.is_superuser)

    def test_email_obligatoire(self):
        with self.assertRaises(ValueError):
            User.objects.create_user(email="", password="abc12345")