from django.contrib import messages
from django.contrib.auth import logout
from django.contrib.auth.views import LoginView, PasswordChangeView
from apps.accounts.login_limits import StaffLoginForm
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils.http import urlencode
from django.views.decorators.http import require_POST

from apps.catalog.models import Book, BookCopy, Category, Location
from apps.catalog.services import (
    REFERENCE_MODELS,
    add_copies,
    archive_book,
    create_book,
    create_reference,
    get_or_create_authors,
    move_copy,
    rename_reference,
    restore_book,
    set_copy_status,
    set_reference_active,
    update_book,
    update_copy,
)
from apps.circulation.models import Loan
from apps.circulation.services import ReturnOutcome, checkout_copy, return_copy
from apps.members.models import Member
from apps.members.services import (
    LINK_CODE_VALIDITY_DAYS,
    accept_member,
    create_link_code,
    reactivate_member,
    reject_member,
    register_member,
    suspend_member,
)

from . import queries
from .access import SESSION_KEY, active_memberships, library_required, staff_required
from .forms import (
    BookForm,
    CopiesForm,
    CopyEditForm,
    CopyStatusForm,
    MemberForm,
    NewBookForm,
    ReferenceForm,
)

# ---------------------------------------------------------------- outils


def _render_page_or_partial(request, page, partial, context):
    """HTMX demande seulement le fragment ; un accès direct reçoit la page."""
    template = partial if request.headers.get("HX-Request") else page
    response = render(request, template, context)
    response["Vary"] = "HX-Request"
    return response


def _paginate(request, queryset, per_page):
    page = Paginator(queryset, per_page).get_page(request.GET.get("page"))
    params = request.GET.copy()
    params.pop("page", None)
    return page, params.urlencode()


def _to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _back(url_name, **params):
    params = {key: value for key, value in params.items() if value}
    url = reverse(url_name)
    return redirect(f"{url}?{urlencode(params)}" if params else url)


def _error_text(error):
    return " ; ".join(error.messages)


# ---------------------------------------------------------------- accès


class StaffLoginView(LoginView):
    template_name = "staff/login.html"
    authentication_form = StaffLoginForm
    redirect_authenticated_user = True

    def get_default_redirect_url(self):
        return reverse("staff:dashboard")


@require_POST
def logout_view(request):
    logout(request)
    return redirect("staff:login")


class StaffPasswordChangeView(PasswordChangeView):
    template_name = "staff/password_change.html"
    success_url = reverse_lazy("staff:dashboard")

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect("staff:login")
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        response = super().form_valid(form)
        user = self.request.user
        if user.must_change_password:
            user.must_change_password = False
            user.save(update_fields=["must_change_password"])
        messages.success(self.request, "Mot de passe modifié.")
        return response


def choose_organization(request):
    """Choix de la structure de travail quand le compte en a plusieurs."""
    if not request.user.is_authenticated:
        return redirect("staff:login")
    if request.user.must_change_password:
        return redirect("staff:password")

    memberships = active_memberships(request.user)
    if not memberships:
        return render(
            request,
            "staff/no_access.html",
            {"message": "Ce compte n'est rattaché à aucune structure active."},
            status=403,
        )

    if request.method == "POST":
        organization_id = _to_int(request.POST.get("organization"))
        chosen = next(
            (m for m in memberships if m.organization_id == organization_id), None
        )
        if chosen is None:
            messages.error(request, "Cette structure n'est pas disponible pour votre compte.")
        else:
            request.session[SESSION_KEY] = chosen.organization_id
            return redirect("staff:dashboard")

    return render(request, "staff/organizations.html", {"memberships": memberships})


# ---------------------------------------------------------------- accueil


@staff_required
def dashboard(request):
    context = {}
    if request.organization.library_module:
        context["stats"] = queries.dashboard_stats(request.organization)
    return render(request, "staff/dashboard.html", context)


# ---------------------------------------------------------------- recherche


