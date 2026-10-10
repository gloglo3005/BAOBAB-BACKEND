from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from apps.organizations.models import Organization
from apps.organizations.services import create_organization

from .models import LinkCode, Member
from .services import (
    accept_member,
    change_member_status,
    create_link_code,
    reactivate_member,
    redeem_link_code,
    register_member,
    reject_member,
    request_membership,
    suspend_member,
)

User = get_user_model()


class MembersTestCase(TestCase):
    """Deux structures et un compte lecteur, disponibles pour chaque test."""

    def setUp(self):
        self.org_a = create_organization(name="Bibliothèque A", slug="a")
        self.org_b = create_organization(name="Bibliothèque B", slug="b")
        self.reader = User.objects.create_user(
            email="lecteur@example.com", password="abc12345",
            first_name="Ama", last_name="Kofi", phone="90000000",
        )


class RegisterMemberTests(MembersTestCase):
    def test_inscription_au_comptoir_est_acceptee_sans_compte(self):
        member = register_member(
            organization=self.org_a, first_name="Kossi", last_name="Amah"
        )
        self.assertEqual(member.status, Member.Status.ACCEPTED)
        self.assertIsNone(member.user)
        self.assertEqual(member.joined_at, timezone.localdate())

    def test_numeros_automatiques_se_suivent(self):
        first = register_member(organization=self.org_a, first_name="A", last_name="A")
        second = register_member(organization=self.org_a, first_name="B", last_name="B")
        self.assertEqual(first.member_number, "MEM-0001")
        self.assertEqual(second.member_number, "MEM-0002")

    def test_numeros_independants_par_structure(self):
        member_a = register_member(organization=self.org_a, first_name="A", last_name="A")
        member_b = register_member(organization=self.org_b, first_name="B", last_name="B")
        self.assertEqual(member_a.member_number, "MEM-0001")
        self.assertEqual(member_b.member_number, "MEM-0001")

    def test_numero_saisi_a_la_main_est_respecte(self):
        member = register_member(
            organization=self.org_a, first_name="A", last_name="A",
            member_number="CARTE-77",
        )
        self.assertEqual(member.member_number, "CARTE-77")

    def test_numero_saisi_en_double_refuse(self):
        register_member(
            organization=self.org_a, first_name="A", last_name="A",
            member_number="CARTE-77",
        )
        with self.assertRaises(ValidationError):
            register_member(
                organization=self.org_a, first_name="B", last_name="B",
                member_number="CARTE-77",
            )

    def test_le_compteur_saute_un_numero_deja_pris(self):
        register_member(
            organization=self.org_a, first_name="A", last_name="A",
            member_number="MEM-0001",
        )
        member = register_member(organization=self.org_a, first_name="B", last_name="B")
        self.assertEqual(member.member_number, "MEM-0002")

    def test_meme_numero_autorise_dans_deux_structures(self):
        register_member(
            organization=self.org_a, first_name="A", last_name="A", member_number="X-1"
        )
        register_member(
            organization=self.org_b, first_name="B", last_name="B", member_number="X-1"
        )
        self.assertEqual(Member.objects.filter(member_number="X-1").count(), 2)


