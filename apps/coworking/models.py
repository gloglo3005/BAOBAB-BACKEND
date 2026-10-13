"""Domaine coworking : espaces, ressources, tarifs, clients, réservations, sessions, paiements.

Tout appartient à une Organization (même socle que la bibliothèque). Rien ici ne
réutilise Book, BookCopy, Loan ou Member.

Choix de modélisation (voir le cahier des charges v3, partie II) :
- une ressource est réservable par une seule personne à la fois ; un open space
  se modélise avec un poste par ressource ;
- les montants sont des entiers en francs CFA (XOF n'a pas de centimes) ;
- le tarif appliqué est recopié sur la réservation / la session : changer un
  tarif plus tard ne modifie pas l'historique ;
- rien ne se supprime : on annule, on clôt, on annule un paiement (statut).
"""
import datetime

from django.conf import settings
from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import DateTimeRangeField, RangeBoundary, RangeOperators
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Func, Q
from django.db.models.functions import Lower
from django.utils import timezone

from apps.organizations.base import OrganizationOwnedModel, check_same_organization
from apps.organizations.models import Organization

CURRENCY = "XOF"
DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class TsTzRange(Func):
    function = "TSTZRANGE"
    output_field = DateTimeRangeField()


def _parse_hhmm(value):
    try:
        hours, minutes = str(value).split(":")
        return datetime.time(int(hours), int(minutes))
    except (ValueError, TypeError):
        raise ValidationError(f"Heure invalide : « {value} » (format attendu HH:MM).")


class CoworkingProfile(models.Model):
    """Fiche et règles de fonctionnement du coworking d'une structure."""

    organization = models.OneToOneField(
        Organization, on_delete=models.CASCADE, related_name="coworking_profile"
    )
    public_description = models.TextField("présentation publique", blank=True)
    # {"mon": ["08:00", "18:00"], "tue": ["08:00", "18:00"], "sun": null, ...}
    # Vide = aucune restriction d'horaire. Un jour absent ou null = fermé.
    opening_hours = models.JSONField("horaires d'ouverture", default=dict, blank=True)
    booking_enabled = models.BooleanField("réservation à l'avance", default=True)
    walk_in_enabled = models.BooleanField("accueil sans réservation", default=True)
    min_booking_minutes = models.PositiveIntegerField("durée minimale (min)", default=30)
    max_booking_minutes = models.PositiveIntegerField("durée maximale (min)", default=480)
    cancellation_deadline_hours = models.PositiveIntegerField(
        "délai d'annulation (heures avant le début)", default=2
    )
    no_show_grace_minutes = models.PositiveIntegerField(
        "délai de grâce avant absence (min)", default=30
    )
    walk_in_buffer_minutes = models.PositiveIntegerField(
        "marge avant une réservation pour un accueil sans réservation (min)", default=30
    )
    check_in_early_minutes = models.PositiveIntegerField(
        "arrivée autorisée avant l'heure réservée (min)", default=15
    )
    next_customer_number = models.PositiveIntegerField(default=1)
    next_receipt_number = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "fiche coworking"
        verbose_name_plural = "fiches coworking"

    def __str__(self):
        return f"Coworking de {self.organization}"

    def clean(self):
        super().clean()
        errors = {}
        if self.min_booking_minutes > self.max_booking_minutes:
            errors["min_booking_minutes"] = "La durée minimale dépasse la durée maximale."
        try:
            self.parsed_opening_hours()
        except ValidationError as error:
            errors["opening_hours"] = error.messages
        if errors:
            raise ValidationError(errors)

    def parsed_opening_hours(self):
        """{0: (ouverture, fermeture), ...} (0 = lundi) ; lève ValidationError si le format est faux."""
        hours = self.opening_hours or {}
        if not isinstance(hours, dict):
            raise ValidationError("Les horaires doivent être un objet {jour: [ouverture, fermeture]}.")
        parsed = {}
        for key, value in hours.items():
            if key not in DAY_KEYS:
                raise ValidationError(f"Jour inconnu : « {key} » (attendu : {', '.join(DAY_KEYS)}).")
            if value is None:
                continue
            if not (isinstance(value, (list, tuple)) and len(value) == 2):
                raise ValidationError(f"Horaires de {key} : attendu [ouverture, fermeture] ou null.")
            opening, closing = _parse_hhmm(value[0]), _parse_hhmm(value[1])
            if opening >= closing:
                raise ValidationError(f"Horaires de {key} : l'ouverture doit précéder la fermeture.")
            parsed[DAY_KEYS.index(key)] = (opening, closing)
        return parsed

    def is_open_between(self, starts_at, ends_at):
        """La période tient-elle entièrement dans une journée d'ouverture ?"""
        if not self.opening_hours:
            return True
        start, end = timezone.localtime(starts_at), timezone.localtime(ends_at)
        if start.date() != end.date():
            return False
        window = self.parsed_opening_hours().get(start.weekday())
        if window is None:
            return False
        return window[0] <= start.time() and end.time() <= window[1]


