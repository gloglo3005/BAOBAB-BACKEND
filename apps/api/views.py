from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView

from .serializers import (
    LoginResponseSerializer,
    LoginSerializer,
    MeSerializer,
    PasswordChangeSerializer,
)
from .throttles import LoginRateThrottle


class PingView(APIView):
    """Route publique pour vérifier que l'API répond."""

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


class MeView(APIView):
    """Profil du compte connecté, ses structures de staff et ses fiches de lecteur."""

    permission_classes = [IsAuthenticated]

    @extend_schema(responses=MeSerializer)
    def get(self, request):
        return Response(MeSerializer(request.user).data)


class PasswordChangeView(APIView):
    """Changement de mot de passe ; lève l'obligation de changement."""

    permission_classes = [IsAuthenticated]

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
