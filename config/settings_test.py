"""Réglages utilisés uniquement pour accélérer les tests (jamais en production).

Usage : python manage.py test --keepdb --settings=config.settings_test

Seul le hachage des mots de passe change : même algorithme (PBKDF2-SHA256), mais avec très peu
d'itérations. Chaque test crée plusieurs comptes ; avec les réglages normaux, chaque création
coûte environ une seconde. Tout le reste vient de config.settings.
"""
from django.contrib.auth.hashers import PBKDF2PasswordHasher

from .settings import *  # noqa: F401,F403


class FastPBKDF2PasswordHasher(PBKDF2PasswordHasher):
    iterations = 1000


PASSWORD_HASHERS = ["config.settings_test.FastPBKDF2PasswordHasher"]