class Zone(OrganizationOwnedModel):
    name = models.CharField("nom", max_length=100)
    description = models.TextField(blank=True)
    active = models.BooleanField("active", default=True)

    class Meta:
        verbose_name = "zone"
        verbose_name_plural = "zones"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                Lower("name"), "organization", name="unique_cw_zone_name_per_organization"
            ),
        ]

    def __str__(self):
        return self.name


class Resource(OrganizationOwnedModel):
    class Type(models.TextChoices):
        DESK = "DESK", "Poste individuel"
        ROOM = "ROOM", "Salle"
        OTHER = "OTHER", "Autre"

    zone = models.ForeignKey(Zone, on_delete=models.PROTECT, related_name="resources")
    code = models.CharField("code", max_length=50)
    name = models.CharField("nom", max_length=100)
    resource_type = models.CharField(
        "type", max_length=10, choices=Type.choices, default=Type.DESK
    )
    capacity = models.PositiveIntegerField("capacité (personnes)", default=1)
    active = models.BooleanField("active", default=True)
    notes = models.TextField(blank=True)

    class Meta:
        verbose_name = "ressource"
        verbose_name_plural = "ressources"
        ordering = ["code"]
        constraints = [
            models.UniqueConstraint(
                Lower("code"), "organization", name="unique_cw_resource_code_per_organization"
            ),
            models.CheckConstraint(
                condition=Q(capacity__gte=1), name="cw_resource_capacity_gte_1"
            ),
        ]

    def __str__(self):
        return f"{self.code} - {self.name}"

    def clean(self):
        super().clean()
        check_same_organization(self, zone=self.zone if self.zone_id else None)


class Rate(OrganizationOwnedModel):
    """Un tarif : pour une ressource précise OU pour un type de ressource."""

    class Unit(models.TextChoices):
        HOUR = "HOUR", "Heure"
        HALF_DAY = "HALF_DAY", "Demi-journée (4 h)"
        DAY = "DAY", "Journée (8 h)"

    resource = models.ForeignKey(
        Resource, on_delete=models.PROTECT, null=True, blank=True, related_name="rates"
    )
    resource_type = models.CharField(
        "type de ressource", max_length=10, choices=Resource.Type.choices, blank=True
    )
    unit = models.CharField("unité", max_length=10, choices=Unit.choices)
    amount = models.PositiveIntegerField("montant (XOF)")
    currency = models.CharField("devise", max_length=3, default=CURRENCY)
    valid_from = models.DateField("valable à partir du", null=True, blank=True)
    valid_until = models.DateField("valable jusqu'au", null=True, blank=True)
    active = models.BooleanField("actif", default=True)

    class Meta:
        verbose_name = "tarif"
        verbose_name_plural = "tarifs"
        ordering = ["resource_type", "unit"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(resource__isnull=False, resource_type="")
                    | (Q(resource__isnull=True) & ~Q(resource_type=""))
                ),
                name="cw_rate_resource_xor_type",
            ),
            models.CheckConstraint(
                condition=(
                    Q(valid_from__isnull=True)
                    | Q(valid_until__isnull=True)
                    | Q(valid_until__gte=F("valid_from"))
                ),
                name="cw_rate_valid_period",
            ),
        ]

    def __str__(self):
        target = self.resource or self.get_resource_type_display()
        return f"{target} : {self.amount} {self.currency} / {self.get_unit_display()}"

    def clean(self):
        super().clean()
        check_same_organization(self, resource=self.resource if self.resource_id else None)
        if self.resource_id and self.resource_type:
            raise ValidationError("Choisissez une ressource OU un type de ressource, pas les deux.")
        if not self.resource_id and not self.resource_type:
            raise ValidationError("Choisissez une ressource ou un type de ressource.")


