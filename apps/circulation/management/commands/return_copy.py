from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from apps.circulation.models import Loan
from apps.circulation.services import ReturnOutcome, return_copy
from apps.organizations.models import Organization

User = get_user_model()


class Command(BaseCommand):
    help = "Enregistre le retour d'un exemplaire (test en terminal, avant l'API)."

    def add_arguments(self, parser):
        parser.add_argument("--organization", required=True, help="slug de la structure")
        parser.add_argument("--staff-email", required=True, help="email de l'agent qui reçoit")
        which = parser.add_mutually_exclusive_group(required=True)
        which.add_argument("--loan-id", type=int, help="numéro du prêt")
        which.add_argument(
            "--inventory-code", help="code d'inventaire (prend le prêt en cours de cet exemplaire)"
        )
        parser.add_argument(
            "--outcome",
            choices=[choice.value for choice in ReturnOutcome],
            default=ReturnOutcome.GOOD.value,
            help="GOOD (bon état), DAMAGED (endommagé) ou LOST (perdu)",
        )
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

        loan_id = options["loan_id"]
        if loan_id is None:
            loan = Loan.objects.active().filter(
                organization=organization,
                book_copy__inventory_code=options["inventory_code"],
            ).first()
            if loan is None:
                raise CommandError(
                    f"Aucun prêt en cours pour l'exemplaire « {options['inventory_code']} »."
                )
            loan_id = loan.pk

        try:
            loan = return_copy(
                organization=organization,
                loan_id=loan_id,
                received_by=staff,
                outcome=options["outcome"],
                notes=options["notes"],
            )
        except ValidationError as error:
            raise CommandError(" ; ".join(error.messages))

        self.stdout.write(self.style.SUCCESS(
            f"Prêt n° {loan.pk} clôturé ({loan.get_status_display()}). "
            f"Exemplaire : {loan.book_copy.get_status_display()}."
        ))
