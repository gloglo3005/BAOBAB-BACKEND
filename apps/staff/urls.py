from django.urls import path

from . import views

app_name = "staff"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("login/", views.StaffLoginView.as_view(), name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("password/", views.StaffPasswordChangeView.as_view(), name="password"),
    path("organization/", views.choose_organization, name="organizations"),
    path("search/", views.search, name="search"),
    path("loan/", views.loan, name="loan"),
    path("loan/member-lookup/", views.loan_member_lookup, name="loan_member_lookup"),
    path("loan/copy-lookup/", views.loan_copy_lookup, name="loan_copy_lookup"),
    path("loan/<int:pk>/receipt/", views.loan_receipt, name="loan_receipt"),
    path("return/", views.return_view, name="return"),
    path("return/lookup/", views.return_lookup, name="return_lookup"),
    path("overdue/", views.overdue, name="overdue"),
    path("members/", views.members, name="members"),
    path("members/new/", views.member_new, name="member_new"),
    path("members/<int:pk>/", views.member_detail, name="member_detail"),
    path("members/<int:pk>/action/", views.member_action, name="member_action"),
    path("catalog/", views.catalog, name="catalog"),
    path("catalog/new/", views.book_new, name="book_new"),
    path("catalog/settings/", views.catalog_settings, name="catalog_settings"),
    path("catalog/settings/action/", views.catalog_settings_action, name="catalog_settings_action"),
    path("catalog/<int:pk>/", views.book_detail, name="book_detail"),
    path("catalog/<int:pk>/edit/", views.book_edit, name="book_edit"),
    path("catalog/<int:pk>/archive/", views.book_archive, name="book_archive"),
    path("catalog/<int:pk>/copies/add/", views.copies_add, name="copies_add"),
    path("catalog/copies/<int:pk>/", views.copy_edit, name="copy_edit"),
    path("catalog/copies/<int:pk>/status/", views.copy_status, name="copy_status"),
    path("history/", views.history, name="history"),
]
