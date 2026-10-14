"""Formulaires de l'interface coworking : zones, ressources, tarifs, règles de fonctionnement.

La structure (organization) n'est jamais un champ : elle vient de la session du staff.
Les contrôles d'unicité sont faits ici avec des messages lisibles ; le service
`save_object` refait ensuite une validation complète avant d'enregistrer.
"""
import datetime

from django import forms
from django.core.exceptions import ValidationError
from django.db.models import Q

from apps.coworking.models import DAY_KEYS, CoworkingProfile, Rate, Resource, Zone

DAY_LABELS = [
    ("mon", "Lundi"),
    ("tue", "Mardi"),
    ("wed", "Mercredi"),
    ("thu", "Jeudi"),
    ("fri", "Vendredi"),
    ("sat", "Samedi"),
    ("sun", "Dimanche"),
]


def _active_or_current(model, organization, current=None):
    """Éléments actifs de la structure, plus l'élément déjà choisi même désactivé."""
    condition = Q(active=True)
    if current is not None and current.pk:
        condition |= Q(pk=current.pk)
    return model.objects.filter(organization=organization).filter(condition)


class OrganizationModelForm(forms.ModelForm):
    """Base : connaît la structure de travail et n'affiche pas les messages techniques de contrainte."""

    def __init__(self, *args, organization, **kwargs):
        super().__init__(*args, **kwargs)
        self.organization = organization

    def validate_constraints(self):
        # Les règles d'unicité sont vérifiées dans clean_*() avec un message clair ;
        # le service rejoue full_clean() avant d'enregistrer.
        return


class ZoneForm(OrganizationModelForm):
    class Meta:
        model = Zone
        fields = ["name", "description", "active"]
        widgets = {"description": forms.Textarea(attrs={"rows": 3})}
        help_texts = {
            "active": "Une zone désactivée rend ses ressources indisponibles à la réservation et à l'accueil.",
        }

    def clean_name(self):
        name = " ".join(self.cleaned_data["name"].split())
        taken = Zone.objects.filter(organization=self.organization, name__iexact=name)
        if taken.exclude(pk=self.instance.pk).exists():
            raise ValidationError("Une zone porte déjà ce nom.")
        return name


class ResourceForm(OrganizationModelForm):
    class Meta:
        model = Resource
        fields = ["zone", "code", "name", "resource_type", "capacity", "active", "notes"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 3})}
        help_texts = {
            "code": "Le code identifie la ressource au comptoir (ex. D01, SALLE-A). Il doit être unique.",
            "capacity": "Une ressource est utilisée par une seule personne ou un seul groupe à la fois : "
                        "pour un open space, créez un poste par place.",
            "active": "Une ressource désactivée n'est plus proposée, mais son historique reste.",
        }

    def __init__(self, *args, organization, **kwargs):
        super().__init__(*args, organization=organization, **kwargs)
        self.fields["zone"].queryset = _active_or_current(
            Zone, organization, self.instance.zone if self.instance.zone_id else None
        ).order_by("name")
        self.fields["zone"].empty_label = "Choisissez une zone"

    def clean_code(self):
        code = self.cleaned_data["code"].strip()
        taken = Resource.objects.filter(organization=self.organization, code__iexact=code)
        if taken.exclude(pk=self.instance.pk).exists():
            raise ValidationError("Une ressource porte déjà ce code.")
        return code