@library_required
def search(request):
    text = request.GET.get("q", "").strip()
    results = queries.search_all(request.organization, text)
    context = {
        "q": text,
        "results": results,
        "too_short": bool(text) and results is None,
    }
    return _render_page_or_partial(
        request, "staff/search.html", "staff/_search_results.html", context
    )


# ---------------------------------------------------------------- prêt


def _member_panel(organization, text):
    text = (text or "").strip()
    panel = {"query": text, "rows": [], "too_short": False, "single": False}
    if not text:
        return panel
    if len(text) < queries.MIN_QUERY_LENGTH:
        panel["too_short"] = True
        return panel

    members = queries.member_matches(organization, text)
    exact = members.filter(member_number__iexact=text)
    members = exact if exact.exists() else members
    members = members.annotate(
        active_loans=Count("loans", filter=Q(loans__status=Loan.Status.ACTIVE), distinct=True)
    ).order_by("last_name", "first_name")[:6]
    panel["rows"] = [
        {"member": m, "selectable": m.status == Member.Status.ACCEPTED} for m in members
    ]
    panel["single"] = len(panel["rows"]) == 1 and panel["rows"][0]["selectable"]
    return panel


def _copy_panel(organization, text):
    text = (text or "").strip()
    panel = {"query": text, "rows": [], "too_short": False, "single": False}
    if not text:
        return panel
    if len(text) < queries.MIN_QUERY_LENGTH:
        panel["too_short"] = True
        return panel

    base = BookCopy.objects.filter(organization=organization).select_related("book")
    exact = base.filter(inventory_code__iexact=text)
    copies = list(exact if exact.exists() else base.filter(inventory_code__icontains=text)[:6])
    loans = queries.current_loans_by_copy(organization, copies)
    panel["rows"] = [
        {
            "copy": copy,
            "selectable": copy.status == BookCopy.Status.AVAILABLE and copy.book.active,
            "loan": loans.get(copy.pk),
        }
        for copy in copies
    ]
    panel["single"] = len(panel["rows"]) == 1 and panel["rows"][0]["selectable"]
    return panel


@library_required
def loan(request):
    if request.method == "POST":
        member_id = _to_int(request.POST.get("member_id"))
        copy_id = _to_int(request.POST.get("copy_id"))
        back = {
            "member_q": request.POST.get("member_q", ""),
            "copy_q": request.POST.get("copy_q", ""),
        }
        if member_id is None or copy_id is None:
            messages.error(request, "Choisissez un membre et un exemplaire avant de prêter.")
            return _back("staff:loan", **back)
        try:
            created = checkout_copy(
                organization=request.organization,
                book_copy_id=copy_id,
                member_id=member_id,
                issued_by=request.user,
                notes=request.POST.get("notes", "").strip(),
            )
        except ValidationError as error:
            messages.error(request, _error_text(error))
            return _back("staff:loan", **back)
        return redirect("staff:loan_receipt", pk=created.pk)

    member_q = request.GET.get("member_q", "")
    copy_q = request.GET.get("copy_q", "")
    return render(
        request,
        "staff/loan.html",
        {
            "member_q": member_q,
            "copy_q": copy_q,
            "member_panel": _member_panel(request.organization, member_q),
            "copy_panel": _copy_panel(request.organization, copy_q),
        },
    )


@library_required
def loan_member_lookup(request):
    text = request.GET.get("member_q", "")
    return render(
        request, "staff/_member_lookup.html",
        {"panel": _member_panel(request.organization, text)},
    )


@library_required
def loan_copy_lookup(request):
    text = request.GET.get("copy_q", "")
    return render(
        request, "staff/_copy_lookup.html",
        {"panel": _copy_panel(request.organization, text)},
    )


@library_required
def loan_receipt(request, pk):
    loan_obj = get_object_or_404(
        Loan.objects.for_organization(request.organization).select_related(
            "member", "book_copy__book", "issued_by"
        ),
        pk=pk,
    )
    return render(request, "staff/loan_receipt.html", {"loan": loan_obj})


# ---------------------------------------------------------------- retour


