from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone

from apps.catalog.models import BookCopy
from apps.members.models import Member
from apps.audit.models import AuditLog
from apps.audit.services import log_action
from apps.organizations.models import LoanPolicy, Organization, OrganizationStaff

from .models import Loan


class ReturnOutcome(models.TextChoices):
    """Ce que le Staff constate au moment du retour."""

    GOOD = "GOOD", "Rendu en bon état"
    DAMAGED = "DAMAGED", "Rendu endommagé"
    LOST = "LOST", "Perdu"


# Pour chaque constat, l'état dans lequel l'exemplaire se retrouve.
COPY_STATUS_AFTER_RETURN = {
    ReturnOutcome.GOOD: BookCopy.Status.AVAILABLE,
    ReturnOutcome.DAMAGED: BookCopy.Status.DAMAGED,
    ReturnOutcome.LOST: BookCopy.Status.LOST,
}

# Messages adaptés au statut de l'exemplaire quand il n'est pas disponible.
COPY_UNAVAILABLE_MESSAGES = {
    BookCopy.Status.BORROWED: "Cet exemplaire est déjà emprunté.",
    BookCopy.Status.LOST: "Cet exemplaire est déclaré perdu.",
    BookCopy.Status.DAMAGED: "Cet exemplaire est endommagé et ne peut pas être prêté.",
    BookCopy.Status.UNAVAILABLE: "Cet exemplaire est indisponible.",
}


def _get_or_refuse(queryset, message, **filters):
    """Verrouille et retourne un objet, ou refuse avec un message neutre.

    Le même message sert pour « n'existe pas » et « appartient à une autre
    structure » : on ne révèle jamais l'existence d'une ressource d'ailleurs.
    """
    try:
        return queryset.select_for_update().get(**filters)
    except queryset.model.DoesNotExist:
        raise ValidationError(message)


def _require_active_staff(organization, user):
    """Refuse un agent qui n'est pas Staff actif de cette structure.

    L'agent qui prête ou reçoit un livre doit être rattaché à la structure
    (OrganizationStaff actif) et avoir un compte actif. Un Staff d'une autre
    structure, un Staff désactivé ou un compte sans rattachement sont refusés.
    """
    is_staff_here = OrganizationStaff.objects.filter(
        organization=organization,
        user=user,
        active=True,
        user__is_active=True,
    ).exists()
    if not is_staff_here:
        raise ValidationError(
            "Cet agent n'est pas autorisé à gérer les prêts de cette structure."
        )


@transaction.atomic
def checkout_copy(*, organization, book_copy_id, member_id, issued_by, notes=""):
    """Enregistre le prêt d'un exemplaire à un membre (RG11, RG12, RG15).

    `organization` vient toujours du contexte du Staff connecté, jamais d'une
    valeur envoyée par le client (RG08). Les identifiants de l'exemplaire et
    du membre sont cherchés DANS cette structure : un identifiant d'une autre
    structure donne simplement « introuvable ».

    Tout ou rien : si une règle échoue, une ValidationError est levée et
    rien n'est enregistré.
    """
    # 1. La structure doit être active et avoir le module bibliothèque.
    #    On relit la base : l'objet reçu peut être périmé.
    organization = Organization.objects.get(pk=organization.pk)
    if organization.status != Organization.Status.ACTIVE:
        raise ValidationError("Cette structure n'est pas active.")
    if not organization.has_module(Organization.Module.LIBRARY):
        raise ValidationError("Le module bibliothèque n'est pas activé.")

    # 2. L'agent doit être Staff actif de cette structure.
    _require_active_staff(organization, issued_by)

    # 3. On verrouille l'exemplaire. Deux prêts simultanés de la même copie
    #    se mettent en file : le second attend, puis voit « BORROWED » et refuse.
    book_copy = _get_or_refuse(
        BookCopy.objects, "Exemplaire introuvable.",
        pk=book_copy_id, organization=organization,
    )
    if book_copy.status != BookCopy.Status.AVAILABLE:
        raise ValidationError(
            COPY_UNAVAILABLE_MESSAGES.get(book_copy.status, "Exemplaire non disponible.")
        )
    if not book_copy.book.active:
        raise ValidationError("Ce livre est archivé : il ne peut plus être prêté.")

    # 4. On verrouille le membre. Cela sérialise ses prêts : sans ce verrou,
    #    deux prêts simultanés pourraient passer sa limite ensemble.
    member = _get_or_refuse(
        Member.objects, "Membre introuvable.",
        pk=member_id, organization=organization,
    )
    if member.status == Member.Status.SUSPENDED:
        raise ValidationError("Ce membre est suspendu.")
    if member.status != Member.Status.ACCEPTED:
        raise ValidationError("Ce membre n'a pas été accepté.")

    # 5. La limite de prêts actifs vient de la politique de la structure.
    policy, _ = LoanPolicy.objects.get_or_create(organization=organization)
    active_loans = Loan.objects.active().filter(member=member).count()
    if active_loans >= policy.max_active_loans:
        raise ValidationError(
            f"Limite atteinte : ce membre a déjà {active_loans} prêt(s) en cours "
            f"(maximum {policy.max_active_loans})."
        )

    # 6. L'échéance se calcule à partir du même instant que borrowed_at.
    now = timezone.now()
    due_date = timezone.localtime(now).date() + timedelta(days=policy.loan_duration_days)

    loan = Loan(
        organization=organization,
        book_copy=book_copy,
        member=member,
        borrowed_at=now,
        due_date=due_date,
        issued_by=issued_by,
        notes=notes,
    )
    loan.full_clean()
    loan.save()

    book_copy.status = BookCopy.Status.BORROWED
    book_copy.save(update_fields=["status", "updated_at"])

    log_action(
        organization=organization,
        user=issued_by,
        action=AuditLog.Action.LOAN_CREATED,
        entity=loan,
        description=f"Prêt de {book_copy.inventory_code} à {member.member_number}.",
        metadata={
            "book_copy": book_copy.inventory_code,
            "member": member.member_number,
            "due_date": loan.due_date,
            "copy_status": {
                "from": BookCopy.Status.AVAILABLE,
                "to": BookCopy.Status.BORROWED,
            },
        },
    )
    return loan


