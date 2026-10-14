"""Formulaires du suivi coworking : réservation et correction de session."""
import datetime

from django import forms
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.coworking.models import AccessSession, Resource

DATETIME_LOCAL = "%Y-%m-%dT%H:%M"


class PeriodForm(forms.Form):
    """Un jour et deux heures : donne `starts_at` et `ends_at` (aware) dans cleaned_data."""

    date = forms.DateField(label="Jour", widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"))
    start = forms.TimeField(label="Début", widget=forms.TimeInput(attrs={"type": "time"}, format="%H:%M"))
    end = forms.TimeField(label="Fin", widget=forms.TimeInput(attrs={"type": "time"}, format="%H:%M"))
    resource_type = forms.ChoiceField(
        label="Type de ressource",
        required=False,
        choices=[("", "Tous les types")] + list(Resource.Type.choices),
    )

    def clean(self):
        data = super().clean()
        if all(key in data for key in ("date", "start", "end")):
            if data["end"] <= data["start"]:
                raise ValidationError("La fin doit être postérieure au début.")
            data["starts_at"] = timezone.make_aware(datetime.datetime.combine(data["date"], data["start"]))
            data["ends_at"] = timezone.make_aware(datetime.datetime.combine(data["date"], data["end"]))
        return data


class ReservationForm(PeriodForm):
    customer_id = forms.IntegerField(error_messages={"required": "Choisissez un client."})
    resource_id = forms.IntegerField(error_messages={"required": "Choisissez une ressource libre."})
    notes = forms.CharField(
        label="Note (facultatif)", max_length=500, required=False, widget=forms.Textarea(attrs={"rows": 2})
    )


class CorrectionForm(forms.Form):
    """Correction d'une session : seuls les champs réellement modifiés sont transmis au service."""

    checked_in_at = forms.DateTimeField(
        label="Arrivée",
        required=False,
        input_formats=[DATETIME_LOCAL, "%Y-%m-%d %H:%M"],
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format=DATETIME_LOCAL),
    )
    checked_out_at = forms.DateTimeField(
        label="Sortie",
        required=False,
        input_formats=[DATETIME_LOCAL, "%Y-%m-%d %H:%M"],
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format=DATETIME_LOCAL),
    )
    amount = forms.IntegerField(
        label="Montant dû (XOF)",
        min_value=0,
        required=False,
        help_text=(
            "Laissez tel quel pour recalculer d'après les nouvelles heures. "
            "Une session issue d'une réservation garde le montant de la réservation : "
            "saisissez alors le montant voulu."
        ),
    )
    reason = forms.CharField(
        label="Motif de la correction (obligatoire)",
        max_length=200,
        error_messages={"required": "Le motif de la correction est obligatoire."},
    )

    def __init__(self, *args, session, **kwargs):
        super().__init__(*args, **kwargs)
        self.session = session
        if session.status != AccessSession.Status.CLOSED:
            # Le montant et l'heure de sortie n'existent qu'à la clôture de la session.
            del self.fields["checked_out_at"]
            del self.fields["amount"]

    @staticmethod
    def _minute(value):
        return value.replace(second=0, microsecond=0) if value else value

    def changes(self):
        """Arguments de correct_session : uniquement ce qui diffère de la session actuelle."""
        data = self.cleaned_data
        changes = {}
        for name in ("checked_in_at", "checked_out_at"):
            new = data.get(name)
            if new is not None and self._minute(new) != self._minute(getattr(self.session, name)):
                changes[name] = new
        amount = data.get("amount")
        if amount is not None and amount != self.session.amount:
            changes["amount"] = amount
        return changes
