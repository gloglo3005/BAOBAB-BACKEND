import re

from django import forms
from django.db.models import Q

from apps.catalog.models import BookCopy, Category, Location
from apps.catalog.services import MANUAL_COPY_STATUSES


class MemberForm(forms.Form):
    first_name = forms.CharField(label="Prénom", max_length=100)
    last_name = forms.CharField(label="Nom", max_length=100)
    phone = forms.CharField(label="Téléphone", max_length=30, required=False)
    member_number = forms.CharField(
        label="Numéro de membre",
        max_length=50,
        required=False,
        help_text="Laissez vide pour un numéro automatique (MEM-0001, MEM-0002...).",
    )


def _active_or_current(model, organization, current=None):
    """Éléments actifs de la structure, plus l'élément déjà choisi même désactivé."""
    condition = Q(active=True)
    if current is not None:
        condition |= Q(pk=current.pk)
    return model.objects.filter(organization=organization).filter(condition).order_by("name")


class BookForm(forms.Form):
    title = forms.CharField(label="Titre", max_length=255)
    subtitle = forms.CharField(label="Sous-titre", max_length=255, required=False)
    isbn = forms.CharField(label="ISBN", max_length=20, required=False)
    authors_text = forms.CharField(
        label="Auteurs",
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Un auteur par ligne, sous la forme « Prénom Nom ».",
    )
    category = forms.ModelChoiceField(
        label="Catégorie", queryset=Category.objects.none(), required=False,
        empty_label="Aucune",
    )
    publisher = forms.CharField(label="Éditeur", max_length=150, required=False)
    publication_year = forms.IntegerField(
        label="Année de publication", required=False, min_value=1, max_value=2100
    )
    language = forms.CharField(label="Langue", max_length=50, required=False)
    description = forms.CharField(
        label="Description", required=False, widget=forms.Textarea(attrs={"rows": 4})
    )

    def __init__(self, *args, organization, current_category=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["category"].queryset = _active_or_current(
            Category, organization, current_category
        )

    def clean_isbn(self):
        # « 978-2-07 036822 8 » et « 9782070368228 » sont le même numéro.
        return re.sub(r"[\s-]", "", self.cleaned_data["isbn"]).upper()

    def clean_title(self):
        return " ".join(self.cleaned_data["title"].split())


class CopiesFieldsMixin:
    """Champs communs pour poser un lot d'exemplaires."""

    def _setup_location(self, organization, current=None):
        self.fields["location"].queryset = _active_or_current(Location, organization, current)


class NewBookForm(CopiesFieldsMixin, BookForm):
    codes = forms.CharField(
        label="Exemplaires à ajouter maintenant",
        required=False,
        widget=forms.Textarea(attrs={"rows": 3, "class": "mono"}),
        help_text="Un code d'inventaire par ligne (facultatif, vous pourrez en ajouter plus tard).",
    )
    location = forms.ModelChoiceField(
        label="Emplacement", queryset=Location.objects.none(), required=False,
        empty_label="Aucun",
    )
    condition = forms.ChoiceField(
        label="État des exemplaires",
        choices=BookCopy.Condition.choices,
        initial=BookCopy.Condition.GOOD,
    )

    def __init__(self, *args, organization, **kwargs):
        super().__init__(*args, organization=organization, **kwargs)
        self._setup_location(organization)


class CopiesForm(CopiesFieldsMixin, forms.Form):
    codes = forms.CharField(
        label="Codes d'inventaire",
        widget=forms.Textarea(attrs={"rows": 6, "class": "mono"}),
        help_text="Un code par ligne. Si un code existe déjà, rien n'est créé.",
    )
    location = forms.ModelChoiceField(
        label="Emplacement", queryset=Location.objects.none(), required=False,
        empty_label="Aucun",
    )
    condition = forms.ChoiceField(
        label="État", choices=BookCopy.Condition.choices, initial=BookCopy.Condition.GOOD
    )

    def __init__(self, *args, organization, **kwargs):
        super().__init__(*args, **kwargs)
        self._setup_location(organization)


class CopyEditForm(CopiesFieldsMixin, forms.Form):
    condition = forms.ChoiceField(label="État", choices=BookCopy.Condition.choices)
    location = forms.ModelChoiceField(
        label="Emplacement", queryset=Location.objects.none(), required=False,
        empty_label="Aucun",
    )
    acquisition_date = forms.DateField(
        label="Date d'acquisition", required=False,
        widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
    )
    notes = forms.CharField(
        label="Notes", required=False, widget=forms.Textarea(attrs={"rows": 3})
    )

    def __init__(self, *args, organization, current_location=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._setup_location(organization, current_location)


class CopyStatusForm(forms.Form):
    status = forms.ChoiceField(
        label="Nouveau statut",
        choices=[(s.value, s.label) for s in MANUAL_COPY_STATUSES],
    )


class ReferenceForm(forms.Form):
    name = forms.CharField(label="Nom", max_length=100)
    description = forms.CharField(label="Description", required=False, max_length=300)
