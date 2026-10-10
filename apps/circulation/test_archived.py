from django.core.exceptions import ValidationError

from apps.catalog.services import archive_book, restore_book

from .models import Loan
from .tests import CirculationTestCase


class ArchivedBookTests(CirculationTestCase):
    def test_un_livre_archive_ne_se_prete_plus(self):
        archive_book(book=self.book_a)
        with self.assertRaisesMessage(ValidationError, "archivé"):
            self.checkout()
        self.assertEqual(Loan.objects.count(), 0)

    def test_restaure_il_se_prete_a_nouveau(self):
        archive_book(book=self.book_a)
        restore_book(book=self.book_a)
        self.assertEqual(self.checkout().book_copy, self.copy_a)
