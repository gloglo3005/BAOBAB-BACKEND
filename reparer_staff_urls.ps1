# Réparation : restaure apps\staff\urls.py (URL de l'interface web Staff)
# Ce fichier avait été écrasé par le contenu de apps\api\staff_urls.py (copie au mauvais endroit).
# À lancer depuis le dossier biblio (celui qui contient manage.py) :
#     powershell -ExecutionPolicy Bypass -File .\reparer_staff_urls.ps1
$ErrorActionPreference = "Stop"

if (-not (Test-Path "manage.py")) {
    throw "Lancez ce script depuis le dossier biblio (celui qui contient manage.py)."
}
$root = (Get-Location).Path
$backup = Join-Path $root "_sauvegarde_reparation"
$utf8 = New-Object System.Text.UTF8Encoding($false)
$target = Join-Path $root "apps\staff\urls.py"

if (-not (Test-Path $target)) {
    throw "apps\staff\urls.py est introuvable."
}
$current = [System.IO.File]::ReadAllText($target)
if ($current -notmatch "staff_catalog_views") {
    Write-Host "apps\staff\urls.py ne contient pas le contenu de l'API : rien à réparer, fichier laissé tel quel."
} else {
    $saved = Join-Path $backup "apps\staff\urls.py"
    New-Item -ItemType Directory -Force (Split-Path $saved -Parent) | Out-Null
    Copy-Item $target $saved -Force
    $content = @'
from django.urls import path

from . import views, views_coworking, views_coworking_booking, views_coworking_desk

app_name = "staff"

urlpatterns = [
    # Tableau de bord commun : bibliothèque et/ou coworking selon les modules activés.
    path("", views_coworking.dashboard, name="dashboard"),
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
    # Coworking : configuration (livraison 1)
    path("coworking/", views_coworking.setup, name="cw_setup"),
    path("coworking/zones/", views_coworking.zones, name="cw_zones"),
    path("coworking/zones/new/", views_coworking.zone_new, name="cw_zone_new"),
    path("coworking/zones/<int:pk>/", views_coworking.zone_edit, name="cw_zone_edit"),
    path("coworking/resources/", views_coworking.resources, name="cw_resources"),
    path("coworking/resources/new/", views_coworking.resource_new, name="cw_resource_new"),
    path("coworking/resources/<int:pk>/", views_coworking.resource_edit, name="cw_resource_edit"),
    path("coworking/rates/", views_coworking.rates, name="cw_rates"),
    path("coworking/rates/new/", views_coworking.rate_new, name="cw_rate_new"),
    path("coworking/rates/<int:pk>/", views_coworking.rate_edit, name="cw_rate_edit"),
    path("coworking/rules/", views_coworking.rules, name="cw_rules"),
    # Coworking : comptoir (livraison 2)
    path("coworking/customers/", views_coworking_desk.customers, name="cw_customers"),
    path("coworking/customers/new/", views_coworking_desk.customer_new, name="cw_customer_new"),
    path("coworking/customers/lookup/", views_coworking_desk.customer_lookup, name="cw_customer_lookup"),
    path("coworking/customers/<int:pk>/", views_coworking_desk.customer_detail, name="cw_customer_detail"),
    path("coworking/customers/<int:pk>/edit/", views_coworking_desk.customer_edit, name="cw_customer_edit"),
    path("coworking/customers/<int:pk>/action/", views_coworking_desk.customer_action, name="cw_customer_action"),
    path("coworking/reception/", views_coworking_desk.reception, name="cw_reception"),
    path("coworking/reception/check-in/", views_coworking_desk.check_in, name="cw_check_in"),
    path("coworking/sessions/<int:pk>/", views_coworking_desk.session_detail, name="cw_session_detail"),
    path("coworking/sessions/<int:pk>/check-out/", views_coworking_desk.check_out, name="cw_check_out"),
    path("coworking/unpaid/", views_coworking_desk.unpaid, name="cw_unpaid"),
    path("coworking/payments/", views_coworking_desk.payments, name="cw_payments"),
    path("coworking/payments/new/", views_coworking_desk.payment_new, name="cw_payment_new"),
    path("coworking/payments/customer-lookup/", views_coworking_desk.payment_customer_lookup, name="cw_payment_customer_lookup"),
    path("coworking/payments/<int:pk>/", views_coworking_desk.payment_receipt, name="cw_payment_receipt"),
    path("coworking/payments/<int:pk>/void/", views_coworking_desk.payment_void, name="cw_payment_void"),
    # Coworking : réservations et suivi (livraison 3)
    path("coworking/planning/", views_coworking_booking.planning, name="cw_planning"),
    path("coworking/reservations/", views_coworking_booking.reservations, name="cw_reservations"),
    path("coworking/reservations/new/", views_coworking_booking.reservation_new, name="cw_reservation_new"),
    path("coworking/reservations/availability/", views_coworking_booking.reservation_availability, name="cw_reservation_availability"),
    path("coworking/reservations/customer-lookup/", views_coworking_booking.reservation_customer_lookup, name="cw_reservation_customer_lookup"),
    path("coworking/reservations/<int:pk>/", views_coworking_booking.reservation_detail, name="cw_reservation_detail"),
    path("coworking/reservations/<int:pk>/cancel/", views_coworking_booking.reservation_cancel, name="cw_reservation_cancel"),
    path("coworking/reservations/<int:pk>/no-show/", views_coworking_booking.reservation_no_show, name="cw_reservation_no_show"),
    path("coworking/sessions/", views_coworking_booking.sessions, name="cw_sessions"),
    path("coworking/sessions/<int:pk>/correct/", views_coworking_booking.session_correct, name="cw_session_correct"),
]

'@
    [System.IO.File]::WriteAllText($target, $content, $utf8)
    Write-Host "restauré  : apps\staff\urls.py (ancienne version dans $backup)"
}

# Contrôle : le bon fichier de l'API doit contenir les routes de l'action 0.1
$api = Join-Path $root "apps\api\staff_urls.py"
if ((Test-Path $api) -and ([System.IO.File]::ReadAllText($api) -match "staff_updates_views")) {
    Write-Host "OK        : apps\api\staff_urls.py contient bien les routes renew / PATCH membre"
} else {
    Write-Host "ATTENTION : apps\api\staff_urls.py n'a pas encore l'action 0.1. Lancez ensuite installer_phase0_2.ps1 (il l'inclut)."
}
