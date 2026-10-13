"""Règles métier du coworking.

Comme pour la bibliothèque : `organization` vient toujours du contexte du staff
connecté (jamais d'une valeur envoyée par le client), chaque opération est
atomique, verrouille les lignes concernées, et écrit dans le journal d'audit.
Un objet introuvable ou d'une autre structure donne le même message neutre
« ... introuvable » : on ne révèle jamais l'existence d'une ressource d'ailleurs.
"""
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.audit.services import log_action
from apps.organizations.models import Organization, OrganizationStaff

from .models import (
    AccessSession,
    CoworkingProfile,
    Customer,
    Payment,
    Reservation,
    Resource,
    Zone,
)
from .pricing import quote

CREATE_ACTIONS = {
    Zone: AuditLog.Action.ZONE_CREATED,
    Resource: AuditLog.Action.RESOURCE_CREATED,
    Customer: AuditLog.Action.CUSTOMER_CREATED,
}
UPDATE_ACTIONS = {
    CoworkingProfile: AuditLog.Action.COWORKING_PROFILE_UPDATED,
    Zone: AuditLog.Action.ZONE_UPDATED,
    Resource: AuditLog.Action.RESOURCE_UPDATED,
    Customer: AuditLog.Action.CUSTOMER_UPDATED,
}


# ---------------------------------------------------------------- outils


def get_profile(organization):
    profile, _ = CoworkingProfile.objects.get_or_create(organization=organization)
    return profile


def _locked_profile(organization):
    profile = get_profile(organization)
    return CoworkingProfile.objects.select_for_update().get(pk=profile.pk)


def _prepare(organization, user):
    """Structure active, module activé, agent rattaché : sinon refus.

    On relit la base : l'objet reçu peut être périmé.
    """
    organization = Organization.objects.get(pk=organization.pk)
    if organization.status != Organization.Status.ACTIVE:
        raise ValidationError("Cette structure n'est pas active.")
    if not organization.has_module(Organization.Module.COWORKING):
        raise ValidationError("Le module coworking n'est pas activé.")
    is_staff_here = OrganizationStaff.objects.filter(
        organization=organization, user=user, active=True, user__is_active=True
    ).exists()
    if not is_staff_here:
        raise ValidationError(
            "Cet agent n'est pas autorisé à gérer le coworking de cette structure."
        )
    return organization


def _lock(manager, label, **filters):
    try:
        return manager.select_for_update().get(**filters)
    except manager.model.DoesNotExist:
        raise ValidationError(f"{label} introuvable.")


def _aware(value):
    return timezone.make_aware(value) if timezone.is_naive(value) else value


def _next_number(profile, counter_field, existing_check, fmt):
    """Prochain numéro libre ; `profile` doit être verrouillé (select_for_update)."""
    while True:
        number = getattr(profile, counter_field)
        setattr(profile, counter_field, number + 1)
        code = fmt.format(number)
        if not existing_check(code):
            break
    profile.save(update_fields=[counter_field, "updated_at"])
    return code


def _integrity_refusal(message):
    return ValidationError(message)


def _reservation_conflicts(resource, starts_at, ends_at, exclude_pk=None):
    conflicts = Reservation.objects.filter(
        resource=resource,
        status=Reservation.Status.CONFIRMED,
        starts_at__lt=ends_at,
        ends_at__gt=starts_at,
    )
    if exclude_pk is not None:
        conflicts = conflicts.exclude(pk=exclude_pk)
    return conflicts


def _require_usable_resource(resource):
    if not resource.active or not Zone.objects.filter(pk=resource.zone_id, active=True).exists():
        raise ValidationError("Cette ressource n'est pas disponible.")


# ---------------------------------------------------------------- création et modification simples


@transaction.atomic
def save_object(*, organization, user, instance, created, metadata=None):
    """Enregistre une zone, ressource, client, tarif ou la fiche coworking, avec audit."""
    organization = _prepare(organization, user)
    if created:
        instance.organization = organization
        action = CREATE_ACTIONS.get(type(instance)) or _rate_action(created)
    else:
        if instance.organization_id != organization.pk:
            raise ValidationError(f"{type(instance)._meta.verbose_name.capitalize()} introuvable.")
        action = UPDATE_ACTIONS.get(type(instance)) or _rate_action(created)
    instance.full_clean()
    instance.save()
    verb = "créé" if created else "modifié"
    log_action(
        organization=organization,
        user=user,
        action=action,
        entity=instance,
        description=f"{type(instance)._meta.verbose_name.capitalize()} {verb} : {instance}.",
        metadata=metadata or {},
    )
    return instance


