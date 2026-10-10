from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from apps.catalog.models import BookCopy
from apps.circulation.services import checkout_copy
from apps.members.models import Member
from apps.organizations.models import Organization

User = get_user_model()


class Command(BaseCommand):
    help = "Prête un exemplaire à un membre (test en terminal, avant l'API)."

    def add_arguments(self, parser):
        parser.add_argument("--organization", required=True, help="slug de la structure")
        parser.add_argument("--staff-email", required=True, help="email de l'agent qui prête")
        parser.add_argument("--inventory-code", required=True, help="code d'inventaire de l'exemplaire")
        parser.add_argument("--member-number", required=True, help="numéro du membre (ex. MEM-0001)")
        parser.add_argument("--notes", default="")

    def handle(self, *args, **options):
        try:
            organization = Organization.objects.get(slug=options["organization"])
        except Organization.DoesNotExist:
            raise CommandError(
                f"Aucune structure avec le slug « {options['organization']} »."
            )

        staff = User.objects.filter(email__iexact=options["staff_email"]).first()
        if staff is None:
            raise CommandError(f"Aucun compte avec l'email « {options['staff_email']} ».")

        copy = BookCopy.objects.filter(
            organization=organization, inventory_code=options["inventory_code"]
        ).first()
        if copy is None:
            raise CommandError(
                f"Exemplaire « {options['inventory_code']} » introuvable dans cette structure."
            )

        member = Member.objects.filter(
            organization=organization, member_number=options["member_number"]
        ).first()
        if member is None:
            raise CommandError(
                f"Membre « {options['member_number']} » introuvable dans cette structure."
            )

        try:
            loan = checkout_copy(
                organization=organization,
                book_copy_id=copy.pk,
                member_id=member.pk,
                issued_by=staff,
                notes=options["notes"],
            )
        except ValidationError as error:
            raise CommandError(" ; ".join(error.messages))

        self.stdout.write(self.style.SUCCESS(
            f"Prêt n° {loan.pk} enregistré : {copy.inventory_code} → {member}. "
            f"À rendre le {loan.due_date:%d/%m/%Y}."
        ))
