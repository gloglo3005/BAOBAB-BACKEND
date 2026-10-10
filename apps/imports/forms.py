import os

from django import forms

from .readers import ALLOWED_EXTENSIONS, MAX_BYTES


class UploadForm(forms.Form):
    file = forms.FileField(
        label="Fichier à importer",
        help_text="CSV (UTF-8, virgule ou point-virgule) ou XLSX (première feuille). "
                  "5 000 lignes et 5 Mo au plus.",
        widget=forms.ClearableFileInput(attrs={"accept": ".csv,.xlsx"}),
    )

    def clean_file(self):
        uploaded = self.cleaned_data["file"]
        extension = os.path.splitext(uploaded.name)[1].lower()
        if extension not in ALLOWED_EXTENSIONS:
            raise forms.ValidationError(
                "Format non pris en charge : envoyez un fichier .csv ou .xlsx."
            )
        if uploaded.size > MAX_BYTES:
            raise forms.ValidationError("Le fichier dépasse 5 Mo.")
        return uploaded
