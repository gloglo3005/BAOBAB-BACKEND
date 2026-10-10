import secrets
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.audit.services import log_action
from apps.organizations.models import Organization

from .models import LinkCode, Member

LINK_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # sans 0, O, 1, I
LINK_CODE_LENGTH = 8
LINK_CODE_VALIDITY_DAYS = 7

ALLOWED_TRANSITIONS = {
    Member.Status.PENDING: {Member.Status.ACCEPTED, Member.Status.REJECTED},
    Member.Status.ACCEPTED: {Member.Status.SUSPENDED},
    Member.Status.SUSPENDED: {Member.Status.ACCEPTED},
    Member.Status.REJECTED: {Member.Status.ACCEPTED},
}


def generate_member_number(organization):
    """Retourne le prochain numéro libre (MEM-0001, MEM-0002...).

    À appeler DANS une transaction : la ligne de la structure est
    verrouillée jusqu'à la fin de la transaction, ce qui empêche deux
    inscriptions simultanées de recevoir le même numéro.
    """
    locked = Organization.objects.select_for_update().get(pk=organization.pk)

    while True:
        number = locked.next_member_number
        locked.next_member_number = number + 1
        code = f"MEM-{number:04d}"
        # On saute les numéros déjà saisis à la main (cartes existantes, import).
        if not Member.objects.filter(organization=locked, member_number=code).exists():
            break

    locked.save(update_fields=["next_member_number", "updated_at"])
    return code


def _log_member_created(member, user, *, origin):
    log_action(
        organization=member.organization,
        user=user,
        action=AuditLog.Action.MEMBER_CREATED,
        entity=member,
        description=f"Membre {member.member_number} créé ({member.first_name} {member.last_name}).",
        metadata={
            "member_number": member.member_number,
            "status": member.status,
            "origin": origin,
        },
    )


@transaction.atomic
def register_member(
    *, organization, first_name, last_name, phone="", member_number=None, performed_by=None
):
    """Inscription au comptoir : membre accepté tout de suite, sans compte."""
    if member_number and member_number.strip():
        number = member_number.strip()
    else:
        number = generate_member_number(organization)

    member = Member(
        organization=organization,
        member_number=number,
        first_name=first_name,
        last_name=last_name,
        phone=phone,
        status=Member.Status.ACCEPTED,
        joined_at=timezone.localdate(),
    )
    member.full_clean()
    member.save()
    _log_member_created(member, performed_by, origin="counter")
    return member


@transaction.atomic
def request_membership(*, user, organization):
    """Demande d'un lecteur : crée un membre en attente de validation."""
    if organization.status != Organization.Status.ACTIVE:
        raise ValidationError("Cette structure n'accepte pas de nouvelles demandes.")
    if Member.objects.filter(organization=organization, user=user).exists():
        raise ValidationError(
            "Ce compte est déjà membre de cette structure, "
            "ou a déjà fait une demande."
        )

    member = Member(
        organization=organization,
        user=user,
        member_number=generate_member_number(organization),
        first_name=user.first_name,
        last_name=user.last_name,
        phone=user.phone,
        status=Member.Status.PENDING,
    )
    member.full_clean()
    member.save()
    _log_member_created(member, user, origin="request")
    return member


@transaction.atomic
def change_member_status(*, member, new_status, performed_by=None):
    """Change le statut d'un membre en respectant les transitions autorisées."""
    # On relit la ligne en la verrouillant : deux staffs qui agissent en même
    # temps sur le même membre passent l'un après l'autre.
    member = Member.objects.select_for_update().get(pk=member.pk)

    allowed = ALLOWED_TRANSITIONS.get(member.status, set())
    if new_status not in allowed:
        raise ValidationError(
            f"Passage impossible de « {member.status} » à « {new_status} »."
        )

    previous_status = member.status
    member.status = new_status
    if new_status == Member.Status.ACCEPTED and member.joined_at is None:
        member.joined_at = timezone.localdate()
    member.save(update_fields=["status", "joined_at", "updated_at"])

    if new_status == Member.Status.SUSPENDED:
        log_action(
            organization=member.organization,
            user=performed_by,
            action=AuditLog.Action.MEMBER_SUSPENDED,
            entity=member,
            description=f"Membre {member.member_number} suspendu.",
            metadata={
                "member_number": member.member_number,
                "status": {"from": previous_status, "to": new_status},
            },
        )
    return member


def accept_member(member, performed_by=None):
    return change_member_status(
        member=member, new_status=Member.Status.ACCEPTED, performed_by=performed_by
    )


def reject_member(member, performed_by=None):
    return change_member_status(
        member=member, new_status=Member.Status.REJECTED, performed_by=performed_by
    )


def suspend_member(member, performed_by=None):
    return change_member_status(
        member=member, new_status=Member.Status.SUSPENDED, performed_by=performed_by
    )


def reactivate_member(member, performed_by=None):
    return change_member_status(
        member=member, new_status=Member.Status.ACCEPTED, performed_by=performed_by
    )


def _random_code():
    return "".join(secrets.choice(LINK_CODE_ALPHABET) for _ in range(LINK_CODE_LENGTH))


@transaction.atomic
def create_link_code(*, member, created_by=None):
    """Génère un code de rattachement pour un membre accepté sans compte."""
    if member.user_id is not None:
        raise ValidationError("Ce membre est déjà rattaché à un compte.")
    if member.status != Member.Status.ACCEPTED:
        raise ValidationError(
            "Seul un membre accepté peut recevoir un code de rattachement."
        )

    now = timezone.now()

    # Un seul code actif à la fois : les précédents sont invalidés.
    LinkCode.objects.filter(
        member=member, used_at__isnull=True, expires_at__gt=now
    ).update(expires_at=now, updated_at=now)

    while True:
        code = _random_code()
        if not LinkCode.objects.filter(code=code).exists():
            break

    link_code = LinkCode(
        organization=member.organization,
        member=member,
        code=code,
        expires_at=now + timedelta(days=LINK_CODE_VALIDITY_DAYS),
        created_by=created_by,
    )
    link_code.full_clean()
    link_code.save()
    return link_code


@transaction.atomic
def redeem_link_code(*, user, code):
    """Relie le compte d'un lecteur à sa fiche de membre grâce au code."""
    code = code.strip().upper()
    now = timezone.now()

    try:
        link_code = LinkCode.objects.select_for_update().get(code=code)
    except LinkCode.DoesNotExist:
        raise ValidationError("Code invalide ou expiré.")

    # Même message pour un code inconnu, utilisé ou expiré : on ne renseigne
    # pas quelqu'un qui essaie des codes au hasard.
    if link_code.used_at is not None or link_code.expires_at <= now:
        raise ValidationError("Code invalide ou expiré.")

    member = Member.objects.select_for_update().get(pk=link_code.member_id)

    if member.user_id is not None:
        raise ValidationError("Ce membre est déjà rattaché à un compte.")
    if Member.objects.filter(organization_id=member.organization_id, user=user).exists():
        raise ValidationError("Ce compte est déjà membre de cette structure.")

    member.user = user
    update_fields = ["user", "updated_at"]
    # On complète le téléphone de la fiche avec celui du compte, mais seulement
    # s'il est vide : ce que le staff a saisi au comptoir n'est jamais écrasé.
    if not member.phone and user.phone:
        member.phone = user.phone
        update_fields.append("phone")
    member.save(update_fields=update_fields)

    link_code.used_at = now
    link_code.save(update_fields=["used_at", "updated_at"])
    return member