def _rate_action(created):
    return AuditLog.Action.RATE_CREATED if created else AuditLog.Action.RATE_UPDATED


@transaction.atomic
def create_customer(
    *, organization, first_name, last_name, performed_by, phone="", email="", user=None
):
    """Fiche client créée à l'accueil (le client n'a pas besoin de compte en ligne)."""
    organization = _prepare(organization, performed_by)
    profile = _locked_profile(organization)
    number = _next_number(
        profile,
        "next_customer_number",
        lambda code: Customer.objects.filter(organization=organization, customer_number=code).exists(),
        "CLI-{:04d}",
    )
    customer = Customer(
        organization=organization,
        user=user,
        customer_number=number,
        first_name=first_name,
        last_name=last_name,
        phone=phone,
        email=email,
    )
    customer.full_clean()
    customer.save()
    log_action(
        organization=organization,
        user=performed_by,
        action=AuditLog.Action.CUSTOMER_CREATED,
        entity=customer,
        description=f"Client {number} créé ({first_name} {last_name}).",
        metadata={"customer_number": number},
    )
    return customer


# ---------------------------------------------------------------- disponibilité


def available_resources(organization, starts_at, ends_at, resource_type=None):
    """Ressources actives libres sur toute la période (ni réservées, ni occupées)."""
    starts_at, ends_at = _aware(starts_at), _aware(ends_at)
    if ends_at <= starts_at:
        raise ValidationError("La fin doit être postérieure au début.")
    now = timezone.now()
    resources = Resource.objects.filter(organization=organization, active=True, zone__active=True)
    if resource_type:
        resources = resources.filter(resource_type=resource_type)
    busy = set(
        Reservation.objects.filter(
            organization=organization,
            status=Reservation.Status.CONFIRMED,
            starts_at__lt=ends_at,
            ends_at__gt=starts_at,
        ).values_list("resource_id", flat=True)
    )
    # Une session ouverte occupe la ressource jusqu'à sa sortie (inconnue) :
    # elle bloque toute période qui touche le présent.
    if starts_at <= now < ends_at:
        busy |= set(
            AccessSession.objects.filter(
                organization=organization, status=AccessSession.Status.OPEN
            ).values_list("resource_id", flat=True)
        )
    return resources.exclude(pk__in=busy).select_related("zone")


# ---------------------------------------------------------------- réservations


@transaction.atomic
def create_reservation(
    *, organization, customer_id, resource_id, starts_at, ends_at, created_by, notes=""
):
    organization = _prepare(organization, created_by)
    profile = get_profile(organization)
    if not profile.booking_enabled:
        raise ValidationError("Les réservations à l'avance ne sont pas activées.")

    starts_at, ends_at = _aware(starts_at), _aware(ends_at)
    now = timezone.now()
    if ends_at <= starts_at:
        raise ValidationError("La fin doit être postérieure au début.")
    if starts_at < now - timedelta(minutes=5):
        raise ValidationError(
            "Le début est déjà passé. Pour quelqu'un qui est là maintenant, "
            "utilisez l'accueil sans réservation (arrivée)."
        )
    minutes = (ends_at - starts_at).total_seconds() / 60
    if minutes < profile.min_booking_minutes:
        raise ValidationError(f"Durée minimale : {profile.min_booking_minutes} minutes.")
    if minutes > profile.max_booking_minutes:
        raise ValidationError(f"Durée maximale : {profile.max_booking_minutes} minutes.")
    if not profile.is_open_between(starts_at, ends_at):
        raise ValidationError("La période est en dehors des horaires d'ouverture.")

    resource = _lock(Resource.objects, "Ressource", pk=resource_id, organization=organization)
    _require_usable_resource(resource)
    customer = _lock(Customer.objects, "Client", pk=customer_id, organization=organization)
    if customer.status != Customer.Status.ACTIVE:
        raise ValidationError("Ce client est suspendu.")

    if _reservation_conflicts(resource, starts_at, ends_at).exists():
        raise ValidationError("Cette ressource est déjà réservée sur cette période.")
    occupied_now = AccessSession.objects.filter(
        resource=resource, status=AccessSession.Status.OPEN
    ).exists()
    if occupied_now and starts_at <= now + timedelta(minutes=profile.walk_in_buffer_minutes):
        raise ValidationError("Cette ressource est occupée en ce moment.")

    amount, rate = quote(resource, starts_at, ends_at)
    reservation = Reservation(
        organization=organization,
        customer=customer,
        resource=resource,
        starts_at=starts_at,
        ends_at=ends_at,
        rate=rate,
        quoted_amount=amount,
        notes=notes,
        created_by=created_by,
    )
    reservation.full_clean()
    try:
        with transaction.atomic():
            reservation.save()
    except IntegrityError:
        raise _integrity_refusal("Cette ressource est déjà réservée sur cette période.")
    log_action(
        organization=organization,
        user=created_by,
        action=AuditLog.Action.RESERVATION_CREATED,
        entity=reservation,
        description=f"Réservation de {resource.code} pour {customer.customer_number}.",
        metadata={
            "resource": resource.code,
            "customer": customer.customer_number,
            "starts_at": starts_at.isoformat(),
            "ends_at": ends_at.isoformat(),
            "quoted_amount": amount,
            "rate": rate.pk if rate else None,
        },
    )
    return reservation


