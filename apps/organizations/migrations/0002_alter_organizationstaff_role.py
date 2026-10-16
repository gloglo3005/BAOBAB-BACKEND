from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("organizations", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="organizationstaff",
            name="role",
            field=models.CharField(
                choices=[
                    ("STAFF", "Personnel de structure"),
                    ("ADMIN", "Responsable de structure"),
                ],
                default="STAFF",
                max_length=20,
            ),
        ),
    ]
