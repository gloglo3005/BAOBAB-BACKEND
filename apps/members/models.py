from django.conf import settings
from django.db import models

from apps.organizations.base import OrganizationOwnedModel, check_same_organization

class Member(OrganizationOwnedModel):
    class Status(models.TextChoices):
        PENDING = "PENDING", "En attente"
        ACCEPTED = "ACCEPTED", "Accepté"
        REJECTED = "REJECTED", "Refusé"
        SUSPENDED = "SUSPENDED", "Suspendu"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="memberships",
    )
    member_number = models.CharField("numéro de membre", max_length=50)
    first_name = models.CharField("prénom", max_length=100)
    last_name = models.CharField("nom", max_length=100)
    phone = models.CharField("téléphone", max_length=30, blank=True)
    status = models.CharField(
        "statut", max_length=20, choices=Status.choices, default=Status.PENDING
    )
    joined_at = models.DateField("date d'adhésion", null=True, blank=True)

    class Meta:
        verbose_name = "membre"
        verbose_name_plural = "membres"
        ordering = ["last_name", "first_name"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "member_number"],
                name="unique_member_number_per_organization",
            ),
            models.UniqueConstraint(
                fields=["organization", "user"],
                condition=models.Q(user__isnull=False),
                name="unique_member_user_per_organization",
            ),
        ]

    def __str__(self):
        return f"{self.member_number} - {self.first_name} {self.last_name}"

class LinkCode(OrganizationOwnedModel):
    member = models.ForeignKey(
        Member, on_delete=models.PROTECT, related_name="link_codes"
    )
    code = models.CharField("code", max_length=20, unique=True)
    expires_at = models.DateTimeField("expire le")
    used_at = models.DateTimeField("utilisé le", null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta:
        verbose_name = "code de rattachement"
        verbose_name_plural = "codes de rattachement"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.code} ({self.member})"

    def clean(self):
        super().clean()
        check_same_organization(self, member=self.member if self.member_id else None)    