from typing import Any

from rest_framework.permissions import AllowAny
from rest_framework.views import APIView
from rest_framework.viewsets import GenericViewSet

from glowderma.accounts.api.throttles import StaffRateThrottle
from glowderma.accounts.authentication import StaffJWTAuthentication
from glowderma.accounts.permissions import IsActiveStaff, IsActiveWorkforceAccount, IsSpecialist
from glowderma.core.api.parsers import JSONParser


class WorkforceAPIView(APIView):
    parser_classes = [JSONParser]
    authentication_classes = [StaffJWTAuthentication]
    permission_classes = [IsActiveWorkforceAccount]
    throttle_classes = [StaffRateThrottle]


class AdminAPIView(WorkforceAPIView):
    permission_classes = [IsActiveStaff]


class SpecialistAPIView(WorkforceAPIView):
    permission_classes = [IsSpecialist]


class AdminGenericViewSet(GenericViewSet[Any]):
    parser_classes = [JSONParser]
    authentication_classes = [StaffJWTAuthentication]
    permission_classes = [IsActiveStaff]
    throttle_classes = [StaffRateThrottle]


# The public routes of the staff sign-in (login, refresh, logout, the password tokens, the MFA's second step):
# JSON only, no authentication, no default throttles (each view names its own). No docstring: it would become
# the OpenAPI description of every such operation.
class PublicAuthAPIView(APIView):
    parser_classes = [JSONParser]
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = []
