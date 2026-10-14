"""Formulaires du comptoir coworking : clients et paiements."""
from django import forms

from apps.coworking.models import AccessSession, Customer, Payment

from .forms_coworking import OrganizationModelForm


class CustomerForm(OrganizationModelForm):
    class Meta:
        model = Customer
        fields = ["first_name", "last_name", "phone", "email"]
        help_texts = {
            "phone": "Facultatif, mais pratique pour retrouver le client au comptoir.",
            "email": "Facultatif. Le client n'a pas besoin de compte en ligne.",
        }

    def clean_first_name(self):
        return " ".join(self.cleaned_data["first_name"].split())

    def clean_last_name(self):
        return " ".join(self.cleaned_data["last_name"].split())

    def clean_phone(self):
        return self.cleaned_data["phone"].strip()


class PaymentForm(forms.Form):
    amount = forms.IntegerField(
        label="Montant reçu (XOF)", min_value=1,
        help_text="Nombre entier de francs CFA.",
    )
    method = forms.ChoiceField(
        label="Moyen de paiement", choices=Payment.Method.choices, initial=Payment.Method.CASH
    )
    session = forms.ModelChoiceField(
        label="Pour quelle session ?",
        queryset=AccessSession.objects.none(),
        required=False,
        empty_label="Aucune (acompte libre)",
    )
    note = forms.CharField(label="Note (facultatif)", max_length=200, required=False)

    def __init__(self, *args, sessions, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["session"].queryset = sessions
