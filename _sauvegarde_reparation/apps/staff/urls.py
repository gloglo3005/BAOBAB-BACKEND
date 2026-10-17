from django.urls import path

from . import staff_catalog_views as k
from . import staff_circulation_views as c
from . import staff_members_views as m
from . import staff_updates_views as u
from . import import_views as imp
from . import staff_views as v
from . import team_views as t

urlpatterns = [
    path("dashboard/", v.DashboardView.as_view(), name="staff-dashboard"),
    # Import du catalogue (CSV / XLSX)
    path("imports/", imp.ImportListCreateView.as_view(), name="staff-imports"),
    path("imports/template/", imp.ImportTemplateView.as_view(), name="staff-import-template"),
    path("imports/<str:pk>/", imp.ImportDetailView.as_view(), name="staff-import-detail"),
    path("imports/<str:pk>/confirm/", imp.ImportConfirmView.as_view(), name="staff-import-confirm"),
    # Administration de la structure (responsable)
    path("team/", t.TeamListCreateView.as_view(), name="staff-team"),
    path("team/<str:pk>/", t.TeamDetailView.as_view(), name="staff-team-detail"),
    path("team/<str:pk>/reset-password/", t.TeamResetPasswordView.as_view(), name="staff-team-reset"),
    path("organization/", t.StructureOrganizationView.as_view(), name="staff-organization"),
    path("loan-policy/", t.LoanPolicyView.as_view(), name="staff-loan-policy"),
    path("audit/", t.StructureAuditListView.as_view(), name="staff-audit"),
    # Livres
    path("books/", k.BookListCreateView.as_view(), name="staff-books"),
    path("books/<str:pk>/", k.BookDetailUpdateView.as_view(), name="staff-book-detail"),
    path("books/<str:pk>/archive/", k.BookArchiveView.as_view(), name="staff-book-archive"),
    path("books/<str:pk>/restore/", k.BookRestoreView.as_view(), name="staff-book-restore"),
    path("books/<str:pk>/copies/", k.BookCopiesCreateView.as_view(), name="staff-book-copies"),
    # Exemplaires
    path("copies/", v.CopyListView.as_view(), name="staff-copies"),
    path("copies/<str:pk>/", k.CopyDetailUpdateView.as_view(), name="staff-copy-detail"),
    path("copies/<str:pk>/move/", k.CopyMoveView.as_view(), name="staff-copy-move"),
    path("copies/<str:pk>/status/", k.CopyStatusView.as_view(), name="staff-copy-status"),
    # Catégories et emplacements
    path("categories/", k.CategoryListCreateView.as_view(), name="staff-categories"),
    path("categories/<str:pk>/", k.CategoryDetailView.as_view(), name="staff-category-detail"),
    path("locations/", k.LocationListCreateView.as_view(), name="staff-locations"),
    path("locations/<str:pk>/", k.LocationDetailView.as_view(), name="staff-location-detail"),
    # Membres
    path("members/", m.MemberListCreateView.as_view(), name="staff-members"),
    path("members/<str:pk>/", u.MemberDetailUpdateView.as_view(), name="staff-member-detail"),
    path("members/<str:pk>/accept/", m.MemberAcceptView.as_view(), name="staff-member-accept"),
    path("members/<str:pk>/reject/", m.MemberRejectView.as_view(), name="staff-member-reject"),
    path("members/<str:pk>/suspend/", m.MemberSuspendView.as_view(), name="staff-member-suspend"),
    path(
        "members/<str:pk>/reactivate/",
        m.MemberReactivateView.as_view(),
        name="staff-member-reactivate",
    ),
    path("members/<str:pk>/link-code/", m.MemberLinkCodeView.as_view(), name="staff-member-link-code"),
    # Prêts
    path("loans/", c.LoanListCreateView.as_view(), name="staff-loans"),
    path("loans/<str:pk>/", v.LoanDetailView.as_view(), name="staff-loan-detail"),
    path("loans/<str:pk>/renew/", u.LoanRenewView.as_view(), name="staff-loan-renew"),
    path("loans/<str:pk>/return/", c.LoanReturnView.as_view(), name="staff-loan-return"),
    path("loans/<str:pk>/lost/", c.LoanLostView.as_view(), name="staff-loan-lost"),
]