def _return_panel(organization, text):
    text = (text or "").strip()
    panel = {"query": text, "rows": [], "too_short": False, "single": False}
    if not text:
        return panel
    if len(text) < queries.MIN_QUERY_LENGTH:
        panel["too_short"] = True
        return panel

    active = (
        Loan.objects.for_organization(organization)
        .active()
        .select_related("member", "book_copy__book")
    )
    exact = active.filter(
        Q(book_copy__inventory_code__iexact=text) | Q(member__member_number__iexact=text)
    )
    loans = exact if exact.exists() else active.filter(book_copy__inventory_code__icontains=text)
    panel["rows"] = list(loans.order_by("due_date")[:8])
    panel["single"] = len(panel["rows"]) == 1
    return panel


@library_required
def return_view(request):
    if request.method == "POST":
        loan_id = _to_int(request.POST.get("loan_id"))
        outcome = request.POST.get("outcome", ReturnOutcome.GOOD.value)
        copy_q = request.POST.get("copy_q", "")
        if loan_id is None:
            messages.error(request, "Choisissez le prêt à clôturer.")
            return _back("staff:return", copy_q=copy_q)
        try:
            closed = return_copy(
                organization=request.organization,
                loan_id=loan_id,
                received_by=request.user,
                outcome=outcome,
                notes=request.POST.get("notes", "").strip(),
            )
        except (ValidationError, ValueError) as error:
            text = _error_text(error) if isinstance(error, ValidationError) else "État de retour inconnu."
            messages.error(request, text)
            return _back("staff:return", copy_q=copy_q)

        code = closed.book_copy.inventory_code
        if closed.status == Loan.Status.LOST:
            messages.warning(request, f"{code} déclaré perdu (membre {closed.member.member_number}).")
        else:
            messages.success(
                request,
                f"{code} rendu par {closed.member.member_number} : "
                f"exemplaire {closed.book_copy.get_status_display().lower()}.",
            )
        return redirect("staff:return")

    copy_q = request.GET.get("copy_q", "")
    return render(
        request,
        "staff/return.html",
        {
            "copy_q": copy_q,
            "panel": _return_panel(request.organization, copy_q),
            "outcome_choices": ReturnOutcome.choices,
        },
    )


@library_required
def return_lookup(request):
    text = request.GET.get("copy_q", "")
    return render(
        request, "staff/_return_lookup.html",
        {"panel": _return_panel(request.organization, text)},
    )


# ---------------------------------------------------------------- retards


@library_required
def overdue(request):
    loans = (
        Loan.objects.for_organization(request.organization)
        .overdue()
        .select_related("member", "book_copy__book")
        .order_by("due_date")
    )
    page, querystring = _paginate(request, loans, 25)
    return render(
        request, "staff/overdue.html", {"page": page, "querystring": querystring}
    )


# ---------------------------------------------------------------- membres


@library_required
def members(request):
    text = request.GET.get("q", "").strip()
    status = request.GET.get("status", "")
    queryset = queries.member_matches(request.organization, text)
    if status in Member.Status.values:
        queryset = queryset.filter(status=status)
    queryset = queryset.select_related("user").annotate(
        active_loans=Count("loans", filter=Q(loans__status=Loan.Status.ACTIVE), distinct=True)
    ).order_by("last_name", "first_name")
    page, querystring = _paginate(request, queryset, 25)
    return render(
        request,
        "staff/members.html",
        {
            "page": page,
            "querystring": querystring,
            "q": text,
            "status": status,
            "statuses": Member.Status.choices,
        },
    )


@library_required
def member_new(request):
    form = MemberForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            member = register_member(
                organization=request.organization,
                first_name=form.cleaned_data["first_name"].strip(),
                last_name=form.cleaned_data["last_name"].strip(),
                phone=form.cleaned_data["phone"].strip(),
                member_number=form.cleaned_data["member_number"],
                performed_by=request.user,
            )
        except ValidationError as error:
            form.add_error(None, _error_text(error))
        else:
            messages.success(request, f"Membre {member.member_number} inscrit.")
            return redirect("staff:member_detail", pk=member.pk)
    return render(request, "staff/member_new.html", {"form": form})


