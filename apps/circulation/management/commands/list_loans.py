from django.core.management.base import BaseCommand, CommandError

from apps.circulation.models import Loan
from apps.organizations.models import Organization


class Command(BaseCommand):
    help = (
        "Liste les prêts d'une structure : en cours par défaut, "
        "seulement les retards avec --overdue, tous avec --all."
    )

    def add_arguments(self, parser):
        parser.add_argument("--organization", required=True, help="slug de la structure")
        which = parser.add_mutually_exclusive_group()
        which.add_argument(
            "--overdue", action="store_true", help="seulement les prêts en retard"
        )
        which.add_argument(
            "--all", action="store_true", help="tous les prêts, rendus et perdus compris"
        )

    def handle(self, *args, **options):
        try:
            organization = Organization.objects.get(slug=options["organization"])
        except Organization.DoesNotExist:
            raise CommandError(
                f"Aucune structure avec le slug « {options['organization']} »."
            )

        loans = Loan.objects.for_organization(organization).select_related(
            "book_copy", "member"
        )
        if options["all"]:
            title = "Tous les prêts"
        elif options["overdue"]:
            loans = loans.overdue().order_by("due_date")
            title = "Prêts en retard"
        else:
            loans = loans.active().order_by("due_date")
            title = "Prêts en cours"

        loans = list(loans)
        if not loans:
            self.stdout.write(f"{title} : aucun ({organization}).")
            return

        self.stdout.write(self.style.SUCCESS(f"{title} ({organization}) : {len(loans)}"))
        for loan in loans:
            line = (
                f"n°{loan.pk:<4} {loan.book_copy.inventory_code:<12} "
                f"{loan.member.member_number:<10} "
                f"{loan.member.first_name} {loan.member.last_name}  "
                f"à rendre le {loan.due_date:%d/%m/%Y}"
            )
            if loan.is_overdue:
                self.stdout.write(self.style.WARNING(
                    f"{line}  EN RETARD ({loan.days_overdue} j)"
                ))
            else:
                self.stdout.write(f"{line}  [{loan.get_status_display()}]")