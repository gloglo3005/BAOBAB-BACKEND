"""Interface Staff : suivi du coworking (planning, réservations, historique des sessions, corrections).

Même principe que le reste : la structure vient de la session, chaque écriture
passe par les services de apps.coworking (règles, verrous, audit) et les
messages d'erreur sont ceux des services (donc ceux de l'API).
"""
from datetime import timedelta

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.coworking import services
from apps.coworking.models import AccessSession, Customer, Payment, Reservation, Resource, Zone
from apps.coworking.pricing import quote

from . import queries_coworking as cq
from . import queries_coworking_booking as bq
from .access import coworking_required
from .forms_coworking_booking import CorrectionForm, PeriodForm, ReservationForm
from .views import _error_text, _paginate, _to_int
from .views_coworking import _add_errors
from .views_coworking_desk import _customer_panel, _duration_label

# ---------------------------------------------------------------- planning


@coworking_required
def planning(request):
    organization = request.organization
    profile = services.get_profile(organization)
    today = timezone.localdate()
    day = bq.parse_date(request.GET.get("date")) or today
    zone_id = _to_int(request.GET.get("zone"))
    resource_type = request.GET.get("type", "")
    if resource_type not in Resource.Type.values:
        resource_type = ""
    context = bq.planning(organization, profile, day, zone_id, resource_type)
    context.update(
        {
            "profile": profile,
            "day": day,
            "previous_day": day - timedelta(days=1),
            "next_day": day + timedelta(days=1),
            "today": today,
            "zone_id": zone_id,
            "resource_type": resource_type,
            "zones": Zone.objects.filter(organization=organization, active=True),
            "types": Resource.Type.choices,
            "to_decide": bq.awaiting_no_show(organization, profile).count(),
        }
    )
    return render(request, "staff/coworking/planning.html", context)


# ---------------------------------------------------------------- liste des réservations


@coworking_required
def reservations(request):
    organization = request.organization
    profile = services.get_profile(organization)
    text = request.GET.get("q", "").strip()
    state = request.GET.get("state", "")
    date_from = bq.parse_date(request.GET.get("from"))
    date_to = bq.parse_date(request.GET.get("to"))

    items = bq.reservation_matches(organization, text)
    if state == "TO_DECIDE":
        items = bq.awaiting_no_show(organization, profile).filter(pk__in=items.values("pk"))
    else:
        items = bq.with_has_session(items)
        if state in Reservation.Status.values:
            items = items.filter(status=state)
    if date_from:
        items = items.filter(starts_at__gte=bq.local_day_bounds(date_from)[0])
    if date_to:
        items = items.filter(starts_at__lt=bq.local_day_bounds(date_to)[1])

    page, querystring = _paginate(
        request, items.select_related("customer", "resource").order_by("-starts_at", "-pk"), 25
    )
    limit = timezone.now() - timedelta(minutes=profile.no_show_grace_minutes)
    for item in page.object_list:
        item.to_decide = (
            item.status == Reservation.Status.CONFIRMED and not item.has_session and item.starts_at < limit
        )
    return render(
        request,
        "staff/coworking/reservations.html",
        {
            "page": page,
            "querystring": querystring,
            "q": text,
            "state": state,
            "date_from": date_from,
            "date_to": date_to,
            "statuses": Reservation.Status.choices,
            "profile": profile,
            "to_decide": bq.awaiting_no_show(organization, profile).count(),
        },
    )


# ---------------------------------------------------------------- nouvelle réservation


def _period_problem(profile, starts_at, ends_at):
    """Mêmes refus que le service, pour prévenir avant d'afficher des ressources."""
    now = timezone.now()
    if starts_at < now - timedelta(minutes=5):
        return (
            "Le début est déjà passé. Pour quelqu'un qui est là maintenant, "
            "utilisez l'accueil sans réservation (arrivée)."
        )
    minutes = (ends_at - starts_at).total_seconds() / 60
    if minutes < profile.min_booking_minutes:
        return f"Durée minimale : {profile.min_booking_minutes} minutes."
    if minutes > profile.max_booking_minutes:
        return f"Durée maximale : {profile.max_booking_minutes} minutes."
    if not profile.is_open_between(starts_at, ends_at):
        return "La période est en dehors des horaires d'ouverture."
    return ""


def _availability(organization, profile, starts_at, ends_at, resource_type, selected):
    """Ressources libres sur la période, avec le montant estimé de chacune."""
    problem = _period_problem(profile, starts_at, ends_at)
    if problem:
        return {"problem": problem, "rows": []}
    rows = []
    free = services.available_resources(organization, starts_at, ends_at, resource_type or None)
    for resource in free.order_by("zone__name", "code"):
        amount, rate = quote(resource, starts_at, ends_at)
        rows.append({"resource": resource, "amount": amount, "rate": rate, "selected": resource.pk == selected})
    return {"problem": "", "rows": rows}


