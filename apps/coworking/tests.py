import threading
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from apps.api.test_staff_read import make_org, make_user
from apps.audit.models import AuditLog
from apps.coworking import services
from apps.coworking.models import (
    AccessSession,
    Customer,
    Payment,
    Rate,
    Reservation,
    Resource,
    Zone,
)
from apps.organizations.models import OrganizationStaff


def future(hours=2, minutes=0):
    return timezone.now() + timedelta(hours=hours, minutes=minutes)


class CoworkingBase:
    def build(self):
        self.org = make_org("Cowork A", library_module=False)
        self.org.coworking_module = True
        self.org.save()
        self.other_org = make_org("Cowork B", library_module=False)
        self.other_org.coworking_module = True
        self.other_org.save()
        self.staff = make_user("cw-staff")
        OrganizationStaff.objects.create(user=self.staff, organization=self.org, active=True)
        self.zone = Zone.objects.create(organization=self.org, name="Open space")
        self.desk = Resource.objects.create(
            organization=self.org, zone=self.zone, code="D1", name="Bureau 1",
            resource_type=Resource.Type.DESK,
        )
        self.desk2 = Resource.objects.create(
            organization=self.org, zone=self.zone, code="D2", name="Bureau 2",
            resource_type=Resource.Type.DESK,
        )
        Rate.objects.create(
            organization=self.org, resource_type=Resource.Type.DESK,
            unit=Rate.Unit.HOUR, amount=500,
        )
        self.customer = services.create_customer(
            organization=self.org, performed_by=self.staff, first_name="Ada", last_name="Test"
        )
        self.customer2 = services.create_customer(
            organization=self.org, performed_by=self.staff, first_name="Bob", last_name="Test"
        )

    def book(self, resource=None, customer=None, start=None, end=None):
        start = start or future(2)
        return services.create_reservation(
            organization=self.org, customer_id=(customer or self.customer).pk,
            resource_id=(resource or self.desk).pk, starts_at=start,
            ends_at=end or start + timedelta(hours=2), created_by=self.staff,
        )


