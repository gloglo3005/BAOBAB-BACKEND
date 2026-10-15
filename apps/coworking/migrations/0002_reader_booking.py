from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("coworking", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="coworkingprofile",
            name="max_upcoming_reservations",
            field=models.PositiveIntegerField(
                default=3,
                help_text="0 = pas de limite. Ne s'applique qu'aux réservations faites depuis l'application.",
                verbose_name="réservations à venir par client (application)",
            ),
        ),
        migrations.AddField(
            model_name="reservation",
            name="source",
            field=models.CharField(
                choices=[("COUNTER", "Accueil"), ("APP", "Application")],
                default="COUNTER",
                max_length=10,
                verbose_name="origine",
            ),
        ),
    ]
