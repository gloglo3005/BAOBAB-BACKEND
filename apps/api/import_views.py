"""API d'import du catalogue (CSV / XLSX) : /api/v1/staff/<organization_id>/imports/...

Même fonctionnement que les pages web : on envoie le fichier, on regarde l'aperçu
(livres à créer, doublons reconnus, erreurs ligne par ligne), puis on confirme.
Rien n'est écrit dans le catalogue avant la confirmation ; l'import est tout ou rien.
"""
import os

from django.core.exceptions import ValidationError as DjangoValidationError
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import generics, serializers, status
from rest_framework.generics import get_object_or_404
from rest_framework.parsers import MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.imports.catalog_import import COLUMNS
from apps.imports.models import ImportBatch
from apps.imports.readers import ALLOWED_EXTENSIONS, MAX_BYTES
from apps.imports.services import confirm_batch, create_batch
from apps.imports.views import TEMPLATE_ROWS

from .coworking_views import error_response
from .permissions import STAFF_LIBRARY
from .staff_views import StaffPagination
from .throttles import ImportRateThrottle

SHOWN_ERRORS = 200
SHOWN_WARNINGS = 20
SHOWN_MATCHES = 100


class ImportBatchSerializer(serializers.ModelSerializer):
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    user_email = serializers.SerializerMethodField()
    can_confirm = serializers.BooleanField(read_only=True)

    class Meta:
        model = ImportBatch
        fields = [
            "id", "file_name", "status", "status_label", "row_count", "imported_count",
            "error_count", "can_confirm", "user_email", "created_at",
        ]
        read_only_fields = fields

    def get_user_email(self, batch):
        return batch.user.email if batch.user_id else ""


class ImportBatchDetailSerializer(ImportBatchSerializer):
    """Aperçu complet : résumé, erreurs ligne par ligne, avertissements, livres déjà présents."""

    errors = serializers.SerializerMethodField()
    errors_hidden = serializers.SerializerMethodField()
    warnings = serializers.SerializerMethodField()
    matches = serializers.SerializerMethodField()
    summary = serializers.SerializerMethodField()
    result = serializers.SerializerMethodField()

    class Meta(ImportBatchSerializer.Meta):
        fields = ImportBatchSerializer.Meta.fields + [
            "summary", "result", "errors", "errors_hidden", "warnings", "matches",
        ]
        read_only_fields = fields

    def get_errors(self, batch):
        return batch.error_report[:SHOWN_ERRORS]

    def get_errors_hidden(self, batch):
        return max(batch.error_count - SHOWN_ERRORS, 0)

    def get_warnings(self, batch):
        return (batch.summary or {}).get("warnings", [])[:SHOWN_WARNINGS]

    def get_matches(self, batch):
        return (batch.summary or {}).get("matches", [])[:SHOWN_MATCHES]

    def get_result(self, batch):
        return (batch.summary or {}).get("result")

    def get_summary(self, batch):
        data = dict(batch.summary or {})
        for key in ("matches", "warnings", "result"):
            data.pop(key, None)
        return data


class ImportUploadSerializer(serializers.Serializer):
    file = serializers.FileField()

    def validate_file(self, uploaded):
        if os.path.splitext(uploaded.name)[1].lower() not in ALLOWED_EXTENSIONS:
            raise serializers.ValidationError("Format non pris en charge : envoyez un fichier .csv ou .xlsx.")
        if uploaded.size > MAX_BYTES:
            raise serializers.ValidationError("Le fichier dépasse 5 Mo.")
        return uploaded


class ImportThrottleMixin:
    def get_throttles(self):
        throttles = super().get_throttles()
        if self.request.method == "POST":
            throttles.append(ImportRateThrottle())
        return throttles


def batches(request):
    return ImportBatch.objects.filter(organization=request.organization).select_related("user")


class ImportListCreateView(ImportThrottleMixin, generics.ListAPIView):
    """GET : historique des imports. POST (multipart, champ « file ») : envoie un fichier et renvoie l'aperçu."""

    permission_classes = STAFF_LIBRARY
    pagination_class = StaffPagination
    serializer_class = ImportBatchSerializer
    parser_classes = [MultiPartParser]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return ImportBatch.objects.none()
        return batches(self.request)

    @extend_schema(request={"multipart/form-data": ImportUploadSerializer}, responses={201: ImportBatchDetailSerializer})
    def post(self, request, organization_id):
        data = ImportUploadSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        try:
            batch = create_batch(
                organization=request.organization,
                uploaded_file=data.validated_data["file"],
                performed_by=request.user,
            )
        except DjangoValidationError as exc:
            return Response({"file": exc.messages}, status=status.HTTP_400_BAD_REQUEST)
        return Response(ImportBatchDetailSerializer(batch).data, status=status.HTTP_201_CREATED)


class ImportDetailView(generics.RetrieveAPIView):
    permission_classes = STAFF_LIBRARY
    serializer_class = ImportBatchDetailSerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return ImportBatch.objects.none()
        return batches(self.request)


class ImportConfirmView(ImportThrottleMixin, APIView):
    """Importe le fichier prévisualisé (tout ou rien, une seule ligne d'audit)."""

    permission_classes = STAFF_LIBRARY

    @extend_schema(request=None, responses=ImportBatchDetailSerializer)
    def post(self, request, organization_id, pk):
        batch = get_object_or_404(batches(request), pk=pk)
        try:
            confirmed = confirm_batch(batch_id=batch.pk, organization=request.organization, performed_by=request.user)
        except DjangoValidationError as exc:
            return error_response(exc)
        return Response(ImportBatchDetailSerializer(confirmed).data)


class ImportTemplateView(APIView):
    """Colonnes et lignes d'exemple pour construire le modèle de fichier."""

    permission_classes = STAFF_LIBRARY

    @extend_schema(responses=OpenApiTypes.OBJECT)
    def get(self, request, organization_id):
        return Response({
            "filename": "modele_import_catalogue.csv",
            "columns": list(COLUMNS),
            "required": ["title"],
            "rows": TEMPLATE_ROWS,
        })
