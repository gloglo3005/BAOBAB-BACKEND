from django.db import IntegrityError
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView

from apps.reader.forms import RegisterForm

from .serializers import (
    LoginResponseSerializer,
    LoginSerializer,
    MeSerializer,
    PasswordChangeSerializer,
)
from .throttles import LoginRateThrottle, PasswordChangeRateThrottle, RegisterRateThrottle


class PingView(APIView):
    """Route publique pour vÃ©rifier que l'API rÃ©pond."""

    permission_classes = [AllowAny]
    authentication_classes = []

    @extend_schema(
        responses=inline_serializer(
            "Ping",
            {"status": serializers.CharField(), "version": serializers.CharField()},
        )
    )
    def get(self, request):
        return Response({"status": "ok", "version": "v1"})


class LoginView(TokenObtainPairView):
    """Connexion par email et mot de passe."""

    serializer_class = LoginSerializer
    throttle_classes = [LoginRateThrottle]

    @extend_schema(responses=LoginResponseSerializer)
    def post(self, request, *args, **kwargs):
        return super().post(request, *args, **kwargs)


class RegisterView(APIView):
    """CrÃ©ation d'un compte lecteur par le lecteur lui-mÃªme.

    MÃªmes rÃ¨gles que le formulaire /reader/inscription/ (RegisterForm) : aucun champ
    de rÃ´le n'est lu, le compte n'est jamais staff ni administrateur. Renvoie les
    jetons, comme la connexion : le lecteur est connectÃ© dÃ¨s son inscription.
    """

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [RegisterRateThrottle]

    @extend_schema(
        request=inline_serializer(
            "Register",
            {
                "first_name": serializers.CharField(),
                "last_name": serializers.CharField(),
                "email": serializers.EmailField(),
                "phone": serializers.CharField(required=False),
                "password1": serializers.CharField(),
                "password2": serializers.CharField(),
            },
        ),
        responses={201: LoginResponseSerializer},
    )
    def post(self, request):
        form = RegisterForm(request.data)
        if not form.is_valid():
            return Response(
                {field: list(errors) for field, errors in form.errors.items()},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            user = form.save()
        except IntegrityError:
            return Response(
                {"email": ["Un compte existe dÃ©jÃ  avec cette adresse. Connectez-vous."]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        refresh = RefreshToken.for_user(user)
        return Response(
            {
                "access": str(refresh.access_token),
                "refresh": str(refresh),
                "must_change_password": False,
            },
            status=status.HTTP_201_CREATED,
        )


class MeView(APIView):
    """Profil du compte connectÃ©, ses structures de staff et ses fiches de lecteur."""

    permission_classes = [IsAuthenticated]

    @extend_schema(responses=MeSerializer)
    def get(self, request):
        return Response(MeSerializer(request.user).data)


class PasswordChangeView(APIView):
    """Changement de mot de passe ; lÃ¨ve l'obligation de changement."""

    permission_classes = [IsAuthenticated]
    throttle_classes = [PasswordChangeRateThrottle]

    @extend_schema(request=PasswordChangeSerializer, responses=LoginResponseSerializer)
    def post(self, request):
        serializer = PasswordChangeSerializer(
            data=request.data, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        user = request.user
        user.set_password(serializer.validated_data["new_password"])
        user.must_change_password = False
        user.save(update_fields=["password", "must_change_password"])
        refresh = RefreshToken.for_user(user)
        return Response(
            {
                "access": str(refresh.access_token),
                "refresh": str(refresh),
                "must_change_password": False,
            }
        )