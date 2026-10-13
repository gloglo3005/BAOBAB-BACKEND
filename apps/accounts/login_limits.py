"""Limite les tentatives de connexion des formulaires web (anti force brute).

L'API a déjà sa limite (apps.api.throttles). Les pages /staff/login/,
/reader/login/ et l'administration globale /global/ n'en avaient pas : un
robot pouvait essayer des mots de passe sans fin sur un compte connu.

Principe : après MAX_FAILURES échecs sur la même adresse email en
WINDOW_SECONDS, les nouvelles tentatives sont refusées avant même de tester
le mot de passe. Une connexion réussie remet le compteur à zéro. Le compteur
vit dans le cache Django (voir CACHE_TABLE dans settings.py pour le partager
entre plusieurs processus gunicorn).
"""
import hashlib

from django.contrib.admin.forms import AdminAuthenticationForm
from django.contrib.auth.forms import AuthenticationForm
from django.core.cache import cache
from django.core.exceptions import ValidationError

MAX_FAILURES = 10
WINDOW_SECONDS = 15 * 60

TOO_MANY_MESSAGE = "Trop de tentatives de connexion. Réessayez dans quelques minutes."


def _key(username):
    digest = hashlib.sha256(username.strip().lower().encode("utf-8")).hexdigest()
    return f"web-login-failures:{digest}"


def _register_failure(key):
    # add() ne crée la clé que si elle n'existe pas : la fenêtre part du premier échec.
    cache.add(key, 0, WINDOW_SECONDS)
    try:
        cache.incr(key)
    except ValueError:
        cache.set(key, 1, WINDOW_SECONDS)


class LimitedLoginMixin:
    """À placer avant AuthenticationForm : refuse les essais en trop."""

    def clean(self):
        username = self.cleaned_data.get("username") or ""
        key = _key(username) if username else None

        if key and cache.get(key, 0) >= MAX_FAILURES:
            raise ValidationError(TOO_MANY_MESSAGE, code="too_many_attempts")

        try:
            cleaned = super().clean()
        except ValidationError:
            if key:
                _register_failure(key)
            raise

        if key and self.get_user() is not None:
            cache.delete(key)
        return cleaned


class StaffLoginForm(LimitedLoginMixin, AuthenticationForm):
    pass


class AdminLoginForm(LimitedLoginMixin, AdminAuthenticationForm):
    pass
