from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.password_validation import validate_password
from apps.accounts.login_limits import LimitedLoginMixin
from django.core.exceptions import ValidationError

User = get_user_model()


class ReaderLoginForm(LimitedLoginMixin, AuthenticationForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"].label = "Adresse email"
        self.fields["username"].widget = forms.EmailInput(
            attrs={"autocomplete": "email", "autofocus": True}
        )
        self.fields["password"].widget = forms.PasswordInput(
            attrs={"autocomplete": "current-password"}
        )

    def clean_username(self):
        # « Kossi@Example.com » retrouve le compte enregistré en minuscules.
        value = self.cleaned_data["username"].strip()
        found = list(
            User.objects.filter(email__iexact=value).values_list("email", flat=True)[:2]
        )
        return found[0] if len(found) == 1 else value


class RegisterForm(forms.Form):
    """Inscription d'un lecteur.

    Aucun champ de rôle ni de privilège : seuls ces champs sont lus, tout autre
    paramètre envoyé par le navigateur est ignoré.
    """

    # 100 caractères : c'est la limite de la fiche Member, qui recopie ces noms.
    first_name = forms.CharField(
        label="Prénom", max_length=100,
        widget=forms.TextInput(attrs={"autocomplete": "given-name"}),
    )
    last_name = forms.CharField(
        label="Nom", max_length=100,
        widget=forms.TextInput(attrs={"autocomplete": "family-name"}),
    )
    email = forms.EmailField(
        label="Adresse email",
        widget=forms.EmailInput(attrs={"autocomplete": "email"}),
    )
    phone = forms.CharField(
        label="Téléphone (facultatif)", max_length=30, required=False,
        widget=forms.TextInput(attrs={"autocomplete": "tel", "inputmode": "tel"}),
    )
    password1 = forms.CharField(
        label="Mot de passe",
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )
    password2 = forms.CharField(
        label="Confirmer le mot de passe",
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )

    def clean_first_name(self):
        return " ".join(self.cleaned_data["first_name"].split())

    def clean_last_name(self):
        return " ".join(self.cleaned_data["last_name"].split())

    def clean_phone(self):
        return " ".join(self.cleaned_data["phone"].split())

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise ValidationError(
                "Un compte existe déjà avec cette adresse. Connectez-vous."
            )
        return email

    def clean(self):
        data = super().clean()
        password1, password2 = data.get("password1"), data.get("password2")
        if password1 and password2:
            if password1 != password2:
                self.add_error("password2", "Les deux mots de passe ne correspondent pas.")
            else:
                candidate = User(
                    email=data.get("email", ""),
                    first_name=data.get("first_name", ""),
                    last_name=data.get("last_name", ""),
                )
                try:
                    validate_password(password1, candidate)
                except ValidationError as error:
                    self.add_error("password1", error)
        return data

    def save(self):
        data = self.cleaned_data
        # create_user : jamais Staff, jamais administrateur de la plateforme.
        return User.objects.create_user(
            email=data["email"],
            password=data["password1"],
            first_name=data["first_name"],
            last_name=data["last_name"],
            phone=data["phone"],
        )


class LinkCodeForm(forms.Form):
    code = forms.CharField(
        label="Code de rattachement", max_length=20,
        widget=forms.TextInput(
            attrs={"autocomplete": "off", "autocapitalize": "characters", "class": "code-input"}
        ),
        help_text="Le code remis par la bibliothèque (8 caractères).",
    )

    def clean_code(self):
        return self.cleaned_data["code"].strip().upper()
