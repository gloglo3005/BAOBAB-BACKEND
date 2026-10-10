from django.contrib.auth import get_user_model

from .services import create_link_code, redeem_link_code, register_member
from .tests import MembersTestCase

User = get_user_model()


class LinkContactTests(MembersTestCase):
    def link(self, user, **member_data):
        member = register_member(
            organization=self.org_a, first_name="Ama", last_name="Kofi", **member_data
        )
        code = create_link_code(member=member)
        redeem_link_code(user=user, code=code.code)
        member.refresh_from_db()
        return member

    def test_le_telephone_du_compte_complete_une_fiche_sans_telephone(self):
        member = self.link(self.reader)
        self.assertEqual(member.user, self.reader)
        self.assertEqual(member.phone, "90000000")

    def test_le_telephone_saisi_au_comptoir_n_est_jamais_ecrase(self):
        member = self.link(self.reader, phone="91111111")
        self.assertEqual(member.phone, "91111111")

    def test_compte_sans_telephone_laisse_la_fiche_vide(self):
        sans_telephone = User.objects.create_user(
            email="sans@example.com", password="abc12345", first_name="Sans", last_name="Tel"
        )
        member = self.link(sans_telephone)
        self.assertEqual(member.phone, "")