"""API staff en écriture, circulation : prêt, retour, perte.

Chaque route appelle les services de apps.circulation : règles métier,
verrous et journal d'audit sont donc les mêmes que dans l'interface /staff/.
"""
from django.core.exceptions import ValidationError as DjangoValidationError
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.circulation.services import ReturnOutcome, checkout_copy, return_copy

from .permissions import STAFF_LIBRARY
from .staff_serializers import LoanSerializer
from .staff_views import LoanListView


class LoanCreateSerializer(serializers.Serializer):
    book_copy = serializers.CharField(help_text="Identifiant de l'exemplaire.")
    member = serializers.CharField(help_text="Identifiant du membre.")
    notes = serializers.CharField(required=False, allow_blank=True, default="")


class LoanReturnSerializer(serializers.Serializer):
    outcome = serializers.ChoiceField(
        choices=ReturnOutcome.choices,
        default=ReturnOutcome.GOOD,
        help_text="GOOD (bon état), DAMAGED (endommagé) ou LOST (perdu).",
    )
    notes = serializers.CharField(required=False, allow_blank=True, default="")


class LoanLostSerializer(serializers.Serializer):
    notes = serializers.CharField(required=False, allow_blank=True, default="")


def refuse(exc):
    """Refus d'un service : 404 si l'objet est introuvable, sinon 409 (règle métier)."""
    text = " ".join(exc.messages)
    code = (
        status.HTTP_404_NOT_FOUND
        if "introuvable" in text.lower()
        else status.HTTP_409_CONFLICT
    )
    return Response({"detail": text}, status=code)


def invalid_identifier():
    return Response({"detail": "Identifiant invalide."}, status=status.HTTP_404_NOT_FOUND)


class LoanListCreateView(LoanListView):
    """GET : liste des prêts (lecture). POST : enregistre un prêt."""

    @extend_schema(request=LoanCreateSerializer, responses={201: LoanSerializer})
    def post(self, request, organization_id):
        data = LoanCreateSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        try:
            loan = checkout_copy(
                organization=request.organization,
                book_copy_id=data.validated_data["book_copy"],
                member_id=data.validated_data["member"],
                issued_by=request.user,
                notes=data.validated_data["notes"],
            )
        except DjangoValidationError as exc:
            return refuse(exc)
        except (ValueError, TypeError):
            return invalid_identifier()
        return Response(LoanSerializer(loan).data, status=status.HTTP_201_CREATED)


def close_loan(request, pk, outcome, notes):
    try:
        loan = return_copy(
            organization=request.organization,
            loan_id=pk,
            received_by=request.user,
            outcome=outcome,
            notes=notes,
        )
    except DjangoValidationError as exc:
        return refuse(exc)
    except (ValueError, TypeError):
        return invalid_identifier()
    return Response(LoanSerializer(loan).data)


class LoanReturnView(APIView):
    """Enregistre le retour d'un prêt (bon état, endommagé ou perdu)."""

    permission_classes = STAFF_LIBRARY

    @extend_schema(request=LoanReturnSerializer, responses=LoanSerializer)
    def post(self, request, organization_id, pk):
        data = LoanReturnSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        return close_loan(
            request, pk, data.validated_data["outcome"], data.validated_data["notes"]
        )


class LoanLostView(APIView):
    """Raccourci : déclare un prêt perdu (équivaut à un retour avec outcome=LOST)."""

    permission_classes = STAFF_LIBRARY

    @extend_schema(request=LoanLostSerializer, responses=LoanSerializer)
    def post(self, request, organization_id, pk):
        data = LoanLostSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        return close_loan(request, pk, ReturnOutcome.LOST, data.validated_data["notes"])