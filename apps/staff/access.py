"""Contrôle d'accès de l'interface Staff.

Règle : on entre dans /staff/ seulement si le compte est Staff ACTIF d'au
moins une structure ACTIVE. La structure de travail est choisie dans la
session, mais elle est revérifiée à chaque requête : le navigateur ne prouve
rien, c'est la base de données qui décide.
"""
from functools import wraps

from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import urlencode

from apps.organizations.models import Organization, OrganizationStaff

SESSION_KEY = "staff_organization_id"


def active_memberships(user):
    return list(
        OrganizationStaff.objects.filter(
            user=user,
            active=True,
            organization__status=Organization.Status.ACTIVE,
        )
        .select_related("organization")
        .order_by("organization__name")
    )


def _login_redirect(request):
    query = urlencode({"next": request.get_full_path()})
    return redirect(f"{reverse('staff:login')}?{query}")


def _forbidden(request, message):
    return render(request, "staff/no_access.html", {"message": message}, status=403)


def staff_required(view):
    """Staff actif d'une structure active, structure de travail choisie."""

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        user = request.user
        if not user.is_authenticated:
            return _login_redirect(request)
        if not user.is_active:
            return _login_redirect(request)
        if user.must_change_password:
            return redirect("staff:password")

        memberships = active_memberships(user)
        request.staff_memberships = memberships
        if not memberships:
            return _forbidden(
                request,
                "Ce compte n'est rattaché à aucune structure active. "
                "Contactez l'administrateur de la plateforme.",
            )

        wanted = request.session.get(SESSION_KEY)
        chosen = next((m for m in memberships if m.organization_id == wanted), None)
        if chosen is None:
            if len(memberships) == 1:
                chosen = memberships[0]
                request.session[SESSION_KEY] = chosen.organization_id
            else:
                request.session.pop(SESSION_KEY, None)
                return redirect("staff:organizations")

        request.membership = chosen
        request.organization = chosen.organization
        return view(request, *args, **kwargs)

    return wrapper


def library_required(view):
    """En plus : le module bibliothèque doit être activé pour la structure."""

    @staff_required
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.organization.library_module:
            return _forbidden(
                request,
                "Le module bibliothèque n'est pas activé pour cette structure.",
            )
        return view(request, *args, **kwargs)

    return wrapper


def coworking_required(view):
    """En plus : le module coworking doit être activé pour la structure."""

    @staff_required
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.organization.coworking_module:
            return _forbidden(
                request,
                "Le module coworking n'est pas activé pour cette structure.",
            )
        return view(request, *args, **kwargs)

    return wrapper
