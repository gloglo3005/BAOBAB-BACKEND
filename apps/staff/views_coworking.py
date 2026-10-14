"""Interface Staff : coworking (configuration) et tableau de bord commun.

Même principe que views.py : la structure vient de la session (request.organization),
chaque écriture passe par les services de apps.coworking (règles, verrous, audit).
"""
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from apps.coworking import services
from apps.coworking.models import Rate, Resource, Zone

from . import queries, queries_coworking
from .access import coworking_required, staff_required
from .forms_coworking import ProfileForm, RateForm, ResourceForm, ZoneForm
from .views import _error_text, _paginate

# ---------------------------------------------------------------- outils


def _plain(value):
    if hasattr(value, "pk"):
        return value.pk
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _changes(form):
    """{champ: {from, to}} pour le journal d'audit (champs réellement modifiés)."""
    return {
        name: {"from": _plain(form.initial.get(name)), "to": _plain(form.cleaned_data.get(name))}
        for name in form.changed_data
    }


def _add_errors(form, error):
    if hasattr(error, "error_dict"):
        for field, messages_list in error.message_dict.items():
            target = field if field in form.fields else None
            for text in messages_list:
                form.add_error(target, text)
    else:
        form.add_error(None, _error_text(error))


def _save(request, form, *, created):
    """Enregistre via le service (validation, audit). Retourne l'objet, ou None si refus."""
    instance = form.save(commit=False)
    changes = {} if created else _changes(form)
    try:
        return services.save_object(
            organization=request.organization,
            user=request.user,
            instance=instance,
            created=created,
            metadata={"changes": changes} if changes else None,
        )
    except ValidationError as error:
        _add_errors(form, error)
        return None


def _object_form(
    request, *, form_class, instance, created, title, back, sentence, intro="", back_args=()
):
    form = form_class(request.POST or None, instance=instance, organization=request.organization)
    if request.method == "POST" and form.is_valid():
        saved = _save(request, form, created=created)
        if saved is not None:
            messages.success(request, sentence(saved))
            return redirect(back, *back_args)
    return render(
        request,
        "staff/coworking/form.html",
        {
            "form": form,
            "title": title,
            "back_url": reverse(back, args=back_args),
            "intro": intro,
            "created": created,
        },
    )


# ---------------------------------------------------------------- tableau de bord commun


@staff_required
def dashboard(request):
    """Bibliothèque et/ou coworking, selon les modules activés pour la structure."""
    organization = request.organization
    context = {}
    if organization.library_module:
        context["stats"] = queries.dashboard_stats(organization)
    if organization.coworking_module:
        context["cw_stats"] = services.dashboard_stats(organization)
        context["cw_unpaid"] = queries_coworking.unpaid_summary(organization)
    return render(request, "staff/dashboard.html", context)


# ---------------------------------------------------------------- page d'accueil de la configuration


def _resources_without_rate(organization):
    """Ressources actives sans aucun tarif actif (leur usage serait compté 0 XOF)."""
    rates = Rate.objects.filter(organization=organization, active=True)
    own = rates.filter(resource__isnull=False).values("resource_id")
    types = rates.filter(resource__isnull=True).exclude(resource_type="").values("resource_type")
    return (
        Resource.objects.filter(organization=organization, active=True, zone__active=True)
        .exclude(pk__in=own)
        .exclude(resource_type__in=types)
        .count()
    )


@coworking_required
def setup(request):
    organization = request.organization
    zones = Zone.objects.filter(organization=organization)
    resources = Resource.objects.filter(organization=organization)
    rates = Rate.objects.filter(organization=organization)
    context = {
        "zone_count": zones.filter(active=True).count(),
        "resource_count": resources.filter(active=True).count(),
        "rate_count": rates.filter(active=True).count(),
        "without_rate": _resources_without_rate(organization),
        "profile": services.get_profile(organization),
    }
    return render(request, "staff/coworking/setup.html", context)


# ---------------------------------------------------------------- zones


@coworking_required
def zones(request):
    items = (
        Zone.objects.filter(organization=request.organization)
        .annotate(resource_count=Count("resources"))
        .order_by("name")
    )
    return render(request, "staff/coworking/zones.html", {"zones": items})


