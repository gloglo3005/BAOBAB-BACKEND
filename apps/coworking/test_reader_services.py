from datetime import timedelta

from django.core.exceptions import ValidationError
from django.test import TestCase

from apps.api.test_staff_read import make_org, make_user
from apps.audit.models import AuditLog
from apps.coworking import reader_services, services
from apps.coworking.models import Customer, Reservation
from apps.coworking.tests import CoworkingBase, future
from apps.organizations.models import Organization


class ReaderServiceTests(CoworkingBase, TestCase):
    def setUp(self):
        self.build()
        self.reader = make_user("lecteur")
        self.reader2 = make_user("lecteur2")

    def register(self, user=None, org=None):
        return reader_services.register_customer(
            organization=org or self.org, user=user or self.reader,
            first_name="Awa", last_name="Lecteur", phone="90000000",
        )

    def book(self, user=None, start=None, end=None, resource=None, org=None):
        start = start or future(5)
        return reader_services.create_own_reservation(
            organization=org or self.org, user=user or self.reader,
            resource_id=(resource or self.desk).pk, starts_at=start,
            ends_at=end or start + timedelta(hours=2),
        )

    def refused(self, message):
        return self.assertRaisesMessage(ValidationError, message)

    # ---------------------------------------------------------------- fiche client

    def test_register_creates_active_linked_customer_and_audit(self):
        customer = self.register()
        self.assertEqual(customer.user, self.reader)
        self.assertEqual(customer.status, Customer.Status.ACTIVE)
        self.assertEqual(customer.customer_number, "CLI-0003")
        log = AuditLog.objects.get(action=AuditLog.Action.CUSTOMER_CREATED, user=self.reader)
        self.assertEqual(log.metadata["source"], "APP")

    def test_register_twice_refused(self):
        self.register()
        with self.refused("Vous avez déjà une fiche client dans cette structure."):
            self.register()
        self.assertEqual(Customer.objects.filter(user=self.reader).count(), 1)

    def test_register_in_two_structures_gives_two_customers(self):
        self.register()
        self.register(org=self.other_org)
        self.assertEqual(Customer.objects.filter(user=self.reader).count(), 2)

    def test_register_refused_without_module_or_when_inactive(self):
        library_only = make_org("Biblio seule")
        with self.refused("Le module coworking n'est pas activé."):
            self.register(org=library_only)
        Organization.objects.filter(pk=self.org.pk).update(status=Organization.Status.SUSPENDED)
        with self.refused("Cette structure n'est pas active."):
            self.register()

    def test_register_refused_for_inactive_account(self):
        self.reader.is_active = False
        self.reader.save()
        with self.refused("Ce compte est désactivé."):
            self.register()

    def test_register_requires_names(self):
        with self.assertRaises(ValidationError):
            reader_services.register_customer(
                organization=self.org, user=self.reader, first_name=" ", last_name=""
            )
        self.assertFalse(Customer.objects.filter(user=self.reader).exists())

    # ---------------------------------------------------------------- réserver

    def test_reservation_requires_own_customer(self):
        with self.refused("Créez d'abord votre fiche client dans cette structure."):
            self.book()

    def test_own_reservation_is_confirmed_quoted_and_marked_app(self):
        customer = self.register()
        reservation = self.book()
        self.assertEqual(reservation.status, Reservation.Status.CONFIRMED)
        self.assertEqual(reservation.source, Reservation.Source.APP)
        self.assertEqual(reservation.quoted_amount, 1000)
        self.assertEqual(reservation.customer, customer)
        self.assertEqual(reservation.created_by, self.reader)
        log = AuditLog.objects.get(action=AuditLog.Action.RESERVATION_CREATED, user=self.reader)
        self.assertEqual(log.metadata["source"], "APP")

    def test_same_rules_and_messages_as_the_counter(self):
        self.register()
        start = future(5)
        services.create_reservation(
            organization=self.org, customer_id=self.customer.pk, resource_id=self.desk.pk,
            starts_at=start, ends_at=start + timedelta(hours=2), created_by=self.staff,
        )
        with self.refused("Cette ressource est déjà réservée sur cette période."):
            self.book(start=start + timedelta(minutes=30))
        with self.refused("Durée minimale : 30 minutes."):
            self.book(start=future(8), end=future(8, 10))
        with self.refused("Durée maximale : 480 minutes."):
            self.book(start=future(8), end=future(20))
        with self.refused("Le début est déjà passé."):
            self.book(start=future(-3), end=future(-1))

    def test_suspended_customer_refused(self):
        customer = self.register()
        customer.status = Customer.Status.SUSPENDED
        customer.save()
        with self.refused("Ce client est suspendu."):
            self.book()

    def test_upcoming_limit_applies_to_app_only(self):
        self.register()
        profile = services.get_profile(self.org)
        profile.max_upcoming_reservations = 2
        profile.save()
        self.book(start=future(5))
        self.book(start=future(8))
        with self.refused(
            "Limite atteinte : 2 réservations à venir au maximum. "
            "Annulez-en une ou passez à l'accueil."
        ):
            self.book(start=future(11))
        # À l'accueil, le staff n'est pas limité.
        customer = Customer.objects.get(user=self.reader)
        services.create_reservation(
            organization=self.org, customer_id=customer.pk, resource_id=self.desk.pk,
            starts_at=future(11), ends_at=future(13), created_by=self.staff,
        )
        # 0 = pas de limite.
        profile.max_upcoming_reservations = 0
        profile.save()
        self.book(start=future(14))

    def test_limit_frees_up_after_cancellation(self):
        self.register()
        profile = services.get_profile(self.org)
        profile.max_upcoming_reservations = 1
        profile.save()
        first = self.book(start=future(5))
        with self.assertRaises(ValidationError):
            self.book(start=future(8))
        reader_services.cancel_own_reservation(
            organization=self.org, user=self.reader, reservation_id=first.pk, reason="Imprévu"
        )
        self.book(start=future(8))

    # ---------------------------------------------------------------- annuler

    def test_cancel_own_reservation_with_reason_never_deletes(self):
        self.register()
        reservation = self.book()
        reader_services.cancel_own_reservation(
            organization=self.org, user=self.reader, reservation_id=reservation.pk,
            reason="Imprévu",
        )
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.CANCELLED)
        self.assertEqual(reservation.cancelled_by, self.reader)
        self.assertEqual(reservation.cancel_reason, "Imprévu")
        self.assertEqual(Reservation.objects.count(), 1)

    def test_cancel_requires_reason(self):
        self.register()
        reservation = self.book()
        with self.refused("Le motif de l'annulation est obligatoire."):
            reader_services.cancel_own_reservation(
                organization=self.org, user=self.reader, reservation_id=reservation.pk,
                reason=" ",
            )
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.CONFIRMED)

    def test_cancel_after_deadline_refused_but_staff_can(self):
        self.register()
        reservation = self.book(start=future(1))
        with self.refused(
            "Le délai d'annulation (2 h avant le début) est dépassé : contactez la structure."
        ):
            reader_services.cancel_own_reservation(
                organization=self.org, user=self.reader, reservation_id=reservation.pk,
                reason="Imprévu",
            )
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.CONFIRMED)
        services.cancel_reservation(
            organization=self.org, reservation_id=reservation.pk, performed_by=self.staff,
            reason="Demande du client",
        )
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.CANCELLED)

    def test_cannot_cancel_someone_elses_reservation(self):
        self.register()
        self.register(user=self.reader2)
        mine = self.book()
        by_staff = self.book_for_staff()
        for reader, target in ((self.reader2, mine), (self.reader, by_staff)):
            with self.refused("Réservation introuvable."):
                reader_services.cancel_own_reservation(
                    organization=self.org, user=reader, reservation_id=target.pk,
                    reason="Test",
                )
        mine.refresh_from_db()
        self.assertEqual(mine.status, Reservation.Status.CONFIRMED)

    def test_cannot_cancel_in_another_structure(self):
        self.register()
        reservation = self.book()
        with self.refused("Réservation introuvable."):
            reader_services.cancel_own_reservation(
                organization=self.other_org, user=self.reader, reservation_id=reservation.pk,
                reason="Test",
            )

    # ---------------------------------------------------------------- le staff ne change pas

    def book_for_staff(self):
        return super().book(start=future(20))

    def test_counter_reservations_stay_counter_and_staff_services_stay_staff_only(self):
        reservation = super().book(start=future(5))
        self.assertEqual(reservation.source, Reservation.Source.COUNTER)
        with self.refused("Cet agent n'est pas autorisé à gérer le coworking de cette structure."):
            services.create_reservation(
                organization=self.org, customer_id=self.customer.pk, resource_id=self.desk.pk,
                starts_at=future(8), ends_at=future(10), created_by=self.reader,
            )
        with self.refused("Cet agent n'est pas autorisé à gérer le coworking de cette structure."):
            services.cancel_reservation(
                organization=self.org, reservation_id=reservation.pk, performed_by=self.reader,
                reason="Test",
            )
