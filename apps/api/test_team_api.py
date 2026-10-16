from django.test import TestCase
from rest_framework.test import APIClient

from apps.api.test_platform_api import as_user, staff_user
from apps.api.test_staff_read import make_org, make_user
from apps.audit.models import AuditLog
from apps.organizations.models import LoanPolicy, OrganizationStaff

ADMIN = OrganizationStaff.Role.ADMIN


class TeamApiTests(TestCase):
    def setUp(self):
        self.org = make_org("Biblio Nord")
        self.other = make_org("Biblio Sud")
        self.resp = staff_user(self.org, "resp", ADMIN)
        self.agent = staff_user(self.org, "agent")
        self.foreign = staff_user(self.other, "etranger")
        self.client = as_user(self.resp)

    def url(self, path, org=None):
        return f"/api/v1/staff/{(org or self.org).pk}/{path}"

    def member(self, user):
        return OrganizationStaff.objects.get(user=user)

    # ------------------------------------------------------------------ accès

    def test_agent_cannot_use_admin_routes(self):
        agent = as_user(self.agent)
        for path in ("team/", "audit/"):
            self.assertEqual(agent.get(self.url(path)).status_code, 403, path)
        self.assertEqual(agent.post(self.url("team/"), {}, format="json").status_code, 403)

    def test_responsable_of_another_structure_refused(self):
        self.assertEqual(self.client.get(self.url("team/", self.other)).status_code, 403)
        self.assertEqual(self.client.get(self.url("audit/", self.other)).status_code, 403)

    # ------------------------------------------------------------------ équipe

    def test_list_team(self):
        emails = {m["email"] for m in self.client.get(self.url("team/")).data}
        self.assertEqual(emails, {"resp@test.com", "agent@test.com"})

    def test_create_agent(self):
        response = self.client.post(
            self.url("team/"), {"email": "nouveau@test.com", "first_name": "Kofi", "last_name": "Mensah"}, format="json"
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.data["member"]["role"], "STAFF")
        self.assertTrue(response.data["temporary_password"])
        self.assertTrue(AuditLog.objects.filter(organization=self.org, action="STAFF_CREATED", user=self.resp).exists())

    def test_cannot_take_over_an_existing_account(self):
        make_user("personne")
        for email in ("personne@test.com", "etranger@test.com", "PERSONNE@test.com"):
            response = self.client.post(
                self.url("team/"), {"email": email, "first_name": "X", "last_name": "Y"}, format="json"
            )
            self.assertEqual(response.status_code, 409, email)
        self.assertFalse(OrganizationStaff.objects.filter(organization=self.org, user__email="personne@test.com").exists())

    def test_deactivate_and_reactivate_agent(self):
        url = self.url(f"team/{self.member(self.agent).pk}/")
        self.assertEqual(self.client.patch(url, {"active": False}, format="json").status_code, 200)
        self.assertEqual(as_user(self.agent).get(self.url("dashboard/")).status_code, 403)
        self.assertEqual(self.client.patch(url, {"active": True}, format="json").status_code, 200)
        self.assertEqual(as_user(self.agent).get(self.url("dashboard/")).status_code, 200)

    def test_cannot_touch_self_other_responsable_or_foreign_staff(self):
        self.assertEqual(self.client.patch(self.url(f"team/{self.member(self.resp).pk}/"), {"active": False}, format="json").status_code, 409)
        second = staff_user(self.org, "resp2", ADMIN)
        self.assertEqual(self.client.patch(self.url(f"team/{self.member(second).pk}/"), {"active": False}, format="json").status_code, 403)
        self.assertEqual(self.client.patch(self.url(f"team/{self.member(self.foreign).pk}/"), {"active": False}, format="json").status_code, 404)
        self.assertEqual(self.client.patch(self.url("team/abc/"), {"active": False}, format="json").status_code, 404)

    def test_reset_agent_password(self):
        response = self.client.post(self.url(f"team/{self.member(self.agent).pk}/reset-password/"))
        self.assertEqual(response.status_code, 200, response.content)
        self.agent.refresh_from_db()
        self.assertTrue(self.agent.check_password(response.data["temporary_password"]))
        self.assertTrue(self.agent.must_change_password)

    def test_cannot_reset_account_shared_with_another_structure(self):
        OrganizationStaff.objects.create(user=self.agent, organization=self.other)
        response = self.client.post(self.url(f"team/{self.member_in(self.agent, self.org).pk}/reset-password/"))
        self.assertEqual(response.status_code, 403)

    def member_in(self, user, org):
        return OrganizationStaff.objects.get(user=user, organization=org)

    def test_cannot_reset_a_reader_account_attached_by_the_platform(self):
        reader = make_user("lecteur")
        from apps.members.models import Member
        Member.objects.create(organization=self.org, user=reader, first_name="L", last_name="R", member_number="M-1", status="ACTIVE")
        membership = OrganizationStaff.objects.create(user=reader, organization=self.org)
        response = self.client.post(self.url(f"team/{membership.pk}/reset-password/"))
        self.assertEqual(response.status_code, 403)

    # ------------------------------------------------------------------ structure et politique de prêt

    def test_organization_read_by_all_edit_by_responsable(self):
        self.assertEqual(as_user(self.agent).get(self.url("organization/")).status_code, 200)
        self.assertEqual(as_user(self.agent).patch(self.url("organization/"), {"city": "X"}, format="json").status_code, 403)
        response = self.client.patch(self.url("organization/"), {"city": "Kara", "name": "Piraté", "coworking_module": True}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.org.refresh_from_db()
        self.assertEqual((self.org.city, self.org.name, self.org.coworking_module), ("Kara", "Biblio Nord", False))
        self.assertTrue(AuditLog.objects.filter(organization=self.org, action="ORGANIZATION_UPDATED").exists())

    def test_loan_policy(self):
        LoanPolicy.objects.get_or_create(organization=self.org)
        self.assertEqual(as_user(self.agent).get(self.url("loan-policy/")).data["loan_duration_days"], 14)
        self.assertEqual(as_user(self.agent).patch(self.url("loan-policy/"), {"loan_duration_days": 7}, format="json").status_code, 403)
        ok = self.client.patch(self.url("loan-policy/"), {"loan_duration_days": 21, "max_renewals": 2, "allow_renewal": False}, format="json")
        self.assertEqual(ok.status_code, 200, ok.content)
        policy = LoanPolicy.objects.get(organization=self.org)
        self.assertEqual((policy.loan_duration_days, policy.max_renewals, policy.allow_renewal), (21, 2, False))
        self.assertTrue(AuditLog.objects.filter(organization=self.org, action="LOAN_POLICY_UPDATED").exists())

    def test_loan_policy_validation(self):
        for bad in ({"loan_duration_days": 0}, {"max_active_loans": 0}, {"loan_duration_days": 4000}, {"max_renewals": -1}):
            self.assertEqual(self.client.patch(self.url("loan-policy/"), bad, format="json").status_code, 400, bad)

    # ------------------------------------------------------------------ journal

    def test_audit_only_own_structure(self):
        self.client.post(self.url("team/"), {"email": "a@test.com", "first_name": "A", "last_name": "B"}, format="json")
        from apps.organizations.services import create_staff_account
        create_staff_account(organization=self.other, email="z@test.com", first_name="Z", last_name="Z")
        data = self.client.get(self.url("audit/")).data
        self.assertTrue(data["count"] >= 1)
        self.assertEqual({r["organization"] for r in data["results"]}, {self.org.pk})
        self.assertEqual(self.client.get(self.url("audit/?action=STAFF_CREATED")).data["count"], data["count"])
