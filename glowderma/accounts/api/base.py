from typing import Any

from rest_framework.views import APIView
from rest_framework.viewsets import GenericViewSet

from glowderma.accounts.api.throttles import StaffRateThrottle
from glowderma.accounts.authentication import StaffJWTAuthentication
from glowderma.accounts.permissions import IsActiveStaff
from glowderma.core.api.parsers import JSONParser


class AdminAPIView(APIView):
    parser_classes = [JSONParser]
    authentication_classes = [StaffJWTAuthentication]
    permission_classes = [IsActiveStaff]
    throttle_classes = [StaffRateThrottle]


class AdminGenericViewSet(GenericViewSet[Any]):
    parser_classes = [JSONParser]
    authentication_classes = [StaffJWTAuthentication]
    permission_classes = [IsActiveStaff]
    throttle_classes = [StaffRateThrottle]
