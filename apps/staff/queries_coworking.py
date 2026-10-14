"""Requêtes de l'interface coworking (comptoir). Toutes sont filtrées par structure."""
from datetime import timedelta

from django.db.models import Count, Exists, F, IntegerField, OuterRef, Q, Subquery, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.coworking import services
from apps.coworking.models import AccessSession, Customer, Payment, Reservation

MIN_QUERY_LENGTH = 2


def tokens(text):
    return [part for part in (text or "").split() if part]


def customer_matches(organization, text):
    """Clients dont le numéro, le nom, le prénom, le téléphone ou l'email contient chaque mot."""
    queryset = Customer.objects.filter(organization=organization)
    for token in tokens(text):
        queryset = queryset.filter(
            Q(customer_number__icontains=token)
            | Q(first_name__icontains=token)
            | Q(last_name__icontains=token)
            | Q(phone__icontains=token)
            | Q(email__icontains=token)
        )
    return queryset


def with_open_session(customers):
    return customers.annotate(
        has_open_session=Exists(
            AccessSession.objects.filter(customer=OuterRef("pk"), status=AccessSession.Status.OPEN)
        )
    )


def with_balance(sessions):
    """Ajoute paid (payé sur la session + sur sa réservation) et balance (montant dû - payé).

    Seuls les paiements enregistrés comptent (un paiement annulé ne compte plus).
    """
    on_session = (
        Payment.objects.filter(session=OuterRef("pk"), status=Payment.Status.RECORDED)
        .order_by().values("session").annotate(total=Sum("amount")).values("total")
    )
    on_reservation = (
        Payment.objects.filter(reservation=OuterRef("reservation_id"), status=Payment.Status.RECORDED)
        .order_by().values("reservation").annotate(total=Sum("amount")).values("total")
    )
    return sessions.annotate(
        paid_on_session=Coalesce(Subquery(on_session, output_field=IntegerField()), 0),
        paid_on_reservation=Coalesce(Subquery(on_reservation, output_field=IntegerField()), 0),
    ).annotate(
        paid=F("paid_on_session") + F("paid_on_reservation"),
        balance=F("amount") - F("paid_on_session") - F("paid_on_reservation"),
    )


def unpaid_sessions(organization, customer=None):
    """Sessions terminées dont il reste quelque chose à payer."""
    sessions = AccessSession.objects.filter(
        organization=organization, status=AccessSession.Status.CLOSED
    )
    if customer is not None:
        sessions = sessions.filter(customer=customer)
    return with_balance(sessions).filter(balance__gt=0)


def unpaid_summary(organization):
    result = unpaid_sessions(organization).aggregate(count=Count("pk"), amount=Sum("balance"))
    return {"count": result["count"] or 0, "amount": result["amount"] or 0}


def reservations_to_welcome(organization, profile):
    """Réservations confirmées dont l'arrivée est possible maintenant (fenêtre d'arrivée ouverte)."""
    now = timezone.now()
    return (
        Reservation.objects.filter(
            organization=organization,
            status=Reservation.Status.CONFIRMED,
            starts_at__lte=now + timedelta(minutes=profile.check_in_early_minutes),
            ends_at__gt=now,
            sessions__isnull=True,
        )
        .select_related("customer", "resource")
        .order_by("starts_at")
    )


def walk_in_resources(organization, profile):
    """Ressources libres maintenant et pendant la marge d'accueil sans réservation."""
    now = timezone.now()
    return services.available_resources(
        organization, now, now + timedelta(minutes=max(profile.walk_in_buffer_minutes, 1))
    ).order_by("zone__name", "code")