def _period_panel(organization, profile, source, selected):
    """Panneau « ressources libres » pour des valeurs reçues (GET ou POST) ; None tant que la période est incomplète."""
    if not all(source.get(key) for key in ("date", "start", "end")):
        return None
    period = PeriodForm(source)
    if not period.is_valid():
        problem = " ".join(str(text) for errors in period.errors.values() for text in errors)
        return {"problem": problem, "rows": []}
    data = period.cleaned_data
    return _availability(
        organization, profile, data["starts_at"], data["ends_at"], data.get("resource_type", ""), selected
    )


@coworking_required
def reservation_new(request):
    organization = request.organization
    profile = services.get_profile(organization)
    if not profile.booking_enabled:
        messages.info(
            request, "Les réservations à l'avance sont désactivées dans les règles de fonctionnement."
        )
        return redirect("staff:cw_planning")

    if request.method == "POST":
        source = request.POST
        form = ReservationForm(source)
    else:
        source = {
            "date": request.GET.get("date") or timezone.localdate().isoformat(),
            "start": request.GET.get("start", ""),
            "end": request.GET.get("end", ""),
            "resource_type": request.GET.get("type", ""),
            "customer_id": request.GET.get("customer", ""),
            "resource_id": request.GET.get("resource", ""),
        }
        form = ReservationForm(initial=source)

    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        try:
            reservation = services.create_reservation(
                organization=organization,
                customer_id=data["customer_id"],
                resource_id=data["resource_id"],
                starts_at=data["starts_at"],
                ends_at=data["ends_at"],
                created_by=request.user,
                notes=data["notes"],
            )
        except ValidationError as error:
            _add_errors(form, error)
        else:
            messages.success(
                request,
                f"Réservation de {reservation.resource.code} enregistrée pour "
                f"{reservation.customer.first_name} {reservation.customer.last_name} "
                f"({reservation.quoted_amount:,} XOF estimés).".replace(",", " "),
            )
            return redirect("staff:cw_reservation_detail", reservation.pk)

    customer_id = _to_int(source.get("customer_id"))
    customer = (
        Customer.objects.filter(pk=customer_id, organization=organization).first() if customer_id else None
    )
    return render(
        request,
        "staff/coworking/reservation_new.html",
        {
            "form": form,
            "customer": customer,
            "panel": _period_panel(organization, profile, source, _to_int(source.get("resource_id"))),
            "profile": profile,
        },
    )


@coworking_required
def reservation_availability(request):
    """Fragment HTMX : ressources libres et montant estimé pour la période saisie."""
    organization = request.organization
    profile = services.get_profile(organization)
    panel = _period_panel(
        organization, profile, request.GET, _to_int(request.GET.get("resource_id"))
    )
    return render(request, "staff/coworking/_availability.html", {"panel": panel})


@coworking_required
def reservation_customer_lookup(request):
    """Fragment HTMX : choix du client pour la réservation (même panneau que l'arrivée)."""
    panel = _customer_panel(request.organization, request.GET.get("customer_q", ""))
    return render(request, "staff/coworking/_customer_lookup.html", {"panel": panel})


# ---------------------------------------------------------------- fiche d'une réservation


@coworking_required
def reservation_detail(request, pk):
    organization = request.organization
    profile = services.get_profile(organization)
    reservation = get_object_or_404(
        Reservation.objects.select_related(
            "customer", "resource", "rate", "created_by", "cancelled_by"
        ),
        pk=pk,
        organization=organization,
    )
    now = timezone.now()
    sessions = list(
        reservation.sessions.select_related("resource").order_by("-checked_in_at")
    )
    confirmed = reservation.status == Reservation.Status.CONFIRMED
    free_of_session = confirmed and not sessions
    no_show_from = reservation.starts_at + timedelta(minutes=profile.no_show_grace_minutes)
    paid = services.amount_paid(reservation=reservation)
    context = {
        "reservation": reservation,
        "sessions": sessions,
        "payments": Payment.objects.filter(organization=organization, reservation=reservation)
        .select_related("recorded_by")
        .order_by("paid_at", "pk"),
        "paid": paid,
        "balance": max(reservation.quoted_amount - paid, 0),
        "profile": profile,
        "can_cancel": free_of_session,
        "late": free_of_session
        and reservation.starts_at - now < timedelta(hours=profile.cancellation_deadline_hours),
        "can_check_in": free_of_session
        and reservation.starts_at - timedelta(minutes=profile.check_in_early_minutes) <= now < reservation.ends_at,
        "can_no_show": free_of_session and now >= no_show_from,
        "no_show_from": no_show_from,
        "waiting_grace": free_of_session and now < no_show_from,
    }
    return render(request, "staff/coworking/reservation_detail.html", context)


