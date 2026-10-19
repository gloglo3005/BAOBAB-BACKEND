import io
import json
import urllib.error
from unittest import mock

from django.core.mail import EmailMessage, EmailMultiAlternatives
from django.test import SimpleTestCase

from .mail_backends import API_URL, ResendEmailBackend, ResendError

URLOPEN = "apps.accounts.mail_backends.urllib.request.urlopen"


def fake_response():
    response = mock.MagicMock()
    response.__enter__.return_value = response
    response.read.return_value = b'{"id": "abc"}'
    return response


class ResendEmailBackendTests(SimpleTestCase):
    def backend(self, **options):
        return ResendEmailBackend(**{"api_key": "re_test", "timeout": 5, **options})

    def test_envoie_un_texte_simple(self):
        message = EmailMessage("Bienvenue", "Bonjour", "Baobab <no-reply@exemple.org>", ["a@exemple.org"])
        with mock.patch(URLOPEN, return_value=fake_response()) as urlopen:
            sent = self.backend().send_messages([message])
        self.assertEqual(sent, 1)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, API_URL)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer re_test")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 5)
        body = json.loads(request.data)
        self.assertEqual(body["from"], "Baobab <no-reply@exemple.org>")
        self.assertEqual(body["to"], ["a@exemple.org"])
        self.assertEqual(body["subject"], "Bienvenue")
        self.assertEqual(body["text"], "Bonjour")
        self.assertNotIn("html", body)

    def test_version_html_et_texte(self):
        message = EmailMultiAlternatives("Sujet", "Texte", "no-reply@exemple.org", ["a@exemple.org"])
        message.attach_alternative("<p>Texte</p>", "text/html")
        with mock.patch(URLOPEN, return_value=fake_response()) as urlopen:
            self.backend().send_messages([message])
        body = json.loads(urlopen.call_args.args[0].data)
        self.assertEqual(body["html"], "<p>Texte</p>")
        self.assertEqual(body["text"], "Texte")

    def test_copies_et_reponse(self):
        message = EmailMessage(
            "Sujet", "Corps", "no-reply@exemple.org", ["a@exemple.org"],
            bcc=["b@exemple.org"], cc=["c@exemple.org"], reply_to=["r@exemple.org"],
        )
        with mock.patch(URLOPEN, return_value=fake_response()) as urlopen:
            self.backend().send_messages([message])
        body = json.loads(urlopen.call_args.args[0].data)
        self.assertEqual(body["bcc"], ["b@exemple.org"])
        self.assertEqual(body["cc"], ["c@exemple.org"])
        self.assertEqual(body["reply_to"], ["r@exemple.org"])

    def test_refus_http_leve_une_erreur_sans_reveler_la_cle(self):
        refus = urllib.error.HTTPError(
            API_URL, 403, "Forbidden", {}, io.BytesIO(b'{"message": "domain is not verified"}')
        )
        message = EmailMessage("Sujet", "Corps", "no-reply@exemple.org", ["a@exemple.org"])
        with mock.patch(URLOPEN, side_effect=refus):
            with self.assertRaises(ResendError) as raised:
                self.backend().send_messages([message])
        self.assertIn("403", str(raised.exception))
        self.assertIn("domain is not verified", str(raised.exception))
        self.assertNotIn("re_test", str(raised.exception))

    def test_connexion_impossible(self):
        message = EmailMessage("Sujet", "Corps", "no-reply@exemple.org", ["a@exemple.org"])
        with mock.patch(URLOPEN, side_effect=urllib.error.URLError("timed out")):
            with self.assertRaises(OSError):
                self.backend().send_messages([message])

    def test_message_sans_destinataire_ignore(self):
        message = EmailMessage("Sujet", "Corps", "no-reply@exemple.org", [])
        with mock.patch(URLOPEN) as urlopen:
            self.assertEqual(self.backend().send_messages([message]), 0)
        urlopen.assert_not_called()

    def test_pieces_jointes_refusees(self):
        message = EmailMessage("Sujet", "Corps", "no-reply@exemple.org", ["a@exemple.org"])
        message.attach("fichier.txt", "contenu", "text/plain")
        with mock.patch(URLOPEN) as urlopen:
            with self.assertRaises(ResendError):
                self.backend().send_messages([message])
        urlopen.assert_not_called()

    def test_sans_cle_api(self):
        message = EmailMessage("Sujet", "Corps", "no-reply@exemple.org", ["a@exemple.org"])
        with mock.patch(URLOPEN) as urlopen:
            with self.assertRaises(ResendError):
                self.backend(api_key="").send_messages([message])
        urlopen.assert_not_called()
