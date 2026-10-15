"""Règles métier du coworking côté lecteur (application mobile).

Un lecteur agit seulement sur SA fiche client et SES réservations. Il passe par
les mêmes règles que le staff (apps.coworking.services) : mêmes contrôles, mêmes
messages. Seuls changent la vérification de l'appelant (compte actif au lieu
d'agent rattaché) et les limites propres à l'application : nombre de
réservations à venir, annulation seulement avant le délai prévu.
"""
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.organizations.models import Organization

from . import services
from .models import Customer, Reservation

ALREADY_CUSTOMER = "Vous avez déjà une fiche client dans cette structure."


def _prepare_reader(organization, user):
    """Compte actif, structure active avec le module coworking (relue en base)."""
    if not user.is_active:
        raise ValidationError("Ce compte est désactivé.")
    organization = Organization.objects.get(pk=organization.pk)
    if organization.status != Organization.Status.ACTIVE:
        raise ValidationError("Cette structure n'est pas active.")
    if not organization.has_module(Organization.Module.COWORKING):
        raise ValidationError("Le module coworking n'est pas activé.")
    return organization


def get_my_customer(organization, user):
    """La fiche client du lecteur dans cette structure, ou None."""
    return Customer.objects.filter(organization=organization, user=user).first()


@transaction.atomic
def register_customer(*, organization, user, first_name, last_name, phone="", email=""):
    """Le lecteur crée sa fiche client (active tout de suite) dans une structure."""
    organization = _prepare_reader(organization, user)
    if get_my_customer(organization, user) is not None:
        raise ValidationError(ALREADY_CUSTOMER)
    try:
        with transaction.atomic():
            return services._create_customer(
                organization,
                first_name=(first_name or "").strip(),
                last_name=(last_name or "").strip(),
                phone=(phone or "").strip(),
                email=(email or "").strip(),
                user=user,
                performed_by=user,
                metadata_extra={"source": Reservation.Source.APP},
            )
    except IntegrityError:
        # Deux demandes en même temps : la base garde une seule fiche par lecteur.
        raise ValidationError(ALREADY_CUSTOMER)


@transaction.atomic
def create_own_reservation(*, organization, user, resource_id, starts_at, ends_at, notes=""):
    """Réserve pour soi : confirmée tout de suite, avec les règles de l'accueil."""
    organization = _prepare_reader(organization, user)
    customer = get_my_customer(organization, user)
    if customer is None:
        raise ValidationError("Créez d'abord votre fiche client dans cette structure.")
    profile = services.get_profile(organization)
    return services._create_reservation(
        organization,
        customer_id=customer.pk,
        resource_id=resource_id,
        starts_at=starts_at,
        ends_at=ends_at,
        created_by=user,
        notes=notes,
        source=Reservation.Source.APP,
        max_upcoming=profile.max_upcoming_reservations,
    )


@transaction.atomic
def cancel_own_reservation(*, organization, user, reservation_id, reason):
    """Annule sa propre réservation, avant le délai d'annulation, avec un motif."""
    organization = _prepare_reader(organization, user)
    mine = Reservation.objects.filter(
        pk=reservation_id, organization=organization, customer__user=user
    ).exists()
    if not mine:
        # Même message pour « n'existe pas » et « appartient à quelqu'un d'autre ».
        raise ValidationError("Réservation introuvable.")
    return services._cancel_reservation(
        organization,
        reservation_id=reservation_id,
        performed_by=user,
        reason=reason,
        enforce_deadline=True,
    )
