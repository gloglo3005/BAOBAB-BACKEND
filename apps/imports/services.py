import logging

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.audit.models import AuditLog
from apps.audit.services import log_action

from . import catalog_import
from .models import ImportBatch
from .readers import MAX_BYTES, ReadError, read_table

logger = logging.getLogger(__name__)


class ImportFailure(Exception):
    """L'import ne peut pas aller au bout ; le message est destiné à l'agent."""


def _read(data, filename):
    return read_table(
        data, filename, catalog_import.ALIASES, catalog_import.REQUIRED_COLUMNS
    )


def create_batch(*, organization, uploaded_file, performed_by=None):
    """Lit et analyse le fichier, puis enregistre l'import en « Prévisualisé ».

    Un fichier illisible (format, taille, encodage, colonne obligatoire absente,
    trop de lignes) est refusé tout de suite : rien n'est enregistré.
    Des erreurs ligne par ligne, elles, sont conservées dans l'aperçu.
    """
    if uploaded_file.size > MAX_BYTES:
        raise ValidationError("Le fichier dépasse 5 Mo.")
    data = uploaded_file.read()
    try:
        table = _read(data, uploaded_file.name)
    except ReadError as error:
        raise ValidationError(str(error)) from None

    plan = catalog_import.analyze(table, organization)
    summary = catalog_import.plan_summary(plan)

    uploaded_file.seek(0)
    with transaction.atomic():
        batch = ImportBatch.objects.create(
            organization=organization,
            user=performed_by,
            kind=ImportBatch.Kind.CATALOG,
            file_name=uploaded_file.name[:255],
            file=uploaded_file,
            status=ImportBatch.Status.UPLOADED,
        )
        batch.row_count = plan.row_count
        batch.error_count = len(plan.errors)
        batch.error_report = plan.errors[: catalog_import.MAX_STORED_ERRORS]
        batch.summary = summary
        batch.status = ImportBatch.Status.PREVIEWED
        batch.save()
    return batch


def _check_confirmable(batch):
    if batch.status == ImportBatch.Status.IMPORTED:
        raise ValidationError("Cet import a déjà été effectué.")
    if batch.status != ImportBatch.Status.PREVIEWED:
        raise ValidationError(
            f"Cet import ne peut plus être confirmé (statut : {batch.get_status_display()})."
        )
    if batch.error_count:
        raise ValidationError(
            "Cet import contient des erreurs : corrigez le fichier et renvoyez-le."
        )


def confirm_batch(*, batch_id, organization, performed_by=None):
    """Importe le fichier : tout ou rien, une seule ligne d'audit.

    Le verrou sur la ligne ImportBatch fait attendre un second clic jusqu'à la
    fin du premier ; il trouve alors « Importé » et est refusé. Le fichier est
    relu et réanalysé ici : si le catalogue a changé depuis l'aperçu (un code
    pris entre-temps), rien n'est importé.
    """
    try:
        with transaction.atomic():
            try:
                batch = ImportBatch.objects.select_for_update().get(
                    pk=batch_id, organization=organization
                )
            except ImportBatch.DoesNotExist:
                raise ValidationError("Import introuvable.") from None
            _check_confirmable(batch)

            with batch.file.open("rb") as handle:
                data = handle.read()
            plan = catalog_import.analyze(_read(data, batch.file.name), organization)
            if plan.errors:
                raise ImportFailure(
                    f"Le catalogue a changé depuis l'aperçu ({len(plan.errors)} erreur(s) "
                    "détectée(s)). Renvoyez le fichier pour un nouvel aperçu."
                )

            batch.status = ImportBatch.Status.CONFIRMED
            batch.save(update_fields=["status", "updated_at"])

            result = catalog_import.apply_plan(plan, organization)

            batch.status = ImportBatch.Status.IMPORTED
            batch.imported_count = plan.row_count
            batch.summary = {**batch.summary, "result": result}
            batch.save(update_fields=["status", "imported_count", "summary", "updated_at"])

            log_action(
                organization=organization,
                user=performed_by,
                action=AuditLog.Action.IMPORT_CONFIRMED,
                entity=batch,
                description=(
                    f"Import « {batch.file_name} » : {result['books_created']} livre(s) "
                    f"créé(s), {result['copies_created']} exemplaire(s) ajouté(s)."
                ),
                metadata={"file_name": batch.file_name, "rows": plan.row_count, **result},
            )
        return batch
    except ValidationError:
        raise
    except Exception as error:
        if isinstance(error, (ImportFailure, ReadError)):
            message = str(error)
        elif isinstance(error, IntegrityError):
            message = (
                "Un code d'inventaire ou un ISBN a été pris pendant l'import. "
                "Renvoyez le fichier pour un nouvel aperçu."
            )
        else:
            logger.exception("Import %s : erreur inattendue", batch_id)
            message = "Erreur inattendue pendant l'import."
        _mark_failed(batch_id, organization, performed_by, message)
        raise ValidationError(
            f"L'import a échoué : rien n'a été importé. {message}"
        ) from error


def _mark_failed(batch_id, organization, performed_by, message):
    """Après l'annulation de l'import : statut « Échoué » et ligne d'audit."""
    with transaction.atomic():
        batch = (
            ImportBatch.objects.select_for_update()
            .filter(pk=batch_id, organization=organization)
            .first()
        )
        if batch is None or batch.status != ImportBatch.Status.PREVIEWED:
            return
        batch.status = ImportBatch.Status.FAILED
        batch.error_report = [{"line": None, "message": message}]
        batch.save(update_fields=["status", "error_report", "updated_at"])
        log_action(
            organization=organization,
            user=performed_by,
            action=AuditLog.Action.IMPORT_FAILED,
            entity=batch,
            description=f"Import « {batch.file_name} » échoué : {message}",
            metadata={"file_name": batch.file_name, "message": message},
        )
