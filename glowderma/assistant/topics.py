"""Outbox topics of `assistant` (its own handlers in ``assistant.tasks``)."""

# A generation job was queued (``services.start_job`` / ``start_test_job``): payload ``{"job_id"}`` only.
JOB_QUEUED = "assistant.job_queued"
# A key test was asked (``credentials.start_rotation`` / ``start_active_test`` / ``rollback``): payload
# ``{"config_id", "version", "actor_id"}`` (ids only, ``version`` None = the active key). Through the outbox,
# so a broker outage after the commit never leaves a pending test without its task.
CREDENTIAL_TEST = "assistant.credential_test"
