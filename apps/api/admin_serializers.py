"""Sérialiseurs de l'administration : plateforme (admin global) et structure (responsable)."""
from rest_framework import serializers

from apps.audit.models import AuditLog
from apps.organizations.models import LoanPolicy, Organization, OrganizationStaff

ORGANIZATION_FIELDS = [
    "description", "address", "city", "country", "phone", "email", "website",
]


class PlatformOrganizationSerializer(serializers.ModelSerializer):
    staff_count = serializers.IntegerField(read_only=True, default=0)
    admin_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = Organization
        fields = [
            "id", "name", "slug", *ORGANIZATION_FIELDS, "status",
            "library_module", "coworking_module", "created_at", "staff_count", "admin_count",
        ]
        read_only_fields = ["id", "slug", "status", "created_at"]

    def validate(self, attrs):
        library = attrs.get("library_module", getattr(self.instance, "library_module", True))
        coworking = attrs.get("coworking_module", getattr(self.instance, "coworking_module", False))
        if not (library or coworking):
            raise serializers.ValidationError("Activez au moins un module (bibliothèque ou coworking).")
        return attrs


class PlatformOrganizationCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=200)
    slug = serializers.SlugField(max_length=100, required=False, allow_blank=True, default="")
    description = serializers.CharField(required=False, allow_blank=True, default="")
    address = serializers.CharField(required=False, allow_blank=True, default="", max_length=255)
    city = serializers.CharField(required=False, allow_blank=True, default="", max_length=100)
    country = serializers.CharField(required=False, allow_blank=True, default="Togo", max_length=100)
    phone = serializers.CharField(required=False, allow_blank=True, default="", max_length=30)
    email = serializers.EmailField(required=False, allow_blank=True, default="")
    website = serializers.URLField(required=False, allow_blank=True, default="")
    library_module = serializers.BooleanField(required=False, default=True)
    coworking_module = serializers.BooleanField(required=False, default=False)
    status = serializers.ChoiceField(choices=Organization.Status.choices, required=False, default=Organization.Status.ACTIVE)

    def validate_slug(self, value):
        if value and Organization.objects.filter(slug=value).exists():
            raise serializers.ValidationError("Cet identifiant court est déjà utilisé.")
        return value

    def validate(self, attrs):
        if not (attrs["library_module"] or attrs["coworking_module"]):
            raise serializers.ValidationError("Activez au moins un module (bibliothèque ou coworking).")
        return attrs


class StatusInputSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=Organization.Status.choices)


class StaffMemberSerializer(serializers.ModelSerializer):
    user_id = serializers.IntegerField(source="user.id", read_only=True)
    email = serializers.EmailField(source="user.email", read_only=True)
    first_name = serializers.CharField(source="user.first_name", read_only=True)
    last_name = serializers.CharField(source="user.last_name", read_only=True)
    phone = serializers.CharField(source="user.phone", read_only=True)
    must_change_password = serializers.BooleanField(source="user.must_change_password", read_only=True)
    last_login = serializers.DateTimeField(source="user.last_login", read_only=True)

    class Meta:
        model = OrganizationStaff
        fields = [
            "id", "user_id", "email", "first_name", "last_name", "phone",
            "role", "active", "must_change_password", "last_login", "created_at",
        ]
        read_only_fields = fields


class StaffCreateSerializer(serializers.Serializer):
    email = serializers.EmailField()
    first_name = serializers.CharField(max_length=150)
    last_name = serializers.CharField(max_length=150)
    phone = serializers.CharField(required=False, allow_blank=True, default="", max_length=30)
    role = serializers.ChoiceField(choices=OrganizationStaff.Role.choices, required=False)


class StaffUpdateSerializer(serializers.Serializer):
    active = serializers.BooleanField(required=False)
    role = serializers.ChoiceField(choices=OrganizationStaff.Role.choices, required=False)


class StaffCredentialsSerializer(serializers.Serializer):
    """Réponse à une création ou une réinitialisation : le mot de passe n'est montré qu'une fois."""

    member = StaffMemberSerializer(allow_null=True)
    temporary_password = serializers.CharField(allow_null=True)


class LoanPolicySerializer(serializers.ModelSerializer):
    class Meta:
        model = LoanPolicy
        fields = ["loan_duration_days", "max_active_loans", "max_renewals", "allow_renewal", "updated_at"]
        read_only_fields = ["updated_at"]
        extra_kwargs = {
            "loan_duration_days": {"min_value": 1, "max_value": 365},
            "max_active_loans": {"min_value": 1, "max_value": 100},
            "max_renewals": {"min_value": 0, "max_value": 20},
        }


class StructureOrganizationSerializer(serializers.ModelSerializer):
    """Ce qu'un responsable peut modifier de sa structure (le nom, les modules et le statut restent à la plateforme)."""

    class Meta:
        model = Organization
        fields = ["id", "name", "slug", "status", "library_module", "coworking_module", *ORGANIZATION_FIELDS]
        read_only_fields = ["id", "name", "slug", "status", "library_module", "coworking_module"]


class AuditLogSerializer(serializers.ModelSerializer):
    action_label = serializers.CharField(source="get_action_display", read_only=True)
    user_email = serializers.SerializerMethodField()
    organization_name = serializers.CharField(source="organization.name", read_only=True)

    class Meta:
        model = AuditLog
        fields = [
            "id", "organization", "organization_name", "user_email", "action", "action_label",
            "entity_type", "entity_id", "description", "metadata", "created_at",
        ]
        read_only_fields = fields

    def get_user_email(self, log):
        return log.user.email if log.user_id else ""
