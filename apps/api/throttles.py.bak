from rest_framework.throttling import AnonRateThrottle, UserRateThrottle


class LoginRateThrottle(AnonRateThrottle):
    """Limite les tentatives de connexion (anti force brute)."""

    scope = "login"
    rate = "10/min"


class PasswordChangeRateThrottle(UserRateThrottle):
    """Limite les changements de mot de passe par compte."""

    scope = "password"
    rate = "5/min"


class LinkCodeRateThrottle(UserRateThrottle):
    """Limite les essais de codes de rattachement (codes à 8 caractères) par compte."""

    scope = "link"
    rate = "10/hour"