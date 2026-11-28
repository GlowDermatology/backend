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
    BannerImageCreateSerializer,
    BannerImageUseResponseSerializer,
    BannerImageUseSerializer,
    SceneAcceptResponseSerializer,
    SceneAcceptSerializer,
    SceneJobCreateSerializer,
    SceneJobSerializer,
    SceneStatusSerializer,
    SceneTemplateSaveSerializer,
    SceneTemplateSerializer,
    SceneTemplatesSerializer,
    SceneTemplateWriteSerializer,
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
        "`scene_occlusion_not_fillable`, `scene_custom_refused`, `scene_no_product`, `scene_mask_unusable`, "
        "`scene_product_drift`, `scene_result_rejected`. `ai_scene` takes `template_id` or `custom_prompt`."
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

SCENE_JOB_TEMPLATE_POST = extend_schema(
    operation_id="admin_scene_jobs_template_create",
    description=(
        "Save a finished scene staff typed (`template.key == custom`) as an AI template for everyone: its "
        "English description, the given `name`, and the scene as the card's preview. Allowed while it waits "
        "for review or after it was accepted, once per job: 409 `scene_not_custom` / `scene_template_exists`."
    ),
    request=SceneTemplateSaveSerializer,
    responses={201: SceneTemplateSerializer, **ADMIN_ERRORS},
)

BANNER_IMAGE_START_POST = extend_schema(
    operation_id="admin_banner_images_create",
    description=(
        "Make a banner's image with AI (scenes must be on, AI scenes too): 1-3 products in a scene "
        "(`products`, each from its main photo, never redrawn) or a scene alone (`scene`), from an AI "
        "`template_id` or `custom_prompt`. The image is exactly 2.15:1 (the app's banner; the website shows "
        "its middle 2:1, `banner.web_crop`), text-free, its lower left kept calm for the title. 202 with a "
        "new scene job (kind `banner`), 200 with the staff member's banner image still being made. Poll "
        "`scenes/jobs/{id}/`; refusals as the job's `error.code`: `banner_product_photo_unusable` (with "
        "`banner.failed_product`), `scene_source_changed`, `scene_custom_refused`, `scene_product_drift`, "
        "`scene_result_rejected`."
    ),
    request=BannerImageCreateSerializer,
    responses={200: SceneJobSerializer, 202: SceneJobSerializer, **ADMIN_ERRORS},
)

BANNER_IMAGE_USE_POST = extend_schema(
    operation_id="admin_scene_jobs_banner_create",
    description=(
        "Use a finished banner image (kind `banner`, waiting for review): a new banner with `banner` as in "
        "`POST banners/` (without `image`), or an existing one (`banner_id`, `banner` as in "
        "`PATCH banners/{id}/`), saved with the image in one transaction (the same checks and variants as an "
        "upload). 409 `scene_job_not_ready` once used, discarded or expired."
    ),
    request=BannerImageUseSerializer,
    responses={200: BannerImageUseResponseSerializer, **ADMIN_ERRORS},
)

SCENE_TEMPLATE_CREATE = extend_schema(
    operation_id="admin_scene_templates_create",
    description=(
        "A new AI template (§2.20.9), last in the list: `name` (`uz` required) and `text` (the scene in any "
        "language) and/or `reference` (a picture of the scene alone). JSON or multipart (`name.uz`, …, the "
        "file part `reference`). The AI checks it on the worker: 202 the job (kind `template`); poll "
        "`scenes/jobs/{id}/` until `succeeded` (`template.saved_template_id` is the new template) or "
        "`failed` "
        "(nothing saved): `scene_custom_refused` (the text asks for people, text or brands; field `text`), "
        "`template_reference_product` / `template_reference_people` / `template_reference_text` (the "
        "picture; field `reference`), or the AI's own codes. 400 `text` `scene` without either, `reference` "
        "for an unreadable picture; 409 `scenes_disabled` / `scene_ai_disabled`."
    ),
    request={
        "multipart/form-data": SceneTemplateWriteSerializer,
        "application/json": SceneTemplateWriteSerializer,
    },
    responses={202: SceneJobSerializer, **ADMIN_ERRORS},
)

SCENE_TEMPLATE_PATCH = extend_schema(
    operation_id="admin_scene_templates_partial_update",
    description=(
        "Edit a template (§2.20.9): `name`, `enabled` (false hides it from the dialogs; the ops console "
        "shows it again), and for an AI template `text`, `reference` (replace) or `remove_reference`. 200 "
        "the template when nothing needs the AI (names, visibility, a removed reference, a cleared text). "
        "A changed text or a new reference is checked by the AI on the worker: 202 the job (kind "
        "`template`, failures as for create; the whole edit is saved when it succeeds); 409 "
        "`scene_template_saving` while an earlier one is checked. A changed text clears a reference made "
        "from the old one (an uploaded one stays). A studio template's colours stay in the ops console "
        "(400 `text`)."
    ),
    request={
        "multipart/form-data": SceneTemplateWriteSerializer,
        "application/json": SceneTemplateWriteSerializer,
    },
    responses={200: SceneTemplateSerializer, 202: SceneJobSerializer, **ADMIN_ERRORS},
)

SCENE_TEMPLATE_REFERENCE_POST = extend_schema(
    operation_id="admin_scene_templates_reference_create",
    description=(
        "«Rasmini yaratish»: an AI template's reference made from its text (a picture of the scene alone, "
        "4:5), one image call within the scene budget. 202 the job (kind `preview`; a second call returns "
        "the pending one); poll `scenes/jobs/{id}/`, then re-read the templates. 409 "
        "`scene_template_needs_text` without text, `scene_budget_exceeded`, `scene_ai_disabled`."
    ),
    request=None,
    responses={202: SceneJobSerializer, **ADMIN_ERRORS},
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
    "BANNER_IMAGE_START_POST",
    "BANNER_IMAGE_USE_POST",
    "SCENES_STATUS_GET",
    "SCENE_JOB_ACCEPT_POST",
    "SCENE_JOB_DETAIL_GET",
    "SCENE_JOB_DISCARD_POST",
    "SCENE_JOB_START_POST",
    "SCENE_TEMPLATES_GET",
    "SCENE_JOB_TEMPLATE_POST",
    "SCENE_TEMPLATE_CREATE",
    "SCENE_TEMPLATE_PATCH",
    "SCENE_TEMPLATE_REFERENCE_POST",
]