@transaction.atomic
def return_copy(*, organization, loan_id, received_by, outcome=ReturnOutcome.GOOD, notes=""):
    """Clôture un prêt actif et remet l'exemplaire dans le bon état (RG15).

    Convention pour un exemplaire perdu : le prêt passe à LOST et
    `returned_at` reste vide, car rien n'a été rendu (c'est la contrainte
    posée dans le modèle). `received_by` garde la trace de l'agent qui a clos
    le prêt.
    """
    outcome = ReturnOutcome(outcome)

    # L'agent doit être Staff actif de cette structure.
    _require_active_staff(organization, received_by)

    # Même ordre de verrouillage que checkout_copy : l'exemplaire d'abord.
    # On lit d'abord (sans verrou) quel exemplaire porte ce prêt, dans cette
    # structure uniquement : un prêt d'une autre structure donne « introuvable ».
    copy_id = (
        Loan.objects.filter(pk=loan_id, organization=organization)
        .values_list("book_copy_id", flat=True)
        .first()
    )
    if copy_id is None:
        raise ValidationError("Prêt introuvable.")

    book_copy = BookCopy.objects.select_for_update().get(pk=copy_id)
    loan = _get_or_refuse(
        Loan.objects, "Prêt introuvable.",
        pk=loan_id, organization=organization,
    )
    # Un double clic, ou deux agents en même temps : le second arrive ici
    # après le premier et trouve le prêt déjà clôturé.
    if loan.status != Loan.Status.ACTIVE:
        raise ValidationError("Ce prêt est déjà clôturé.")

    # Le prêt renvoyé doit porter l'exemplaire verrouillé (donc à jour après
    # le changement d'état ci-dessous), pas une copie chargée plus tôt.
    loan.book_copy = book_copy

    # État avant la clôture, pour le journal d'audit.
    previous_copy_status = book_copy.status
    days_overdue = loan.days_overdue

    if outcome == ReturnOutcome.LOST:
        loan.status = Loan.Status.LOST
        loan.returned_at = None
    else:
        loan.status = Loan.Status.RETURNED
        loan.returned_at = timezone.now()
    loan.received_by = received_by
    if notes:
        loan.notes = f"{loan.notes}\n{notes}".strip()
    loan.full_clean()
    loan.save(update_fields=[
        "status", "returned_at", "received_by", "notes", "updated_at",
    ])

    book_copy.status = COPY_STATUS_AFTER_RETURN[outcome]
    book_copy.save(update_fields=["status", "updated_at"])

    code = book_copy.inventory_code
    member_number = loan.member.member_number
    metadata = {
        "loan_id": loan.pk,
        "book_copy": code,
        "member": member_number,
        "outcome": outcome.value,
        "days_overdue": days_overdue,
        "copy_status": {"from": previous_copy_status, "to": book_copy.status},
        "returned_at": loan.returned_at,
    }
    if outcome == ReturnOutcome.LOST:
        # Rien n'a été rendu : on trace la perte de l'exemplaire, pas un retour.
        log_action(
            organization=organization,
            user=received_by,
            action=AuditLog.Action.COPY_MARKED_LOST,
            entity=book_copy,
            description=(
                f"Exemplaire {code} déclaré perdu "
                f"(prêt n° {loan.pk}, membre {member_number})."
            ),
            metadata=metadata,
        )
    else:
        suffix = " (endommagé)" if outcome == ReturnOutcome.DAMAGED else ""
        log_action(
            organization=organization,
            user=received_by,
            action=AuditLog.Action.LOAN_RETURNED,
            entity=loan,
            description=f"Retour de {code} par {member_number}{suffix}.",
            metadata=metadata,
        )
    return loan