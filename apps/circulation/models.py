from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.catalog.models import BookCopy
from apps.organizations.base import OrganizationOwnedModel, OrganizationQuerySet, check_same_organization
from apps.members.models import Member


class LoanQuerySet(OrganizationQuerySet):
    def active(self):
        return self.filter(status=Loan.Status.ACTIVE)

    def overdue(self):
        return self.active().filter(due_date__lt=timezone.localdate())


class Loan(OrganizationOwnedModel):
    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "En cours"
        RETURNED = "RETURNED", "Rendu"
        LOST = "LOST", "Perdu"

    book_copy = models.ForeignKey(
        BookCopy, on_delete=models.PROTECT, related_name="loans"
    )
    member = models.ForeignKey(
        Member, on_delete=models.PROTECT, related_name="loans"
    )
    borrowed_at = models.DateTimeField("emprunté le", default=timezone.now)
    due_date = models.DateField("à rendre le")
    returned_at = models.DateTimeField("rendu le", null=True, blank=True)
    status = models.CharField(
        "statut", max_length=20, choices=Status.choices, default=Status.ACTIVE
    )
    renewal_count = models.PositiveIntegerField("renouvellements", default=0)
    issued_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="loans_issued",
    )
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="loans_received",
    )
    notes = models.TextField(blank=True)

    objects = LoanQuerySet.as_manager()

    class Meta:
        verbose_name = "prêt"
        verbose_name_plural = "prêts"
        ordering = ["-borrowed_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["book_copy"],
                condition=models.Q(status="ACTIVE"),
                name="unique_active_loan_per_copy",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(status="ACTIVE", returned_at__isnull=True)
                    | models.Q(status="RETURNED", returned_at__isnull=False)
                    | models.Q(status="LOST", returned_at__isnull=True)
                ),
                name="loan_status_matches_returned_at",
            ),
        ]

    def __str__(self):
        return f"{self.book_copy.inventory_code} -> {self.member}"

    @property
    def is_overdue(self):
        return (
            self.status == self.Status.ACTIVE
            and self.due_date < timezone.localdate()
        )

    @property
    def days_overdue(self):
        if not self.is_overdue:
            return 0
        return (timezone.localdate() - self.due_date).days

    def clean(self):
        super().clean()
        check_same_organization(
            self,
            book_copy=self.book_copy if self.book_copy_id else None,
            member=self.member if self.member_id else None,
        )