class RequestMembershipTests(MembersTestCase):
    def test_demande_cree_un_membre_en_attente(self):
        member = request_membership(user=self.reader, organization=self.org_a)
        self.assertEqual(member.status, Member.Status.PENDING)
        self.assertEqual(member.user, self.reader)
        self.assertEqual(member.first_name, "Ama")
        self.assertEqual(member.phone, "90000000")
        self.assertEqual(member.member_number, "MEM-0001")
        self.assertIsNone(member.joined_at)

    def test_double_demande_refusee(self):
        request_membership(user=self.reader, organization=self.org_a)
        with self.assertRaises(ValidationError):
            request_membership(user=self.reader, organization=self.org_a)

    def test_un_compte_peut_etre_membre_de_deux_structures(self):
        request_membership(user=self.reader, organization=self.org_a)
        request_membership(user=self.reader, organization=self.org_b)
        self.assertEqual(Member.objects.filter(user=self.reader).count(), 2)

    def test_structure_non_active_refuse_les_demandes(self):
        suspended = create_organization(
            name="C", slug="c", status=Organization.Status.SUSPENDED
        )
        with self.assertRaises(ValidationError):
            request_membership(user=self.reader, organization=suspended)

    def test_la_base_refuse_deux_fois_le_meme_compte(self):
        Member.objects.create(
            organization=self.org_a, user=self.reader, member_number="M1",
            first_name="A", last_name="B",
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Member.objects.create(
                    organization=self.org_a, user=self.reader, member_number="M2",
                    first_name="A", last_name="B",
                )


class MemberStatusTests(MembersTestCase):
    def test_accepter_une_demande(self):
        member = request_membership(user=self.reader, organization=self.org_a)
        accepted = accept_member(member)
        self.assertEqual(accepted.status, Member.Status.ACCEPTED)
        self.assertEqual(accepted.joined_at, timezone.localdate())

    def test_refuser_une_demande(self):
        member = request_membership(user=self.reader, organization=self.org_a)
        rejected = reject_member(member)
        self.assertEqual(rejected.status, Member.Status.REJECTED)

    def test_suspendre_puis_reactiver(self):
        member = register_member(organization=self.org_a, first_name="A", last_name="A")
        joined = member.joined_at
        suspend_member(member)
        member.refresh_from_db()
        self.assertEqual(member.status, Member.Status.SUSPENDED)
        reactivate_member(member)
        member.refresh_from_db()
        self.assertEqual(member.status, Member.Status.ACCEPTED)
        self.assertEqual(member.joined_at, joined)

    def test_un_refus_peut_etre_annule(self):
        member = request_membership(user=self.reader, organization=self.org_a)
        reject_member(member)
        accepted = accept_member(member)
        self.assertEqual(accepted.status, Member.Status.ACCEPTED)

    def test_accepte_ne_peut_pas_etre_refuse(self):
        member = register_member(organization=self.org_a, first_name="A", last_name="A")
        with self.assertRaises(ValidationError):
            reject_member(member)
        member.refresh_from_db()
        self.assertEqual(member.status, Member.Status.ACCEPTED)

    def test_en_attente_ne_peut_pas_etre_suspendu(self):
        member = request_membership(user=self.reader, organization=self.org_a)
        with self.assertRaises(ValidationError):
            suspend_member(member)

    def test_statut_inconnu_refuse(self):
        member = register_member(organization=self.org_a, first_name="A", last_name="A")
        with self.assertRaises(ValidationError):
            change_member_status(member=member, new_status="BANNI")


class LinkCodeTests(MembersTestCase):
    def setUp(self):
        super().setUp()
        self.member = register_member(
            organization=self.org_a, first_name="Kossi", last_name="Amah"
        )

    def test_code_de_8_caracteres_valable_7_jours(self):
        link = create_link_code(member=self.member)
        now = timezone.now()
        self.assertEqual(len(link.code), 8)
        self.assertGreater(link.expires_at, now + timedelta(days=6))
        self.assertLess(link.expires_at, now + timedelta(days=8))
        self.assertEqual(link.organization, self.org_a)

    def test_refuse_pour_un_membre_deja_rattache(self):
        attached = request_membership(user=self.reader, organization=self.org_a)
        with self.assertRaises(ValidationError):
            create_link_code(member=attached)

    def test_refuse_pour_un_membre_non_accepte(self):
        suspended = suspend_member(self.member)
        with self.assertRaises(ValidationError):
            create_link_code(member=suspended)

    def test_un_nouveau_code_invalide_l_ancien(self):
        first = create_link_code(member=self.member)
        second = create_link_code(member=self.member)
        first.refresh_from_db()
        self.assertLessEqual(first.expires_at, timezone.now())
        self.assertNotEqual(first.code, second.code)
        with self.assertRaises(ValidationError):
            redeem_link_code(user=self.reader, code=first.code)

    def test_rattachement_sans_doublon(self):
        link = create_link_code(member=self.member)
        result = redeem_link_code(user=self.reader, code=link.code)
        self.assertEqual(result.pk, self.member.pk)
        self.member.refresh_from_db()
        self.assertEqual(self.member.user, self.reader)
        self.assertEqual(Member.objects.for_organization(self.org_a).count(), 1)
        link.refresh_from_db()
        self.assertIsNotNone(link.used_at)

    def test_code_utilisable_une_seule_fois(self):
        other = User.objects.create_user(email="autre@example.com", password="abc12345")
        link = create_link_code(member=self.member)
        redeem_link_code(user=self.reader, code=link.code)
        with self.assertRaises(ValidationError):
            redeem_link_code(user=other, code=link.code)

    def test_code_expire_refuse(self):
        link = create_link_code(member=self.member)
        LinkCode.objects.filter(pk=link.pk).update(
            expires_at=timezone.now() - timedelta(minutes=1)
        )
        with self.assertRaises(ValidationError):
            redeem_link_code(user=self.reader, code=link.code)

    def test_code_inconnu_refuse(self):
        with self.assertRaises(ValidationError):
            redeem_link_code(user=self.reader, code="ZZZZZZZZ")

    def test_code_insensible_a_la_casse_et_aux_espaces(self):
        link = create_link_code(member=self.member)
        result = redeem_link_code(user=self.reader, code=f"  {link.code.lower()} ")
        self.assertEqual(result.user, self.reader)

    def test_compte_deja_membre_ne_peut_pas_prendre_une_autre_fiche(self):
        request_membership(user=self.reader, organization=self.org_a)
        link = create_link_code(member=self.member)
        with self.assertRaises(ValidationError):
            redeem_link_code(user=self.reader, code=link.code)
        self.member.refresh_from_db()
        self.assertIsNone(self.member.user)
        link.refresh_from_db()
        self.assertIsNone(link.used_at)

    def test_rattachement_possible_pour_un_compte_membre_ailleurs(self):
        request_membership(user=self.reader, organization=self.org_b)
        link = create_link_code(member=self.member)
        redeem_link_code(user=self.reader, code=link.code)
        self.assertEqual(Member.objects.filter(user=self.reader).count(), 2)