@library_required
def member_detail(request, pk):
    member = get_object_or_404(
        Member.objects.select_related("user"), pk=pk, organization=request.organization
    )
    loans = (
        Loan.objects.for_organization(request.organization)
        .filter(member=member)
        .select_related("book_copy__book")
    )
    return render(
        request,
        "staff/member_detail.html",
        {
            "member": member,
            "active_loans": loans.active().order_by("due_date"),
            "history": loans.exclude(status=Loan.Status.ACTIVE)[:20],
            "can_link": member.user_id is None and member.status == Member.Status.ACCEPTED,
        },
    )


MEMBER_ACTIONS = {
    "accept": (accept_member, "accepté"),
    "reject": (reject_member, "refusé"),
    "suspend": (suspend_member, "suspendu"),
    "reactivate": (reactivate_member, "réactivé"),
}


@library_required
@require_POST
def member_action(request, pk):
    member = get_object_or_404(Member, pk=pk, organization=request.organization)
    action = request.POST.get("action", "")

    if action == "link_code":
        try:
            code = create_link_code(member=member, created_by=request.user)
        except ValidationError as error:
            messages.error(request, _error_text(error))
        else:
            messages.success(
                request,
                f"Code de rattachement : {code.code} "
                f"(valable {LINK_CODE_VALIDITY_DAYS} jours, à donner au membre).",
            )
    elif action in MEMBER_ACTIONS:
        service, label = MEMBER_ACTIONS[action]
        try:
            service(member, performed_by=request.user)
        except ValidationError as error:
            messages.error(request, _error_text(error))
        else:
            messages.success(request, f"Membre {member.member_number} {label}.")
    else:
        messages.error(request, "Action inconnue.")
    return redirect("staff:member_detail", pk=member.pk)


# ---------------------------------------------------------------- catalogue


@library_required
def catalog(request):
    text = request.GET.get("q", "").strip()
    state = request.GET.get("state", "active")
    category_id = _to_int(request.GET.get("category"))

    books = (
        queries.with_copy_counts(queries.book_matches(request.organization, text))
        .prefetch_related("authors")
        .select_related("category")
        .order_by("title")
    )
    if state == "archived":
        books = books.filter(active=False)
    elif state != "all":
        state = "active"
        books = books.filter(active=True)
    if category_id:
        books = books.filter(category_id=category_id)

    page, querystring = _paginate(request, books, 25)
    return render(
        request,
        "staff/catalog.html",
        {
            "page": page,
            "querystring": querystring,
            "q": text,
            "state": state,
            "category": category_id,
            "categories": Category.objects.filter(organization=request.organization),
        },
    )


@library_required
def book_detail(request, pk):
    book = get_object_or_404(
        Book.objects.filter(organization=request.organization).select_related("category"),
        pk=pk,
    )
    copies = list(book.copies.select_related("location").order_by("inventory_code"))
    loans = queries.current_loans_by_copy(request.organization, copies)
    for copy in copies:
        copy.current_loan = loans.get(copy.pk)
    return render(
        request,
        "staff/book_detail.html",
        {
            "book": book,
            "authors": book.authors.all(),
            "copies": copies,
            "borrowed_count": sum(1 for c in copies if c.status == BookCopy.Status.BORROWED),
        },
    )


# ---------------------------------------------------------------- gestion du catalogue

BOOK_TEXT_FIELDS = ("subtitle", "isbn", "publisher", "language", "description")


def _book_values(data):
    """Champs d'un livre issus du formulaire (hors titre, catégorie, auteurs)."""
    values = {name: (data[name] or "").strip() for name in BOOK_TEXT_FIELDS}
    values["publication_year"] = data["publication_year"]
    return values