class ServiceTests(CoworkingBase, TestCase):
    def setUp(self):
        self.build()

    def test_customer_numbers_increment_and_audited(self):
        self.assertEqual(self.customer.customer_number, "CLI-0001")
        self.assertEqual(self.customer2.customer_number, "CLI-0002")
        self.assertTrue(
            AuditLog.objects.filter(action=AuditLog.Action.CUSTOMER_CREATED).count() >= 2
        )

    def test_reservation_quotes_price(self):
        reservation = self.book()
        self.assertEqual(reservation.quoted_amount, 1000)
        self.assertEqual(reservation.status, Reservation.Status.CONFIRMED)

    def test_overlap_refused(self):
        start = future(2)
        self.book(start=start)
        with self.assertRaises(ValidationError):
            self.book(customer=self.customer2, start=start + timedelta(minutes=30))
        # une autre ressource reste possible
        self.book(resource=self.desk2, customer=self.customer2, start=start)

    def test_back_to_back_allowed(self):
        start = future(2)
        self.book(start=start)
        self.book(customer=self.customer2, start=start + timedelta(hours=2))

    def test_duration_limits_and_past(self):
        with self.assertRaises(ValidationError):
            self.book(start=future(2), end=future(2, 10))  # trop court
        with self.assertRaises(ValidationError):
            self.book(start=future(2), end=future(12))  # trop long
        with self.assertRaises(ValidationError):
            self.book(start=future(-3), end=future(-1))  # passé

    def test_other_organization_is_invisible(self):
        stranger = make_user("stranger")
        OrganizationStaff.objects.create(user=stranger, organization=self.other_org, active=True)
        with self.assertRaises(ValidationError) as ctx:
            services.create_reservation(
                organization=self.other_org, customer_id=self.customer.pk,
                resource_id=self.desk.pk, starts_at=future(2), ends_at=future(4),
                created_by=stranger,
            )
        self.assertIn("introuvable", " ".join(ctx.exception.messages))

    def test_non_staff_refused(self):
        outsider = make_user("outsider")
        with self.assertRaises(ValidationError):
            services.create_customer(
                organization=self.org, performed_by=outsider, first_name="X", last_name="Y"
            )

    def test_module_disabled_refused(self):
        self.org.coworking_module = False
        self.org.save()
        with self.assertRaises(ValidationError):
            self.book()

    def test_cancel_then_slot_free(self):
        start = future(2)
        reservation = self.book(start=start)
        services.cancel_reservation(
            organization=self.org, reservation_id=reservation.pk, performed_by=self.staff,
            reason="Client indisponible",
        )
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.CANCELLED)
        self.book(customer=self.customer2, start=start)
        with self.assertRaises(ValidationError):
            services.cancel_reservation(
                organization=self.org, reservation_id=reservation.pk, performed_by=self.staff,
                reason="Client indisponible",
            )

    def test_cancel_requires_reason(self):
        reservation = self.book()
        for reason in (None, "", "   "):
            with self.assertRaisesMessage(ValidationError, "Le motif de l'annulation est obligatoire."):
                services.cancel_reservation(
                    organization=self.org, reservation_id=reservation.pk, performed_by=self.staff,
                    reason=reason,
                )
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.CONFIRMED)

    def test_no_show_only_after_grace(self):
        reservation = self.book()
        with self.assertRaises(ValidationError):
            services.mark_no_show(
                organization=self.org, reservation_id=reservation.pk, performed_by=self.staff
            )
        Reservation.objects.filter(pk=reservation.pk).update(
            starts_at=timezone.now() - timedelta(minutes=45),
            ends_at=timezone.now() + timedelta(minutes=75),
        )
        services.mark_no_show(
            organization=self.org, reservation_id=reservation.pk, performed_by=self.staff
        )
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.NO_SHOW)

    def test_walk_in_check_in_out_and_amount(self):
        session = services.check_in(
            organization=self.org, performed_by=self.staff,
            resource_id=self.desk.pk, customer_id=self.customer.pk,
        )
        self.assertEqual(session.status, AccessSession.Status.OPEN)
        AccessSession.objects.filter(pk=session.pk).update(
            checked_in_at=timezone.now() - timedelta(minutes=90)
        )
        closed = services.check_out(
            organization=self.org, session_id=session.pk, performed_by=self.staff
        )
        self.assertEqual(closed.status, AccessSession.Status.CLOSED)
        self.assertEqual(closed.amount, 1000)  # 2 h entamées x 500

    def test_check_in_conflicts(self):
        services.check_in(
            organization=self.org, performed_by=self.staff,
            resource_id=self.desk.pk, customer_id=self.customer.pk,
        )
        with self.assertRaises(ValidationError):  # ressource occupée
            services.check_in(
                organization=self.org, performed_by=self.staff,
                resource_id=self.desk.pk, customer_id=self.customer2.pk,
            )
        with self.assertRaises(ValidationError):  # client déjà présent
            services.check_in(
                organization=self.org, performed_by=self.staff,
                resource_id=self.desk2.pk, customer_id=self.customer.pk,
            )

    def test_walk_in_blocked_by_imminent_reservation(self):
        self.book(start=future(0, 10), end=future(2))
        with self.assertRaises(ValidationError):
            services.check_in(
                organization=self.org, performed_by=self.staff,
                resource_id=self.desk.pk, customer_id=self.customer2.pk,
            )

    def test_check_in_with_reservation_too_early(self):
        reservation = self.book(start=future(5))
        with self.assertRaises(ValidationError):
            services.check_in(
                organization=self.org, performed_by=self.staff, reservation_id=reservation.pk
            )

    def test_check_in_with_reservation_keeps_quoted_price(self):
        reservation = self.book(start=future(0, 5), end=future(2))
        session = services.check_in(
            organization=self.org, performed_by=self.staff, reservation_id=reservation.pk
        )
        closed = services.check_out(
            organization=self.org, session_id=session.pk, performed_by=self.staff
        )
        self.assertEqual(closed.amount, reservation.quoted_amount)
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.COMPLETED)

    def test_payment_rules(self):
        session = services.check_in(
            organization=self.org, performed_by=self.staff,
            resource_id=self.desk.pk, customer_id=self.customer.pk,
        )
        with self.assertRaises(ValidationError):  # session pas encore close
            services.record_payment(
                organization=self.org, performed_by=self.staff,
                customer_id=self.customer.pk, amount=100, session_id=session.pk,
            )
        AccessSession.objects.filter(pk=session.pk).update(
            checked_in_at=timezone.now() - timedelta(minutes=50)
        )
        closed = services.check_out(
            organization=self.org, session_id=session.pk, performed_by=self.staff
        )
        self.assertEqual(closed.amount, 500)
        first = services.record_payment(
            organization=self.org, performed_by=self.staff,
            customer_id=self.customer.pk, amount=300, session_id=session.pk,
        )
        self.assertEqual(first.receipt_number, "REC-000001")
        with self.assertRaises(ValidationError):  # dépasse le solde
            services.record_payment(
                organization=self.org, performed_by=self.staff,
                customer_id=self.customer.pk, amount=300, session_id=session.pk,
            )
        with self.assertRaises(ValidationError):  # montant non entier
            services.record_payment(
                organization=self.org, performed_by=self.staff,
                customer_id=self.customer.pk, amount=10.5, session_id=session.pk,
            )
        services.void_payment(
            organization=self.org, payment_id=first.pk, performed_by=self.staff, reason="Erreur"
        )
        self.assertEqual(services.amount_paid(session=closed), 0)
        with self.assertRaises(ValidationError):
            services.void_payment(
                organization=self.org, payment_id=first.pk, performed_by=self.staff, reason="Encore"
            )
        with self.assertRaises(ValidationError):  # motif obligatoire
            services.void_payment(
                organization=self.org, payment_id=first.pk, performed_by=self.staff, reason=" "
            )

    def test_correct_session_requires_reason_and_is_audited(self):
        session = services.check_in(
            organization=self.org, performed_by=self.staff,
            resource_id=self.desk.pk, customer_id=self.customer.pk,
        )
        services.check_out(organization=self.org, session_id=session.pk, performed_by=self.staff)
        with self.assertRaises(ValidationError):
            services.correct_session(
                organization=self.org, session_id=session.pk, performed_by=self.staff,
                reason="", amount=0,
            )
        corrected = services.correct_session(
            organization=self.org, session_id=session.pk, performed_by=self.staff,
            reason="Remise accordée", amount=200,
        )
        self.assertEqual(corrected.amount, 200)
        self.assertTrue(
            AuditLog.objects.filter(action=AuditLog.Action.SESSION_CORRECTED).exists()
        )
        with self.assertRaises(ValidationError):  # heure future
            services.correct_session(
                organization=self.org, session_id=session.pk, performed_by=self.staff,
                reason="Test", checked_out_at=future(3),
            )

    def test_available_resources(self):
        start = future(2)
        self.book(start=start)
        free = services.available_resources(self.org, start, start + timedelta(hours=1))
        self.assertEqual([r.code for r in free], ["D2"])

    def test_free_when_no_rate(self):
        Rate.objects.all().delete()
        reservation = self.book()
        self.assertEqual(reservation.quoted_amount, 0)

    def test_dashboard_counts(self):
        services.check_in(
            organization=self.org, performed_by=self.staff,
            resource_id=self.desk.pk, customer_id=self.customer.pk,
        )
        stats = services.dashboard_stats(self.org)
        self.assertEqual(stats["resources"]["total"], 2)
        self.assertEqual(stats["resources"]["occupied"], 1)
        self.assertEqual(stats["resources"]["free"], 1)
        self.assertEqual(stats["sessions"]["open"], 1)


