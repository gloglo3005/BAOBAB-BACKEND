"""Calcul du prix d'une période d'utilisation d'une ressource.

Règle (simple et expliquable au client) :
1. Si des tarifs existent pour la ressource elle-même, seuls ceux-là comptent ;
   sinon on prend les tarifs de son type (poste, salle...).
2. Seuls les tarifs actifs et valables à la date de début sont retenus.
3. Pour chaque tarif, le prix est « nombre d'unités entamées x montant »
   (heure = 60 min, demi-journée = 240 min, journée = 480 min).
4. Le client paie le moins cher de ces prix.
5. Aucun tarif applicable : la période est gratuite (montant 0, tarif vide).

Le résultat (montant + tarif retenu) est recopié sur la réservation ou la
session : modifier un tarif plus tard ne change jamais l'historique.
"""
import math

from django.db.models import Q
from django.utils import timezone

from .models import Rate

UNIT_MINUTES = {
    Rate.Unit.HOUR: 60,
    Rate.Unit.HALF_DAY: 240,
    Rate.Unit.DAY: 480,
}


def applicable_rates(resource, on_date):
    rates = Rate.objects.filter(organization=resource.organization_id, active=True).filter(
        Q(valid_from__isnull=True) | Q(valid_from__lte=on_date)
    ).filter(Q(valid_until__isnull=True) | Q(valid_until__gte=on_date))
    specific = list(rates.filter(resource=resource))
    if specific:
        return specific
    return list(rates.filter(resource__isnull=True, resource_type=resource.resource_type))


def duration_minutes(starts_at, ends_at):
    return max(1, math.ceil((ends_at - starts_at).total_seconds() / 60))


def quote(resource, starts_at, ends_at):
    """Retourne (montant en XOF, Rate retenu ou None)."""
    minutes = duration_minutes(starts_at, ends_at)
    on_date = timezone.localtime(starts_at).date()
    best_amount, best_rate = None, None
    for rate in applicable_rates(resource, on_date):
        amount = math.ceil(minutes / UNIT_MINUTES[rate.unit]) * rate.amount
        # À prix égal, on garde le tarif de la plus grande unité (plus simple à lire).
        if (
            best_amount is None
            or amount < best_amount
            or (amount == best_amount and UNIT_MINUTES[rate.unit] > UNIT_MINUTES[best_rate.unit])
        ):
            best_amount, best_rate = amount, rate
    if best_rate is None:
        return 0, None
    return best_amount, best_rate
