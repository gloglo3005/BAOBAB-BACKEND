"""Réglages Django : développement local et production (Render + Neon)."""

from pathlib import Path
import os

import dj_database_url
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

SECRET_KEY = os.environ["SECRET_KEY"]
DEBUG = os.environ.get("DEBUG", "False") == "True"

ALLOWED_HOSTS = ["localhost", "127.0.0.1"]
CSRF_TRUSTED_ORIGINS = []

# Render fournit automatiquement RENDER_EXTERNAL_HOSTNAME.
_render_host = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
if _render_host:
    ALLOWED_HOSTS.append(_render_host)
    CSRF_TRUSTED_ORIGINS.append(f"https://{_render_host}")

# Domaines supplémentaires (plus tard), séparés par des virgules.
for _host in os.environ.get("EXTRA_ALLOWED_HOSTS", "").split(","):
    _host = _host.strip()
    if _host:
        ALLOWED_HOSTS.append(_host)
        CSRF_TRUSTED_ORIGINS.append(f"https://{_host}")

# Sécurité renforcée uniquement sur Render (la variable RENDER y est définie).
if os.environ.get("RENDER"):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True


INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
    "rest_framework",
    "corsheaders",
    "drf_spectacular",
    "apps.accounts",
    "apps.organizations",
    "apps.catalog",
    "apps.members",
    "apps.circulation",
    "apps.audit",
    "apps.staff",
    "apps.imports",
    "apps.reader",
    "apps.api",
    "apps.coworking",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"


# Base de données : DATABASE_URL (Neon) si elle existe, sinon les variables DB_* (local).
if os.environ.get("DATABASE_URL"):
    DATABASES = {
        "default": dj_database_url.config(conn_max_age=0, ssl_require=True)
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ["DB_NAME"],
            "USER": os.environ["DB_USER"],
            "PASSWORD": os.environ["DB_PASSWORD"],
            "HOST": os.environ["DB_HOST"],
            "PORT": os.environ["DB_PORT"],
        }
    }


AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


LANGUAGE_CODE = "fr-fr"
TIME_ZONE = "Africa/Lome"
USE_TZ = True
USE_I18N = True


# Fichiers statiques servis par WhiteNoise.
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}

# Fichiers envoyés (imports CSV/XLSX) : conservés sur le disque, jamais servis publiquement.
MEDIA_ROOT = BASE_DIR / "media"


# E-mails (Django 6.1 : MAILERS). Sans RESEND_API_KEY (développement, tests), les messages
# s'affichent dans la console. Avec RESEND_API_KEY, ils partent par l'API HTTPS de Resend
# (apps/accounts/mail_backends.py) : les services gratuits de Render bloquent le SMTP sortant.
# L'expéditeur doit appartenir à un domaine vérifié chez Resend.
_resend_api_key = os.environ.get("RESEND_API_KEY", "")
if _resend_api_key:
    MAILERS = {
        "default": {
            "BACKEND": "apps.accounts.mail_backends.ResendEmailBackend",
            "OPTIONS": {"api_key": _resend_api_key, "timeout": 10},
        },
    }
else:
    MAILERS = {
        "default": {
            "BACKEND": "django.core.mail.backends.console.EmailBackend",
        },
    }

# Adresse d'expédition. onboarding@resend.dev ne sert qu'aux essais (vers votre propre adresse).
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "Baobab <onboarding@resend.dev>")

AUTH_USER_MODEL = "accounts.User"

# Connexion par e-mail, sans tenir compte des majuscules (claviers de téléphone).
AUTHENTICATION_BACKENDS = ["apps.accounts.backends.CaseInsensitiveEmailBackend"]

# --- API (DRF, JWT, documentation, CORS) ---
from datetime import timedelta

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "apps.api.permissions.IsReady",
    ],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=30),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    # Un changement de mot de passe invalide les jetons déjà émis.
    "CHECK_REVOKE_TOKEN": True,
}

SPECTACULAR_SETTINGS = {
    "TITLE": "API de la plateforme multi-activités",
    "DESCRIPTION": "API utilisée par le logiciel de bureau (staff) et l'application mobile (lecteurs).",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
}

# Origines autorisées pour le client web/bureau (séparées par des virgules).
CORS_ALLOWED_ORIGINS = [
    o.strip()
    for o in os.environ.get(
        "CORS_ALLOWED_ORIGINS", "http://localhost:5173,http://localhost:3000"
    ).split(",")
    if o.strip()
]


# --- Action 7 : débit, production, cache ---
# JSON seul en production (l'interface navigable de DRF reste disponible en local).
_renderers = ["rest_framework.renderers.JSONRenderer"]
if DEBUG:
    _renderers.append("rest_framework.renderers.BrowsableAPIRenderer")

# Sans cette valeur, DRF croit l'en-tête X-Forwarded-For envoyé par le client et les limites de
# débit se contournent. Sur Render il y a un seul relais : 1 par défaut (surchargeable par NUM_PROXIES).
if os.environ.get("NUM_PROXIES"):
    _num_proxies = int(os.environ["NUM_PROXIES"])
elif os.environ.get("RENDER"):
    _num_proxies = 1
else:
    _num_proxies = None

REST_FRAMEWORK.update({
    "DEFAULT_RENDERER_CLASSES": _renderers,
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {"anon": "60/min", "user": "300/min"},
    # Nombre de proxys entre Internet et l'application (voir la vérification après déploiement).
    "NUM_PROXIES": _num_proxies,
})

# En production, aucune origine CORS par défaut : CORS_ALLOWED_ORIGINS doit être défini.
if not DEBUG and "CORS_ALLOWED_ORIGINS" not in os.environ:
    CORS_ALLOWED_ORIGINS = []

# HTTPS renforcé sur Render.
if os.environ.get("RENDER"):
    SECURE_SSL_REDIRECT = True
    SECURE_HSTS_SECONDS = 3600
    SECURE_REDIRECT_EXEMPT = [r"^api/v1/ping/$"]

# Cache partagé entre les processus (optionnel) : définir CACHE_TABLE, par exemple api_cache.
# Utile seulement si gunicorn tourne avec plusieurs workers.
# Sur Render (plusieurs workers gunicorn possibles), le cache est partagé par défaut dans la base
# (table api_cache, créée par build.sh) : sinon chaque processus compterait ses propres essais.
_cache_table = os.environ.get("CACHE_TABLE") or ("api_cache" if os.environ.get("RENDER") else "")
if _cache_table:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.db.DatabaseCache",
            "LOCATION": _cache_table,
        }
    }

# Noms stables pour les énumérations de statut dans le schéma OpenAPI (clients React et Flutter).
SPECTACULAR_SETTINGS["ENUM_NAME_OVERRIDES"] = {
    "MemberStatusEnum": [
        ("PENDING", "En attente"), ("ACCEPTED", "Accepté"),
        ("REJECTED", "Refusé"), ("SUSPENDED", "Suspendu"),
    ],
    "LoanStatusEnum": [("ACTIVE", "En cours"), ("RETURNED", "Rendu"), ("LOST", "Perdu")],
    "CopyStatusEnum": [
        ("AVAILABLE", "Disponible"), ("BORROWED", "Emprunté"), ("LOST", "Perdu"),
        ("DAMAGED", "Endommagé"), ("UNAVAILABLE", "Indisponible"),
    ],
}