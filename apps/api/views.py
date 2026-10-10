from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView


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