@library_required
def book_new(request):
    form = NewBookForm(request.POST or None, organization=request.organization)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        codes = data["codes"].splitlines()
        wants_copies = any(code.strip() for code in codes)
        try:
            with transaction.atomic():
                authors = get_or_create_authors(request.organization, data["authors_text"])
                book = create_book(
                    organization=request.organization,
                    title=data["title"],
                    authors=authors,
                    category=data["category"],
                    performed_by=request.user,
                    **_book_values(data),
                )
                created = (
                    add_copies(
                        book=book, codes=codes, location=data["location"],
                        condition=data["condition"], performed_by=request.user,
                    )
                    if wants_copies else []
                )
        except ValidationError as error:
            form.add_error(None, _error_text(error))
        else:
            text = f"Livre « {book.title} » créé"
            if created:
                text += f" avec {len(created)} exemplaire{'s' if len(created) > 1 else ''}"
            messages.success(request, text + ".")
            return redirect("staff:book_detail", pk=book.pk)
    return render(request, "staff/book_form.html", {"form": form, "book": None})


@library_required
def book_edit(request, pk):
    book = get_object_or_404(Book, pk=pk, organization=request.organization)
    initial = {
        "title": book.title,
        "subtitle": book.subtitle,
        "isbn": book.isbn,
        "publisher": book.publisher,
        "publication_year": book.publication_year,
        "language": book.language,
        "description": book.description,
        "category": book.category_id,
        "authors_text": "\n".join(str(author) for author in book.authors.all()),
    }
    form = BookForm(
        request.POST or None, initial=initial,
        organization=request.organization, current_category=book.category,
    )
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        try:
            with transaction.atomic():
                authors = get_or_create_authors(request.organization, data["authors_text"])
                update_book(
                    book=book,
                    authors=authors,
                    performed_by=request.user,
                    title=data["title"],
                    category=data["category"],
                    **_book_values(data),
                )
        except ValidationError as error:
            form.add_error(None, _error_text(error))
        else:
            messages.success(request, "Livre enregistré.")
            return redirect("staff:book_detail", pk=book.pk)
    return render(request, "staff/book_form.html", {"form": form, "book": book})


@library_required
@require_POST
def book_archive(request, pk):
    book = get_object_or_404(Book, pk=pk, organization=request.organization)
    try:
        if request.POST.get("action") == "restore":
            restore_book(book=book, performed_by=request.user)
            messages.success(request, f"« {book.title} » est de nouveau dans le catalogue.")
        else:
            archive_book(book=book, performed_by=request.user)
            messages.success(request, f"« {book.title} » est archivé.")
    except ValidationError as error:
        messages.error(request, _error_text(error))
    return redirect("staff:book_detail", pk=book.pk)


@library_required
def copies_add(request, pk):
    book = get_object_or_404(Book, pk=pk, organization=request.organization)
    form = CopiesForm(request.POST or None, organization=request.organization)
    if request.method == "POST" and form.is_valid():
        try:
            created = add_copies(
                book=book,
                codes=form.cleaned_data["codes"].splitlines(),
                location=form.cleaned_data["location"],
                condition=form.cleaned_data["condition"],
                performed_by=request.user,
            )
        except ValidationError as error:
            form.add_error(None, _error_text(error))
        else:
            messages.success(
                request,
                f"{len(created)} exemplaire{'s' if len(created) > 1 else ''} ajouté"
                f"{'s' if len(created) > 1 else ''}.",
            )
            return redirect("staff:book_detail", pk=book.pk)
    return render(request, "staff/copies_add.html", {"form": form, "book": book})


def _get_copy(request, pk):
    return get_object_or_404(
        BookCopy.objects.select_related("book", "location"),
        pk=pk, organization=request.organization,
    )


@library_required
def copy_edit(request, pk):
    copy = _get_copy(request, pk)
    initial = {
        "condition": copy.condition,
        "location": copy.location_id,
        "acquisition_date": copy.acquisition_date,
        "notes": copy.notes,
    }
    form = CopyEditForm(
        request.POST or None, initial=initial,
        organization=request.organization, current_location=copy.location,
    )
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        try:
            with transaction.atomic():
                update_copy(
                    copy=copy,
                    condition=data["condition"],
                    notes=data["notes"].strip(),
                    acquisition_date=data["acquisition_date"],
                )
                move_copy(copy=copy, location=data["location"], performed_by=request.user)
        except ValidationError as error:
            form.add_error(None, _error_text(error))
        else:
            messages.success(request, f"Exemplaire {copy.inventory_code} enregistré.")
            return redirect("staff:book_detail", pk=copy.book_id)

    loan_obj = None
    if copy.status == BookCopy.Status.BORROWED:
        loan_obj = (
            Loan.objects.for_organization(request.organization)
            .active().filter(book_copy=copy).select_related("member").first()
        )
    return render(
        request,
        "staff/copy_edit.html",
        {
            "form": form,
            "copy": copy,
            "loan": loan_obj,
            "status_form": CopyStatusForm(),
        },
    )


