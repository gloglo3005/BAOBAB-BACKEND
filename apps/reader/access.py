"""Contrôle d'accès de l'interface Lecteur.

Il n'existe pas de champ « rôle » à envoyer : un lecteur est un compte actif
sans privilège, et ce qu'il voit dépend uniquement des fiches `Member` rattachées
à son compte (Member.user). Chaque page de bibliothèque recharge la fiche depuis
la base, filtrée sur l'utilisateur connecté : l'identifiant dans l'URL ne prouve
rien, et celui d'un autre lecteur donne une page introuvable.
"""
from functools import wraps

from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import urlencode

from apps.circulation.models import Loan
from apps.members.models import Member
from apps.organizations.models import Organization


def _login_redirect(request):
    query = urlencode({"next": request.get_full_path()})
    return redirect(f"{reverse('reader:login')}?{query}")


def forbidden(request, message):
    return render(request, "reader/no_access.html", {"message": message}, status=403)


def reader_required(view):
    """Compte connecté et actif."""

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        user = request.user
        if not user.is_authenticated or not user.is_active:
            return _login_redirect(request)
        return view(request, *args, **kwargs)

    return wrapper


def member_required(view):
    """Pages d'une bibliothèque : /reader/bibliotheque/<pk>/...

    <pk> est celui de la fiche Member du lecteur connecté. Place `request.member`
    et `request.organization`.
    """

    @reader_required
    @wraps(view)
    def wrapper(request, pk, *args, **kwargs):
        member = get_object_or_404(
            Member.objects.select_related("organization"), pk=pk, user=request.user
        )
        organization = member.organization
        if organization.status != Organization.Status.ACTIVE or not organization.library_module:
            return forbidden(
                request, "Cette bibliothèque n'est pas disponible pour le moment."
            )
        if member.status == Member.Status.REJECTED:
            return forbidden(
                request, "Votre demande d'adhésion a été refusée par cette bibliothèque."
            )
        request.member = member
        request.organization = organization
        request.overdue_count = (
            Loan.objects.filter(organization=organization, member=member).overdue().count()
        )
        return view(request, *args, **kwargs)

    return wrapper
