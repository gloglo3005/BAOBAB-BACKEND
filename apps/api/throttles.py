from rest_framework.throttling import AnonRateThrottle


class LoginRateThrottle(AnonRateThrottle):
    """Limite les tentatives de connexion (anti force brute)."""

    scope = "login"
    rate = "10/min"
