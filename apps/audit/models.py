from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models

from apps.organizations.models import Organization


class AuditLogImmutableError(Exception):
    """Levée quand on tente de modifier ou de supprimer une ligne du journal."""


class AuditLogQuerySet(models.QuerySet):
    """Le journal s'écrit et se lit, il ne se corrige pas en masse."""

    def update(self, **kwargs):
        raise AuditLogImmutableError("Le journal d'audit ne peut pas être modifié.")

    def bulk_update(self, objs, fields, batch_size=None):
        raise AuditLogImmutableError("Le journal d'audit ne peut pas être modifié.")

    def delete(self):
        raise AuditLogImmutableError("Le journal d'audit ne peut pas être supprimé.")


class AuditLog(models.Model):
    class Action(models.TextChoices):
        BOOK_CREATED = "BOOK_CREATED", "Livre créé"
        BOOK_UPDATED = "BOOK_UPDATED", "Livre modifié"
        BOOK_ARCHIVED = "BOOK_ARCHIVED", "Livre archivé"
        COPY_CREATED = "COPY_CREATED", "Exemplaire créé"
        COPY_MOVED = "COPY_MOVED", "Exemplaire déplacé"
        COPY_MARKED_LOST = "COPY_MARKED_LOST", "Exemplaire déclaré perdu"
        COPY_STATUS_CHANGED = "COPY_STATUS_CHANGED", "Statut d'exemplaire modifié"
        MEMBER_CREATED = "MEMBER_CREATED", "Membre créé"
        MEMBER_SUSPENDED = "MEMBER_SUSPENDED", "Membre suspendu"
        LOAN_CREATED = "LOAN_CREATED", "Prêt créé"
        LOAN_RETURNED = "LOAN_RETURNED", "Prêt retourné"
        LOAN_RENEWED = "LOAN_RENEWED", "Prêt renouvelé"
        MEMBER_UPDATED = "MEMBER_UPDATED", "Membre modifié"
        IMPORT_CONFIRMED = "IMPORT_CONFIRMED", "Import confirmé"
        IMPORT_FAILED = "IMPORT_FAILED", "Import échoué"
        # --- coworking
        COWORKING_PROFILE_UPDATED = "COWORKING_PROFILE_UPDATED", "Fiche coworking modifiée"
        ZONE_CREATED = "ZONE_CREATED", "Zone créée"
        ZONE_UPDATED = "ZONE_UPDATED", "Zone modifiée"
        RESOURCE_CREATED = "RESOURCE_CREATED", "Ressource créée"
        RESOURCE_UPDATED = "RESOURCE_UPDATED", "Ressource modifiée"
        RATE_CREATED = "RATE_CREATED", "Tarif créé"
        RATE_UPDATED = "RATE_UPDATED", "Tarif modifié"
        CUSTOMER_CREATED = "CUSTOMER_CREATED", "Client coworking créé"
        CUSTOMER_UPDATED = "CUSTOMER_UPDATED", "Client coworking modifié"
        RESERVATION_CREATED = "RESERVATION_CREATED", "Réservation créée"
        RESERVATION_CANCELLED = "RESERVATION_CANCELLED", "Réservation annulée"
        RESERVATION_NO_SHOW = "RESERVATION_NO_SHOW", "Absence enregistrée"
        SESSION_CHECKED_IN = "SESSION_CHECKED_IN", "Arrivée enregistrée"
        SESSION_CHECKED_OUT = "SESSION_CHECKED_OUT", "Sortie enregistrée"
        SESSION_CORRECTED = "SESSION_CORRECTED", "Session corrigée"
        PAYMENT_RECORDED = "PAYMENT_RECORDED", "Paiement enregistré"
        PAYMENT_VOIDED = "PAYMENT_VOIDED", "Paiement annulé"
        # --- administration des structures
        ORGANIZATION_CREATED = "ORGANIZATION_CREATED", "Structure créée"
        ORGANIZATION_UPDATED = "ORGANIZATION_UPDATED", "Structure modifiée"
        ORGANIZATION_STATUS_CHANGED = "ORGANIZATION_STATUS_CHANGED", "Statut de structure modifié"
        LOAN_POLICY_UPDATED = "LOAN_POLICY_UPDATED", "Politique de prêt modifiée"
        STAFF_CREATED = "STAFF_CREATED", "Compte du personnel créé"
        STAFF_UPDATED = "STAFF_UPDATED", "Compte du personnel modifié"
        STAFF_PASSWORD_RESET = "STAFF_PASSWORD_RESET", "Mot de passe du personnel réinitialisé"

    organization = models.ForeignKey(
        Organization, on_delete=models.PROTECT, related_name="audit_logs",
        verbose_name="structure",
    )
    # Vide quand l'action vient d'une commande ou du système.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        null=True, blank=True, related_name="audit_logs", verbose_name="auteur",
    )
    action = models.CharField("action", max_length=40, choices=Action.choices)
    entity_type = models.CharField("type d'objet", max_length=50)
    entity_id = models.CharField("identifiant de l'objet", max_length=64)
    description = models.TextField("description", blank=True)
    metadata = models.JSONField(
        "détails", default=dict, blank=True, encoder=DjangoJSONEncoder
    )
    created_at = models.DateTimeField("date", auto_now_add=True)

    objects = AuditLogQuerySet.as_manager()

    class Meta:
        verbose_name = "ligne du journal"
        verbose_name_plural = "journal d'audit"
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["organization", "-created_at"]),
            models.Index(fields=["organization", "action"]),
            models.Index(fields=["entity_type", "entity_id"]),
        ]

    def __str__(self):
        return f"{self.created_at:%d/%m/%Y %H:%M} {self.action} {self.entity_type}#{self.entity_id}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise AuditLogImmutableError("Le journal d'audit ne peut pas être modifié.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise AuditLogImmutableError("Le journal d'audit ne peut pas être supprimé.")
