from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.api.test_staff_read import make_org, make_user
from apps.audit.models import AuditLog
from apps.organizations import services
from apps.organizations.models import LoanPolicy, Organization, OrganizationStaff

User = get_user_model()


def as_user(user):
    client = APIClient()
    client.force_authenticate(user)
    return client


def staff_user(org, label, role=OrganizationStaff.Role.STAFF):
    user, _ = services.create_staff_account(
        organization=org, email=f"{label}@test.com", first_name=label.title(), last_name="Test", role=role
    )
    user.must_change_password = False
    user.save()
    return user


class PlatformApiTests(TestCase):
    def setUp(self):
        self.admin = make_user("plateforme")
        self.admin.is_platform_admin = True
        self.admin.save()
        self.client = as_user(self.admin)
        self.org = make_org("Biblio Centrale")

    # ------------------------------------------------------------------ accès

    def test_anonymous_and_ordinary_users_refused(self):
        self.assertIn(APIClient().get("/api/v1/platform/organizations/").status_code, (401, 403))
        agent = staff_user(self.org, "agent")
        self.assertEqual(as_user(agent).get("/api/v1/platform/organizations/").status_code, 403)
        self.assertEqual(as_user(make_user("simple")).get("/api/v1/platform/dashboard/").status_code, 403)

    def test_admin_must_change_password_first(self):
        self.admin.must_change_password = True
        self.admin.save()
        self.assertEqual(self.client.get("/api/v1/platform/organizations/").status_code, 403)

    # ------------------------------------------------------------------ structures

    def test_create_organization(self):
        response = self.client.post(
            "/api/v1/platform/organizations/",
            {"name": "Espace Lomé", "city": "Lomé", "coworking_module": True}, format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.data["slug"], "espace-lome")
        self.assertEqual(response.data["status"], "ACTIVE")
        organization = Organization.objects.get(slug="espace-lome")
        self.assertTrue(organization.coworking_module and organization.library_module)
        self.assertTrue(LoanPolicy.objects.filter(organization=organization).exists())
        self.assertTrue(AuditLog.objects.filter(organization=organization, action="ORGANIZATION_CREATED", user=self.admin).exists())

    def test_slug_is_made_unique(self):
        first = self.client.post("/api/v1/platform/organizations/", {"name": "Doublon"}, format="json")
        second = self.client.post("/api/v1/platform/organizations/", {"name": "Doublon"}, format="json")
        self.assertEqual((first.data["slug"], second.data["slug"]), ("doublon", "doublon-2"))

    def test_create_needs_a_module_and_free_slug(self):
        none = self.client.post(
            "/api/v1/platform/organizations/",
            {"name": "Vide", "library_module": False, "coworking_module": False}, format="json",
        )
        self.assertEqual(none.status_code, 400)
        taken = self.client.post("/api/v1/platform/organizations/", {"name": "X", "slug": self.org.slug}, format="json")
        self.assertEqual(taken.status_code, 400)

    def test_list_filters_and_counts(self):
        staff_user(self.org, "resp", OrganizationStaff.Role.ADMIN)
        staff_user(self.org, "agent")
        other = make_org("Cowork Sud", library_module=False)
        other.coworking_module = True
        other.save()
        everything = self.client.get("/api/v1/platform/organizations/").data
        self.assertEqual(everything["count"], 2)
        mine = next(o for o in everything["results"] if o["id"] == self.org.pk)
        self.assertEqual((mine["staff_count"], mine["admin_count"]), (2, 1))
        self.assertEqual(self.client.get("/api/v1/platform/organizations/?module=COWORKING").data["count"], 1)
        self.assertEqual(self.client.get("/api/v1/platform/organizations/?q=centrale").data["count"], 1)
        self.assertEqual(self.client.get("/api/v1/platform/organizations/?status=PENDING").data["count"], 0)

    def test_patch_ignores_status_and_slug(self):
        response = self.client.patch(
            f"/api/v1/platform/organizations/{self.org.pk}/",
            {"city": "Kara", "status": "SUSPENDED", "slug": "autre"}, format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.org.refresh_from_db()
        self.assertEqual((self.org.city, self.org.status, self.org.slug), ("Kara", "ACTIVE", "biblio-centrale"))
        self.assertTrue(AuditLog.objects.filter(organization=self.org, action="ORGANIZATION_UPDATED").exists())

    def test_cannot_switch_off_all_modules(self):
        response = self.client.patch(
            f"/api/v1/platform/organizations/{self.org.pk}/", {"library_module": False}, format="json"
        )
        self.assertEqual(response.status_code, 400)

    def test_suspending_blocks_the_structure_staff(self):
        agent = staff_user(self.org, "agent")
        self.assertEqual(as_user(agent).get(f"/api/v1/staff/{self.org.pk}/dashboard/").status_code, 200)
        response = self.client.post(
            f"/api/v1/platform/organizations/{self.org.pk}/status/", {"status": "SUSPENDED"}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(as_user(agent).get(f"/api/v1/staff/{self.org.pk}/dashboard/").status_code, 403)
        self.assertTrue(AuditLog.objects.filter(organization=self.org, action="ORGANIZATION_STATUS_CHANGED").exists())

    def test_invalid_status_and_unknown_ids(self):
        bad = self.client.post(f"/api/v1/platform/organizations/{self.org.pk}/status/", {"status": "NOPE"}, format="json")
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(self.client.get("/api/v1/platform/organizations/99999/").status_code, 404)
        self.assertEqual(self.client.get("/api/v1/platform/organizations/abc/").status_code, 404)
        self.assertEqual(self.client.get("/api/v1/platform/organizations/abc/staff/").status_code, 404)

    # ------------------------------------------------------------------ comptes du personnel

    def test_create_responsable_with_temporary_password(self):
        response = self.client.post(
            f"/api/v1/platform/organizations/{self.org.pk}/staff/",
            {"email": "resp@test.com", "first_name": "Awa", "last_name": "Kodjo"}, format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.data["member"]["role"], "ADMIN")
        password = response.data["temporary_password"]
        self.assertTrue(password)
        user = User.objects.get(email="resp@test.com")
        self.assertTrue(user.must_change_password and user.check_password(password))
        self.assertTrue(AuditLog.objects.filter(organization=self.org, action="STAFF_CREATED", user=self.admin).exists())

    def test_existing_account_is_attached_without_new_password(self):
        make_user("deja")
        response = self.client.post(
            f"/api/v1/platform/organizations/{self.org.pk}/staff/",
            {"email": "deja@test.com", "first_name": "D", "last_name": "J"}, format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertIsNone(response.data["temporary_password"])
        self.assertEqual(response.data["member"]["role"], "ADMIN")

    def test_change_role_and_deactivate(self):
        agent = staff_user(self.org, "agent")
        membership = OrganizationStaff.objects.get(user=agent)
        url = f"/api/v1/platform/organizations/{self.org.pk}/staff/{membership.pk}/"
        response = self.client.patch(url, {"role": "ADMIN", "active": False}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        membership.refresh_from_db()
        self.assertEqual((membership.role, membership.active), ("ADMIN", False))
        self.assertTrue(AuditLog.objects.filter(organization=self.org, action="STAFF_UPDATED").exists())

    def test_staff_of_another_organization_is_not_reachable(self):
        other = make_org("Autre")
        agent = staff_user(other, "etranger")
        membership = OrganizationStaff.objects.get(user=agent)
        url = f"/api/v1/platform/organizations/{self.org.pk}/staff/{membership.pk}/"
        self.assertEqual(self.client.patch(url, {"active": False}, format="json").status_code, 404)

    def test_reset_password(self):
        agent = staff_user(self.org, "agent")
        membership = OrganizationStaff.objects.get(user=agent)
        response = self.client.post(f"/api/v1/platform/organizations/{self.org.pk}/staff/{membership.pk}/reset-password/")
        self.assertEqual(response.status_code, 200, response.content)
        agent.refresh_from_db()
        self.assertTrue(agent.must_change_password and agent.check_password(response.data["temporary_password"]))
        self.assertTrue(AuditLog.objects.filter(organization=self.org, action="STAFF_PASSWORD_RESET").exists())

    def test_platform_admin_password_cannot_be_reset(self):
        membership = OrganizationStaff.objects.create(user=self.admin, organization=self.org)
        response = self.client.post(f"/api/v1/platform/organizations/{self.org.pk}/staff/{membership.pk}/reset-password/")
        self.assertEqual(response.status_code, 409)

    # ------------------------------------------------------------------ journal et tableau de bord

    def test_audit_filters(self):
        other = make_org("Autre")
        self.client.post(f"/api/v1/platform/organizations/{other.pk}/status/", {"status": "SUSPENDED"}, format="json")
        self.client.patch(f"/api/v1/platform/organizations/{self.org.pk}/", {"city": "Kara"}, format="json")
        everything = self.client.get("/api/v1/platform/audit/").data
        self.assertEqual(everything["count"], 2)
        by_org = self.client.get(f"/api/v1/platform/audit/?organization={other.pk}").data
        self.assertEqual(by_org["count"], 1)
        self.assertEqual(by_org["results"][0]["action"], "ORGANIZATION_STATUS_CHANGED")
        self.assertEqual(by_org["results"][0]["user_email"], self.admin.email)
        self.assertEqual(self.client.get("/api/v1/platform/audit/?action=ORGANIZATION_UPDATED").data["count"], 1)
        self.assertEqual(self.client.get("/api/v1/platform/audit/?date_from=pas-une-date").data["count"], 0)

    def test_dashboard_lists_structures_without_responsable(self):
        data = self.client.get("/api/v1/platform/dashboard/").data
        self.assertEqual(data["organizations"]["active"], 1)
        self.assertEqual([o["name"] for o in data["without_admin"]], ["Biblio Centrale"])
        staff_user(self.org, "resp", OrganizationStaff.Role.ADMIN)
        self.assertEqual(self.client.get("/api/v1/platform/dashboard/").data["without_admin"], [])

    # ------------------------------------------------------------------ /auth/me

    def test_me_exposes_the_role(self):
        resp = staff_user(self.org, "resp", OrganizationStaff.Role.ADMIN)
        agent = staff_user(self.org, "agent")
        self.assertEqual(as_user(resp).get("/api/v1/auth/me/").data["staff_organizations"][0]["role"], "ADMIN")
        self.assertEqual(as_user(agent).get("/api/v1/auth/me/").data["staff_organizations"][0]["role"], "STAFF")
