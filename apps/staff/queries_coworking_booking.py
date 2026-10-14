"""Requêtes du suivi coworking (planning, réservations, historique des sessions). Toutes filtrées par structure."""
import datetime
from datetime import timedelta

from django.db.models import CharField, Exists, OuterRef, Q
from django.db.models.functions import Cast
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.audit.models import AuditLog
from apps.coworking.models import AccessSession, Reservation, Resource

from .queries_coworking import tokens

DEFAULT_WINDOW = (6, 22)


def parse_date(value):
    try:
        return datetime.date.fromisoformat((value or "").strip())
    except ValueError:
        return None


def local_day_bounds(day):
    start = timezone.make_aware(datetime.datetime.combine(day, datetime.time.min))
    return start, start + timedelta(days=1)


# ---------------------------------------------------------------- planning


def _window(profile, day, items):
    """(heure d'ouverture, heure de fermeture, fermé ?) : l'affichage s'élargit si une activité déborde."""
    first, last = DEFAULT_WINDOW
    closed = False
    if profile.opening_hours:
        opening = profile.parsed_opening_hours().get(day.weekday())
        if opening is None:
            closed = True
        else:
            first = opening[0].hour
            last = opening[1].hour + (1 if opening[1].minute else 0)
    for item in items:
        first = min(first, timezone.localtime(item["start"]).hour)
        end = timezone.localtime(item["end"])
        last = max(last, end.hour + (1 if end.minute or end.second else 0))
    return max(first, 0), min(max(last, first + 1), 24), closed


def planning(organization, profile, day, zone_id=None, resource_type=""):
    """Une ligne par ressource active : réservations et présences du jour, positionnées sur la journée."""
    now = timezone.now()
    day_start, day_end = local_day_bounds(day)
    resources = Resource.objects.filter(
        organization=organization, active=True, zone__active=True
    ).select_related("zone")
    if zone_id:
        resources = resources.filter(zone_id=zone_id)
    if resource_type:
        resources = resources.filter(resource_type=resource_type)
    resources = list(resources.order_by("zone__name", "code"))
    ids = [resource.pk for resource in resources]

    sessions = list(
        AccessSession.objects.filter(
            organization=organization, resource_id__in=ids, checked_in_at__lt=day_end
        )
        .filter(Q(checked_out_at__isnull=True) | Q(checked_out_at__gt=day_start))
        .select_related("customer")
    )
    # Une réservation déjà honorée est représentée par sa session (la présence réelle).
    honored = {session.reservation_id for session in sessions if session.reservation_id}
    reservations = [
        reservation
        for reservation in Reservation.objects.filter(
            organization=organization,
            resource_id__in=ids,
            starts_at__lt=day_end,
            ends_at__gt=day_start,
        )
        .exclude(status=Reservation.Status.CANCELLED)
        .select_related("customer")
        if reservation.pk not in honored
    ]

    by_resource = {resource.pk: [] for resource in resources}
    for reservation in reservations:
        by_resource[reservation.resource_id].append(
            {
                "kind": "reservation",
                "start": reservation.starts_at,
                "end": reservation.ends_at,
                "status": reservation.status.lower(),
                "status_label": reservation.get_status_display(),
                "label": f"{reservation.customer.first_name} {reservation.customer.last_name}",
                "open": False,
                "pk": reservation.pk,
            }
        )
    for session in sessions:
        end = session.checked_out_at or max(now, session.checked_in_at + timedelta(minutes=15))
        by_resource[session.resource_id].append(
            {
                "kind": "session",
                "start": session.checked_in_at,
                "end": end,
                "status": session.status.lower(),
                "status_label": "Présence en cours" if session.checked_out_at is None else "Présence terminée",
                "label": f"{session.customer.first_name} {session.customer.last_name}",
                "open": session.checked_out_at is None,
                "pk": session.pk,
            }
        )

    every_item = [item for items in by_resource.values() for item in items]
    for item in every_item:
        item["start"] = max(item["start"], day_start)
        item["end"] = min(item["end"], day_end)
    first, last, closed = _window(profile, day, every_item)
    origin = day_start + timedelta(hours=first)
    total = (last - first) * 60

    def percent(moment):
        return round(100 * (moment - origin).total_seconds() / 60 / total, 2)

    rows = []
    for resource in resources:
        items = sorted(by_resource[resource.pk], key=lambda item: item["start"])
        for item in items:
            left = percent(item["start"])
            item["left"] = left
            item["width"] = min(max(percent(item["end"]) - left, 1.5), 100 - left)
            local_start = timezone.localtime(item["start"]).strftime("%H:%M")
            local_end = "…" if item["open"] else timezone.localtime(item["end"]).strftime("%H:%M")
            item["time"] = f"{local_start}-{local_end}"
        rows.append({"resource": resource, "items": items})

    now_left = percent(now) if day_start <= now < day_end and origin <= now < origin + timedelta(minutes=total) else None
    return {
        "rows": rows,
        "hours": [{"label": f"{hour:02d}h", "left": round(100 * (hour - first) / (last - first), 2)} for hour in range(first, last)],
        "closed": closed,
        "now_left": now_left,
    }


