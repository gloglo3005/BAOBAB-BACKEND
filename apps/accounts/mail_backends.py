"""Envoi d'e-mails par l'API HTTPS de Resend.

Pourquoi pas le SMTP ? Les services web gratuits de Render bloquent les ports SMTP sortants
(25, 465 et 587). L'API HTTPS fonctionne sur toutes les offres et chez tous les hébergeurs.
Aucune dépendance : seule la bibliothèque standard est utilisée.

Configuration (config/settings.py, Django 6.1) :

    MAILERS = {
        "default": {
            "BACKEND": "apps.accounts.mail_backends.ResendEmailBackend",
            "OPTIONS": {"api_key": "re_...", "timeout": 10},
        },
    }

Documentation de l'API : https://resend.com/docs/api-reference/emails/send-email
"""
import json
import urllib.error
import urllib.request

from django.core.mail.backends.base import BaseEmailBackend

API_URL = "https://api.resend.com/emails"
# Un User-Agent explicite : le User-Agent par défaut de urllib est parfois refusé par les pare-feu.
USER_AGENT = "baobab-backend/1.0"


class ResendError(OSError):
    """Échec de l'envoi. Hérite d'OSError, comme les erreurs réseau et SMTP de Django."""


class ResendEmailBackend(BaseEmailBackend):
    def __init__(self, api_key=None, timeout=10, **kwargs):
        # Les options propres à ce backend ne sont pas transmises à la classe parente
        # (Django 6.1 refuse les options inconnues).
        super().__init__(**kwargs)
        self.api_key = api_key
        self.timeout = timeout

    def send_messages(self, email_messages):
        """Envoie chaque message ; renvoie le nombre de messages envoyés."""
        if not email_messages:
            return 0
        sent = 0
        for message in email_messages:
            if not message.recipients():
                continue
            self._send(self._payload(message))
            sent += 1
        return sent

    def _payload(self, message):
        if message.attachments:
            raise ResendError("Les pièces jointes ne sont pas prises en charge par ce backend.")
        payload = {
            "from": message.from_email,
            "to": list(message.to),
            "subject": message.subject,
        }
        if message.cc:
            payload["cc"] = list(message.cc)
        if message.bcc:
            payload["bcc"] = list(message.bcc)
        if message.reply_to:
            payload["reply_to"] = list(message.reply_to)
        if message.extra_headers:
            payload["headers"] = {str(k): str(v) for k, v in message.extra_headers.items()}

        html = None
        for content, mimetype in getattr(message, "alternatives", None) or []:
            if mimetype == "text/html":
                html = content
                break
        if html is not None:
            payload["html"] = html
            payload["text"] = message.body
        elif message.content_subtype == "html":
            payload["html"] = message.body
        else:
            payload["text"] = message.body
        return payload

    def _send(self, payload):
        if not self.api_key:
            raise ResendError("Clé API Resend absente (variable RESEND_API_KEY).")
        request = urllib.request.Request(
            API_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                response.read()
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:500]
            raise ResendError(f"Resend a refusé l'e-mail (HTTP {error.code}) : {detail}") from error
        except urllib.error.URLError as error:
            raise ResendError(f"Connexion impossible à Resend : {error.reason}") from error
