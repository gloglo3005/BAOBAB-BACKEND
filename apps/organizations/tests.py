from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase

from .models import Organization, OrganizationStaff
from .services import create_organization, create_staff_account

User = get_user_model()


class CreateOrganizationTests(TestCase):
    def test_creation_cree_la_politique_par_defaut(self):
        organization = create_organization(name="Bibliothèque de Lomé", slug="lome")
        policy = organization.loan_policy
        self.assertEqual(policy.loan_duration_days, 14)
        self.assertEqual(policy.max_active_loans, 3)
        self.assertEqual(policy.max_renewals, 1)

    def test_slug_unique(self):
        create_organization(name="A", slug="a")
        # Après une erreur SQL, la transaction du test est cassée :
        # on l'isole dans un atomic() pour pouvoir continuer.
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                create_organization(name="B", slug="a")

    def test_duree_de_pret_zero_refusee_par_la_base(self):
        organization = create_organization(name="A", slug="a")
        policy = organization.loan_policy
        policy.loan_duration_days = 0
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                policy.save()


class ModulesTests(TestCase):
    def test_par_defaut_seul_le_module_bibliotheque_est_actif(self):
        organization = create_organization(name="A", slug="a")
        self.assertTrue(organization.has_module(Organization.Module.LIBRARY))
        self.assertFalse(organization.has_module(Organization.Module.COWORKING))
        self.assertEqual(organization.enabled_modules, [Organization.Module.LIBRARY])

    def test_on_peut_activer_les_deux_modules_a_la_creation(self):
        organization = create_organization(
            name="Espace Tech", slug="tech", coworking_module=True
        )
        self.assertEqual(
            organization.enabled_modules,
            [Organization.Module.LIBRARY, Organization.Module.COWORKING],
        )

    def test_un_coworking_pur_n_a_pas_le_module_bibliotheque(self):
        organization = create_organization(
            name="Coworking", slug="cow", library_module=False, coworking_module=True
        )
        self.assertFalse(organization.has_module(Organization.Module.LIBRARY))
        self.assertEqual(organization.enabled_modules, [Organization.Module.COWORKING])


class OrganizationStaffTests(TestCase):
    def setUp(self):
        # setUp s'exécute avant chaque test de la classe.
        self.organization = create_organization(name="Lomé", slug="lome")

    def test_cree_un_compte_avec_mot_de_passe_temporaire(self):
        user, password = create_staff_account(
            organization=self.organization, email="staff@example.com",
            first_name="Kossi", last_name="Amah",
        )
        self.assertTrue(user.check_password(password))
        self.assertTrue(user.must_change_password)
        self.assertTrue(
            OrganizationStaff.objects.filter(user=user, organization=self.organization).exists()
        )

    def test_ne_cree_jamais_un_admin_de_plateforme(self):
        user, _ = create_staff_account(
            organization=self.organization, email="staff@example.com",
            first_name="Kossi", last_name="Amah",
        )
        self.assertFalse(user.is_platform_admin)
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)

    def test_compte_existant_est_rattache_sans_nouveau_mot_de_passe(self):
        existing = User.objects.create_user(
            email="lecteur@example.com", password="abc12345",
            first_name="Ama", last_name="Kofi",
        )
        user, password = create_staff_account(
            organization=self.organization, email="Lecteur@Example.com",
            first_name="Ama", last_name="Kofi",
        )
        self.assertEqual(user, existing)
        self.assertIsNone(password)
        self.assertTrue(user.check_password("abc12345"))
        self.assertFalse(user.must_change_password)
        self.assertEqual(User.objects.count(), 1)

    def test_pas_de_doublon_de_rattachement_via_le_service(self):
        for _ in range(2):
            create_staff_account(
                organization=self.organization, email="staff@example.com",
                first_name="Kossi", last_name="Amah",
            )
        self.assertEqual(OrganizationStaff.objects.count(), 1)

    def test_un_staff_ne_peut_pas_etre_rattache_deux_fois_a_la_meme_structure(self):
        user = User.objects.create_user(email="staff@example.com", password="abc12345")
        OrganizationStaff.objects.create(user=user, organization=self.organization)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                OrganizationStaff.objects.create(user=user, organization=self.organization)

    def test_un_staff_peut_appartenir_a_deux_structures(self):
        other = create_organization(name="Kara", slug="kara")
        user = User.objects.create_user(email="staff@example.com", password="abc12345")
        OrganizationStaff.objects.create(user=user, organization=self.organization)
        OrganizationStaff.objects.create(user=user, organization=other)
        self.assertEqual(user.organization_staff_memberships.count(), 2)