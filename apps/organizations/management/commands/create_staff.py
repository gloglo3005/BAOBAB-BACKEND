from django.core.management.base import BaseCommand, CommandError

from apps.organizations.models import Organization, OrganizationStaff
from apps.organizations.services import create_staff_account


class Command(BaseCommand):
    help = "Crée un compte Staff et le rattache à une structure."

    def add_arguments(self, parser):
        parser.add_argument("--organization", required=True, help="slug de la structure")
        parser.add_argument("--email", required=True)
        parser.add_argument("--first-name", required=True)
        parser.add_argument("--last-name", required=True)
        parser.add_argument("--phone", default="")
        parser.add_argument("--admin", action="store_true", help="crée un responsable de structure")

    def handle(self, *args, **options):
        try:
            organization = Organization.objects.get(slug=options["organization"])
        except Organization.DoesNotExist:
            raise CommandError(
                f"Aucune structure avec le slug « {options['organization']} »."
            )

        user, password = create_staff_account(
            organization=organization,
            email=options["email"],
            first_name=options["first_name"],
            last_name=options["last_name"],
            phone=options["phone"],
            role=OrganizationStaff.Role.ADMIN if options["admin"] else OrganizationStaff.Role.STAFF,
        )

        if password is None:
            self.stdout.write(self.style.WARNING(
                f"{user.email} existait déjà : rattaché à « {organization} ». "
                "Mot de passe inchangé."
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                f"Compte Staff créé pour « {organization} »."
            ))
            self.stdout.write(f"Email : {user.email}")
            self.stdout.write(f"Mot de passe temporaire : {password}")
            self.stdout.write(
                "À communiquer une seule fois ; il devra être changé "
                "à la première connexion."
            )