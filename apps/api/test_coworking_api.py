from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.api.test_staff_read import make_org, make_user
from apps.audit.models import AuditLog
from apps.coworking.models import AccessSession, Customer, Rate, Resource, Zone
from apps.organizations.models import OrganizationStaff


_BASE = timezone.now().replace(second=0, microsecond=0)


def iso(hours, minutes=0):
    """Instants alignés sur une base fixe : 2 h exactes restent 2 h exactes."""
    return (_BASE + timedelta(hours=hours, minutes=minutes)).isoformat()


class CoworkingApiTests(TestCase):
    def setUp(self):
        self.org = make_org("Espace A", library_module=False)
        self.org.coworking_module = True
        self.org.save()
        self.other = make_org("Espace B", library_module=False)
        self.other.coworking_module = True
        self.other.save()
        self.library_only = make_org("Biblio seule")
        self.staff = make_user("cw-a")
        self.staff_other = make_user("cw-b")
        self.staff_lib = make_user("cw-lib")
        OrganizationStaff.objects.create(user=self.staff, organization=self.org, active=True)
        OrganizationStaff.objects.create(user=self.staff_other, organization=self.other, active=True)
        OrganizationStaff.objects.create(
            user=self.staff_lib, organization=self.library_only, active=True
        )
        self.client = APIClient()
        self.client.force_authenticate(self.staff)

    def url(self, name, org=None, **kwargs):
        return reverse(f"api:{name}", kwargs={"organization_id": (org or self.org).pk, **kwargs})

    def seed(self):
        zone = self.client.post(self.url("cw-zones"), {"name": "Open space"}, format="json")
        self.assertEqual(zone.status_code, 201, zone.content)
        res = self.client.post(
            self.url("cw-resources"),
            {"zone": zone.data["id"], "code": "D1", "name": "Bureau 1", "resource_type": "DESK"},
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.content)
        rate = self.client.post(
            self.url("cw-rates"),
            {"resource_type": "DESK", "unit": "HOUR", "amount": 500},
            format="json",
        )
        self.assertEqual(rate.status_code, 201, rate.content)
        cust = self.client.post(
            self.url("cw-customers"), {"first_name": "Ada", "last_name": "Test"}, format="json"
        )
        self.assertEqual(cust.status_code, 201, cust.content)
        return zone.data, res.data, cust.data

    def test_requires_authentication(self):
        anonymous = APIClient()
        self.assertIn(anonymous.get(self.url("cw-dashboard")).status_code, (401, 403))

    def test_other_organization_staff_refused(self):
        self.client.force_authenticate(self.staff_other)
        self.assertEqual(self.client.get(self.url("cw-dashboard")).status_code, 403)

    def test_module_disabled_refused(self):
        self.client.force_authenticate(self.staff_lib)
        response = self.client.get(self.url("cw-dashboard", org=self.library_only))
        self.assertEqual(response.status_code, 403)

    def test_full_flow(self):
        _, resource, customer = self.seed()
        self.assertEqual(customer["customer_number"], "CLI-0001")

        booked = self.client.post(
            self.url("cw-reservations"),
            {
                "customer": customer["id"], "resource": resource["id"],
                "starts_at": iso(2), "ends_at": iso(4),
            },
            format="json",
        )
        self.assertEqual(booked.status_code, 201, booked.content)
        self.assertEqual(booked.data["quoted_amount"], 1000)

        clash = self.client.post(
            self.url("cw-reservations"),
            {
                "customer": customer["id"], "resource": resource["id"],
                "starts_at": iso(3), "ends_at": iso(5),
            },
            format="json",
        )
        self.assertEqual(clash.status_code, 409)

        listing = self.client.get(self.url("cw-reservations"))
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.data["count"], 1)

        no_reason = self.client.post(self.url("cw-reservation-cancel", pk=booked.data["id"]), {}, format="json")
        self.assertEqual(no_reason.status_code, 409)
        self.assertEqual(no_reason.data["detail"], "Le motif de l'annulation est obligatoire.")

        cancelled = self.client.post(
            self.url("cw-reservation-cancel", pk=booked.data["id"]), {"reason": "Annulé"}, format="json"
        )
        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(cancelled.data["status"], "CANCELLED")

        # accueil sans réservation, sortie, paiement
        check_in = self.client.post(
            self.url("cw-check-in"),
            {"customer": customer["id"], "resource": resource["id"]},
            format="json",
        )
        self.assertEqual(check_in.status_code, 201, check_in.content)
        session_id = check_in.data["id"]
        AccessSession.objects.filter(pk=session_id).update(
            checked_in_at=timezone.now() - timedelta(minutes=40)
        )
        out = self.client.post(self.url("cw-check-out", pk=session_id), {}, format="json")
        self.assertEqual(out.status_code, 200, out.content)
        self.assertEqual(out.data["amount"], 500)
        self.assertEqual(out.data["balance"], 500)

        pay = self.client.post(
            self.url("cw-payments"),
            {"customer": customer["id"], "session": session_id, "amount": 500, "method": "CASH"},
            format="json",
        )
        self.assertEqual(pay.status_code, 201, pay.content)
        self.assertEqual(pay.data["receipt_number"], "REC-000001")
        over = self.client.post(
            self.url("cw-payments"),
            {"customer": customer["id"], "session": session_id, "amount": 1},
            format="json",
        )
        self.assertEqual(over.status_code, 409)

        void = self.client.post(
            self.url("cw-payment-void", pk=pay.data["id"]), {"reason": "Erreur de saisie"}, format="json"
        )
        self.assertEqual(void.status_code, 200)
        self.assertEqual(void.data["status"], "VOIDED")
        no_reason = self.client.post(
            self.url("cw-payment-void", pk=pay.data["id"]), {}, format="json"
        )
        self.assertEqual(no_reason.status_code, 400)

        detail = self.client.get(self.url("cw-session-detail", pk=session_id))
        self.assertEqual(detail.data["amount_paid"], 0)

        dashboard = self.client.get(self.url("cw-dashboard"))
        self.assertEqual(dashboard.status_code, 200)
        self.assertEqual(dashboard.data["sessions"]["open"], 0)

        actions = set(AuditLog.objects.values_list("action", flat=True))
        for expected in (
            "ZONE_CREATED", "RESOURCE_CREATED", "RATE_CREATED", "CUSTOMER_CREATED",
            "RESERVATION_CREATED", "RESERVATION_CANCELLED", "SESSION_CHECKED_IN",
            "SESSION_CHECKED_OUT", "PAYMENT_RECORDED", "PAYMENT_VOIDED",
        ):
            self.assertIn(expected, actions)

    def test_isolation_between_organizations(self):
        _, resource, customer = self.seed()
        self.client.force_authenticate(self.staff_other)
        # liste vide, détail introuvable, création avec les identifiants d'ailleurs refusée
        self.assertEqual(self.client.get(self.url("cw-customers", org=self.other)).data["count"], 0)
        self.assertEqual(
            self.client.get(self.url("cw-customer-detail", org=self.other, pk=customer["id"])).status_code,
            404,
        )
        response = self.client.post(
            self.url("cw-reservations", org=self.other),
            {
                "customer": customer["id"], "resource": resource["id"],
                "starts_at": iso(2), "ends_at": iso(4),
            },
            format="json",
        )
        self.assertEqual(response.status_code, 404)
        # une ressource d'une autre structure n'est pas acceptée comme zone
        response = self.client.post(
            self.url("cw-resources", org=self.other),
            {"zone": resource["zone"], "code": "X1", "name": "X", "resource_type": "DESK"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        # le client d'une autre structure ne peut pas être modifié
        patch = self.client.patch(
            self.url("cw-customer-detail", org=self.other, pk=customer["id"]),
            {"first_name": "Pirate"}, format="json",
        )
        self.assertEqual(patch.status_code, 404)
        self.assertEqual(Customer.objects.get(pk=customer["id"]).first_name, "Ada")

    def test_cross_organization_url_forbidden(self):
        # le staff de A ne peut pas lire B en changeant l'identifiant dans l'URL
        response = self.client.get(self.url("cw-customers", org=self.other))
        self.assertEqual(response.status_code, 403)

    def test_patch_is_audited_and_no_delete(self):
        zone, resource, _ = self.seed()
        patch = self.client.patch(
            self.url("cw-resource-detail", pk=resource["id"]), {"name": "Bureau A"}, format="json"
        )
        self.assertEqual(patch.status_code, 200)
        self.assertTrue(AuditLog.objects.filter(action="RESOURCE_UPDATED").exists())
        delete = self.client.delete(self.url("cw-resource-detail", pk=resource["id"]))
        self.assertEqual(delete.status_code, 405)
        self.assertTrue(Resource.objects.filter(pk=resource["id"]).exists())

    def test_profile_get_and_patch(self):
        self.assertEqual(self.client.get(self.url("cw-profile")).status_code, 200)
        patch = self.client.patch(
            self.url("cw-profile"),
            {"opening_hours": {"mon": ["08:00", "18:00"]}, "no_show_grace_minutes": 20},
            format="json",
        )
        self.assertEqual(patch.status_code, 200, patch.content)
        bad = self.client.patch(
            self.url("cw-profile"), {"opening_hours": {"mon": ["18:00", "08:00"]}}, format="json"
        )
        self.assertEqual(bad.status_code, 400)

    def test_availability(self):
        _, resource, customer = self.seed()
        self.client.post(
            self.url("cw-reservations"),
            {
                "customer": customer["id"], "resource": resource["id"],
                "starts_at": iso(2), "ends_at": iso(4),
            },
            format="json",
        )
        busy = self.client.get(self.url("cw-availability"), {"start": iso(2, 10), "end": iso(3)})
        self.assertEqual(busy.status_code, 200)
        self.assertEqual(busy.data, [])
        free = self.client.get(self.url("cw-availability"), {"start": iso(5), "end": iso(6)})
        self.assertEqual(len(free.data), 1)
        self.assertEqual(self.client.get(self.url("cw-availability")).status_code, 400)

    def test_invalid_identifiers_do_not_crash(self):
        for name in ("cw-reservation-detail", "cw-session-detail", "cw-payment-detail"):
            self.assertEqual(self.client.get(self.url(name, pk="abc")).status_code, 404)
        bad = self.client.post(self.url("cw-reservation-cancel", pk="abc"), {}, format="json")
        self.assertEqual(bad.status_code, 404)
        listing = self.client.get(self.url("cw-reservations"), {"resource": "abc", "date": "pas-une-date"})
        self.assertEqual(listing.status_code, 200)

    def test_rate_requires_exactly_one_target(self):
        both = self.client.post(
            self.url("cw-rates"), {"unit": "HOUR", "amount": 100}, format="json"
        )
        self.assertEqual(both.status_code, 400)
        self.assertEqual(Rate.objects.count(), 0)
        self.assertEqual(Zone.objects.count(), 0)