@coworking_required
@require_POST
def reservation_cancel(request, pk):
    organization = request.organization
    reservation = get_object_or_404(Reservation, pk=pk, organization=organization)
    reason = request.POST.get("reason", "").strip()
    if not reason:
        messages.error(request, "Le motif de l'annulation est obligatoire.")
        return redirect("staff:cw_reservation_detail", reservation.pk)
    profile = services.get_profile(organization)
    late = reservation.starts_at - timezone.now() < timedelta(hours=profile.cancellation_deadline_hours)
    try:
        services.cancel_reservation(
            organization=organization,
            reservation_id=reservation.pk,
            performed_by=request.user,
            reason=reason,
        )
    except ValidationError as error:
        messages.error(request, _error_text(error))
    else:
        messages.success(request, "Réservation annulée. Elle reste visible dans l'historique.")
        if late:
            messages.warning(
                request,
                f"Annulation tardive (moins de {profile.cancellation_deadline_hours} h avant le début) : "
                "elle est notée dans le journal.",
            )
    return redirect("staff:cw_reservation_detail", reservation.pk)


@coworking_required
@require_POST
def reservation_no_show(request, pk):
    reservation = get_object_or_404(Reservation, pk=pk, organization=request.organization)
    try:
        services.mark_no_show(
            organization=request.organization, reservation_id=reservation.pk, performed_by=request.user
        )
    except ValidationError as error:
        messages.error(request, _error_text(error))
    else:
        messages.success(request, "Absence enregistrée.")
    return redirect("staff:cw_reservation_detail", reservation.pk)


# ---------------------------------------------------------------- historique des sessions et corrections


@coworking_required
def sessions(request):
    organization = request.organization
    text = request.GET.get("q", "").strip()
    state = request.GET.get("state", "")
    date_from = bq.parse_date(request.GET.get("from"))
    date_to = bq.parse_date(request.GET.get("to"))
    only_unpaid = request.GET.get("unpaid") == "1"
    only_corrected = request.GET.get("corrected") == "1"

    items = cq.with_balance(bq.with_corrected(bq.session_matches(organization, text)))
    if state in AccessSession.Status.values:
        items = items.filter(status=state)
    if date_from:
        items = items.filter(checked_in_at__gte=bq.local_day_bounds(date_from)[0])
    if date_to:
        items = items.filter(checked_in_at__lt=bq.local_day_bounds(date_to)[1])
    if only_unpaid:
        items = items.filter(status=AccessSession.Status.CLOSED, balance__gt=0)
    if only_corrected:
        items = items.filter(corrected=True)

    page, querystring = _paginate(
        request, items.select_related("customer", "resource").order_by("-checked_in_at", "-pk"), 30
    )
    for item in page.object_list:
        item.duration = _duration_label(item.checked_in_at, item.checked_out_at)
    return render(
        request,
        "staff/coworking/sessions.html",
        {
            "page": page,
            "querystring": querystring,
            "q": text,
            "state": state,
            "date_from": date_from,
            "date_to": date_to,
            "only_unpaid": only_unpaid,
            "only_corrected": only_corrected,
        },
    )


@coworking_required
def session_correct(request, pk):
    session = get_object_or_404(
        AccessSession.objects.select_related("customer", "resource", "reservation"),
        pk=pk,
        organization=request.organization,
    )
    initial = {"checked_in_at": timezone.localtime(session.checked_in_at), "amount": session.amount}
    if session.checked_out_at:
        initial["checked_out_at"] = timezone.localtime(session.checked_out_at)
    form = CorrectionForm(request.POST or None, session=session, initial=initial)
    if request.method == "POST" and form.is_valid():
        try:
            services.correct_session(
                organization=request.organization,
                session_id=session.pk,
                performed_by=request.user,
                reason=form.cleaned_data["reason"],
                **form.changes(),
            )
        except ValidationError as error:
            _add_errors(form, error)
        else:
            messages.success(request, "Session corrigée. Le motif et les valeurs d'avant sont conservés dans le journal.")
            return redirect("staff:cw_session_detail", session.pk)
    return render(
        request,
        "staff/coworking/session_correct.html",
        {"form": form, "session": session, "duration": _duration_label(session.checked_in_at, session.checked_out_at)},
    )
