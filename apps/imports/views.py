import csv
import io

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.staff.access import library_required

from .catalog_import import COLUMNS
from .forms import UploadForm
from .models import ImportBatch
from .services import confirm_batch, create_batch

TEMPLATE_ROWS = [
    ["Une vie de boy", "BIB-001", "", "Ferdinand Oyono", "Roman", "1956",
     "Présence Africaine", "Français", "Rayon A", "GOOD"],
    ["Une vie de boy", "BIB-002", "", "Ferdinand Oyono", "Roman", "1956",
     "Présence Africaine", "Français", "Rayon A", "GOOD"],
    ["Une vie de boy", "BIB-003", "", "Ferdinand Oyono", "Roman", "1956",
     "Présence Africaine", "Français", "Rayon A", "GOOD"],
]
PAGE_SIZE = 20


def _error_text(error):
    return " ; ".join(error.messages)


def _batches(request):
    return ImportBatch.objects.for_organization(request.organization).select_related("user")


@library_required
def import_upload(request):
    if request.method == "POST":
        form = UploadForm(request.POST, request.FILES)
        if form.is_valid():
            try:
                batch = create_batch(
                    organization=request.organization,
                    uploaded_file=form.cleaned_data["file"],
                    performed_by=request.user,
                )
            except ValidationError as error:
                form.add_error("file", _error_text(error))
            else:
                return redirect("imports:import_detail", pk=batch.pk)
    else:
        form = UploadForm()
    return render(
        request, "imports/upload.html",
        {"form": form, "recent": _batches(request)[:5]},
    )


@library_required
def import_template(request):
    """Modèle CSV : UTF-8 avec signature et point-virgule, pour s'ouvrir bien dans Excel."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")
    writer.writerow(COLUMNS)
    writer.writerows(TEMPLATE_ROWS)
    response = HttpResponse(
        "\ufeff" + buffer.getvalue(), content_type="text/csv; charset=utf-8"
    )
    response["Content-Disposition"] = 'attachment; filename="modele_import_catalogue.csv"'
    return response


@library_required
def import_detail(request, pk):
    batch = get_object_or_404(_batches(request), pk=pk)
    summary = batch.summary or {}
    return render(
        request, "imports/detail.html",
        {
            "batch": batch,
            "summary": summary,
            "result": summary.get("result"),
            "errors": batch.error_report[:200],
            "errors_hidden": max(batch.error_count - 200, 0),
            "warnings": summary.get("warnings", [])[:20],
            "warnings_total": summary.get("warnings_total", 0),
            "matches": summary.get("matches", [])[:100],
            "matches_total": summary.get("matches_total", 0),
        },
    )


@library_required
@require_POST
def import_confirm(request, pk):
    batch = get_object_or_404(_batches(request), pk=pk)
    try:
        confirm_batch(
            batch_id=batch.pk, organization=request.organization,
            performed_by=request.user,
        )
    except ValidationError as error:
        messages.error(request, _error_text(error))
    else:
        messages.success(request, "Import terminé.")
    return redirect("imports:import_detail", pk=batch.pk)


@library_required
def import_history(request):
    page = Paginator(_batches(request), PAGE_SIZE).get_page(request.GET.get("page"))
    return render(
        request, "imports/history.html",
        {"page": page, "querystring": ""},
    )