@library_required
@require_POST
def copy_status(request, pk):
    copy = _get_copy(request, pk)
    form = CopyStatusForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Statut inconnu.")
    else:
        try:
            set_copy_status(
                copy=copy, new_status=form.cleaned_data["status"], performed_by=request.user
            )
        except ValidationError as error:
            messages.error(request, _error_text(error))
        else:
            messages.success(request, f"Statut de {copy.inventory_code} modifié.")
    return redirect("staff:copy_edit", pk=copy.pk)


@library_required
def catalog_settings(request):
    organization = request.organization
    categories = (
        Category.objects.filter(organization=organization)
        .annotate(uses=Count("books", distinct=True)).order_by("name")
    )
    locations = (
        Location.objects.filter(organization=organization)
        .annotate(uses=Count("copies", distinct=True)).order_by("name")
    )
    return render(
        request,
        "staff/catalog_settings.html",
        {
            "categories": categories,
            "locations": locations,
            "category_form": ReferenceForm(prefix="category"),
            "location_form": ReferenceForm(prefix="location"),
        },
    )


@library_required
@require_POST
def catalog_settings_action(request):
    kind = request.POST.get("kind", "")
    action = request.POST.get("action", "")
    if kind not in REFERENCE_MODELS:
        messages.error(request, "Action inconnue.")
        return redirect("staff:catalog_settings")
    model, label = REFERENCE_MODELS[kind]

    try:
        if action == "add":
            form = ReferenceForm(request.POST, prefix=kind)
            if not form.is_valid():
                raise ValidationError("Le nom est obligatoire.")
            item = create_reference(
                kind=kind, organization=request.organization,
                name=form.cleaned_data["name"],
                description=form.cleaned_data["description"],
            )
            messages.success(request, f"« {item.name} » ajouté.")
        elif action in ("rename", "deactivate", "activate"):
            item = get_object_or_404(
                model, pk=_to_int(request.POST.get("id")), organization=request.organization
            )
            if action == "rename":
                item = rename_reference(item=item, name=request.POST.get("name", ""))
                messages.success(request, f"Renommé en « {item.name} ».")
            else:
                item = set_reference_active(item=item, active=action == "activate")
                messages.success(
                    request,
                    f"« {item.name} » {'réactivé' if item.active else 'désactivé'}.",
                )
        else:
            messages.error(request, "Action inconnue.")
    except ValidationError as error:
        messages.error(request, _error_text(error))
    return redirect("staff:catalog_settings")


# ---------------------------------------------------------------- historique


@library_required
def history(request):
    text = request.GET.get("q", "").strip()
    status = request.GET.get("status", "")
    loans = (
        Loan.objects.for_organization(request.organization)
        .select_related("member", "book_copy__book", "issued_by", "received_by")
    )
    for token in queries.tokens(text):
        loans = loans.filter(
            Q(book_copy__inventory_code__icontains=token)
            | Q(book_copy__book__title__icontains=token)
            | Q(member__member_number__icontains=token)
            | Q(member__first_name__icontains=token)
            | Q(member__last_name__icontains=token)
        )
    if status in Loan.Status.values:
        loans = loans.filter(status=status)
    page, querystring = _paginate(request, loans.order_by("-borrowed_at"), 30)
    return render(
        request,
        "staff/history.html",
        {
            "page": page,
            "querystring": querystring,
            "q": text,
            "status": status,
            "statuses": Loan.Status.choices,
        },
    )
