"""OpenAPI declarations for admin views of `assistant` (contract §2.19): status, start, poll, accept, apply,
discard.

Any active staff member may use them (the people who may edit products); providers, keys, prompts and budgets
are managed only in the ops console. Responses never carry tokens, costs, prompts or provider errors."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema

from glowderma.assistant.api.admin.serializers import (
    AssistantAcceptResultSerializer,
    AssistantAcceptSerializer,
    AssistantApplyResultSerializer,
    AssistantApplySerializer,
    AssistantJobCreateSerializer,
    AssistantJobSerializer,
    AssistantStartSerializer,
    AssistantStatusSerializer,
    SceneAcceptResponseSerializer,
    SceneAcceptSerializer,
    SceneJobCreateSerializer,
    SceneJobSerializer,
    SceneStatusSerializer,
    SceneTemplatesSerializer,
)
from glowderma.core.api.schema import ADMIN_ERRORS

ASSISTANT_STATUS_GET = extend_schema(
    operation_id="admin_assistant_status_retrieve",
    description="Whether the product editor shows the AI assistant (switched on and its provider ready).",
    responses={200: AssistantStatusSerializer, **ADMIN_ERRORS},
)

ASSISTANT_JOB_START_POST = extend_schema(
    operation_id="admin_assistant_jobs_create",
    description=(
        "Queue an AI generation for the product (202), or return its queued/running job (200), or "
        "`nothing_to_fill: true` with `job: null` (200) when the mode finds no key to suggest. Poll "
        "`assistant/jobs/{id}/`."
    ),
    request=AssistantJobCreateSerializer,
    responses={200: AssistantStartSerializer, 202: AssistantStartSerializer, **ADMIN_ERRORS},
)

ASSISTANT_JOB_DETAIL_GET = extend_schema(
    operation_id="admin_assistant_jobs_retrieve",
    responses={200: AssistantJobSerializer, **ADMIN_ERRORS},
)

ASSISTANT_JOB_ACCEPT_POST = extend_schema(
    operation_id="admin_assistant_jobs_accept_create",
    description=(
        "Record which suggestions the person took into the (unsaved) product form. The product is not "
        "written — the person saves the form. `outcome` becomes `applied` (every suggestion), `partial` "
        "(some) or `discarded` (none); each key must be one of the job's suggestions. A job that is not "
        "a pending, succeeded one is a 409."
    ),
    request=AssistantAcceptSerializer,
    responses={200: AssistantAcceptResultSerializer, **ADMIN_ERRORS},
)

ASSISTANT_JOB_APPLY_POST = extend_schema(
    operation_id="admin_assistant_jobs_apply_create",
    description=(
        "Apply the selected suggestions (values may be edited; they are checked again). A key whose "
        "product value changed since the generation is returned in `conflicts` and not written."
    ),
    request=AssistantApplySerializer,
    responses={200: AssistantApplyResultSerializer, **ADMIN_ERRORS},
)

ASSISTANT_JOB_DISCARD_POST = extend_schema(
    operation_id="admin_assistant_jobs_discard_create",
    request=None,
    responses={200: AssistantJobSerializer, **ADMIN_ERRORS},
)


SCENES_STATUS_GET = extend_schema(
    operation_id="admin_scenes_status_retrieve",
    description="Whether the product editor offers scenes and screen fitting, and which kinds.",
    responses={200: SceneStatusSerializer, **ADMIN_ERRORS},
)

SCENE_TEMPLATES_GET = extend_schema(
    operation_id="admin_scene_templates_list",
    description="The enabled scene templates, in their console order.",
    responses={200: SceneTemplatesSerializer, **ADMIN_ERRORS},
)

SCENE_JOB_START_POST = extend_schema(
    operation_id="admin_scene_jobs_create",
    description=(
        "Build a scene around a saved image's product (`studio` / `ai_scene` with `template_id`) or propose "
        "its 4:5 and square crops (`fit`). 202 with a new job, 200 with the image's queued/running job. The "
        "product itself is never redrawn: the photo's own pixels are pasted back. Refusals (as the job's "
        "`error.code`): `scene_product_cut_off`, `scene_hand_covers_product`, `scene_occlusion_needs_ai`, "
        "`scene_no_product`, `scene_mask_unusable`, `scene_product_drift`, `scene_result_rejected`."
    ),
    request=SceneJobCreateSerializer,
    responses={200: SceneJobSerializer, 202: SceneJobSerializer, **ADMIN_ERRORS},
)

SCENE_JOB_DETAIL_GET = extend_schema(
    operation_id="admin_scene_jobs_retrieve", responses={200: SceneJobSerializer, **ADMIN_ERRORS}
)

SCENE_JOB_ACCEPT_POST = extend_schema(
    operation_id="admin_scene_jobs_accept_create",
    description=(
        "Accept a finished job: a scene becomes a new gallery image (the source stays; `make_main` puts it "
        "first), a `fit` writes the proposed renditions on the source image. `acknowledge_generated` must be "
        "true when part of the product was drawn by AI. 409 `scene_job_not_ready` / `scene_source_changed`."
    ),
    request=SceneAcceptSerializer,
    responses={200: SceneAcceptResponseSerializer, **ADMIN_ERRORS},
)

SCENE_JOB_DISCARD_POST = extend_schema(
    operation_id="admin_scene_jobs_discard_create",
    request=None,
    responses={200: SceneJobSerializer, **ADMIN_ERRORS},
)


__all__ = [
    "ASSISTANT_JOB_ACCEPT_POST",
    "ASSISTANT_JOB_APPLY_POST",
    "ASSISTANT_JOB_DETAIL_GET",
    "ASSISTANT_JOB_DISCARD_POST",
    "ASSISTANT_JOB_START_POST",
    "ASSISTANT_STATUS_GET",
    "SCENES_STATUS_GET",
    "SCENE_JOB_ACCEPT_POST",
    "SCENE_JOB_DETAIL_GET",
    "SCENE_JOB_DISCARD_POST",
    "SCENE_JOB_START_POST",
    "SCENE_TEMPLATES_GET",
]