class RateForm(OrganizationModelForm):
    class Meta:
        model = Rate
        fields = ["resource", "resource_type", "unit", "amount", "valid_from", "valid_until", "active"]
        labels = {
            "resource": "Ressource précise",
            "resource_type": "Ou type de ressource",
            "amount": "Montant (XOF)",
        }
        help_texts = {
            "resource": "Choisissez une ressource précise OU un type, pas les deux.",
            "resource_type": "Le tarif vaut pour toutes les ressources de ce type, sauf celles qui ont leurs propres tarifs.",
            "amount": "En francs CFA, nombre entier. 0 = gratuit.",
            "valid_from": "Facultatif. Vide = sans limite.",
            "valid_until": "Facultatif. Vide = sans limite.",
        }
        widgets = {
            "valid_from": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "valid_until": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
        }

    def __init__(self, *args, organization, **kwargs):
        super().__init__(*args, organization=organization, **kwargs)
        self.fields["resource"].queryset = _active_or_current(
            Resource, organization, self.instance.resource if self.instance.resource_id else None
        ).order_by("code")
        self.fields["resource"].empty_label = "Aucune"
        self.fields["resource"].required = False

    def clean(self):
        cleaned = super().clean()
        start, end = cleaned.get("valid_from"), cleaned.get("valid_until")
        if start and end and end < start:
            self.add_error("valid_until", "La date de fin précède la date de début.")
        return cleaned


def _time_or_none(value):
    try:
        hours, minutes = str(value).split(":")
        return datetime.time(int(hours), int(minutes))
    except (ValueError, TypeError):
        return None


class ProfileForm(OrganizationModelForm):
    """Règles de fonctionnement + horaires jour par jour (à la place du JSON)."""

    class Meta:
        model = CoworkingProfile
        fields = [
            "public_description",
            "booking_enabled",
            "walk_in_enabled",
            "min_booking_minutes",
            "max_booking_minutes",
            "cancellation_deadline_hours",
            "no_show_grace_minutes",
            "walk_in_buffer_minutes",
            "check_in_early_minutes",
        ]
        widgets = {"public_description": forms.Textarea(attrs={"rows": 3})}
        help_texts = {
            "booking_enabled": "Si décoché, seules les arrivées sans réservation sont possibles.",
            "walk_in_enabled": "Si décoché, une arrivée doit correspondre à une réservation.",
            "cancellation_deadline_hours": "Une annulation plus tardive reste possible, mais elle est signalée dans le journal.",
            "no_show_grace_minutes": "Après ce délai suivant l'heure de début, une absence peut être enregistrée.",
            "walk_in_buffer_minutes": "Un accueil sans réservation est refusé si une réservation commence dans ce délai.",
            "check_in_early_minutes": "Un client peut s'enregistrer jusqu'à ce délai avant l'heure réservée.",
        }

    def __init__(self, *args, organization, **kwargs):
        super().__init__(*args, organization=organization, **kwargs)
        stored = self.instance.opening_hours if isinstance(self.instance.opening_hours, dict) else {}
        for key, label in DAY_LABELS:
            window = stored.get(key) or [None, None]
            for suffix, text, value in (("open", "ouverture", window[0]), ("close", "fermeture", window[1])):
                name = f"{key}_{suffix}"
                self.fields[name] = forms.TimeField(
                    label=f"{label} : {text}",
                    required=False,
                    widget=forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
                )
                self.initial[name] = _time_or_none(value)

    def day_rows(self):
        """[(libellé, champ ouverture, champ fermeture)] pour le gabarit."""
        return [(label, self[f"{key}_open"], self[f"{key}_close"]) for key, label in DAY_LABELS]

    def clean(self):
        cleaned = super().clean()
        hours = {}
        for key in DAY_KEYS:
            opening, closing = cleaned.get(f"{key}_open"), cleaned.get(f"{key}_close")
            if opening is None and closing is None:
                continue
            if opening is None or closing is None:
                self.add_error(f"{key}_close", "Renseignez l'ouverture et la fermeture, ou laissez les deux vides.")
            elif opening >= closing:
                self.add_error(f"{key}_close", "L'ouverture doit précéder la fermeture.")
            else:
                hours[key] = [opening.strftime("%H:%M"), closing.strftime("%H:%M")]
        # Tout vide = aucune restriction d'horaire ; sinon un jour vide est fermé.
        self.instance.opening_hours = hours
        return cleaned
