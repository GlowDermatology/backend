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
    BANNER_IMAGE_START_POST,
    BANNER_IMAGE_USE_POST,
    SCENE_JOB_ACCEPT_POST,
    SCENE_JOB_DETAIL_GET,
    SCENE_JOB_DISCARD_POST,
    SCENE_JOB_START_POST,
    SCENE_JOB_TEMPLATE_POST,
    SCENE_TEMPLATES_GET,
    SCENES_STATUS_GET,
)
from glowderma.assistant.api.admin.serializers import (
    AssistantAcceptSerializer,
    AssistantApplySerializer,
    AssistantJobCreateSerializer,
    AssistantJobSerializer,
    BannerImageCreateSerializer,
    BannerImageUseSerializer,
    SceneAcceptSerializer,
    SceneJobCreateSerializer,
    SceneJobSerializer,
    SceneStatusSerializer,
    SceneTemplateSaveSerializer,
    SceneTemplateSerializer,
)
from glowderma.assistant.models import SceneJob, SceneTemplate
from glowderma.assistant.scenes import banner_jobs
from glowderma.assistant.scenes import services as scene_services
from glowderma.catalog.api.admin.serializers import (
    BannerPatchSerializer,
    BannerSerializer,
    BannerWriteSerializer,
    ProductImageSerializer,
    ProductSerializer,
)
from glowderma.catalog.models import Banner
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


# --- product scenes (contract §2.20) ------------------------------------------------------------------------


class SceneGenerateThrottle(StaffRateThrottle):
    """``products/{id}/images/{image_id}/scenes/`` per staff member (paid providers)."""

    scope = "scene_generate"


class ScenesStatusView(AdminAPIView):
    @SCENES_STATUS_GET
    def get(self, request: Request) -> Response:
        return Response(SceneStatusSerializer(scene_services.status()).data)


class SceneTemplatesView(AdminAPIView):
    @SCENE_TEMPLATES_GET
    def get(self, request: Request) -> Response:
        templates = SceneTemplate.objects.filter(enabled=True).order_by("position", "id")
        return Response(
            {"results": SceneTemplateSerializer(templates, many=True, context={"request": request}).data}
        )


class SceneJobStartView(AdminAPIView):
    throttle_classes = [StaffRateThrottle, SceneGenerateThrottle]

    @SCENE_JOB_START_POST
    @idempotent(required=False)
    def post(self, request: Request, product_id: int, image_id: int) -> Response:
        serializer = SceneJobCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        result = scene_services.start_job(
            product_id,
            image_id,
            kind=data["kind"],
            template_id=data.get("template_id"),
            custom_prompt=data.get("custom_prompt") or "",
            actor=request.user,
        )
        body = SceneJobSerializer(result.job, context={"request": request}).data
        return Response(body, status=202 if result.created else 200)


def _scene_job(pk: int) -> SceneJob:
    job = SceneJob.objects.select_related("created_by").filter(pk=pk).first()
    if job is None:
        raise NotFound
    return job


class SceneJobDetailView(AdminAPIView):
    @SCENE_JOB_DETAIL_GET
    def get(self, request: Request, pk: int) -> Response:
        return Response(SceneJobSerializer(_scene_job(pk), context={"request": request}).data)


class SceneJobAcceptView(AdminAPIView):
    @SCENE_JOB_ACCEPT_POST
    @idempotent(required=False)
    def post(self, request: Request, pk: int) -> Response:
        serializer = SceneAcceptSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        job, image = scene_services.accept(
            pk,
            make_main=serializer.validated_data["make_main"],
            acknowledge_generated=serializer.validated_data["acknowledge_generated"],
            actor=request.user,
            request=request,
        )
        return Response(
            {
                "job": SceneJobSerializer(_scene_job(job.pk), context={"request": request}).data,
                "image": ProductImageSerializer(image).data,
            }
        )


class SceneJobTemplateView(AdminAPIView):
    """«Shablon sifatida saqlash»: a scene staff typed becomes a template for everyone."""

    @SCENE_JOB_TEMPLATE_POST
    @idempotent(required=False)
    def post(self, request: Request, pk: int) -> Response:
        serializer = SceneTemplateSaveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        template = scene_services.save_as_template(
            pk, name=serializer.validated_data["name"], actor=request.user, request=request
        )
        return Response(SceneTemplateSerializer(template, context={"request": request}).data, status=201)


class SceneJobDiscardView(AdminAPIView):
    @SCENE_JOB_DISCARD_POST
    @idempotent(required=False)
    def post(self, request: Request, pk: int) -> Response:
        job = scene_services.discard(pk, actor=request.user, request=request)
        return Response(SceneJobSerializer(_scene_job(job.pk), context={"request": request}).data)


class BannerImageStartView(AdminAPIView):
    """«AI bilan yaratish» of the banner dialog: a banner's image with products or a scene alone."""

    throttle_classes = [StaffRateThrottle, SceneGenerateThrottle]

    @BANNER_IMAGE_START_POST
    @idempotent(required=False)
    def post(self, request: Request) -> Response:
        serializer = BannerImageCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        result = banner_jobs.start(
            mode=data["mode"],
            product_ids=list(data.get("product_ids") or []),
            template_id=data.get("template_id"),
            custom_prompt=data.get("custom_prompt") or "",
            actor=request.user,
        )
        body = SceneJobSerializer(result.job, context={"request": request}).data
        return Response(body, status=202 if result.created else 200)


class BannerImageUseView(AdminAPIView):
    """Save a banner (new or existing) with a finished banner image."""

    @BANNER_IMAGE_USE_POST
    @idempotent(required=False)
    def post(self, request: Request, pk: int) -> Response:
        serializer = BannerImageUseSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        banner_id = serializer.validated_data.get("banner_id")
        fields = serializer.validated_data["banner"]
        if not isinstance(fields, dict):
            fields = {}
        if banner_id is None:
            write = BannerWriteSerializer(data=fields)
        else:
            existing = Banner.objects.select_related("link_category").filter(pk=banner_id).first()
            if existing is None:
                raise NotFound
            write = BannerPatchSerializer(instance=existing, data=fields, partial=True)
        write.is_valid(raise_exception=True)
        job, banner = banner_jobs.use_for_banner(
            pk, write.to_input(), banner_id=banner_id, actor=request.user, request=request
        )
        return Response(
            {
                "job": SceneJobSerializer(_scene_job(job.pk), context={"request": request}).data,
                "banner": BannerSerializer(banner).data,
            }
        )