# ---------------------------------------------------------------- réservations


def with_has_session(reservations):
    return reservations.annotate(
        has_session=Exists(AccessSession.objects.filter(reservation=OuterRef("pk")))
    )


def awaiting_no_show(organization, profile):
    """Réservations confirmées, sans arrivée, dont le délai de grâce est écoulé : l'absence est à décider."""
    limit = timezone.now() - timedelta(minutes=profile.no_show_grace_minutes)
    return with_has_session(
        Reservation.objects.filter(
            organization=organization, status=Reservation.Status.CONFIRMED, starts_at__lt=limit
        )
    ).filter(has_session=False)


def reservation_matches(organization, text):
    """Réservations dont le client (numéro, nom, téléphone) ou la ressource (code, nom) contient chaque mot."""
    queryset = Reservation.objects.filter(organization=organization)
    for token in tokens(text):
        queryset = queryset.filter(
            Q(customer__customer_number__icontains=token)
            | Q(customer__first_name__icontains=token)
            | Q(customer__last_name__icontains=token)
            | Q(customer__phone__icontains=token)
            | Q(resource__code__icontains=token)
            | Q(resource__name__icontains=token)
        )
    return queryset


# ---------------------------------------------------------------- sessions


def with_corrected(sessions):
    """Ajoute `corrected` : la session a au moins une correction dans le journal."""
    return sessions.annotate(
        corrected=Exists(
            AuditLog.objects.filter(
                organization=OuterRef("organization"),
                action=AuditLog.Action.SESSION_CORRECTED,
                entity_type="AccessSession",
                entity_id=Cast(OuterRef("pk"), output_field=CharField()),
            )
        )
    )


def session_matches(organization, text):
    queryset = AccessSession.objects.filter(organization=organization)
    for token in tokens(text):
        queryset = queryset.filter(
            Q(customer__customer_number__icontains=token)
            | Q(customer__first_name__icontains=token)
            | Q(customer__last_name__icontains=token)
            | Q(customer__phone__icontains=token)
            | Q(resource__code__icontains=token)
            | Q(resource__name__icontains=token)
        )
    return queryset


def _when(value):
    parsed = parse_datetime(value) if isinstance(value, str) else None
    return timezone.localtime(parsed) if parsed else None


def session_corrections(session):
    """Corrections de la session, de la plus récente à la plus ancienne (avant / après / motif / auteur)."""
    logs = AuditLog.objects.filter(
        organization=session.organization_id,
        action=AuditLog.Action.SESSION_CORRECTED,
        entity_type="AccessSession",
        entity_id=str(session.pk),
    ).select_related("user")
    result = []
    for log in logs:
        before, after = log.metadata.get("before", {}), log.metadata.get("after", {})
        result.append(
            {
                "at": log.created_at,
                "by": (log.user.get_full_name() or log.user.email) if log.user else "",
                "reason": log.metadata.get("reason", ""),
                "before": {
                    "checked_in_at": _when(before.get("checked_in_at")),
                    "checked_out_at": _when(before.get("checked_out_at")),
                    "amount": before.get("amount"),
                },
                "after": {
                    "checked_in_at": _when(after.get("checked_in_at")),
                    "checked_out_at": _when(after.get("checked_out_at")),
                    "amount": after.get("amount"),
                },
            }
        )
    return result