@transaction.atomic
def cancel_reservation(*, organization, reservation_id, performed_by, reason=""):
    """Annule (sans jamais supprimer). Le staff peut annuler tard : on le note dans l'audit."""
    organization = _prepare(organization, performed_by)
    profile = get_profile(organization)
    reservation = _lock(
        Reservation.objects, "Réservation", pk=reservation_id, organization=organization
    )
    if reservation.status != Reservation.Status.CONFIRMED:
        raise ValidationError("Seule une réservation confirmée peut être annulée.")
    if reservation.sessions.exists():
        raise ValidationError("Une arrivée est déjà enregistrée sur cette réservation.")
    now = timezone.now()
    late = reservation.starts_at - now < timedelta(hours=profile.cancellation_deadline_hours)
    reservation.status = Reservation.Status.CANCELLED
    reservation.cancelled_at = now
    reservation.cancelled_by = performed_by
    reservation.cancel_reason = reason[:200]
    reservation.save(
        update_fields=["status", "cancelled_at", "cancelled_by", "cancel_reason", "updated_at"]
    )
    log_action(
        organization=organization,
        user=performed_by,
        action=AuditLog.Action.RESERVATION_CANCELLED,
        entity=reservation,
        description=f"Réservation {reservation.pk} annulée.",
        metadata={"late": late, "reason": reason, "quoted_amount": reservation.quoted_amount},
    )
    return reservation


@transaction.atomic
def mark_no_show(*, organization, reservation_id, performed_by):
    """Absence : possible seulement après le délai de grâce, sans arrivée enregistrée."""
    organization = _prepare(organization, performed_by)
    profile = get_profile(organization)
    reservation = _lock(
        Reservation.objects, "Réservation", pk=reservation_id, organization=organization
    )
    if reservation.status != Reservation.Status.CONFIRMED:
        raise ValidationError("Seule une réservation confirmée peut être marquée absente.")
    if reservation.sessions.exists():
        raise ValidationError("Une arrivée est déjà enregistrée sur cette réservation.")
    limit = reservation.starts_at + timedelta(minutes=profile.no_show_grace_minutes)
    if timezone.now() < limit:
        raise ValidationError(
            f"Le délai de grâce ({profile.no_show_grace_minutes} min) n'est pas écoulé."
        )
    reservation.status = Reservation.Status.NO_SHOW
    reservation.save(update_fields=["status", "updated_at"])
    log_action(
        organization=organization,
        user=performed_by,
        action=AuditLog.Action.RESERVATION_NO_SHOW,
        entity=reservation,
        description=f"Absence enregistrée pour la réservation {reservation.pk}.",
        metadata={"quoted_amount": reservation.quoted_amount},
    )
    return reservation


# ---------------------------------------------------------------- accueil : arrivée et sortie


