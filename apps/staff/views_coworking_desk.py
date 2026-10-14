"""Interface Staff : comptoir coworking (clients, accueil, paiements).

Même principe que le reste : la structure vient de la session, chaque écriture
passe par les services de apps.coworking (règles, verrous, audit).
"""
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import urlencode
from django.views.decorators.http import require_POST

from apps.coworking import services
from apps.coworking.models import AccessSession, Customer, Payment, Reservation
from apps.coworking.pricing import quote

from . import queries_coworking as cq
from . import queries_coworking_booking as bq
from .access import coworking_required
from .forms_coworking_desk import CustomerForm, PaymentForm
from .views import _error_text, _paginate, _to_int
from .views_coworking import _add_errors, _object_form

# ---------------------------------------------------------------- outils


def _duration_label(start, end=None):
    minutes = max(int(((end or timezone.now()) - start).total_seconds() // 60), 0)
    hours, rest = divmod(minutes, 60)
    return f"{hours} h {rest:02d}" if hours else f"{rest} min"


def _customer_panel(organization, text):
    """Panneau de recherche client pour l'arrivée (même logique que la recherche de membre)."""
    text = text.strip()
    panel = {"query": text, "rows": [], "hint": ""}
    if len(text) < cq.MIN_QUERY_LENGTH:
        panel["hint"] = "Tapez au moins 2 caractères : numéro, nom, téléphone ou email."
        return panel
    customers = cq.with_open_session(cq.customer_matches(organization, text)).order_by(
        "last_name", "first_name"
    )[:6]
    for customer in customers:
        reason = ""
        if customer.status != Customer.Status.ACTIVE:
            reason = "Client suspendu"
        elif customer.has_open_session:
            reason = "Déjà en session"
        panel["rows"].append({"customer": customer, "reason": reason, "selectable": not reason})
    if not panel["rows"]:
        panel["hint"] = "Aucun client trouvé."
    elif len(panel["rows"]) == 1:
        only = panel["rows"][0]
        # Numéro exact scanné ou tapé : le client est sélectionné d'office.
        panel["single"] = only["selectable"] and only["customer"].customer_number.lower() == text.lower()
    return panel


def _session_queryset(organization):
    return cq.with_balance(
        AccessSession.objects.filter(organization=organization).select_related(
            "customer", "resource", "reservation"
        )
    )


# ---------------------------------------------------------------- clients


@coworking_required
def customers(request):
    text = request.GET.get("q", "").strip()
    state = request.GET.get("state", "")
    items = cq.with_open_session(cq.customer_matches(request.organization, text))
    if state in Customer.Status.values:
        items = items.filter(status=state)
    page, querystring = _paginate(request, items.order_by("last_name", "first_name"), 25)
    return render(
        request,
        "staff/coworking/customers.html",
        {"page": page, "querystring": querystring, "q": text, "state": state},
    )


@coworking_required
def customer_new(request):
    form = CustomerForm(request.POST or None, instance=Customer(), organization=request.organization)
    if request.method == "POST" and form.is_valid():
        try:
            customer = services.create_customer(
                organization=request.organization,
                performed_by=request.user,
                **{key: form.cleaned_data[key] for key in ("first_name", "last_name", "phone", "email")},
            )
        except ValidationError as error:
            _add_errors(form, error)
        else:
            messages.success(request, f"Client {customer.customer_number} créé.")
            return redirect("staff:cw_customer_detail", customer.pk)
    return render(
        request,
        "staff/coworking/form.html",
        {
            "form": form,
            "title": "Nouveau client",
            "back_url": reverse("staff:cw_customers"),
            "intro": "Avant de créer, vérifiez dans la liste que le client n'existe pas déjà.",
            "created": True,
        },
    )


@coworking_required
def customer_detail(request, pk):
    organization = request.organization
    customer = get_object_or_404(Customer, pk=pk, organization=organization)
    now = timezone.now()
    sessions = _session_queryset(organization).filter(customer=customer)
    open_session = sessions.filter(status=AccessSession.Status.OPEN).first()
    if open_session:
        open_session.duration = _duration_label(open_session.checked_in_at)
    unpaid = cq.unpaid_sessions(organization, customer)
    unpaid_total = sum(item.balance for item in unpaid)
    context = {
        "customer": customer,
        "open_session": open_session,
        "upcoming": Reservation.objects.filter(
            organization=organization,
            customer=customer,
            status=Reservation.Status.CONFIRMED,
            ends_at__gt=now,
        ).select_related("resource").order_by("starts_at")[:10],
        "sessions": sessions.filter(status=AccessSession.Status.CLOSED).order_by("-checked_in_at")[:10],
        "payments": Payment.objects.filter(organization=organization, customer=customer)
        .select_related("recorded_by").order_by("-paid_at", "-pk")[:10],
        "unpaid_total": unpaid_total,
        "unpaid_count": len(unpaid),
    }
    return render(request, "staff/coworking/customer_detail.html", context)


@coworking_required
def customer_edit(request, pk):
    customer = get_object_or_404(Customer, pk=pk, organization=request.organization)
    return _object_form(
        request,
        form_class=CustomerForm,
        instance=customer,
        created=False,
        title=f"Client {customer.customer_number}",
        back="staff:cw_customer_detail",
        back_args=(customer.pk,),
        sentence=lambda saved: f"Client {saved.customer_number} enregistré.",
    )


@coworking_required
@require_POST
def customer_action(request, pk):
    customer = get_object_or_404(Customer, pk=pk, organization=request.organization)
    action = request.POST.get("action", "")
    target = {"suspend": Customer.Status.SUSPENDED, "reactivate": Customer.Status.ACTIVE}.get(action)
    if target is None:
        messages.error(request, "Action inconnue.")
    elif customer.status == target:
        messages.info(request, "Le client est déjà dans cet état.")
    else:
        previous = customer.status
        customer.status = target
        try:
            services.save_object(
                organization=request.organization,
                user=request.user,
                instance=customer,
                created=False,
                metadata={"changes": {"status": {"from": previous, "to": target}}},
            )
        except ValidationError as error:
            messages.error(request, _error_text(error))
        else:
            if target == Customer.Status.SUSPENDED:
                messages.success(request, f"Client {customer.customer_number} suspendu : plus d'arrivée possible.")
            else:
                messages.success(request, f"Client {customer.customer_number} réactivé.")
    return redirect("staff:cw_customer_detail", customer.pk)


@coworking_required
def customer_lookup(request):
    """Fragment HTMX : résultats de recherche client pour l'arrivée."""
    panel = _customer_panel(request.organization, request.GET.get("customer_q", ""))
    return render(request, "staff/coworking/_customer_lookup.html", {"panel": panel})


# ---------------------------------------------------------------- accueil


@coworking_required
def reception(request):
    organization = request.organization
    profile = services.get_profile(organization)
    now = timezone.now()

    open_sessions = list(
        _session_queryset(organization)
        .filter(status=AccessSession.Status.OPEN)
        .order_by("checked_in_at")
    )
    for item in open_sessions:
        item.duration = _duration_label(item.checked_in_at, now)
        if item.reservation_id:
            item.estimate = item.reservation.quoted_amount
        else:
            item.estimate = quote(item.resource, item.checked_in_at, now)[0]

    customer_q = request.GET.get("customer_q", "")
    context = {
        "profile": profile,
        "open_sessions": open_sessions,
        "to_welcome": cq.reservations_to_welcome(organization, profile) if profile.booking_enabled else [],
        "walk_in_resources": cq.walk_in_resources(organization, profile) if profile.walk_in_enabled else [],
        "panel": _customer_panel(organization, customer_q) if customer_q else None,
        "customer_q": customer_q,
    }
    return render(request, "staff/coworking/reception.html", context)


def _back_to_reception(request):
    customer_q = request.POST.get("customer_q", "").strip()
    url = reverse("staff:cw_reception")
    return redirect(f"{url}?{urlencode({'customer_q': customer_q})}" if customer_q else url)


@coworking_required
@require_POST
def check_in(request):
    reservation_id = _to_int(request.POST.get("reservation_id"))
    customer_id = _to_int(request.POST.get("customer_id"))
    resource_id = _to_int(request.POST.get("resource_id"))
    if not reservation_id and not (customer_id and resource_id):
        messages.error(request, "Choisissez un client et une ressource avant d'enregistrer l'arrivée.")
        return _back_to_reception(request)
    try:
        session = services.check_in(
            organization=request.organization,
            performed_by=request.user,
            reservation_id=reservation_id,
            customer_id=None if reservation_id else customer_id,
            resource_id=None if reservation_id else resource_id,
        )
    except ValidationError as error:
        messages.error(request, _error_text(error))
        return _back_to_reception(request)
    messages.success(
        request,
        f"Arrivée de {session.customer.first_name} {session.customer.last_name} "
        f"sur {session.resource.code} enregistrée.",
    )
    return redirect("staff:cw_reception")


@coworking_required
@require_POST
def check_out(request, pk):
    session = get_object_or_404(AccessSession, pk=pk, organization=request.organization)
    try:
        closed = services.check_out(
            organization=request.organization, performed_by=request.user, session_id=session.pk
        )
    except ValidationError as error:
        messages.error(request, _error_text(error))
        return redirect("staff:cw_reception")
    if closed.amount:
        messages.success(request, f"Sortie enregistrée : {closed.amount:,} XOF à encaisser.".replace(",", " "))
    else:
        messages.success(request, "Sortie enregistrée : rien à payer.")
    return redirect("staff:cw_session_detail", closed.pk)


@coworking_required
def session_detail(request, pk):
    session = get_object_or_404(_session_queryset(request.organization), pk=pk)
    if session.status == AccessSession.Status.OPEN:
        session.duration = _duration_label(session.checked_in_at)
    else:
        session.duration = _duration_label(session.checked_in_at, session.checked_out_at)
    payments = Payment.objects.filter(organization=request.organization).filter(
        Q(session=session) | Q(reservation_id=session.reservation_id, reservation__isnull=False)
    ).select_related("recorded_by").order_by("paid_at", "pk")
    return render(
        request,
        "staff/coworking/session_detail.html",
        {"session": session, "payments": payments, "corrections": bq.session_corrections(session)},
    )


@coworking_required
def unpaid(request):
    items = cq.unpaid_sessions(request.organization).select_related("customer", "resource")
    page, querystring = _paginate(request, items.order_by("checked_out_at"), 25)
    return render(
        request,
        "staff/coworking/unpaid.html",
        {"page": page, "querystring": querystring, "summary": cq.unpaid_summary(request.organization)},
    )


# ---------------------------------------------------------------- paiements


@coworking_required
def payments(request):
    text = request.GET.get("q", "").strip()
    state = request.GET.get("state", "")
    method = request.GET.get("method", "")
    items = Payment.objects.filter(organization=request.organization).select_related(
        "customer", "recorded_by"
    )
    for token in cq.tokens(text):
        items = items.filter(
            Q(receipt_number__icontains=token)
            | Q(customer__customer_number__icontains=token)
            | Q(customer__first_name__icontains=token)
            | Q(customer__last_name__icontains=token)
        )
    if state in Payment.Status.values:
        items = items.filter(status=state)
    if method in Payment.Method.values:
        items = items.filter(method=method)
    page, querystring = _paginate(request, items.order_by("-paid_at", "-pk"), 30)
    return render(
        request,
        "staff/coworking/payments.html",
        {
            "page": page,
            "querystring": querystring,
            "q": text,
            "state": state,
            "method": method,
            "methods": Payment.Method.choices,
            "unpaid": cq.unpaid_summary(request.organization),
        },
    )


@coworking_required
def payment_customer_lookup(request):
    """Fragment HTMX : choix du client avant d'enregistrer un paiement."""
    text = request.GET.get("customer_q", "").strip()
    rows = []
    hint = ""
    if len(text) < cq.MIN_QUERY_LENGTH:
        hint = "Tapez au moins 2 caractères : numéro, nom, téléphone ou email."
    else:
        rows = list(cq.customer_matches(request.organization, text).order_by("last_name", "first_name")[:8])
        if not rows:
            hint = "Aucun client trouvé."
    return render(
        request, "staff/coworking/_customer_pick.html", {"rows": rows, "hint": hint, "query": text}
    )


@coworking_required
def payment_new(request):
    organization = request.organization
    session = customer = None
    session_id = _to_int(request.GET.get("session"))
    customer_id = _to_int(request.GET.get("customer"))
    if session_id:
        session = get_object_or_404(_session_queryset(organization), pk=session_id)
        customer = session.customer
        if session.status == AccessSession.Status.OPEN:
            messages.error(request, "Enregistrez d'abord la sortie du client : le montant n'est fixé qu'à ce moment.")
            return redirect("staff:cw_reception")
        if session.balance <= 0:
            messages.info(request, "Cette session est déjà soldée.")
            return redirect("staff:cw_session_detail", session.pk)
    elif customer_id:
        customer = get_object_or_404(Customer, pk=customer_id, organization=organization)
    if customer is None:
        return render(request, "staff/coworking/payment_pick.html")

    unpaid_sessions = cq.unpaid_sessions(organization, customer).select_related("resource").order_by("checked_out_at")
    initial = {"method": Payment.Method.CASH}
    if session is not None:
        initial.update({"session": session.pk, "amount": session.balance})
    form = PaymentForm(request.POST or None, sessions=unpaid_sessions, initial=initial)
    if request.method == "POST" and form.is_valid():
        target = form.cleaned_data["session"]
        try:
            payment = services.record_payment(
                organization=organization,
                performed_by=request.user,
                customer_id=customer.pk,
                amount=form.cleaned_data["amount"],
                method=form.cleaned_data["method"],
                session_id=target.pk if target else None,
                note=form.cleaned_data["note"],
            )
        except ValidationError as error:
            _add_errors(form, error)
        else:
            messages.success(request, f"Paiement {payment.receipt_number} enregistré.")
            return redirect("staff:cw_payment_receipt", payment.pk)
    return render(
        request,
        "staff/coworking/payment_new.html",
        {"form": form, "customer": customer, "unpaid_sessions": unpaid_sessions, "session": session},
    )


@coworking_required
def payment_receipt(request, pk):
    payment = get_object_or_404(
        Payment.objects.select_related("customer", "recorded_by", "voided_by", "session__resource", "reservation__resource"),
        pk=pk,
        organization=request.organization,
    )
    target_balance = None
    if payment.session_id:
        target_balance = _session_queryset(request.organization).get(pk=payment.session_id).balance
    return render(
        request,
        "staff/coworking/payment_receipt.html",
        {"payment": payment, "target_balance": target_balance},
    )


@coworking_required
@require_POST
def payment_void(request, pk):
    payment = get_object_or_404(Payment, pk=pk, organization=request.organization)
    reason = request.POST.get("reason", "").strip()
    try:
        services.void_payment(
            organization=request.organization,
            performed_by=request.user,
            payment_id=payment.pk,
            reason=reason,
        )
    except ValidationError as error:
        messages.error(request, _error_text(error))
    else:
        messages.success(request, f"Paiement {payment.receipt_number} annulé.")
    return redirect("staff:cw_payment_receipt", payment.pk)
