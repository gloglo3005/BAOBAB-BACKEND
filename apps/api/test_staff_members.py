from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.audit.models import AuditLog
from apps.members.models import LinkCode, Member
from apps.organizations.models import OrganizationStaff

from .test_staff_read import make_org, make_user


class StaffMembersWriteTests(TestCase):
    def setUp(self):
        self.org_a = make_org("Biblio A")
        self.org_b = make_org("Biblio B")
        self.staff_a = make_user("staff-a")
        self.staff_b = make_user("staff-b")
        OrganizationStaff.objects.create(user=self.staff_a, organization=self.org_a, active=True)
        OrganizationStaff.objects.create(user=self.staff_b, organization=self.org_b, active=True)

        self.pending = self.make_member("MEM-0010", Member.Status.PENDING)
        self.accepted = self.make_member("MEM-0011", Member.Status.ACCEPTED)
        self.suspended = self.make_member("MEM-0012", Member.Status.SUSPENDED)
        self.member_b = self.make_member("MEM-0001", Member.Status.ACCEPTED, org=self.org_b)

        self.client = APIClient()
        self.client.force_authenticate(self.staff_a)

    def make_member(self, number, member_status, org=None, **extra):
        return Member.objects.create(
            organization=org or self.org_a, member_number=number,
            first_name="Prénom", last_name=f"Nom{number}", status=member_status, **extra,
        )

    def members_url(self, org=None):
        return reverse("api:staff-members", kwargs={"organization_id": (org or self.org_a).pk})

    def action_url(self, name, member, org=None):
        return reverse(
            f"api:{name}",
            kwargs={"organization_id": (org or self.org_a).pk, "pk": member.pk},
        )

    def post_action(self, name, member, org=None):
        return self.client.post(self.action_url(name, member, org), {}, format="json")

    # ------------------------------------------------------------ inscription

    def test_register_automatic_number(self):
        response = self.client.post(
            self.members_url(),
            {"first_name": "Ada", "last_name": "Lovelace", "phone": "90000000"},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertEqual(data["status"], "ACCEPTED")
        self.assertTrue(data["member_number"].startswith("MEM-"))
        self.assertEqual(data["active_loans"], 0)
        self.assertTrue(
            AuditLog.objects.filter(
                organization=self.org_a, action=AuditLog.Action.MEMBER_CREATED
            ).exists()
        )

    def test_register_manual_number(self):
        response = self.client.post(
            self.members_url(),
            {"first_name": "Ada", "last_name": "Lovelace", "member_number": "CARTE-77"},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["member_number"], "CARTE-77")

    def test_register_duplicate_number_is_400(self):
        response = self.client.post(
            self.members_url(),
            {"first_name": "Ada", "last_name": "Lovelace", "member_number": "MEM-0011"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Member.objects.filter(organization=self.org_a).count(), 3)

    def test_register_same_number_as_other_organization_is_allowed(self):
        response = self.client.post(
            self.members_url(),
            {"first_name": "Ada", "last_name": "Lovelace", "member_number": "MEM-0001"},
            format="json",
        )
        self.assertEqual(response.status_code, 201)

    def test_register_missing_fields_is_400(self):
        response = self.client.post(self.members_url(), {"first_name": "Ada"}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_register_other_organization_refused(self):
        self.client.force_authenticate(self.staff_b)
        response = self.client.post(
            self.members_url(self.org_a),
            {"first_name": "Ada", "last_name": "Lovelace"},
            format="json",
        )
        self.assertEqual(response.status_code, 403)

    def test_register_anonymous_refused(self):
        self.client.force_authenticate(None)
        response = self.client.post(
            self.members_url(), {"first_name": "Ada", "last_name": "Lovelace"}, format="json"
        )
        self.assertEqual(response.status_code, 401)

    def test_list_still_works(self):
        self.assertEqual(self.client.get(self.members_url()).status_code, 200)

    # ------------------------------------------------------------ statuts

    def test_accept_pending(self):
        response = self.post_action("staff-member-accept", self.pending)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ACCEPTED")
        self.pending.refresh_from_db()
        self.assertEqual(self.pending.status, Member.Status.ACCEPTED)
        self.assertIsNotNone(self.pending.joined_at)

    def test_reject_pending(self):
        response = self.post_action("staff-member-reject", self.pending)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "REJECTED")

    def test_suspend_accepted_is_audited(self):
        response = self.post_action("staff-member-suspend", self.accepted)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "SUSPENDED")
        self.assertTrue(
            AuditLog.objects.filter(
                organization=self.org_a, action=AuditLog.Action.MEMBER_SUSPENDED
            ).exists()
        )

    def test_reactivate_suspended(self):
        response = self.post_action("staff-member-reactivate", self.suspended)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ACCEPTED")

    def test_invalid_transition_is_409(self):
        self.assertEqual(self.post_action("staff-member-accept", self.accepted).status_code, 409)
        self.assertEqual(self.post_action("staff-member-reject", self.accepted).status_code, 409)
        self.assertEqual(self.post_action("staff-member-suspend", self.pending).status_code, 409)
        self.accepted.refresh_from_db()
        self.assertEqual(self.accepted.status, Member.Status.ACCEPTED)

    def test_status_change_member_of_other_organization_is_404(self):
        response = self.post_action("staff-member-suspend", self.member_b)
        self.assertEqual(response.status_code, 404)
        self.member_b.refresh_from_db()
        self.assertEqual(self.member_b.status, Member.Status.ACCEPTED)

    def test_status_change_other_organization_staff_refused(self):
        self.client.force_authenticate(self.staff_b)
        self.assertEqual(self.post_action("staff-member-suspend", self.accepted).status_code, 403)

    # ------------------------------------------------------------ code de rattachement

    def test_link_code_ok(self):
        response = self.post_action("staff-member-link-code", self.accepted)
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertEqual(len(data["code"]), 8)
        self.assertIn("expires_at", data)
        self.assertTrue(
            LinkCode.objects.filter(member=self.accepted, code=data["code"]).exists()
        )

    def test_link_code_new_one_invalidates_previous(self):
        self.post_action("staff-member-link-code", self.accepted)
        self.post_action("staff-member-link-code", self.accepted)
        active = LinkCode.objects.filter(
            member=self.accepted, used_at__isnull=True, expires_at__gt=timezone.now()
        )
        self.assertEqual(active.count(), 1)

    def test_link_code_pending_member_is_409(self):
        self.assertEqual(self.post_action("staff-member-link-code", self.pending).status_code, 409)

    def test_link_code_member_already_linked_is_409(self):
        linked = self.make_member("MEM-0020", Member.Status.ACCEPTED, user=make_user("lecteur"))
        self.assertEqual(self.post_action("staff-member-link-code", linked).status_code, 409)

    def test_link_code_member_of_other_organization_is_404(self):
        self.assertEqual(self.post_action("staff-member-link-code", self.member_b).status_code, 404)
        self.assertFalse(LinkCode.objects.filter(member=self.member_b).exists())