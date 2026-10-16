"""Renouvellement d'un prêt.

Règles (politique de prêt de la structure) : renouvellements autorisés, limite
de renouvellements non atteinte, membre toujours accepté, prêt en cours et pas
en retard. La nouvelle échéance = l'ancienne + la durée de prêt de la politique.
"""
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import transaction

from apps.audit.models import AuditLog
from apps.audit.services import log_action
from apps.members.models import Member
from apps.organizations.models import LoanPolicy, Organization

from .models import Loan
from .services import _get_or_refuse, _require_active_staff


@transaction.atomic
def renew_loan(*, organization, loan_id, renewed_by):
    # La structure vient du contexte du Staff connecté ; on relit la base.
    organization = Organization.objects.get(pk=organization.pk)
    if organization.status != Organization.Status.ACTIVE:
        raise ValidationError("Cette structure n'est pas active.")
    if not organization.has_module(Organization.Module.LIBRARY):
        raise ValidationError("Le module bibliothèque n'est pas activé.")
    _require_active_staff(organization, renewed_by)

    # Le verrou fait attendre un second clic jusqu'à la fin du premier.
    loan = _get_or_refuse(
        Loan.objects, "Prêt introuvable.", pk=loan_id, organization=organization
    )
    if loan.status != Loan.Status.ACTIVE:
        raise ValidationError("Ce prêt est déjà clôturé : il ne peut pas être renouvelé.")

    policy, _ = LoanPolicy.objects.get_or_create(organization=organization)
    if not policy.allow_renewal:
        raise ValidationError("Les renouvellements ne sont pas autorisés dans cette structure.")
    if loan.renewal_count >= policy.max_renewals:
        raise ValidationError(
            f"Limite de renouvellements atteinte ({loan.renewal_count} sur {policy.max_renewals})."
        )
    member = loan.member
    if member.status != Member.Status.ACCEPTED:
        raise ValidationError("Ce membre n'est pas actif : le prêt ne peut pas être renouvelé.")
    if loan.is_overdue:
        raise ValidationError(
            f"Ce prêt est en retard de {loan.days_overdue} jour(s) : "
            "le livre doit d'abord être rendu."
        )

    previous_due = loan.due_date
    loan.due_date = previous_due + timedelta(days=policy.loan_duration_days)
    loan.renewal_count += 1
    loan.save(update_fields=["due_date", "renewal_count", "updated_at"])

    code = loan.book_copy.inventory_code
    log_action(
        organization=organization,
        user=renewed_by,
        action=AuditLog.Action.LOAN_RENEWED,
        entity=loan,
        description=(
            f"Renouvellement de {code} pour {member.member_number} : "
            f"échéance du {previous_due:%d/%m/%Y} au {loan.due_date:%d/%m/%Y}."
        ),
        metadata={
            "loan_id": loan.pk,
            "book_copy": code,
            "member": member.member_number,
            "due_date": {"from": previous_due, "to": loan.due_date},
            "renewal_count": loan.renewal_count,
        },
    )
    return loan
