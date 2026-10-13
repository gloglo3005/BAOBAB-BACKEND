from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient, APITestCase

from apps.catalog.models import Book
from apps.members.models import Member
from apps.organizations.models import OrganizationStaff

from .test_auth import NOUVEAU, PASSWORD, make_user
from .test_staff_read import make_org
from .test_staff_read import make_user as make_staff_user


class TokenRevocationTests(APITestCase):
    """Changer son mot de passe doit couper les jetons déjà en circulation."""

    def setUp(self):
        cache.clear()
        self.user = make_user()

    def login(self):
        reponse = self.client.post(
            reverse("api:login"),
            {"email": "agent@example.com", "password": PASSWORD},
            format="json",
        )
        self.assertEqual(reponse.status_code, 200)
        return reponse.json()

    def me_status(self, access):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        return self.client.get(reverse("api:me")).status_code

    def test_anciens_jetons_refuses_apres_changement_de_mot_de_passe(self):
        anciens = self.login()
        self.assertEqual(self.me_status(anciens["access"]), 200)

        reponse = self.client.post(
            reverse("api:password"),
            {"old_password": PASSWORD, "new_password": NOUVEAU},
            format="json",
        )
        self.assertEqual(reponse.status_code, 200)
        nouveaux = reponse.json()

        # L'ancien jeton d'accès ne fonctionne plus...
        self.assertEqual(self.me_status(anciens["access"]), 401)
        # ... et un jeton d'accès obtenu avec l'ancien jeton de renouvellement non plus.
        self.client.credentials()
        renouvele = self.client.post(
            reverse("api:refresh"), {"refresh": anciens["refresh"]}, format="json"
        )
        if renouvele.status_code == 200:
            self.assertEqual(self.me_status(renouvele.json()["access"]), 401)
        # Les jetons rendus par le changement de mot de passe, eux, fonctionnent.
        self.assertEqual(self.me_status(nouveaux["access"]), 200)

    def test_les_jetons_restent_valides_sans_changement(self):
        jetons = self.login()
        self.assertEqual(self.me_status(jetons["access"]), 200)
        self.assertEqual(self.me_status(jetons["access"]), 200)


class StablePaginationTests(TestCase):
    """Les listes paginées gardent le même ordre d'une page à l'autre."""

    def setUp(self):
        self.org = make_org("Biblio Pagination")
        staff = make_staff_user("pagination")
        OrganizationStaff.objects.create(user=staff, organization=self.org, active=True)
        self.client = APIClient()
        self.client.force_authenticate(staff)

    def collect_ids(self, url_name):
        url = reverse(url_name, kwargs={"organization_id": self.org.pk})
        seen = []
        for page in (1, 2, 3):
            reponse = self.client.get(url, {"page": page, "page_size": 10})
            self.assertEqual(reponse.status_code, 200)
            seen += [row["id"] for row in reponse.json()["results"]]
        return seen

    def test_livres_sans_doublon_ni_oubli(self):
        # Titres identiques : l'ordre ne dépend que du critère de départage.
        for _ in range(30):
            Book.objects.create(organization=self.org, title="Même titre")
        ids = self.collect_ids("api:staff-books")
        self.assertEqual(len(ids), 30)
        self.assertEqual(len(set(ids)), 30)

    def test_membres_sans_doublon_ni_oubli(self):
        for index in range(30):
            Member.objects.create(
                organization=self.org, member_number=f"MEM-{index:04d}",
                first_name="Ama", last_name="Kofi", status=Member.Status.ACCEPTED,
            )
        ids = self.collect_ids("api:staff-members")
        self.assertEqual(len(ids), 30)
        self.assertEqual(len(set(ids)), 30)
