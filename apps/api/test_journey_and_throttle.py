from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from apps.audit.models import AuditLog

from .test_staff_read import make_org, make_user

User = get_user_model()
PASSWORD = "Mdp-test-123"


class FullJourneyTests(TestCase):
    """Parcours réel, avec de vrais jetons JWT : du comptoir au retour du livre."""

    def setUp(self):
        cache.clear()
        self.org = make_org("Biblio Parcours")
        self.staff_user = make_user("staff-parcours")
        from apps.organizations.models import OrganizationStaff

        OrganizationStaff.objects.create(user=self.staff_user, organization=self.org, active=True)
        self.reader_user = make_user("lecteur-parcours")
        self.staff = self.logged_client(self.staff_user)
        self.reader = self.logged_client(self.reader_user)

    def logged_client(self, user):
        client = APIClient()
        response = client.post(
            reverse("api:login"),
            {User.USERNAME_FIELD: getattr(user, User.USERNAME_FIELD), "password": PASSWORD},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        client.credentials(HTTP_AUTHORIZATION="Bearer " + response.json()["access"])
        return client

    def su(self, name, **kwargs):
        return reverse(f"api:{name}", kwargs={"organization_id": self.org.pk, **kwargs})

    def ru(self, name, **kwargs):
        return reverse(f"api:{name}", kwargs=kwargs)

    def test_full_journey(self):
        # 1. Le staff inscrit un membre au comptoir et lui génère un code.
        response = self.staff.post(
            self.su("staff-members"), {"first_name": "Awa", "last_name": "Mensah"}, format="json"
        )
        self.assertEqual(response.status_code, 201)
        member_id = response.json()["id"]
        response = self.staff.post(self.su("staff-member-link-code", pk=member_id), {}, format="json")
        self.assertEqual(response.status_code, 201)
        code = response.json()["code"]

        # 2. Le lecteur relie son compte avec le code.
        response = self.reader.post(self.ru("reader-link"), {"code": code}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["id"], member_id)
        memberships = self.reader.get(self.ru("reader-memberships")).json()
        self.assertEqual(len(memberships), 1)
        self.assertEqual(memberships[0]["status"], "ACCEPTED")

        # 3. Le staff crée un livre avec un exemplaire.
        response = self.staff.post(
            self.su("staff-books"),
            {"title": "Le parcours complet", "authors": ["Awa Test"]},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        book_id = response.json()["id"]
        response = self.staff.post(
            self.su("staff-book-copies", pk=book_id), {"codes": ["P-001"]}, format="json"
        )
        self.assertEqual(response.status_code, 201)
        copy_id = response.json()[0]["id"]

        detail = self.ru("reader-book-detail", member_id=member_id, pk=book_id)
        self.assertEqual(self.reader.get(detail).json()["available_total"], 1)

        # 4. Le staff prête le livre ; le lecteur le voit.
        response = self.staff.post(
            self.su("staff-loans"),
            {"book_copy": str(copy_id), "member": str(member_id)},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        loan_id = response.json()["id"]

        summary = self.reader.get(self.ru("reader-summary", member_id=member_id)).json()
        self.assertEqual(summary["loans"]["active"], 1)
        loans = self.reader.get(self.ru("reader-loans", member_id=member_id)).json()
        self.assertEqual(loans["count"], 1)
        self.assertEqual(loans["results"][0]["book"]["title"], "Le parcours complet")
        book = self.reader.get(detail).json()
        self.assertEqual(book["available_total"], 0)
        self.assertEqual(len(book["my_loans"]), 1)

        # 5. Le staff enregistre le retour ; tout revient à la normale.
        response = self.staff.post(self.su("staff-loan-return", pk=loan_id), {}, format="json")
        self.assertEqual(response.status_code, 200)
        summary = self.reader.get(self.ru("reader-summary", member_id=member_id)).json()
        self.assertEqual(summary["loans"]["active"], 0)
        history = self.reader.get(
            self.ru("reader-loans", member_id=member_id), {"status": "history"}
        ).json()
        self.assertEqual(history["count"], 1)
        self.assertEqual(self.reader.get(detail).json()["available_total"], 1)

        # 6. Le journal d'audit a gardé la trace.
        actions = set(
            AuditLog.objects.filter(organization=self.org).values_list("action", flat=True)
        )
        for action in (
            AuditLog.Action.MEMBER_CREATED,
            AuditLog.Action.BOOK_CREATED,
            AuditLog.Action.COPY_CREATED,
            AuditLog.Action.LOAN_CREATED,
            AuditLog.Action.LOAN_RETURNED,
        ):
            self.assertIn(action, actions)

    def test_reader_token_cannot_use_staff_routes(self):
        response = self.reader.get(self.su("staff-books"))
        self.assertEqual(response.status_code, 403)

    def test_staff_cannot_read_a_reader_file_that_is_not_theirs(self):
        from apps.members.models import Member

        member = Member.objects.create(
            organization=self.org, member_number="MEM-0001", first_name="Awa",
            last_name="Test", status=Member.Status.ACCEPTED, user=self.reader_user,
        )
        response = self.staff.get(self.ru("reader-summary", member_id=member.pk))
        self.assertEqual(response.status_code, 403)


class ThrottleTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = make_user("throttle")
        self.client = APIClient()

    def test_login_is_limited(self):
        payload = {User.USERNAME_FIELD: "personne@test.com", "password": "mauvais"}
        for _ in range(10):
            response = self.client.post(reverse("api:login"), payload, format="json")
            self.assertNotEqual(response.status_code, 429)
        response = self.client.post(reverse("api:login"), payload, format="json")
        self.assertEqual(response.status_code, 429)
        self.assertTrue(response.has_header("Retry-After"))

    def test_password_change_is_limited(self):
        self.client.force_authenticate(self.user)
        for _ in range(5):
            response = self.client.post(reverse("api:password"), {}, format="json")
            self.assertNotEqual(response.status_code, 429)
        response = self.client.post(reverse("api:password"), {}, format="json")
        self.assertEqual(response.status_code, 429)

    def test_link_code_is_limited_per_account(self):
        self.client.force_authenticate(self.user)
        for _ in range(10):
            response = self.client.post(reverse("api:reader-link"), {"code": "ZZZZZZZZ"}, format="json")
            self.assertNotEqual(response.status_code, 429)
        response = self.client.post(reverse("api:reader-link"), {"code": "ZZZZZZZZ"}, format="json")
        self.assertEqual(response.status_code, 429)

        # Un autre compte n'est pas touché par la limite du premier.
        other = make_user("throttle-autre")
        self.client.force_authenticate(other)
        response = self.client.post(reverse("api:reader-link"), {"code": "ZZZZZZZZ"}, format="json")
        self.assertEqual(response.status_code, 400)