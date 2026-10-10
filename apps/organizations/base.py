from django.core.exceptions import ValidationError
from django.db import models

from .models import Organization


class OrganizationQuerySet(models.QuerySet):
    def for_organization(self, organization):
        return self.filter(organization=organization)


class OrganizationOwnedModel(models.Model):
    """Base de tout modèle qui appartient à une structure."""

    organization = models.ForeignKey(
        Organization,
        on_delete=models.PROTECT,
        related_name="%(app_label)s_%(class)s_set",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = OrganizationQuerySet.as_manager()

    class Meta:
        abstract = True


def check_same_organization(instance, **related):
    """Refuse tout objet lié qui appartient à une autre structure."""
    if instance.organization_id is None:
        return
    errors = {}
    for field_name, obj in related.items():
        if obj is not None and obj.organization_id != instance.organization_id:
            errors[field_name] = "Doit appartenir à la même structure."
    if errors:
        raise ValidationError(errors)