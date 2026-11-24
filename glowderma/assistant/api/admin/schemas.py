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


__all__ = [
    "ASSISTANT_JOB_ACCEPT_POST",
    "ASSISTANT_JOB_APPLY_POST",
    "ASSISTANT_JOB_DETAIL_GET",
    "ASSISTANT_JOB_DISCARD_POST",
    "ASSISTANT_JOB_START_POST",
    "ASSISTANT_STATUS_GET",
]
