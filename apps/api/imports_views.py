"""API staff : import du catalogue (CSV ou XLSX) : /api/v1/staff/<organization_id>/imports/...

Mêmes services et mêmes règles que l'interface /staff/import/ : le fichier est
d'abord analysé (aperçu, rien n'est écrit dans le catalogue), puis confirmé
(tout ou rien, une seule ligne d'audit).
"""
import csv
import io

from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import HttpResponse
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import generics, serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.imports.catalog_import import COLUMNS
from apps.imports.forms import UploadForm
from apps.imports.models import ImportBatch
from apps.imports.services import confirm_batch, create_batch
from apps.imports.views import TEMPLATE_ROWS

from .permissions import STAFF_LIBRARY
from .staff_members_views import refuse_validation
from .staff_views import StaffPagination

ERRORS_SHOWN = 200
WARNINGS_SHOWN = 20
MATCHES_SHOWN = 100


class ImportBatchSerializer(serializers.ModelSerializer):
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    user_email = serializers.SerializerMethodField()
    can_confirm = serializers.BooleanField(read_only=True)

    class Meta:
        model = ImportBatch
        fields = [
            "id", "file_name", "status", "status_label", "row_count", "imported_count",
            "error_count", "created_at", "user_email", "can_confirm",
        ]

    def get_user_email(self, batch) -> str:
        return batch.user.email if batch.user else ""


def detail_payload(batch):
    """L'import avec son aperçu complet (résumé, erreurs, remarques, livres déjà présents)."""
    summary = batch.summary or {}
    return {
        **ImportBatchSerializer(batch).data,
        "summary": {
            key: summary.get(key)
            for key in (
                "books_new", "books_existing", "copies", "ignored_columns",
                "new_categories", "new_locations", "new_authors",
            )
        },
        "result": summary.get("result"),
        "errors": batch.error_report[:ERRORS_SHOWN],
        "errors_hidden": max(batch.error_count - ERRORS_SHOWN, 0),
        "warnings": summary.get("warnings", [])[:WARNINGS_SHOWN],
        "warnings_total": summary.get("warnings_total", 0),
        "matches": summary.get("matches", [])[:MATCHES_SHOWN],
        "matches_total": summary.get("matches_total", 0),
    }


def batches(request):
    return ImportBatch.objects.for_organization(request.organization).select_related("user")


def find_batch(request, pk):
    try:
        return batches(request).filter(pk=pk).first()
    except (ValueError, TypeError, DjangoValidationError):
        return None


class ImportListCreateView(generics.ListAPIView):
    """GET : historique des imports. POST : envoi d'un fichier (champ « file »), qui renvoie l'aperçu."""

    permission_classes = STAFF_LIBRARY
    pagination_class = StaffPagination
    serializer_class = ImportBatchSerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return ImportBatch.objects.none()
        return batches(self.request)

    @extend_schema(
        request={"multipart/form-data": {
            "type": "object", "properties": {"file": {"type": "string", "format": "binary"}},
            "required": ["file"],
        }},
        responses={201: OpenApiTypes.OBJECT},
    )
    def post(self, request, organization_id):
        form = UploadForm(request.POST, request.FILES)
        if not form.is_valid():
            messages = [m for errors in form.errors.values() for m in errors]
            return Response({"detail": " ".join(messages)}, status=status.HTTP_400_BAD_REQUEST)
        try:
            batch = create_batch(
                organization=request.organization,
                uploaded_file=form.cleaned_data["file"],
                performed_by=request.user,
            )
        except DjangoValidationError as exc:
            return Response({"detail": " ".join(exc.messages)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(detail_payload(batch), status=status.HTTP_201_CREATED)


class ImportTemplateView(APIView):
    """Modèle CSV : UTF-8 avec signature et point-virgule, pour s'ouvrir bien dans Excel."""

    permission_classes = STAFF_LIBRARY

    @extend_schema(responses={(200, "text/csv"): OpenApiTypes.BINARY})
    def get(self, request, organization_id):
        buffer = io.StringIO()
        writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")
        writer.writerow(COLUMNS)
        writer.writerows(TEMPLATE_ROWS)
        response = HttpResponse(
            "﻿" + buffer.getvalue(), content_type="text/csv; charset=utf-8"
        )
        response["Content-Disposition"] = 'attachment; filename="modele_import_catalogue.csv"'
        return response


class ImportDetailView(APIView):
    permission_classes = STAFF_LIBRARY

    @extend_schema(responses=OpenApiTypes.OBJECT)
    def get(self, request, organization_id, pk):
        batch = find_batch(request, pk)
        if batch is None:
            return Response({"detail": "Import introuvable."}, status=status.HTTP_404_NOT_FOUND)
        return Response(detail_payload(batch))


class ImportConfirmView(APIView):
    """POST : confirme l'import. 409 si l'aperçu contient des erreurs ou si l'import est déjà fait."""

    permission_classes = STAFF_LIBRARY

    @extend_schema(request=None, responses=OpenApiTypes.OBJECT)
    def post(self, request, organization_id, pk):
        batch = find_batch(request, pk)
        if batch is None:
            return Response({"detail": "Import introuvable."}, status=status.HTTP_404_NOT_FOUND)
        try:
            confirm_batch(
                batch_id=batch.pk, organization=request.organization,
                performed_by=request.user,
            )
        except DjangoValidationError as exc:
            return refuse_validation(exc)
        return Response(detail_payload(find_batch(request, pk)))
