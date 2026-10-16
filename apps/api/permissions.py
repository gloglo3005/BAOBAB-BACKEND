"""Permissions de l'API.

Rien n'est cru sur parole : chaque requête relit la base de données. Un
identifiant dans l'URL ne prouve rien ; l'accès à une structure ou à une fiche
de lecteur est toujours revérifié pour le compte connecté. Un identifiant mal
formé (par exemple "abc") est simplement refusé.
"""
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework.permissions import BasePermission

from apps.members.models import Member
from apps.organizations.models import Organization, OrganizationStaff

MALFORMED_ID_ERRORS = (ValueError, TypeError, DjangoValidationError)


class IsReady(BasePermission):
    """Compte connecté, actif, sans changement de mot de passe obligatoire."""

    message = "Vous devez d'abord changer votre mot de passe."

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated and user.is_active):
            return False
        return not user.must_change_password


class IsStaffOfOrganization(BasePermission):
    """Staff actif d'une structure active : /api/v1/staff/<organization_id>/...

    Place `request.membership` et `request.organization`.
    """

    message = "Accès refusé à cette structure."

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        try:
            membership = (
                OrganizationStaff.objects.select_related("organization")
                .filter(
                    user=user,
                    organization_id=view.kwargs.get("organization_id"),
                    active=True,
                    organization__status=Organization.Status.ACTIVE,
                )
                .first()
            )
        except MALFORMED_ID_ERRORS:
            return False
        if membership is None:
            return False
        request.membership = membership
        request.organization = membership.organization
        return True


class IsReaderMember(BasePermission):
    """Fiche de lecteur du compte connecté : /api/v1/reader/<member_id>/...

    Place `request.member` et `request.organization`.
    """

    message = "Accès refusé à cette fiche de lecteur."

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        try:
            member = (
                Member.objects.select_related("organization")
                .filter(pk=view.kwargs.get("member_id"), user=user)
                .first()
            )
        except MALFORMED_ID_ERRORS:
            return False
        if member is None:
            return False
        if member.organization.status != Organization.Status.ACTIVE:
            self.message = "Cette bibliothèque n'est pas disponible pour le moment."
            return False
        if member.status == Member.Status.REJECTED:
            self.message = "Votre demande d'adhésion a été refusée par cette bibliothèque."
            return False
        request.member = member
        request.organization = member.organization
        return True


class LibraryModuleEnabled(BasePermission):
    """Le module bibliothèque doit être activé (à placer après les deux précédentes)."""

    message = "Le module bibliothèque n'est pas activé pour cette structure."

    def has_permission(self, request, view):
        organization = getattr(request, "organization", None)
        return bool(organization and organization.library_module)


# Ensembles prêts à l'emploi pour les routes bibliothèque.
STAFF_LIBRARY = [IsReady, IsStaffOfOrganization, LibraryModuleEnabled]
READER_LIBRARY = [IsReady, IsReaderMember, LibraryModuleEnabled]


class IsPlatformAdmin(BasePermission):
    """Administrateur de la plateforme (compte actif, sans changement de mot de passe en attente)."""

    message = "Réservé à l'administrateur de la plateforme."

    def has_permission(self, request, view):
        user = request.user
        return bool(
            user and user.is_authenticated and user.is_active
            and user.is_platform_admin and not user.must_change_password
        )


class IsStructureAdmin(IsStaffOfOrganization):
    """Responsable actif d'une structure active : gère l'équipe, la politique de prêt et le journal."""

    message = "Réservé au responsable de la structure."

    def has_permission(self, request, view):
        if not super().has_permission(request, view):
            return False
        return request.membership.role == OrganizationStaff.Role.ADMIN