class ConstraintTests(CoworkingBase, TestCase):
    def setUp(self):
        self.build()

    def test_database_refuses_overlapping_confirmed_reservations(self):
        start = future(2)
        self.book(start=start)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Reservation.objects.create(
                organization=self.org, customer=self.customer2, resource=self.desk,
                starts_at=start + timedelta(minutes=30), ends_at=start + timedelta(hours=3),
                quoted_amount=0, created_by=self.staff,
            )

    def test_cancelled_reservation_does_not_block(self):
        start = future(2)
        first = self.book(start=start)
        Reservation.objects.filter(pk=first.pk).update(status=Reservation.Status.CANCELLED)
        Reservation.objects.create(
            organization=self.org, customer=self.customer2, resource=self.desk,
            starts_at=start, ends_at=start + timedelta(hours=1), quoted_amount=0,
            created_by=self.staff,
        )

    def test_database_refuses_two_open_sessions_on_one_resource(self):
        AccessSession.objects.create(
            organization=self.org, customer=self.customer, resource=self.desk,
            checked_in_at=timezone.now(), checked_in_by=self.staff,
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            AccessSession.objects.create(
                organization=self.org, customer=self.customer2, resource=self.desk,
                checked_in_at=timezone.now(), checked_in_by=self.staff,
            )

    def test_database_refuses_two_open_sessions_for_one_customer(self):
        AccessSession.objects.create(
            organization=self.org, customer=self.customer, resource=self.desk,
            checked_in_at=timezone.now(), checked_in_by=self.staff,
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            AccessSession.objects.create(
                organization=self.org, customer=self.customer, resource=self.desk2,
                checked_in_at=timezone.now(), checked_in_by=self.staff,
            )

    def test_resource_code_unique_per_organization_case_insensitive(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Resource.objects.create(
                organization=self.org, zone=self.zone, code="d1", name="Doublon",
            )
        other_zone = Zone.objects.create(organization=self.other_org, name="Autre")
        Resource.objects.create(
            organization=self.other_org, zone=other_zone, code="D1", name="Autre D1"
        )


class ReservationConcurrencyTests(CoworkingBase, TransactionTestCase):
    def setUp(self):
        self.build()

    def test_two_simultaneous_bookings_one_wins(self):
        start = future(2)
        barrier = threading.Barrier(2)
        results = []

        def worker(customer):
            try:
                barrier.wait()
                services.create_reservation(
                    organization=self.org, customer_id=customer.pk, resource_id=self.desk.pk,
                    starts_at=start, ends_at=start + timedelta(hours=2), created_by=self.staff,
                )
                results.append("ok")
            except ValidationError:
                results.append("refused")
            finally:
                connection.close()

        threads = [
            threading.Thread(target=worker, args=(c,)) for c in (self.customer, self.customer2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(results), ["ok", "refused"])
        self.assertEqual(Reservation.objects.filter(resource=self.desk).count(), 1)

    def test_two_simultaneous_check_ins_one_wins(self):
        barrier = threading.Barrier(2)
        results = []

        def worker(customer):
            try:
                barrier.wait()
                services.check_in(
                    organization=self.org, performed_by=self.staff,
                    resource_id=self.desk.pk, customer_id=customer.pk,
                )
                results.append("ok")
            except ValidationError:
                results.append("refused")
            finally:
                connection.close()

        threads = [
            threading.Thread(target=worker, args=(c,)) for c in (self.customer, self.customer2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(results), ["ok", "refused"])
        self.assertEqual(AccessSession.objects.filter(status="OPEN").count(), 1)

    def test_receipt_numbers_unique_under_concurrency(self):
        session = services.check_in(
            organization=self.org, performed_by=self.staff,
            resource_id=self.desk.pk, customer_id=self.customer.pk,
        )
        services.check_out(organization=self.org, session_id=session.pk, performed_by=self.staff)
        AccessSession.objects.filter(pk=session.pk).update(amount=10000)
        barrier = threading.Barrier(4)

        def worker():
            try:
                barrier.wait()
                services.record_payment(
                    organization=self.org, performed_by=self.staff,
                    customer_id=self.customer.pk, amount=100, session_id=session.pk,
                )
            finally:
                connection.close()

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        numbers = list(Payment.objects.values_list("receipt_number", flat=True))
        self.assertEqual(len(numbers), 4)
        self.assertEqual(len(set(numbers)), 4)
        self.assertEqual(Customer.objects.filter(organization=self.org).count(), 2)
