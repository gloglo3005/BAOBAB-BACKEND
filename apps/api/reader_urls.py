from django.urls import path

from . import reader_views as r

urlpatterns = [
    # Sans fiche de membre
    path("organizations/", r.ReaderOrganizationListView.as_view(), name="reader-organizations"),
    path("memberships/", r.MembershipListCreateView.as_view(), name="reader-memberships"),
    path("link/", r.LinkCodeRedeemView.as_view(), name="reader-link"),
    # Avec une fiche de membre
    path("<str:member_id>/summary/", r.ReaderSummaryView.as_view(), name="reader-summary"),
    path("<str:member_id>/categories/", r.ReaderCategoryListView.as_view(), name="reader-categories"),
    path("<str:member_id>/books/", r.ReaderBookListView.as_view(), name="reader-books"),
    path("<str:member_id>/books/<str:pk>/", r.ReaderBookDetailView.as_view(), name="reader-book-detail"),
    path("<str:member_id>/loans/", r.ReaderLoanListView.as_view(), name="reader-loans"),
]