"""API staff : renouvellement d'un prêt et modification d'un membre."""
from django.core.exceptions import ValidationError as DjangoValidationError
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.circulation.models import Loan
from apps.circulation.renewals import renew_loan
from apps.members.editing import update_member

from .permissions import STAFF_LIBRARY
from .staff_circulation_views import invalid_identifier, refuse
from .staff_members_views import refuse_validation, serialized
from .staff_serializers import LoanSerializer, MemberSerializer
from .staff_views import MemberDetailView


class LoanRenewView(APIView):
    """Renouvelle un prêt en cours : l'échéance avance de la durée de la politique de prêt.

    409 si une règle l'interdit (limite atteinte, retard, membre suspendu...).
    """

    permission_classes = STAFF_LIBRARY

    @extend_schema(request=None, responses=LoanSerializer)
    def post(self, request, organization_id, pk):
        try:
            loan = renew_loan(
                organization=request.organization, loan_id=pk, renewed_by=request.user
            )
        except DjangoValidationError as exc:
            return refuse(exc)
        except (ValueError, TypeError):
            return invalid_identifier()
        loan = Loan.objects.select_related("book_copy__book", "member").get(pk=loan.pk)
        return Response(LoanSerializer(loan).data)


class MemberUpdateSerializer(serializers.Serializer):
    first_name = serializers.CharField(max_length=100)
    last_name = serializers.CharField(max_length=100)
    phone = serializers.CharField(max_length=30, allow_blank=True)
    member_number = serializers.CharField(max_length=50)


class MemberDetailUpdateView(MemberDetailView):
    """GET : fiche du membre. PATCH : modifie prénom, nom, téléphone, numéro de membre.

    Le statut ne se change pas ici : voir accept, reject, suspend, reactivate.
    """

    @extend_schema(request=MemberUpdateSerializer, responses=MemberSerializer)
    def patch(self, request, organization_id, pk):
        member = self.get_object()
        data = MemberUpdateSerializer(data=request.data, partial=True)
        data.is_valid(raise_exception=True)
        try:
            update_member(member=member, performed_by=request.user, **data.validated_data)
        except DjangoValidationError as exc:
            return refuse_validation(exc)
        return Response(serialized(member))