@transaction.atomic
def check_in(
    *, organization, performed_by, resource_id=None, customer_id=None, reservation_id=None, notes=""
):
    """Arrivée d'un client : avec une réservation, ou sans (accueil sur place)."""
    organization = _prepare(organization, performed_by)
    profile = get_profile(organization)
    now = timezone.now()

    reservation = None
    if reservation_id is not None:
        reservation = _lock(
            Reservation.objects, "Réservation", pk=reservation_id, organization=organization
        )
        if reservation.status != Reservation.Status.CONFIRMED:
            raise ValidationError("Cette réservation n'est plus active.")
        if now < reservation.starts_at - timedelta(minutes=profile.check_in_early_minutes):
            raise ValidationError("Arrivée trop tôt pour cette réservation.")
        if now >= reservation.ends_at:
            raise ValidationError("Cette réservation est terminée.")
        customer_id = reservation.customer_id
        resource_id = resource_id or reservation.resource_id
        window_end = reservation.ends_at
    else:
        if not profile.walk_in_enabled:
            raise ValidationError("L'accueil sans réservation n'est pas activé.")
        if resource_id is None or customer_id is None:
            raise ValidationError("La ressource et le client sont obligatoires.")
        window_end = now + timedelta(minutes=profile.walk_in_buffer_minutes)

    resource = _lock(Resource.objects, "Ressource", pk=resource_id, organization=organization)
    _require_usable_resource(resource)
    customer = _lock(Customer.objects, "Client", pk=customer_id, organization=organization)
    if customer.status != Customer.Status.ACTIVE:
        raise ValidationError("Ce client est suspendu.")

    open_sessions = AccessSession.objects.filter(status=AccessSession.Status.OPEN)
    if open_sessions.filter(resource=resource).exists():
        raise ValidationError("Cette ressource est déjà occupée.")
    if open_sessions.filter(customer=customer).exists():
        raise ValidationError("Ce client a déjà une session ouverte.")
    other = _reservation_conflicts(
        resource, now, window_end, exclude_pk=reservation.pk if reservation else None
    )
    if other.exists():
        raise ValidationError(
            "Cette ressource est réservée par un autre client sur cette période."
        )

    session = AccessSession(
        organization=organization,
        customer=customer,
        resource=resource,
        reservation=reservation,
        checked_in_at=now,
        notes=notes,
        checked_in_by=performed_by,
    )
    session.full_clean()
    try:
        with transaction.atomic():
            session.save()
    except IntegrityError:
        raise _integrity_refusal("Cette ressource ou ce client a déjà une session ouverte.")
    log_action(
        organization=organization,
        user=performed_by,
        action=AuditLog.Action.SESSION_CHECKED_IN,
        entity=session,
        description=f"Arrivée de {customer.customer_number} sur {resource.code}.",
        metadata={
            "resource": resource.code,
            "customer": customer.customer_number,
            "reservation": reservation.pk if reservation else None,
        },
    )
    return session


