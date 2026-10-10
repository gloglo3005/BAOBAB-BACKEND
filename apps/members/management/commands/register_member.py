from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from apps.organizations.models import Organization
from apps.members.services import register_member


class Command(BaseCommand):
    help = "Inscrit un membre au comptoir (accepté tout de suite, sans compte)."

    def add_arguments(self, parser):
        parser.add_argument("--organization", required=True, help="slug de la structure")
        parser.add_argument("--first-name", required=True)
        parser.add_argument("--last-name", required=True)
        parser.add_argument("--phone", default="")
        parser.add_argument(
            "--member-number",
            default=None,
            help="numéro saisi à la main (sinon attribué automatiquement)",
        )

    def handle(self, *args, **options):
        try:
            organization = Organization.objects.get(slug=options["organization"])
        except Organization.DoesNotExist:
            raise CommandError(
                f"Aucune structure avec le slug « {options['organization']} »."
            )

        try:
            member = register_member(
                organization=organization,
                first_name=options["first_name"],
                last_name=options["last_name"],
                phone=options["phone"],
                member_number=options["member_number"],
            )
        except ValidationError as error:
            raise CommandError(" ; ".join(error.messages))

        self.stdout.write(
            self.style.SUCCESS(f"Membre inscrit : {member} ({organization}).")
        )