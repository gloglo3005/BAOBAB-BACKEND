from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.audit.models import AuditLog
from apps.catalog.models import Book, BookCopy
from apps.circulation.models import Loan
from apps.circulation.services import checkout_copy
from apps.members.models import Member
from apps.organizations.models import LoanPolicy, OrganizationStaff

from .test_staff_read import make_org, make_user


class BaseCase(TestCase):
    def setUp(self):
        self.org = make_org("Biblio A")
        self.other_org = make_org("Biblio B")
        self.agent = make_user("agent")
        self.other_agent = make_user("agent-b")
        OrganizationStaff.objects.create(user=self.agent, organization=self.org)
        OrganizationStaff.objects.create(user=self.other_agent, organization=self.other_org)
        self.member = Member.objects.create(
            organization=self.org, member_number="MEM-0001", first_name="Ada",
            last_name="Test", phone="90000000", status=Member.Status.ACCEPTED,
        )
        self.client = APIClient()
        self.client.force_authenticate(self.agent)

    def url(self, name, org=None, **kwargs):
        return reverse(f"api:{name}", kwargs={"organization_id": (org or self.org).pk, **kwargs})


class LoanRenewalTests(BaseCase):
    def setUp(self):
        super().setUp()
        book = Book.objects.create(organization=self.org, title="Une vie de boy")
        copy = BookCopy.objects.create(organization=self.org, book=book, inventory_code="A-001")
        self.loan = checkout_copy(
            organization=self.org, book_copy_id=copy.pk, member_id=self.member.pk,
            issued_by=self.agent,
        )
        self.renew_url = self.url("staff-loan-renew", pk=self.loan.pk)

    def test_renouvellement_prolonge_lecheance_et_ecrit_laudit(self):
        before = self.loan.due_date
        response = self.client.post(self.renew_url)
        self.assertEqual(response.status_code, 200, response.content)
        self.loan.refresh_from_db()
        self.assertEqual(self.loan.due_date, before + timedelta(days=14))
        self.assertEqual(self.loan.renewal_count, 1)
        self.assertEqual(response.data["renewal_count"], 1)
        log = AuditLog.objects.get(action="LOAN_RENEWED")
        self.assertEqual(log.user, self.agent)
        self.assertEqual(log.organization, self.org)

    def test_la_duree_vient_de_la_politique(self):
        LoanPolicy.objects.filter(organization=self.org).update(loan_duration_days=7, max_renewals=3)
        before = self.loan.due_date
        self.client.post(self.renew_url)
        self.client.post(self.renew_url)
        self.loan.refresh_from_db()
        self.assertEqual(self.loan.due_date, before + timedelta(days=14))
        self.assertEqual(self.loan.renewal_count, 2)

    def test_limite_de_renouvellements(self):
        self.assertEqual(self.client.post(self.renew_url).status_code, 200)
        response = self.client.post(self.renew_url)
        self.assertEqual(response.status_code, 409)
        self.assertIn("Limite", response.data["detail"])
        self.assertEqual(AuditLog.objects.filter(action="LOAN_RENEWED").count(), 1)

    def test_renouvellement_desactive(self):
        LoanPolicy.objects.filter(organization=self.org).update(allow_renewal=False)
        self.assertEqual(self.client.post(self.renew_url).status_code, 409)
        self.loan.refresh_from_db()
        self.assertEqual(self.loan.renewal_count, 0)

    def test_pret_en_retard_refuse(self):
        Loan.objects.filter(pk=self.loan.pk).update(due_date=timezone.localdate() - timedelta(days=2))
        response = self.client.post(self.renew_url)
        self.assertEqual(response.status_code, 409)
        self.assertIn("retard", response.data["detail"])

    def test_membre_suspendu_refuse(self):
        Member.objects.filter(pk=self.member.pk).update(status=Member.Status.SUSPENDED)
        self.assertEqual(self.client.post(self.renew_url).status_code, 409)

    def test_pret_clos_refuse(self):
        Loan.objects.filter(pk=self.loan.pk).update(
            status=Loan.Status.RETURNED, returned_at=timezone.now()
        )
        self.assertEqual(self.client.post(self.renew_url).status_code, 409)

    def test_acces_et_isolation(self):
        other = APIClient()
        other.force_authenticate(self.other_agent)
        self.assertEqual(other.post(self.url("staff-loan-renew", org=self.other_org, pk=self.loan.pk)).status_code, 404)
        self.assertEqual(other.post(self.renew_url).status_code, 403)
        self.assertEqual(APIClient().post(self.renew_url).status_code, 401)
        self.assertEqual(self.client.post(self.url("staff-loan-renew", pk="abc")).status_code, 404)


class MemberUpdateTests(BaseCase):
    def detail(self, org=None, pk=None):
        return self.url("staff-member-detail", org=org, pk=pk or self.member.pk)

    def test_modification_des_informations(self):
        response = self.client.patch(
            self.detail(), {"first_name": " Adèle ", "phone": "91111111"}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.data["first_name"], "Adèle")
        self.member.refresh_from_db()
        self.assertEqual((self.member.first_name, self.member.phone, self.member.last_name), ("Adèle", "91111111", "Test"))
        log = AuditLog.objects.get(action="MEMBER_UPDATED")
        self.assertEqual(log.user, self.agent)
        self.assertEqual(log.metadata["phone"]["après"], "91111111")

    def test_changement_du_numero_de_membre(self):
        response = self.client.patch(self.detail(), {"member_number": "CARTE-77"}, format="json")
        self.assertEqual(response.status_code, 200)
        Member.objects.create(organization=self.org, member_number="MEM-0002", first_name="B", last_name="C")
        taken = self.client.patch(self.detail(), {"member_number": "MEM-0002"}, format="json")
        self.assertEqual(taken.status_code, 400)
        self.assertIn("member_number", taken.data)
        self.assertEqual(self.client.patch(self.detail(), {"member_number": "  "}, format="json").status_code, 400)

    def test_meme_numero_dans_une_autre_structure_est_permis(self):
        Member.objects.create(organization=self.other_org, member_number="X-1", first_name="B", last_name="C")
        self.assertEqual(self.client.patch(self.detail(), {"member_number": "X-1"}, format="json").status_code, 200)

    def test_rien_ne_change_rien_nest_journalise(self):
        response = self.client.patch(self.detail(), {"first_name": "Ada"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(AuditLog.objects.filter(action="MEMBER_UPDATED").exists())

    def test_champs_invalides_et_statut_ignore(self):
        self.assertEqual(self.client.patch(self.detail(), {"first_name": ""}, format="json").status_code, 400)
        response = self.client.patch(self.detail(), {"status": "SUSPENDED", "last_name": "Nouveau"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.member.refresh_from_db()
        self.assertEqual(self.member.status, Member.Status.ACCEPTED)
        self.assertEqual(self.member.last_name, "Nouveau")

    def test_lecture_toujours_possible_et_isolation(self):
        self.assertEqual(self.client.get(self.detail()).status_code, 200)
        other = APIClient()
        other.force_authenticate(self.other_agent)
        self.assertEqual(other.patch(self.detail(org=self.other_org), {"first_name": "X"}, format="json").status_code, 404)
        self.assertEqual(other.patch(self.detail(), {"first_name": "X"}, format="json").status_code, 403)
        self.assertEqual(self.client.patch(self.detail(pk="abc"), {"first_name": "X"}, format="json").status_code, 404)
