import secrets

from django.contrib.auth import get_user_model
from django.db import transaction

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


@transaction.atomic
def create_staff_account(*, organization, email, first_name, last_name, phone=""):
    """Rattache un compte Staff à une structure.

    Retourne (user, mot_de_passe_temporaire). Si un compte existe déjà avec
    cet email, on le rattache simplement et le mot de passe retourné est None.
    """
    email = User.objects.normalize_email(email)

    user = User.objects.filter(email__iexact=email).first()
    if user is not None:
        OrganizationStaff.objects.get_or_create(user=user, organization=organization)
        return user, None

    temporary_password = generate_temporary_password()
    user = User.objects.create_user(
        email=email,
        password=temporary_password,
        first_name=first_name,
        last_name=last_name,
        phone=phone,
        must_change_password=True,
    )
    OrganizationStaff.objects.create(user=user, organization=organization)
    return user, temporary_password