"""Permissions de l'API coworking (même esprit que celles de la bibliothèque).

Le champ `coworking_module` ne protège rien tout seul : chaque route coworking
doit vérifier à la fois l'appartenance active du staff ET l'activation du module.
"""
from rest_framework.permissions import BasePermission

from .permissions import IsReady, IsStaffOfOrganization


class CoworkingModuleEnabled(BasePermission):
    """Le module coworking doit être activé (à placer après IsStaffOfOrganization)."""

    message = "Le module coworking n'est pas activé pour cette structure."

    def has_permission(self, request, view):
        organization = getattr(request, "organization", None)
        return bool(organization and organization.coworking_module)


STAFF_COWORKING = [IsReady, IsStaffOfOrganization, CoworkingModuleEnabled]
