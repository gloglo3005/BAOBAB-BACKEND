from django.conf import settings
from django.db import models


class Organization(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "En attente"
        ACTIVE = "ACTIVE", "Active"
        SUSPENDED = "SUSPENDED", "Suspendue"
        REJECTED = "REJECTED", "Refusée"

    class Module(models.TextChoices):
        LIBRARY = "LIBRARY", "Bibliothèque"
        COWORKING = "COWORKING", "Coworking"

    name = models.CharField("nom", max_length=200)
    slug = models.SlugField("identifiant court", max_length=100, unique=True)
    description = models.TextField(blank=True)
    address = models.CharField("adresse", max_length=255, blank=True)
    city = models.CharField("ville", max_length=100, blank=True)
    country = models.CharField("pays", max_length=100, default="Togo")
    phone = models.CharField("téléphone", max_length=30, blank=True)
    email = models.EmailField(blank=True)
    website = models.URLField("site web", blank=True)
    status = models.CharField(
        "statut", max_length=20, choices=Status.choices, default=Status.PENDING
    )
    # Modules activés pour cette structure. Chaque module ajouté plus tard
    # (cybercafé...) aura son propre interrupteur ici.
    library_module = models.BooleanField("module bibliothèque", default=True)
    coworking_module = models.BooleanField("module coworking", default=False)
    next_member_number = models.PositiveIntegerField(
        "prochain numéro de membre", default=1
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "structure"
        verbose_name_plural = "structures"
        ordering = ["name"]

    def __str__(self):
        return self.name

    def has_module(self, module):
        """Dit si un module est activé pour cette structure."""
        flags = {
            self.Module.LIBRARY: self.library_module,
            self.Module.COWORKING: self.coworking_module,
        }
        return flags[module]

    @property
    def enabled_modules(self):
        return [module for module in self.Module if self.has_module(module)]


class LoanPolicy(models.Model):
    organization = models.OneToOneField(
        Organization, on_delete=models.CASCADE, related_name="loan_policy"
    )
    loan_duration_days = models.PositiveIntegerField("durée du prêt (jours)", default=14)
    max_active_loans = models.PositiveIntegerField("prêts actifs maximum", default=3)
    max_renewals = models.PositiveIntegerField("renouvellements maximum", default=1)
    allow_renewal = models.BooleanField("renouvellement autorisé", default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "politique de prêt"
        verbose_name_plural = "politiques de prêt"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(loan_duration_days__gt=0),
                name="loanpolicy_duration_gt_0",
            ),
        ]

    def __str__(self):
        return f"Politique de {self.organization}"


class OrganizationStaff(models.Model):
    class Role(models.TextChoices):
        STAFF = "STAFF", "Personnel de structure"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="organization_staff_memberships",
    )
    organization = models.ForeignKey(
        Organization, on_delete=models.PROTECT, related_name="staff_members"
    )
    role = models.CharField(
        max_length=20, choices=Role.choices, default=Role.STAFF
    )
    active = models.BooleanField("actif", default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "membre du personnel"
        verbose_name_plural = "membres du personnel"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "organization"], name="unique_staff_per_organization"
            ),
        ]

    def __str__(self):
        return f"{self.user} - {self.organization}"