from django.test import override_settings
from django.urls import path
from rest_framework.response import Response
from rest_framework.test import APITestCase
from rest_framework.views import APIView

from apps.members.models import Member
from apps.organizations.models import Organization, OrganizationStaff

from .permissions import (
    READER_LIBRARY,
    STAFF_LIBRARY,
    IsReady,
    IsStaffOfOrganization,
)
from .test_auth import make_org, make_user


# --- Petites routes de test (aucune vraie route n'existe encore) ---------


class OkView(APIView):
    def get(self, request, *args, **kwargs):
        organization = getattr(request, "organization", None)
        return Response(
            {"ok": True, "organization": organization.pk if organization else None}
        )


class DefaultView(OkView):
    """Aucune permission déclarée : la règle par défaut s'applique."""


class StaffView(OkView):
    permission_classes = [IsReady, IsStaffOfOrganization]


class StaffLibraryView(OkView):
    permission_classes = STAFF_LIBRARY


class ReaderView(OkView):
    permission_classes = READER_LIBRARY


urlpatterns = [
    path("defaut/", DefaultView.as_view()),
    path("staff/<int:organization_id>/", StaffView.as_view()),
    path("staff-biblio/<int:organization_id>/", StaffLibraryView.as_view()),
    path("lecteur/<int:member_id>/", ReaderView.as_view()),
]


@override_settings(ROOT_URLCONF="apps.api.test_permissions")
class PermissionTestCase(APITestCase):
    def setUp(self):
        self.user = make_user()

    def connecter(self, user=None):
        self.client.force_authenticate(user=user or self.user)


class DefaultPermissionTests(PermissionTestCase):
    def test_sans_jeton(self):
        self.assertEqual(self.client.get("/defaut/").status_code, 401)

    def test_changement_de_mot_de_passe_obligatoire(self):
        self.user.must_change_password = True
        self.user.save()
        self.connecter()
        reponse = self.client.get("/defaut/")
        self.assertEqual(reponse.status_code, 403)
        self.assertIn("mot de passe", reponse.json()["detail"])

    def test_compte_normal(self):
        self.connecter()
        self.assertEqual(self.client.get("/defaut/").status_code, 200)


class StaffPermissionTests(PermissionTestCase):
    def setUp(self):
        super().setUp()
        self.org_a = make_org("Biblio A", "biblio-a")
        self.org_b = make_org("Biblio B", "biblio-b")
        self.lien = OrganizationStaff.objects.create(
            user=self.user, organization=self.org_a
        )

    def get(self, organization_id, route="staff"):
        return self.client.get(f"/{route}/{organization_id}/")

    def test_sans_jeton(self):
        self.assertEqual(self.get(self.org_a.pk).status_code, 401)

    def test_staff_actif_de_sa_structure(self):
        self.connecter()
        reponse = self.get(self.org_a.pk)
        self.assertEqual(reponse.status_code, 200)
        self.assertEqual(reponse.json()["organization"], self.org_a.pk)

    def test_refuse_une_autre_structure(self):
        self.connecter()
        self.assertEqual(self.get(self.org_b.pk).status_code, 403)

    def test_structure_inexistante(self):
        self.connecter()
        self.assertEqual(self.get(999999).status_code, 403)

    def test_compte_sans_rattachement(self):
        self.connecter(make_user("autre@example.com"))
        self.assertEqual(self.get(self.org_a.pk).status_code, 403)

    def test_staff_desactive(self):
        OrganizationStaff.objects.filter(pk=self.lien.pk).update(active=False)
        self.connecter()
        self.assertEqual(self.get(self.org_a.pk).status_code, 403)

    def test_structure_suspendue(self):
        Organization.objects.filter(pk=self.org_a.pk).update(
            status=Organization.Status.SUSPENDED
        )
        self.connecter()
        self.assertEqual(self.get(self.org_a.pk).status_code, 403)

    def test_changement_de_mot_de_passe_obligatoire(self):
        self.user.must_change_password = True
        self.user.save()
        self.connecter()
        self.assertEqual(self.get(self.org_a.pk).status_code, 403)

    def test_module_bibliotheque_desactive(self):
        Organization.objects.filter(pk=self.org_a.pk).update(library_module=False)
        self.connecter()
        self.assertEqual(self.get(self.org_a.pk).status_code, 200)
        reponse = self.get(self.org_a.pk, route="staff-biblio")
        self.assertEqual(reponse.status_code, 403)
        self.assertIn("bibliothèque", reponse.json()["detail"])

    def test_module_bibliotheque_actif(self):
        self.connecter()
        self.assertEqual(self.get(self.org_a.pk, route="staff-biblio").status_code, 200)


class ReaderPermissionTests(PermissionTestCase):
    def setUp(self):
        super().setUp()
        self.org = make_org("Biblio A", "biblio-a")
        self.fiche = Member.objects.create(
            organization=self.org, user=self.user, member_number="MEM-0001",
            first_name="Ama", last_name="Kofi", status=Member.Status.ACCEPTED,
        )

    def get(self, member_id):
        return self.client.get(f"/lecteur/{member_id}/")

    def set_status(self, status):
        Member.objects.filter(pk=self.fiche.pk).update(status=status)

    def test_sans_jeton(self):
        self.assertEqual(self.get(self.fiche.pk).status_code, 401)

    def test_sa_propre_fiche(self):
        self.connecter()
        reponse = self.get(self.fiche.pk)
        self.assertEqual(reponse.status_code, 200)
        self.assertEqual(reponse.json()["organization"], self.org.pk)

    def test_demande_en_attente_autorisee(self):
        self.set_status(Member.Status.PENDING)
        self.connecter()
        self.assertEqual(self.get(self.fiche.pk).status_code, 200)

    def test_demande_refusee(self):
        self.set_status(Member.Status.REJECTED)
        self.connecter()
        reponse = self.get(self.fiche.pk)
        self.assertEqual(reponse.status_code, 403)
        self.assertIn("refusée", reponse.json()["detail"])

    def test_fiche_d_un_autre_lecteur(self):
        autre = make_user("autre@example.com")
        fiche_autre = Member.objects.create(
            organization=self.org, user=autre, member_number="MEM-0002",
            first_name="Autre", last_name="Lecteur", status=Member.Status.ACCEPTED,
        )
        self.connecter()
        self.assertEqual(self.get(fiche_autre.pk).status_code, 403)

    def test_fiche_inexistante(self):
        self.connecter()
        self.assertEqual(self.get(999999).status_code, 403)

    def test_structure_suspendue(self):
        Organization.objects.filter(pk=self.org.pk).update(
            status=Organization.Status.SUSPENDED
        )
        self.connecter()
        self.assertEqual(self.get(self.fiche.pk).status_code, 403)

    def test_module_bibliotheque_desactive(self):
        Organization.objects.filter(pk=self.org.pk).update(library_module=False)
        self.connecter()
        self.assertEqual(self.get(self.fiche.pk).status_code, 403)

    def test_changement_de_mot_de_passe_obligatoire(self):
        self.user.must_change_password = True
        self.user.save()
        self.connecter()
        self.assertEqual(self.get(self.fiche.pk).status_code, 403)
