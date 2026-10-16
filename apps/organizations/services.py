import secrets

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.text import slugify

from apps.audit.models import AuditLog
from apps.audit.services import log_action

from .models import Organization, OrganizationStaff, LoanPolicy

User = get_user_model()


@transaction.atomic
def create_organization(*, name, slug, status=Organization.Status.ACTIVE, **extra_fields):
    """Crée une structure et sa politique de prêt par défaut.

    Les modules s'activent avec les champs library_module / coworking_module,
    par exemple create_organization(name=..., slug=..., coworking_module=True).
    """
    organization = Organization.objects.create(
        name=name, slug=slug, status=status, **extra_fields
    )
    LoanPolicy.objects.create(organization=organization)
    return organization


def generate_temporary_password():
    return secrets.token_urlsafe(9)


def unique_slug(name):
    """Identifiant court libre, à partir du nom (ajoute -2, -3... si déjà pris)."""
    base = slugify(name)[:90] or "structure"
    slug, number = base, 2
    while Organization.objects.filter(slug=slug).exists():
        slug = f"{base}-{number}"
        number += 1
    return slug


@transaction.atomic
def create_staff_account(
    *, organization, email, first_name, last_name, phone="",
    role=OrganizationStaff.Role.STAFF, allow_existing=True, performed_by=None,
):
    """Rattache un compte du personnel (agent ou responsable) à une structure.

    Retourne (user, mot_de_passe_temporaire). Si un compte existe déjà avec cet
    email, on le rattache simplement et le mot de passe retourné est None, sauf
    avec allow_existing=False (cas d'un responsable de structure) : on refuse,
    pour qu'un responsable ne puisse pas s'approprier le compte d'une autre personne.
    """
    email = User.objects.normalize_email(email)
    user = User.objects.filter(email__iexact=email).first()
    temporary_password = None
    if user is not None:
        if not allow_existing:
            raise ValidationError(
                "Un compte existe déjà avec cette adresse e-mail. Utilisez une autre adresse "
                "ou demandez à l'administrateur de la plateforme."
            )
        membership, created = OrganizationStaff.objects.get_or_create(
            user=user, organization=organization, defaults={"role": role}
        )
        if not created:
            return user, None
    else:
        temporary_password = generate_temporary_password()
        user = User.objects.create_user(
            email=email,
            password=temporary_password,
            first_name=first_name,
            last_name=last_name,
            phone=phone,
            must_change_password=True,
        )
        membership = OrganizationStaff.objects.create(
            user=user, organization=organization, role=role
        )
    log_action(
        organization=organization,
        action=AuditLog.Action.STAFF_CREATED,
        entity=membership,
        user=performed_by,
        description=f"{user.email} ajouté : {OrganizationStaff.Role(role).label}.",
        metadata={"email": user.email, "role": role, "existing_account": temporary_password is None},
    )
    return user, temporary_password


def responsable_can_manage(membership):
    """Un responsable ne gère que les agents de sa structure, et seulement des comptes
    propres à cette structure (jamais un compte rattaché ailleurs ou administrateur)."""
    user = membership.user
    if membership.role != OrganizationStaff.Role.STAFF:
        return False
    if user.is_platform_admin or user.is_superuser:
        return False
    if OrganizationStaff.objects.filter(user=user).exclude(pk=membership.pk).exists():
        return False
    return not (user.memberships.exists() or user.coworking_customers.exists())


@transaction.atomic
def reset_staff_password(*, membership, performed_by):
    """Nouveau mot de passe temporaire (à communiquer une seule fois)."""
    user = membership.user
    if user.is_platform_admin or user.is_superuser:
        raise ValidationError("Ce compte ne peut pas être réinitialisé ici.")
    password = generate_temporary_password()
    user.set_password(password)
    user.must_change_password = True
    user.save(update_fields=["password", "must_change_password"])
    log_action(
        organization=membership.organization,
        action=AuditLog.Action.STAFF_PASSWORD_RESET,
        entity=membership,
        user=performed_by,
        description=f"Mot de passe de {user.email} réinitialisé.",
    )
    return password


@transaction.atomic
def update_staff_membership(*, membership, performed_by, active=None, role=None):
    """Active ou désactive un compte du personnel, ou change son rôle."""
    changes = {}
    if active is not None and active != membership.active:
        changes["active"] = [membership.active, active]
        membership.active = active
    if role is not None and role != membership.role:
        changes["role"] = [membership.role, role]
        membership.role = role
    if not changes:
        return membership
    membership.save()
    log_action(
        organization=membership.organization,
        action=AuditLog.Action.STAFF_UPDATED,
        entity=membership,
        user=performed_by,
        description=f"Compte {membership.user.email} modifié.",
        metadata=changes,
    )
    return membership


@transaction.atomic
def change_organization_status(*, organization, status, performed_by):
    if status == organization.status:
        return organization
    previous = organization.status
    organization.status = status
    organization.save(update_fields=["status", "updated_at"])
    log_action(
        organization=organization,
        action=AuditLog.Action.ORGANIZATION_STATUS_CHANGED,
        entity=organization,
        user=performed_by,
        description=f"Statut : {previous} → {status}.",
        metadata={"from": previous, "to": status},
    )
    return organization
