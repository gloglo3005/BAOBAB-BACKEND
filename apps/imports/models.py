import uuid
from pathlib import Path

from django.conf import settings
from django.db import models

from apps.organizations.base import OrganizationOwnedModel


def upload_path(instance, filename):
    """Nom neutre : jamais le nom envoyé par l'agent, rangé par structure."""
    extension = Path(filename).suffix.lower()
    return f"imports/{instance.organization_id}/{uuid.uuid4().hex}{extension}"


class ImportBatch(OrganizationOwnedModel):
    class Kind(models.TextChoices):
        CATALOG = "CATALOG", "Catalogue (livres et exemplaires)"

    class Status(models.TextChoices):
        UPLOADED = "UPLOADED", "Téléversé"
        PREVIEWED = "PREVIEWED", "Prévisualisé"
        CONFIRMED = "CONFIRMED", "Confirmé"
        IMPORTED = "IMPORTED", "Importé"
        FAILED = "FAILED", "Échoué"

    kind = models.CharField(
        "type", max_length=20, choices=Kind.choices, default=Kind.CATALOG
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        null=True, blank=True, related_name="import_batches", verbose_name="auteur",
    )
    file_name = models.CharField("nom du fichier", max_length=255)
    file = models.FileField("fichier", upload_to=upload_path, max_length=255)
    row_count = models.PositiveIntegerField("lignes", default=0)
    imported_count = models.PositiveIntegerField("lignes importées", default=0)
    error_count = models.PositiveIntegerField("erreurs", default=0)
    status = models.CharField(
        "statut", max_length=20, choices=Status.choices, default=Status.UPLOADED
    )
    # Liste de {"line": numéro ou None, "message": texte} (les plus anciennes seulement).
    error_report = models.JSONField("rapport d'erreurs", default=list, blank=True)
    # Compteurs, références nouvelles, correspondances, puis résultat final.
    summary = models.JSONField("résumé", default=dict, blank=True)

    class Meta:
        verbose_name = "import"
        verbose_name_plural = "imports"
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["organization", "-created_at"])]

    def __str__(self):
        return f"{self.file_name} ({self.get_status_display()})"

    @property
    def can_confirm(self):
        return self.status == self.Status.PREVIEWED and self.error_count == 0
