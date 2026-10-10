from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from apps.members.models import Member
from apps.organizations.models import Organization

User = get_user_model()


class LoginSerializer(TokenObtainPairSerializer):
    """Connexion par email ; ajoute l'indicateur de changement de mot de passe."""

    def validate(self, attrs):
        data = super().validate(attrs)
        data["must_change_password"] = self.user.must_change_password
        return data


class LoginResponseSerializer(serializers.Serializer):
    access = serializers.CharField()
    refresh = serializers.CharField()
    must_change_password = serializers.BooleanField()


class OrganizationSummarySerializer(serializers.ModelSerializer):
    class Meta:
        model = Organization
        fields = ["id", "name", "slug", "library_module", "coworking_module"]


class MembershipSerializer(serializers.ModelSerializer):
    member_id = serializers.IntegerField(source="id")
    organization = OrganizationSummarySerializer()

    class Meta:
        model = Member
        fields = ["member_id", "member_number", "status", "organization"]


class MeSerializer(serializers.ModelSerializer):
    staff_organizations = serializers.SerializerMethodField()
    memberships = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id",
            "email",
            "first_name",
            "last_name",
            "phone",
            "is_platform_admin",
            "must_change_password",
            "staff_organizations",
            "memberships",
        ]

    @extend_schema_field(OrganizationSummarySerializer(many=True))
    def get_staff_organizations(self, user):
        """Structures actives où le compte est staff actif."""
        organizations = (
            Organization.objects.filter(
                status=Organization.Status.ACTIVE,
                staff_members__user=user,
                staff_members__active=True,
            )
            .distinct()
            .order_by("name")
        )
        return OrganizationSummarySerializer(organizations, many=True).data

    @extend_schema_field(MembershipSerializer(many=True))
    def get_memberships(self, user):
        """Fiches de lecteur du compte (tous statuts : l'app affiche l'état)."""
        members = (
            Member.objects.filter(user=user)
            .select_related("organization")
            .order_by("organization__name")
        )
        return MembershipSerializer(members, many=True).data


class PasswordChangeSerializer(serializers.Serializer):
    old_password = serializers.CharField(write_only=True)
    new_password = serializers.CharField(write_only=True)

    def validate_old_password(self, value):
        if not self.context["request"].user.check_password(value):
            raise serializers.ValidationError("L'ancien mot de passe est incorrect.")
        return value

    def validate_new_password(self, value):
        validate_password(value, self.context["request"].user)
        return value

    def validate(self, attrs):
        if attrs["old_password"] == attrs["new_password"]:
            raise serializers.ValidationError(
                {"new_password": "Le nouveau mot de passe doit être différent de l'ancien."}
            )
        return attrs
