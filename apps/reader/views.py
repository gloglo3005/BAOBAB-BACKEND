from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.views import LoginView, PasswordChangeView
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.members.models import Member
from apps.members.services import redeem_link_code, request_membership
from apps.organizations.models import Organization

from . import queries
from .access import member_required, reader_required
from .forms import LinkCodeForm, ReaderLoginForm, RegisterForm

PAGE_SIZE = 20

LOAN_PAGES = {
    "active": {
        "title": "Mes emprunts",
        "empty": "Vous n'avez aucun livre emprunté en ce moment.",
    },
    "overdue": {
        "title": "Mes retards",
        "empty": "Aucun retard : tous vos livres sont dans les temps.",
    },
    "history": {
        "title": "Historique",
        "empty": "Vous n'avez pas encore rendu de livre.",
    },
}


def _to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _error_text(error):
    return " ; ".join(error.messages)


def _paginate(request, queryset):
    page = Paginator(queryset, PAGE_SIZE).get_page(request.GET.get("page"))
    params = request.GET.copy()
    params.pop("page", None)
    return page, params.urlencode()


# ---------------------------------------------------------------- accès


class ReaderLoginView(LoginView):
    template_name = "reader/login.html"
    authentication_form = ReaderLoginForm
    redirect_authenticated_user = True

    def get_default_redirect_url(self):
        return reverse("reader:home")


@require_POST
def logout_view(request):
    logout(request)
    return redirect("reader:login")


def register(request):
    if request.user.is_authenticated:
        return redirect("reader:home")
    form = RegisterForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            user = form.save()
        except IntegrityError:
            form.add_error("email", "Un compte existe déjà avec cette adresse. Connectez-vous.")
        else:
            login(request, user)
            messages.success(
                request, "Compte créé. Choisissez maintenant votre bibliothèque."
            )
            return redirect("reader:join")
    return render(request, "reader/register.html", {"form": form})


class ReaderPasswordChangeView(PasswordChangeView):
    template_name = "reader/password.html"
    success_url = reverse_lazy("reader:profile")

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect("reader:login")
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        response = super().form_valid(form)
        user = self.request.user
        if user.must_change_password:
            user.must_change_password = False
            user.save(update_fields=["must_change_password"])
        messages.success(self.request, "Mot de passe modifié.")
        return response


# ---------------------------------------------------------------- comptes et bibliothèques


@reader_required
def home(request):
    return render(
        request, "reader/home.html", {"memberships": queries.my_memberships(request.user)}
    )


@reader_required
def profile(request):
    return render(
        request, "reader/profile.html", {"memberships": queries.my_memberships(request.user)}
    )


@reader_required
def join(request):
    """Rejoindre une bibliothèque : avec un code remis au comptoir, ou par demande."""
    code_form = LinkCodeForm(request.POST or None)
    if request.method == "POST" and code_form.is_valid():
        try:
            member = redeem_link_code(user=request.user, code=code_form.cleaned_data["code"])
        except ValidationError as error:
            code_form.add_error("code", _error_text(error))
        else:
            messages.success(request, "Votre compte est relié à votre fiche de membre.")
            return redirect("reader:library", pk=member.pk)

    mine = {m.organization_id: m for m in Member.objects.filter(user=request.user)}
    organizations = list(
        Organization.objects.filter(
            status=Organization.Status.ACTIVE, library_module=True
        ).order_by("name")
    )
    for organization in organizations:
        organization.my_member = mine.get(organization.pk)
    return render(
        request, "reader/join.html",
        {"code_form": code_form, "organizations": organizations},
    )


@reader_required
@require_POST
def join_request(request, org_id):
    organization = get_object_or_404(
        Organization, pk=org_id, status=Organization.Status.ACTIVE, library_module=True
    )
    try:
        member = request_membership(user=request.user, organization=organization)
    except ValidationError as error:
        messages.error(request, _error_text(error))
        return redirect("reader:join")
    messages.success(
        request,
        "Demande envoyée. La bibliothèque doit la valider ; "
        "en attendant, vous pouvez déjà consulter son catalogue.",
    )
    return redirect("reader:library", pk=member.pk)


# ---------------------------------------------------------------- une bibliothèque


@member_required
def library(request):
    today = timezone.localdate()
    upcoming = list(queries.member_loans(request.member, "active")[:3])
    for loan in upcoming:
        loan.days_left = (loan.due_date - today).days
    return render(
        request, "reader/library.html",
        {
            "member": request.member,
            "organization": request.organization,
            "upcoming": upcoming,
            "active_count": queries.member_loans(request.member, "active").count(),
        },
    )


@member_required
def catalog(request):
    text = request.GET.get("q", "").strip()
    category_id = _to_int(request.GET.get("category"))
    only_available = request.GET.get("available") == "1"
    books = queries.catalog_books(request.organization, text, category_id, only_available)
    page, querystring = _paginate(request, books)
    return render(
        request, "reader/catalog.html",
        {
            "member": request.member,
            "page": page,
            "querystring": querystring,
            "q": text,
            "category": category_id,
            "only_available": only_available,
            "categories": queries.catalog_categories(request.organization),
        },
    )


@member_required
def book_detail(request, book_id):
    book = queries.book_for_reader(request.organization, book_id)
    if book is None:
        return render(request, "reader/not_found.html", {"member": request.member}, status=404)
    mine = list(queries.member_loans(request.member, "active").filter(book_copy__book=book))
    locations = queries.available_by_location(book)
    return render(
        request, "reader/book_detail.html",
        {
            "member": request.member,
            "book": book,
            "authors": book.authors.all(),
            "locations": locations,
            "available": sum(count for _, count in locations),
            "my_loans": mine,
        },
    )


@member_required
def loans(request, kind):
    today = timezone.localdate()
    page, querystring = _paginate(request, queries.member_loans(request.member, kind))
    for loan in page.object_list:
        loan.days_left = (loan.due_date - today).days
    return render(
        request, "reader/loans.html",
        {
            "member": request.member,
            "kind": kind,
            "page": page,
            "querystring": querystring,
            **LOAN_PAGES[kind],
        },
    )
