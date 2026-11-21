from typing import Any

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