class Customer(OrganizationOwnedModel):
    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Actif"
        SUSPENDED = "SUSPENDED", "Suspendu"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        null=True, blank=True, related_name="coworking_customers",
    )
    customer_number = models.CharField("numéro client", max_length=50)
    first_name = models.CharField("prénom", max_length=100)
    last_name = models.CharField("nom", max_length=100)
    phone = models.CharField("téléphone", max_length=30, blank=True)
    email = models.EmailField(blank=True)
    status = models.CharField(
        "statut", max_length=20, choices=Status.choices, default=Status.ACTIVE
    )

    class Meta:
        verbose_name = "client coworking"
        verbose_name_plural = "clients coworking"
        ordering = ["last_name", "first_name", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "customer_number"],
                name="unique_cw_customer_number_per_organization",
            ),
            models.UniqueConstraint(
                fields=["organization", "user"],
                condition=Q(user__isnull=False),
                name="unique_cw_customer_user_per_organization",
            ),
        ]

    def __str__(self):
        return f"{self.customer_number} - {self.first_name} {self.last_name}"


class Reservation(OrganizationOwnedModel):
    class Status(models.TextChoices):
        CONFIRMED = "CONFIRMED", "Confirmée"
        CANCELLED = "CANCELLED", "Annulée"
        NO_SHOW = "NO_SHOW", "Absence"
        COMPLETED = "COMPLETED", "Terminée"

    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="reservations")
    resource = models.ForeignKey(Resource, on_delete=models.PROTECT, related_name="reservations")
    starts_at = models.DateTimeField("début")
    ends_at = models.DateTimeField("fin")
    status = models.CharField(
        "statut", max_length=20, choices=Status.choices, default=Status.CONFIRMED
    )
    # Tarif appliqué au moment de la réservation (recopié : l'historique ne bouge plus).
    rate = models.ForeignKey(
        Rate, on_delete=models.PROTECT, null=True, blank=True, related_name="reservations"
    )
    quoted_amount = models.PositiveIntegerField("montant estimé", default=0)
    currency = models.CharField("devise", max_length=3, default=CURRENCY)
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    cancel_reason = models.CharField(max_length=200, blank=True)

    class Meta:
        verbose_name = "réservation"
        verbose_name_plural = "réservations"
        ordering = ["-starts_at", "-pk"]
        indexes = [models.Index(fields=["organization", "starts_at"])]
        constraints = [
            models.CheckConstraint(
                condition=Q(ends_at__gt=F("starts_at")), name="cw_reservation_end_after_start"
            ),
            # Garde-fou en base : deux réservations confirmées ne se chevauchent pas
            # sur la même ressource (même si deux requêtes arrivent ensemble).
            ExclusionConstraint(
                name="cw_reservation_no_overlap",
                expressions=[
                    (F("resource"), RangeOperators.EQUAL),
                    (TsTzRange("starts_at", "ends_at", RangeBoundary()), RangeOperators.OVERLAPS),
                ],
                condition=Q(status="CONFIRMED"),
            ),
        ]

    def __str__(self):
        return f"{self.resource.code} {self.starts_at:%d/%m %H:%M}-{self.ends_at:%H:%M}"

    def clean(self):
        super().clean()
        check_same_organization(
            self,
            customer=self.customer if self.customer_id else None,
            resource=self.resource if self.resource_id else None,
            rate=self.rate if self.rate_id else None,
        )


class AccessSession(OrganizationOwnedModel):
    """Présence réelle d'un client sur une ressource (arrivée, sortie)."""

    class Status(models.TextChoices):
        OPEN = "OPEN", "En cours"
        CLOSED = "CLOSED", "Terminée"

    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="sessions")
    resource = models.ForeignKey(Resource, on_delete=models.PROTECT, related_name="sessions")
    reservation = models.ForeignKey(
        Reservation, on_delete=models.PROTECT, null=True, blank=True, related_name="sessions"
    )
    checked_in_at = models.DateTimeField("arrivée", default=timezone.now)
    checked_out_at = models.DateTimeField("sortie", null=True, blank=True)
    status = models.CharField("statut", max_length=10, choices=Status.choices, default=Status.OPEN)
    rate = models.ForeignKey(
        Rate, on_delete=models.PROTECT, null=True, blank=True, related_name="sessions"
    )
    amount = models.PositiveIntegerField("montant dû", default=0)
    currency = models.CharField("devise", max_length=3, default=CURRENCY)
    notes = models.TextField(blank=True)
    checked_in_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    checked_out_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    class Meta:
        verbose_name = "session de présence"
        verbose_name_plural = "sessions de présence"
        ordering = ["-checked_in_at", "-pk"]
        indexes = [models.Index(fields=["organization", "checked_in_at"])]
        constraints = [
            models.UniqueConstraint(
                fields=["resource"], condition=Q(status="OPEN"),
                name="cw_one_open_session_per_resource",
            ),
            models.UniqueConstraint(
                fields=["customer"], condition=Q(status="OPEN"),
                name="cw_one_open_session_per_customer",
            ),
            models.CheckConstraint(
                condition=(
                    Q(status="OPEN", checked_out_at__isnull=True)
                    | Q(status="CLOSED", checked_out_at__isnull=False)
                ),
                name="cw_session_status_matches_checkout",
            ),
            models.CheckConstraint(
                condition=Q(checked_out_at__isnull=True) | Q(checked_out_at__gte=F("checked_in_at")),
                name="cw_session_checkout_after_checkin",
            ),
        ]

    def __str__(self):
        return f"{self.customer} sur {self.resource.code}"

    def clean(self):
        super().clean()
        check_same_organization(
            self,
            customer=self.customer if self.customer_id else None,
            resource=self.resource if self.resource_id else None,
            reservation=self.reservation if self.reservation_id else None,
            rate=self.rate if self.rate_id else None,
        )


class Payment(OrganizationOwnedModel):
    """Règlement enregistré à la main (aucun paiement en ligne). Jamais supprimé : on l'annule."""

    class Method(models.TextChoices):
        CASH = "CASH", "Espèces"
        MOBILE_MONEY = "MOBILE_MONEY", "Mobile Money"
        BANK = "BANK", "Virement / banque"
        OTHER = "OTHER", "Autre"

    class Status(models.TextChoices):
        RECORDED = "RECORDED", "Enregistré"
        VOIDED = "VOIDED", "Annulé"

    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="payments")
    reservation = models.ForeignKey(
        Reservation, on_delete=models.PROTECT, null=True, blank=True, related_name="payments"
    )
    session = models.ForeignKey(
        AccessSession, on_delete=models.PROTECT, null=True, blank=True, related_name="payments"
    )
    receipt_number = models.CharField("n° de reçu", max_length=30)
    amount = models.PositiveIntegerField("montant")
    currency = models.CharField("devise", max_length=3, default=CURRENCY)
    method = models.CharField("moyen", max_length=20, choices=Method.choices, default=Method.CASH)
    status = models.CharField("statut", max_length=10, choices=Status.choices, default=Status.RECORDED)
    paid_at = models.DateTimeField("payé le", default=timezone.now)
    note = models.CharField(max_length=200, blank=True)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    voided_at = models.DateTimeField(null=True, blank=True)
    voided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    void_reason = models.CharField(max_length=200, blank=True)

    class Meta:
        verbose_name = "paiement"
        verbose_name_plural = "paiements"
        ordering = ["-paid_at", "-pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "receipt_number"],
                name="unique_cw_receipt_per_organization",
            ),
            models.CheckConstraint(condition=Q(amount__gt=0), name="cw_payment_amount_gt_0"),
            models.CheckConstraint(
                condition=Q(reservation__isnull=True) | Q(session__isnull=True),
                name="cw_payment_one_target",
            ),
        ]

    def __str__(self):
        return f"{self.receipt_number} : {self.amount} {self.currency}"

    def clean(self):
        super().clean()
        check_same_organization(
            self,
            customer=self.customer if self.customer_id else None,
            reservation=self.reservation if self.reservation_id else None,
            session=self.session if self.session_id else None,
        )
