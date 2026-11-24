"""Admin views of `assistant` (contract §2.19): status, start, poll, accept, apply, discard.

Any active staff member may use them (the people who may edit products); providers, keys, prompts and
budgets are managed only in the ops console. Responses never carry tokens, costs, prompts or provider
errors.
"""

from __future__ import annotations

from typing import Any

from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts.api.base import AdminAPIView
from glowderma.accounts.api.throttles import StaffRateThrottle
from glowderma.assistant import selectors, services
from glowderma.assistant.api.admin.schemas import (
    ASSISTANT_JOB_ACCEPT_POST,
    ASSISTANT_JOB_APPLY_POST,
    ASSISTANT_JOB_DETAIL_GET,
    ASSISTANT_JOB_DISCARD_POST,
    ASSISTANT_JOB_START_POST,
    ASSISTANT_STATUS_GET,
)
from glowderma.assistant.api.admin.serializers import (
    AssistantAcceptSerializer,
    AssistantApplySerializer,
    AssistantJobCreateSerializer,
    AssistantJobSerializer,
)
from glowderma.catalog.api.admin.serializers import ProductSerializer
from glowderma.core import enums
from glowderma.core.idempotency import idempotent


class AssistantGenerateThrottle(StaffRateThrottle):
    """``products/{id}/assistant/jobs/`` per staff member (each call may reach the paid provider)."""

    scope = "assistant_generate"


class AssistantStatusView(AdminAPIView):
    @ASSISTANT_STATUS_GET
    def get(self, request: Request) -> Response:
        assistant = services.get_settings()
        return Response({"enabled": services.is_available(), "max_images": assistant.max_images})


class AssistantJobStartView(AdminAPIView):
    throttle_classes = [StaffRateThrottle, AssistantGenerateThrottle]

    @ASSISTANT_JOB_START_POST
    @idempotent(required=False)
    def post(self, request: Request, product_id: int) -> Response:
        serializer = AssistantJobCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        result = services.start_job(
            product_id,
            mode=data["mode"],
            languages=data.get("languages") or list(enums.ContentLanguage.values),
            fields=data.get("keys") or [],
            goal=data["goal"],
            image_ids=data.get("image_ids") or [],
            draft=data.get("draft") or {},
            actor=request.user,
        )
        body: dict[str, Any] = {
            "nothing_to_fill": result.job is None,
            "job": AssistantJobSerializer(result.job, context={"request": request}).data
            if result.job
            else None,
        }
        return Response(body, status=202 if result.created else 200)


class AssistantJobDetailView(AdminAPIView):
    @ASSISTANT_JOB_DETAIL_GET
    def get(self, request: Request, pk: int) -> Response:
        job = selectors.get_job(pk)
        if job is None:
            raise NotFound
        return Response(AssistantJobSerializer(job, context={"request": request}).data)


class AssistantJobAcceptView(AdminAPIView):
    @ASSISTANT_JOB_ACCEPT_POST
    @idempotent(required=False)
    def post(self, request: Request, pk: int) -> Response:
        serializer = AssistantAcceptSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        job = services.accept_job(pk, serializer.validated_data["keys"], actor=request.user, request=request)
        job = selectors.get_job(job.pk) or job
        return Response({"job": AssistantJobSerializer(job, context={"request": request}).data})


class AssistantJobApplyView(AdminAPIView):
    @ASSISTANT_JOB_APPLY_POST
    @idempotent(required=False)
    def post(self, request: Request, pk: int) -> Response:
        serializer = AssistantApplySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        items = [
            services.ApplyItem(key=i["key"], value=i["value"]) for i in serializer.validated_data["items"]
        ]
        result = services.apply_job(pk, items, actor=request.user, request=request)
        job = selectors.get_job(result.job.pk) or result.job
        return Response(
            {
                "job": AssistantJobSerializer(job, context={"request": request}).data,
                "applied": result.applied,
                "conflicts": result.conflicts,
                "product": ProductSerializer(result.product).data,
            }
        )


class AssistantJobDiscardView(AdminAPIView):
    @ASSISTANT_JOB_DISCARD_POST
    @idempotent(required=False)
    def post(self, request: Request, pk: int) -> Response:
        job = services.discard_job(pk, actor=request.user, request=request)
        job = selectors.get_job(job.pk) or job
        return Response(AssistantJobSerializer(job, context={"request": request}).data)
