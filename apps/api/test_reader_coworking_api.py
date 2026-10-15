from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.api.test_staff_read import make_org, make_user
from apps.coworking.models import Customer, Rate, Reservation, Resource, Zone
from apps.coworking import services


def when(days, hour=10):
    base = (timezone.localtime() + timedelta(days=days)).replace(
        hour=hour, minute=0, second=0, microsecond=0
    )
    return base


class ReaderCoworkingApiTests(TestCase):
    def setUp(self):
        self.org = make_org("Espace A", library_module=False)
        self.org.coworking_module = True
        self.org.save()
        self.library_only = make_org("Biblio seule")
        self.reader = make_user("lecteur-cw")
        self.other = make_user("autre-cw")
        for user, first in ((self.reader, "Léa"), (self.other, "Paul")):
            user.first_name, user.last_name = first, "Test"
            user.save()
        zone = Zone.objects.create(organization=self.org, name="Open space")
        self.desk = Resource.objects.create(
            organization=self.org, zone=zone, code="D1", name="Bureau 1",
            resource_type=Resource.Type.DESK,
        )
        Rate.objects.create(
            organization=self.org, resource_type=Resource.Type.DESK, unit=Rate.Unit.HOUR, amount=500
        )
        self.client = APIClient()
        self.client.force_authenticate(self.reader)

    def url(self, name, org=None, **kwargs):
        return reverse(f"api:{name}", kwargs={"organization_id": (org or self.org).pk, **kwargs})

    def register(self, client=None):
        response = (client or self.client).post(self.url("reader-cw-customer"), {}, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        return response.data

    def book(self, client=None, days=3, hours=2):
        start = when(days)
        return (client or self.client).post(
            self.url("reader-cw-reservations"),
            {
                "resource": str(self.desk.pk),
                "starts_at": start.isoformat(),
                "ends_at": (start + timedelta(hours=hours)).isoformat(),
                "notes": "Près de la fenêtre",
            },
            format="json",
        )

    # ---------------------------------------------------------------- accès

    def test_anonymous_refused(self):
        self.assertIn(APIClient().get(self.url("reader-cw-info")).status_code, (401, 403))

    def test_module_disabled_refused(self):
        self.assertEqual(self.client.get(self.url("reader-cw-info", org=self.library_only)).status_code, 403)

    def test_unknown_or_malformed_organization_refused(self):
        response = self.client.get("/api/v1/reader/coworking/abc/")
        self.assertEqual(response.status_code, 403)

    def test_password_change_required_refused(self):
        self.reader.must_change_password = True
        self.reader.save()
        self.assertEqual(self.client.get(self.url("reader-cw-info")).status_code, 403)

    # ---------------------------------------------------------------- consultation

    def test_info_without_customer(self):
        response = self.client.get(self.url("reader-cw-info"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["organization"]["name"], "Espace A")
        self.assertIsNone(response.data["my_customer"])
        self.assertIn("cancellation_deadline_hours", response.data)

    def test_resources_and_rates_are_listed(self):
        resources = self.client.get(self.url("reader-cw-resources")).data
        self.assertEqual([r["code"] for r in resources], ["D1"])
        self.assertNotIn("notes", resources[0])
        rates = self.client.get(self.url("reader-cw-rates")).data
        self.assertEqual(rates[0]["amount"], 500)

    def test_inactive_resource_hidden(self):
        Resource.objects.filter(pk=self.desk.pk).update(active=False)
        self.assertEqual(self.client.get(self.url("reader-cw-resources")).data, [])

    def test_availability_requires_dates_and_hides_busy_resource(self):
        self.assertEqual(self.client.get(self.url("reader-cw-availability")).status_code, 400)
        self.register()
        self.assertEqual(self.book().status_code, 201)
        start = when(3)
        params = {"start": start.isoformat(), "end": (start + timedelta(hours=1)).isoformat()}
        free = self.client.get(self.url("reader-cw-availability"), params)
        self.assertEqual(free.status_code, 200)
        self.assertEqual(free.data, [])

    # ---------------------------------------------------------------- fiche client

    def test_customer_404_then_created_from_account(self):
        self.assertEqual(self.client.get(self.url("reader-cw-customer")).status_code, 404)
        created = self.register()
        self.assertEqual(created["status"], "ACTIVE")
        self.assertEqual(created["email"], self.reader.email)
        self.assertEqual(self.client.get(self.url("reader-cw-customer")).status_code, 200)
        self.assertEqual(Customer.objects.filter(organization=self.org, user=self.reader).count(), 1)

    def test_customer_twice_refused(self):
        self.register()
        again = self.client.post(self.url("reader-cw-customer"), {}, format="json")
        self.assertEqual(again.status_code, 409)

    # ---------------------------------------------------------------- réservations

    def test_booking_requires_customer(self):
        self.assertEqual(self.book().status_code, 404)

    def test_booking_flow_and_amount(self):
        self.register()
        response = self.book()
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.data["status"], "CONFIRMED")
        self.assertEqual(response.data["quoted_amount"], 1000)
        self.assertEqual(response.data["notes"], "Près de la fenêtre")
        stored = Reservation.objects.get(pk=response.data["id"])
        self.assertEqual(stored.source, Reservation.Source.APP)
        mine = self.client.get(self.url("reader-cw-reservations"))
        self.assertEqual(mine.data["count"], 1)

    def test_booking_in_the_past_refused(self):
        self.register()
        self.assertEqual(self.book(days=-2).status_code, 409)

    def test_double_booking_refused(self):
        self.register()
        self.assertEqual(self.book().status_code, 201)
        self.assertEqual(self.book().status_code, 409)

    def test_reader_sees_only_own_reservations(self):
        self.register()
        reservation = self.book().data
        stranger = APIClient()
        stranger.force_authenticate(self.other)
        stranger.post(self.url("reader-cw-customer"), {}, format="json")
        self.assertEqual(stranger.get(self.url("reader-cw-reservations")).data["count"], 0)
        detail = stranger.get(self.url("reader-cw-reservation-detail", pk=reservation["id"]))
        self.assertEqual(detail.status_code, 404)
        cancel = stranger.post(
            self.url("reader-cw-reservation-cancel", pk=reservation["id"]),
            {"reason": "Test"}, format="json",
        )
        self.assertEqual(cancel.status_code, 404)

    def test_counter_notes_stay_internal(self):
        self.register()
        reservation = self.book(days=5).data
        Reservation.objects.filter(pk=reservation["id"]).update(
            source=Reservation.Source.COUNTER, notes="Note interne"
        )
        detail = self.client.get(self.url("reader-cw-reservation-detail", pk=reservation["id"]))
        self.assertEqual(detail.data["notes"], "")

    def test_cancel_needs_reason_and_works(self):
        self.register()
        reservation = self.book(days=7).data
        url = self.url("reader-cw-reservation-cancel", pk=reservation["id"])
        self.assertEqual(self.client.post(url, {}, format="json").status_code, 400)
        done = self.client.post(url, {"reason": "Changement de programme"}, format="json")
        self.assertEqual(done.status_code, 200, done.content)
        self.assertEqual(done.data["status"], "CANCELLED")

    def test_cancel_after_deadline_refused(self):
        self.register()
        reservation = self.book(days=7).data
        profile = services.get_profile(self.org)
        profile.cancellation_deadline_hours = 24 * 30
        profile.save()
        refused = self.client.post(
            self.url("reader-cw-reservation-cancel", pk=reservation["id"]),
            {"reason": "Trop tard"}, format="json",
        )
        self.assertEqual(refused.status_code, 409)

    # ---------------------------------------------------------------- historique

    def test_sessions_and_payments_start_empty(self):
        self.register()
        self.assertEqual(self.client.get(self.url("reader-cw-sessions")).data["count"], 0)
        self.assertEqual(self.client.get(self.url("reader-cw-payments")).data["count"], 0)

    def test_history_without_customer_is_empty(self):
        self.assertEqual(self.client.get(self.url("reader-cw-reservations")).data["count"], 0)
