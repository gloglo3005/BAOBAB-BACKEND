"""Modification des informations d'un membre (le statut a ses propres actions)."""
from django.core.exceptions import ValidationError
from django.db import transaction

from apps.audit.models import AuditLog
from apps.audit.services import log_action

from .models import Member

EDITABLE_FIELDS = ("first_name", "last_name", "phone", "member_number")


@transaction.atomic
def update_member(*, member, performed_by=None, **values):
    """Modifie prénom, nom, téléphone ou numéro de membre. Retourne le membre.

    Rien n'est écrit (ni journal) si aucune valeur ne change.
    """
    unknown = set(values) - set(EDITABLE_FIELDS)
    if unknown:
        raise ValueError(f"Champs non modifiables : {', '.join(sorted(unknown))}")

    # Même verrou que change_member_status : deux agents passent l'un après l'autre.
    member = Member.objects.select_for_update().get(pk=member.pk)
    new_values = {k: v.strip() if isinstance(v, str) else v for k, v in values.items()}
    changes = {
        field: {"avant": getattr(member, field), "après": value}
        for field, value in new_values.items()
        if getattr(member, field) != value
    }
    if not changes:
        return member

    number = changes.get("member_number", {}).get("après")
    if number is not None:
        if not number:
            raise ValidationError({"member_number": "Le numéro de membre est obligatoire."})
        taken = (
            Member.objects.filter(organization=member.organization, member_number=number)
            .exclude(pk=member.pk)
            .exists()
        )
        if taken:
            raise ValidationError({"member_number": "Ce numéro de membre est déjà utilisé."})

    for field, diff in changes.items():
        setattr(member, field, diff["après"])
    member.full_clean()
    member.save(update_fields=[*changes, "updated_at"])

    log_action(
        organization=member.organization,
        user=performed_by,
        action=AuditLog.Action.MEMBER_UPDATED,
        entity=member,
        description=f"Membre {member.member_number} modifié : " + ", ".join(changes) + ".",
        metadata={"member_number": member.member_number, **changes},
    )
    return member