@coworking_required
def zone_new(request):
    return _object_form(
        request,
        form_class=ZoneForm,
        instance=Zone(),
        created=True,
        title="Nouvelle zone",
        back="staff:cw_zones",
        sentence=lambda zone: f"Zone « {zone.name} » créée.",
        intro="Une zone regroupe des ressources : open space, étage, bloc de salles...",
    )


@coworking_required
def zone_edit(request, pk):
    zone = get_object_or_404(Zone, pk=pk, organization=request.organization)
    return _object_form(
        request,
        form_class=ZoneForm,
        instance=zone,
        created=False,
        title=f"Zone : {zone.name}",
        back="staff:cw_zones",
        sentence=lambda saved: f"Zone « {saved.name} » enregistrée.",
    )


# ---------------------------------------------------------------- ressources


@coworking_required
def resources(request):
    text = request.GET.get("q", "").strip()
    zone_id = request.GET.get("zone", "")
    kind = request.GET.get("type", "")
    state = request.GET.get("state", "")

    items = Resource.objects.filter(organization=request.organization).select_related("zone")
    for token in queries.tokens(text):
        items = items.filter(Q(code__icontains=token) | Q(name__icontains=token))
    if zone_id.isdigit():
        items = items.filter(zone_id=int(zone_id))
    if kind in Resource.Type.values:
        items = items.filter(resource_type=kind)
    if state == "active":
        items = items.filter(active=True)
    elif state == "inactive":
        items = items.filter(active=False)

    page, querystring = _paginate(request, items.order_by("code"), 25)
    return render(
        request,
        "staff/coworking/resources.html",
        {
            "page": page,
            "querystring": querystring,
            "q": text,
            "zone": zone_id,
            "kind": kind,
            "state": state,
            "zones": Zone.objects.filter(organization=request.organization).order_by("name"),
            "types": Resource.Type.choices,
        },
    )


@coworking_required
def resource_new(request):
    return _object_form(
        request,
        form_class=ResourceForm,
        instance=Resource(),
        created=True,
        title="Nouvelle ressource",
        back="staff:cw_resources",
        sentence=lambda resource: f"Ressource {resource.code} créée.",
        intro="Il faut d'abord au moins une zone active.",
    )


@coworking_required
def resource_edit(request, pk):
    resource = get_object_or_404(Resource, pk=pk, organization=request.organization)
    return _object_form(
        request,
        form_class=ResourceForm,
        instance=resource,
        created=False,
        title=f"Ressource {resource.code}",
        back="staff:cw_resources",
        sentence=lambda saved: f"Ressource {saved.code} enregistrée.",
    )


# ---------------------------------------------------------------- tarifs


@coworking_required
def rates(request):
    state = request.GET.get("state", "")
    items = Rate.objects.filter(organization=request.organization).select_related("resource")
    if state == "active":
        items = items.filter(active=True)
    elif state == "inactive":
        items = items.filter(active=False)
    items = items.order_by("-active", "resource_type", "resource__code", "unit")
    return render(
        request,
        "staff/coworking/rates.html",
        {"rates": items, "state": state, "without_rate": _resources_without_rate(request.organization)},
    )


@coworking_required
def rate_new(request):
    return _object_form(
        request,
        form_class=RateForm,
        instance=Rate(),
        created=True,
        title="Nouveau tarif",
        back="staff:cw_rates",
        sentence=lambda rate: "Tarif créé.",
        intro="Le prix d'une période est « nombre d'unités entamées × montant » ; "
              "le client paie le moins cher des tarifs applicables.",
    )


@coworking_required
def rate_edit(request, pk):
    rate = get_object_or_404(Rate, pk=pk, organization=request.organization)
    return _object_form(
        request,
        form_class=RateForm,
        instance=rate,
        created=False,
        title="Modifier le tarif",
        back="staff:cw_rates",
        sentence=lambda saved: "Tarif enregistré.",
        intro="Modifier un tarif ne change pas les réservations et sessions déjà enregistrées.",
    )


# ---------------------------------------------------------------- règles de fonctionnement


@coworking_required
def rules(request):
    profile = services.get_profile(request.organization)
    form = ProfileForm(request.POST or None, instance=profile, organization=request.organization)
    if request.method == "POST" and form.is_valid():
        saved = _save(request, form, created=False)
        if saved is not None:
            messages.success(request, "Règles de fonctionnement enregistrées.")
            return redirect("staff:cw_rules")
    return render(request, "staff/coworking/rules.html", {"form": form})
