"""Connexion par e-mail sans tenir compte des majuscules.

Sur téléphone, la première lettre est souvent mise en majuscule par le clavier :
« Jean@mail.com » doit donc retrouver le compte « jean@mail.com ». Les comptes
créés par le staff gardent la casse saisie, on cherche donc sans casse, en
préférant la correspondance exacte s'il existe plusieurs comptes proches.
"""
from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend


class CaseInsensitiveEmailBackend(ModelBackend):
    def authenticate(self, request, username=None, password=None, **kwargs):
        User = get_user_model()
        if username is None:
            username = kwargs.get(User.USERNAME_FIELD)
        if not username or password is None:
            return None
        username = str(username).strip()
        candidates = list(User._default_manager.filter(email__iexact=username)[:5])
        user = next((u for u in candidates if u.email == username), None)
        if user is None and len(candidates) == 1:
            user = candidates[0]
        if user is None:
            # Même durée de calcul qu'un vrai essai : on ne révèle pas si le compte existe.
            User().set_password(password)
            return None
        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None