@transaction.atomic
def check_out(*, organization, session_id, performed_by, notes=""):
    """Sortie : clôt la session et fige le montant dû."""
    organization = _prepare(organization, performed_by)
    session = _lock(AccessSession.objects, "Session", pk=session_id, organization=organization)
    if session.status != AccessSession.Status.OPEN:
        raise ValidationError("Cette session est déjà clôturée.")

    now = timezone.now()
    if session.reservation_id:
        reservation = Reservation.objects.select_for_update().get(pk=session.reservation_id)
        amount, rate = reservation.quoted_amount, reservation.rate
        reservation.status = Reservation.Status.COMPLETED
        reservation.save(update_fields=["status", "updated_at"])
    else:
        amount, rate = quote(session.resource, session.checked_in_at, now)

    session.checked_out_at = now
    session.checked_out_by = performed_by
    session.status = AccessSession.Status.CLOSED
    session.amount = amount
    session.rate = rate
    if notes:
        session.notes = (session.notes + "\n" + notes).strip()
    session.save()
    log_action(
        organization=organization,
        user=performed_by,
        action=AuditLog.Action.SESSION_CHECKED_OUT,
        entity=session,
        description=f"Sortie de {session.customer.customer_number} de {session.resource.code}.",
        metadata={
            "amount": amount,
            "rate": rate.pk if rate else None,
            "minutes": int((now - session.checked_in_at).total_seconds() // 60),
        },
    )
    return session


@transaction.atomic
def correct_session(
    *, organization, session_id, performed_by, reason, checked_in_at=None, checked_out_at=None, amount=None
):
    """Correction manuelle (oubli de sortie, erreur d'heure ou de prix) : motif obligatoire, auditée."""
    organization = _prepare(organization, performed_by)
    if not (reason or "").strip():
        raise ValidationError("Le motif de la correction est obligatoire.")
    session = _lock(AccessSession.objects, "Session", pk=session_id, organization=organization)
    if checked_in_at is None and checked_out_at is None and amount is None:
        raise ValidationError("Rien à corriger.")
    if checked_out_at is not None and session.status != AccessSession.Status.CLOSED:
        raise ValidationError("Clôturez d'abord la session (sortie) avant de corriger l'heure de sortie.")

    now = timezone.now()
    before = {
        "checked_in_at": session.checked_in_at.isoformat(),
        "checked_out_at": session.checked_out_at.isoformat() if session.checked_out_at else None,
        "amount": session.amount,
    }
    new_in = _aware(checked_in_at) if checked_in_at is not None else session.checked_in_at
    new_out = _aware(checked_out_at) if checked_out_at is not None else session.checked_out_at
    if new_in > now or (new_out is not None and new_out > now):
        raise ValidationError("Une heure corrigée ne peut pas être dans le futur.")
    if new_out is not None and new_out < new_in:
        raise ValidationError("La sortie doit suivre l'arrivée.")

    overlapping = AccessSession.objects.filter(
        organization=organization, resource=session.resource, checked_in_at__lt=new_out or now
    ).exclude(pk=session.pk)
    overlapping = overlapping.filter(Q(checked_out_at__isnull=True) | Q(checked_out_at__gt=new_in))
    if overlapping.exists():
        raise ValidationError("Cette correction chevauche une autre session de la même ressource.")

    session.checked_in_at = new_in
    session.checked_out_at = new_out
    if amount is not None:
        if amount < 0:
            raise ValidationError("Le montant ne peut pas être négatif.")
        session.amount = amount
    elif session.status == AccessSession.Status.CLOSED and not session.reservation_id:
        session.amount, session.rate = quote(session.resource, new_in, new_out)
    session.full_clean()
    session.save()
    log_action(
        organization=organization,
        user=performed_by,
        action=AuditLog.Action.SESSION_CORRECTED,
        entity=session,
        description=f"Session {session.pk} corrigée : {reason.strip()}",
        metadata={
            "reason": reason.strip(),
            "before": before,
            "after": {
                "checked_in_at": new_in.isoformat(),
                "checked_out_at": new_out.isoformat() if new_out else None,
                "amount": session.amount,
            },
        },
    )
    return session


# ---------------------------------------------------------------- paiements (saisie manuelle)


def amount_paid(*, reservation=None, session=None):
    target = {"reservation": reservation} if reservation else {"session": session}
    total = Payment.objects.filter(status=Payment.Status.RECORDED, **target).aggregate(
        total=Sum("amount")
    )["total"]
    return total or 0


@transaction.atomic
def record_payment(
    *,
    organization,
    customer_id,
    amount,
    performed_by,
    method=Payment.Method.CASH,
    reservation_id=None,
    session_id=None,
    paid_at=None,
    note="",
):
    """Enregistre un règlement reçu au comptoir. Un paiement ne dépasse jamais le solde."""
    organization = _prepare(organization, performed_by)
    if not isinstance(amount, int) or amount <= 0:
        raise ValidationError("Le montant doit être un nombre entier de francs, supérieur à 0.")
    if reservation_id is not None and session_id is not None:
        raise ValidationError("Un paiement concerne une réservation OU une session, pas les deux.")

    customer = _lock(Customer.objects, "Client", pk=customer_id, organization=organization)
    reservation = session = None
    due = None
    if reservation_id is not None:
        reservation = _lock(
            Reservation.objects, "Réservation", pk=reservation_id, organization=organization
        )
        if reservation.customer_id != customer.pk:
            raise ValidationError("Cette réservation n'appartient pas à ce client.")
        if reservation.status in (Reservation.Status.CANCELLED, Reservation.Status.NO_SHOW):
            raise ValidationError("Cette réservation est annulée ou marquée absente.")
        due = reservation.quoted_amount
    elif session_id is not None:
        session = _lock(AccessSession.objects, "Session", pk=session_id, organization=organization)
        if session.customer_id != customer.pk:
            raise ValidationError("Cette session n'appartient pas à ce client.")
        if session.status != AccessSession.Status.CLOSED:
            raise ValidationError("Clôturez la session (sortie) avant d'enregistrer son paiement.")
        due = session.amount
    if due is not None:
        remaining = due - amount_paid(reservation=reservation, session=session)
        if amount > remaining:
            raise ValidationError(
                f"Le montant dépasse le solde restant ({max(remaining, 0)} XOF)."
            )

    profile = _locked_profile(organization)
    receipt = _next_number(
        profile,
        "next_receipt_number",
        lambda code: Payment.objects.filter(organization=organization, receipt_number=code).exists(),
        "REC-{:06d}",
    )
    payment = Payment(
        organization=organization,
        customer=customer,
        reservation=reservation,
        session=session,
        receipt_number=receipt,
        amount=amount,
        method=method,
        paid_at=_aware(paid_at) if paid_at else timezone.now(),
        note=note,
        recorded_by=performed_by,
    )
    payment.full_clean()
    payment.save()
    log_action(
        organization=organization,
        user=performed_by,
        action=AuditLog.Action.PAYMENT_RECORDED,
        entity=payment,
        description=f"Paiement {receipt} de {amount} XOF ({customer.customer_number}).",
        metadata={
            "receipt": receipt,
            "amount": amount,
            "method": method,
            "reservation": reservation.pk if reservation else None,
            "session": session.pk if session else None,
        },
    )
    return payment


@transaction.atomic
def void_payment(*, organization, payment_id, performed_by, reason):
    """Annule un paiement (il reste visible dans l'historique) : motif obligatoire."""
    organization = _prepare(organization, performed_by)
    if not (reason or "").strip():
        raise ValidationError("Le motif de l'annulation est obligatoire.")
    payment = _lock(Payment.objects, "Paiement", pk=payment_id, organization=organization)
    if payment.status != Payment.Status.RECORDED:
        raise ValidationError("Ce paiement est déjà annulé.")
    payment.status = Payment.Status.VOIDED
    payment.voided_at = timezone.now()
    payment.voided_by = performed_by
    payment.void_reason = reason.strip()[:200]
    payment.save(update_fields=["status", "voided_at", "voided_by", "void_reason", "updated_at"])
    log_action(
        organization=organization,
        user=performed_by,
        action=AuditLog.Action.PAYMENT_VOIDED,
        entity=payment,
        description=f"Paiement {payment.receipt_number} annulé : {reason.strip()}",
        metadata={"receipt": payment.receipt_number, "amount": payment.amount, "reason": reason},
    )
    return payment


# ---------------------------------------------------------------- tableau de bord


def dashboard_stats(organization):
    now = timezone.now()
    local_now = timezone.localtime(now)
    day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    profile = get_profile(organization)

    resources = Resource.objects.filter(organization=organization, active=True, zone__active=True)
    open_sessions = AccessSession.objects.filter(
        organization=organization, status=AccessSession.Status.OPEN
    )
    reserved_now = Reservation.objects.filter(
        organization=organization, status=Reservation.Status.CONFIRMED,
        starts_at__lte=now, ends_at__gt=now,
    ).values_list("resource_id", flat=True)
    occupied_ids = set(open_sessions.values_list("resource_id", flat=True))
    reserved_ids = set(reserved_now) - occupied_ids
    total = resources.count()

    today_reservations = Reservation.objects.filter(
        organization=organization, starts_at__gte=day_start, starts_at__lt=day_end
    )
    by_status = {s: 0 for s in Reservation.Status.values}
    for row in today_reservations.values("status").annotate(n=Count("id")).order_by():
        by_status[row["status"]] = row["n"]
    grace_limit = now - timedelta(minutes=profile.no_show_grace_minutes)
    waiting_no_show = Reservation.objects.filter(
        organization=organization, status=Reservation.Status.CONFIRMED, starts_at__lt=grace_limit,
    ).exclude(sessions__isnull=False).count()
    revenue = Payment.objects.filter(
        organization=organization, status=Payment.Status.RECORDED,
        paid_at__gte=day_start, paid_at__lt=day_end,
    ).aggregate(total=Sum("amount"))["total"] or 0

    return {
        "resources": {
            "total": total,
            "occupied": len(occupied_ids),
            "reserved_now": len(reserved_ids),
            "free": max(total - len(occupied_ids | reserved_ids), 0),
        },
        "sessions": {
            "open": open_sessions.count(),
            "closed_today": AccessSession.objects.filter(
                organization=organization, status=AccessSession.Status.CLOSED,
                checked_out_at__gte=day_start, checked_out_at__lt=day_end,
            ).count(),
        },
        "reservations_today": by_status,
        "reservations_awaiting_no_show_decision": waiting_no_show,
        "revenue_today": {"amount": revenue, "currency": "XOF"},
    }
