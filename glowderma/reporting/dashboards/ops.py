"""«Ops holati» — the console index (``UNFOLD["DASHBOARD_CALLBACK"]``): is the machinery healthy right now?

Six traffic-light tiles (outbox, webhooks, Billz sync, BTS polling, the nightly report, background jobs), the
outbox backlog by topic (K6), webhook failures by provider, provider readiness (live vs fake) and the
periodic tasks' last traces. Statements: outbox backlog 1, webhooks 1, provider rows 1, latest jobs 1,
latest audit rows 1 — plus the readiness probe's own sync read when a syncing provider is live.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from django.conf import settings
from django.urls import reverse
from django.utils.translation import gettext, ngettext
from django.utils.translation import gettext_lazy as _

from glowderma.core import consistency, enums, integration_modes, readiness
from glowderma.integrations import selectors as integration_selectors
from glowderma.integrations.models import IntegrationState, Job, WebhookInbox
from glowderma.reporting import selectors as reporting_selectors
from glowderma.reporting.dashboards import (
    CRITICAL,
    NEUTRAL,
    OK,
    WARN,
    ago,
    by_age,
    by_count,
    change,
    changelist,
    duration,
    generated,
    iso,
    num,
    stamp,
    state,
    worst,
)
from glowderma.reporting.dashboards import report as report_module

if TYPE_CHECKING:
    from django_stubs_ext import StrOrPromise  # a development dependency: never imported at runtime

# --- thresholds ---------------------------------------------------------------------------------------------
# Outbox: any dead message is an action item; open messages overdue (K6 rule) warn, many of them mean the
# dispatcher is down; a long queue warns even when nothing is overdue yet. The dispatcher runs every 10 s: a
# due message waiting longer than OUTBOX_LAG_WARN warns. The topic rows, the tile and the dispatcher's beat
# row apply the same rule (:func:`_backlog_state`), so one late message reads the same everywhere.
OUTBOX_OVERDUE_CRITICAL = 20
OUTBOX_OPEN_WARN = 500
OUTBOX_LAG_WARN = timedelta(minutes=5)
# Provider callbacks rejected or failed in the last 24 h.
WEBHOOK_WINDOW = timedelta(hours=24)
WEBHOOK_FAILED_WARN = 1
WEBHOOK_FAILED_CRITICAL = 10
# A provider's consecutive failures (IntegrationState).
FAILURES_WARN = 1
FAILURES_CRITICAL = 3
# Billz delta sync (live only): warn after three missed runs, critical after a day.
BILLZ_SYNC_MISSED_RUNS = 3
BILLZ_SYNC_CRITICAL = timedelta(hours=24)
# A running or queued job whose heartbeat (or creation) is older than this is stuck (the reaper's horizon).
JOB_STUCK_AFTER = timedelta(minutes=15)
# The nightly report: stale after 36 h, critical after three nights (report_module.age_state).
REPORT_CRITICAL_AFTER = report_module.REPORT_CRITICAL_AFTER

# Brand and protocol names: the same in every language of the console.
PROVIDER_LABELS: dict[str, str] = {
    "click": "Click",
    "payme": "Payme",
    "bts": "BTS",
    "eskiz": "Eskiz SMS",
    "email": "SMTP email",
    "fcm": "FCM push",
    "oauth": "OAuth (Google / Facebook)",
    "billz": "Billz",
}
PROVIDER_ORDER = ("click", "payme", "bts", "eskiz", "email", "fcm", "oauth", "billz")
WEBHOOK_PROVIDERS: tuple[str, ...] = tuple(WebhookInbox.Provider.values)
JOB_LABELS: dict[str, StrOrPromise] = {enums.JobKind.BILLZ_IMPORT.value: _("Billz import")}
JOB_STATE_LABELS: dict[str, StrOrPromise] = {
    "queued": _("Navbatda"),
    "running": _("Ishlamoqda"),
    "succeeded": _("Bajarildi"),
    "failed": _("Xato"),
}


def _state_link(provider: str) -> str:
    return changelist("integrations_integrationstate", provider__exact=provider)


def _lag(now: datetime, rows: list[consistency.TopicBacklog]) -> timedelta | None:
    """How long the oldest due open message of ``rows`` has been waiting (``None``: nothing is due)."""
    due = [row.oldest_due for row in rows if row.oldest_due is not None and row.oldest_due <= now]
    return now - min(due) if due else None


def _backlog_state(overdue: int, lag: timedelta | None) -> str:
    """Late outbox messages: ``warn`` from one overdue message (K6) or a due message waiting longer than
    :data:`OUTBOX_LAG_WARN`, ``critical`` from :data:`OUTBOX_OVERDUE_CRITICAL` overdue ones."""
    return worst(
        [
            by_count(overdue, critical=OUTBOX_OVERDUE_CRITICAL),
            WARN if lag is not None and lag > OUTBOX_LAG_WARN else OK,
        ]
    )


def _outbox(now: datetime, overdue: timedelta, topics: list[consistency.TopicBacklog]) -> dict[str, Any]:
    open_statuses = ",".join(consistency.OPEN_OUTBOX_STATUSES)
    overdue_before = iso(now - overdue)

    def links(topic: str | None) -> dict[str, str]:
        extra = {"topic__exact": topic} if topic else {}
        return {
            "open": changelist("core_outboxmessage", status__in=open_statuses, **extra),
            "dead": changelist("core_outboxmessage", status__exact="dead", **extra),
            "overdue": changelist(
                "core_outboxmessage",
                status__in=open_statuses,
                available_at__lt=overdue_before,
                deferred_since__isnull="True",
                **extra,
            ),
            "deferred": changelist(
                "core_outboxmessage", status__in=open_statuses, deferred_since__isnull="False", **extra
            ),
        }

    rows = []
    for row in topics:
        row_state = worst([CRITICAL if row.dead else OK, _backlog_state(row.overdue, _lag(now, [row]))])
        rows.append(
            {
                "topic": row.topic,
                "open": num(row.open),
                "dead": num(row.dead),
                "overdue": num(row.overdue),
                "deferred": num(row.deferred),
                "oldest": ago(row.oldest_due, now) if row.oldest_due and row.oldest_due < now else "—",
                "raw": {"open": row.open, "dead": row.dead, "overdue": row.overdue, "deferred": row.deferred},
                "links": links(row.topic),
                "state": state(row_state),
            }
        )
    dead = sum(row.dead for row in topics)
    late = sum(row.overdue for row in topics)
    queued = sum(row.open for row in topics)
    tile_state = worst(
        [
            CRITICAL if dead else OK,
            _backlog_state(late, _lag(now, topics)),
            by_count(queued, warn=OUTBOX_OPEN_WARN),
        ]
    )
    totals = links(None)
    href = totals["dead"] if dead else totals["overdue"] if late else changelist("core_outboxmessage")
    return {
        "rows": rows,
        "tile": {
            "title": "Outbox",
            "icon": "outbox",
            "value": num(dead),
            "unit": ngettext("o‘lik xabar", "o‘lik xabar", dead),
            "hint": " · ".join(
                (
                    ngettext("%(count)s kechikkan", "%(count)s kechikkan", late) % {"count": num(late)},
                    ngettext("%(count)s navbatda", "%(count)s navbatda", queued) % {"count": num(queued)},
                )
            ),
            "href": href,
            "state": state(tile_state),
        },
        "raw": {"dead": dead, "overdue": late, "open": queued},
    }


def _webhooks(now: datetime) -> dict[str, Any]:
    since = now - WEBHOOK_WINDOW
    outcomes = integration_selectors.webhook_outcomes_since(since)
    failed_filter = ",".join(integration_selectors.FAILED_WEBHOOK_OUTCOMES)
    rows = []
    for provider in WEBHOOK_PROVIDERS:
        counts = outcomes.get(provider, {"total": 0, "failed": 0})
        rows.append(
            {
                "provider": PROVIDER_LABELS.get(provider, provider),
                "total": num(counts["total"]),
                "failed": num(counts["failed"]),
                "raw": counts,
                "href": changelist(
                    "integrations_webhookinbox", provider__exact=provider, received_at__gte=iso(since)
                ),
                "failed_href": changelist(
                    "integrations_webhookinbox",
                    provider__exact=provider,
                    received_at__gte=iso(since),
                    result__outcome__in=failed_filter,
                ),
                "state": state(
                    by_count(counts["failed"], warn=WEBHOOK_FAILED_WARN, critical=WEBHOOK_FAILED_CRITICAL)
                ),
            }
        )
    failed = sum(counts["failed"] for counts in outcomes.values())
    total = sum(counts["total"] for counts in outcomes.values())
    return {
        "rows": rows,
        "tile": {
            "title": gettext("Webhooklar · 24 soat"),
            "icon": "webhook",
            "value": num(failed),
            "unit": ngettext("xato", "xato", failed),
            "hint": ngettext("%(count)s ta qabul qilindi", "%(count)s ta qabul qilindi", total)
            % {"count": num(total)},
            "href": changelist(
                "integrations_webhookinbox", received_at__gte=iso(since), result__outcome__in=failed_filter
            ),
            "state": state(by_count(failed, warn=WEBHOOK_FAILED_WARN, critical=WEBHOOK_FAILED_CRITICAL)),
        },
        "raw": {"failed": failed, "total": total},
    }


def _failures_state(row: IntegrationState | None) -> str:
    failures = row.consecutive_failures if row is not None else 0
    return by_count(failures, warn=FAILURES_WARN, critical=FAILURES_CRITICAL)


def _consecutive_failures(failures: int) -> str:
    return ngettext("%(count)s ketma-ket xato", "%(count)s ketma-ket xato", failures) % {
        "count": num(failures)
    }


def _billz_tile(now: datetime, row: IntegrationState | None, live: bool) -> dict[str, Any]:
    failures = row.consecutive_failures if row is not None else 0
    last = row.last_sync_at if row is not None else None
    interval = timedelta(seconds=int(settings.BILLZ_DELTA_SYNC_SECONDS))
    if live:
        tile_state = worst(
            [
                _failures_state(row),
                by_age(
                    None if last is None else now - last,
                    warn=interval * BILLZ_SYNC_MISSED_RUNS,
                    critical=BILLZ_SYNC_CRITICAL,
                ),
            ]
        )
        shown = state(tile_state)
    else:
        shown = state(NEUTRAL, gettext("Sinov rejimi"))
    hint = _consecutive_failures(failures) if failures else gettext("Ketma-ket xato yo‘q")
    return {
        "title": gettext("Billz sinxroni"),
        "icon": "sync",
        "value": ago(last, now),
        "unit": "",
        "hint": hint if live else " · ".join((gettext("Sinov adapteri"), hint.lower())),
        "href": _state_link("billz"),
        "state": shown,
        # fake mode: the numbers are the fake adapter's, never an alarm
        "muted": not live,
    }


def _bts_tile(now: datetime, row: IntegrationState | None, live: bool) -> dict[str, Any]:
    failures = row.consecutive_failures if row is not None else 0
    last = row.last_sync_at if row is not None else None
    # Polling writes a sync only when it tracked a parcel: an old stamp with no failure is a quiet day.
    if live:
        return {
            "title": gettext("BTS kuzatuvi"),
            "icon": "local_shipping",
            "value": ngettext("%(count)s xato", "%(count)s xato", failures) % {"count": num(failures)}
            if failures
            else ago(last, now),
            "unit": "",
            "hint": gettext("Oxirgi kuzatuv: %(when)s") % {"when": stamp(last)},
            "href": _state_link("bts"),
            "state": state(_failures_state(row)),
        }
    # Fake mode: the fake adapter's failures are no alarm, so they never become the big number.
    return {
        "title": gettext("BTS kuzatuvi"),
        "icon": "local_shipping",
        "value": ago(last, now),
        "unit": "",
        "hint": gettext("Sinov adapteri") + (f" · {_consecutive_failures(failures)}" if failures else ""),
        "href": _state_link("bts"),
        "state": state(NEUTRAL, gettext("Sinov rejimi")),
        "muted": True,
    }


def _job_row(now: datetime, kind: str, job: Job | None) -> dict[str, Any]:
    if job is None:
        return {
            "kind": str(JOB_LABELS.get(kind, kind)),
            "state_text": gettext("Hali ishlamagan"),
            "when": "—",
            "duration": "—",
            "href": changelist("integrations_job", kind__exact=kind),
            "state": state(NEUTRAL),
            "ran": False,
        }
    heartbeat = job.heartbeat_at or job.started_at or job.created_at
    if job.state == enums.JobState.FAILED:
        job_state = CRITICAL
    elif job.state in (enums.JobState.RUNNING, enums.JobState.QUEUED):
        job_state = WARN if now - heartbeat > JOB_STUCK_AFTER else OK
    else:
        job_state = OK
    end = job.finished_at or (now if job.state == enums.JobState.RUNNING else None)
    took = (end - job.started_at).total_seconds() if end and job.started_at else None
    return {
        "kind": str(JOB_LABELS.get(kind, kind)),
        "state_text": str(JOB_STATE_LABELS.get(job.state, job.state)),
        "when": ago(job.created_at, now),
        "duration": duration(took),
        "href": change("integrations_job", job.pk),
        "state": state(job_state),
        "ran": True,
    }


def _readiness(
    now: datetime, rows: dict[str, IntegrationState], modes: dict[str, str], configured: dict[str, bool]
) -> list[dict[str, Any]]:
    production = not integration_modes.dev_features_allowed()
    result = []
    for provider in PROVIDER_ORDER:
        if provider not in modes:
            continue
        live = modes[provider] == enums.IntegrationMode.LIVE
        row = rows.get(provider)
        if live and not configured.get(provider, False):
            shown = state(CRITICAL, gettext("Sozlanmagan"))
        elif live:
            shown = state(_failures_state(row))
        elif production:
            shown = state(WARN, gettext("Sinov (prod)"))
        else:
            shown = state(NEUTRAL, gettext("Sinov rejimi"))
        result.append(
            {
                "topic": provider,
                "provider": PROVIDER_LABELS[provider],
                "mode": "Live" if live else gettext("Sinov"),
                "live": live,
                "configured": (gettext("Ha") if configured.get(provider) else gettext("Yo‘q"))
                if live
                else "—",
                "last_success": ago(row.last_success_at, now) if row is not None else "—",
                "failures": num(row.consecutive_failures) if row is not None else "—",
                "last_error": stamp(row.last_error_at) if row is not None else "—",
                "href": _state_link(provider) if row is not None else None,
                "state": shown,
            }
        )
    return result


def _beat(
    now: datetime,
    overdue: timedelta,
    topics: list[consistency.TopicBacklog],
    rows: dict[str, IntegrationState],
    modes: dict[str, str],
    jobs: dict[str, Job],
    audits: dict[str, Any],
) -> list[dict[str, Any]]:
    """The periodic tasks (config/celery.py ``beat_schedule``) that leave a trace, judged by it."""
    lag = _lag(now, topics)
    oldest = now - lag if lag is not None else None
    dispatcher = _backlog_state(sum(row.overdue for row in topics), lag)
    billz = rows.get("billz")
    bts = rows.get("bts")
    billz_live = modes.get("billz") == enums.IntegrationMode.LIVE
    bts_live = modes.get("bts") == enums.IntegrationMode.LIVE
    interval = timedelta(seconds=int(settings.BILLZ_DELTA_SYNC_SECONDS))
    billz_last = billz.last_sync_at if billz else None
    job = jobs.get(enums.JobKind.BILLZ_IMPORT)
    report_entry = audits.get(report_module.REPORT_ACTION)
    reconcile_entry = audits.get(report_module.RECONCILE_ACTION)

    def audit_age_state(entry: Any) -> str:
        return report_module.age_state(None if entry is None else entry.created_at, now)

    never_ran = gettext("Hech qachon ishlamagan")
    minutes = interval.seconds // 60
    return [
        {
            "task": gettext("Outbox dispetcheri"),
            "schedule": gettext("har 10 soniyada"),
            "trace": gettext("Navbat bo‘sh")
            if lag is None
            else gettext("Eng eski xabar %(ago)s kutmoqda") % {"ago": ago(oldest, now)},
            "href": changelist("core_outboxmessage", status__in=",".join(consistency.OPEN_OUTBOX_STATUSES)),
            "state": state(dispatcher),
        },
        {
            "task": gettext("Billz delta sinxroni"),
            "schedule": ngettext("har %(count)s daqiqada", "har %(count)s daqiqada", minutes)
            % {"count": minutes},
            "trace": gettext("Oxirgi sinxron %(ago)s") % {"ago": ago(billz_last, now)},
            "href": _state_link("billz"),
            "state": state(
                worst(
                    [
                        _failures_state(billz),
                        by_age(
                            None if billz_last is None else now - billz_last,
                            warn=interval * BILLZ_SYNC_MISSED_RUNS,
                            critical=BILLZ_SYNC_CRITICAL,
                        ),
                    ]
                )
            )
            if billz_live
            else state(NEUTRAL, gettext("Sinov rejimi")),
        },
        {
            "task": gettext("Billz import (fon vazifasi)"),
            "schedule": gettext("har kuni 04:23 + qo‘lda"),
            "trace": gettext("Hali ishlamagan")
            if job is None
            else f"{JOB_STATE_LABELS.get(job.state, job.state)}, {ago(job.created_at, now)}",
            "href": change("integrations_job", job.pk) if job else changelist("integrations_job"),
            "state": _job_row(now, enums.JobKind.BILLZ_IMPORT, job)["state"],
        },
        {
            "task": gettext("BTS kuzatuvi"),
            "schedule": gettext("har 10 daqiqada"),
            "trace": gettext("Oxirgi kuzatilgan posilka %(ago)s")
            % {"ago": ago(bts.last_sync_at if bts else None, now)},
            "href": _state_link("bts"),
            "state": state(_failures_state(bts)) if bts_live else state(NEUTRAL, gettext("Sinov rejimi")),
        },
        {
            "task": gettext("Konsistentlik hisoboti"),
            "schedule": gettext("har kuni 03:40"),
            "trace": never_ran
            if report_entry is None
            else f"{stamp(report_entry.created_at)} ({ago(report_entry.created_at, now)})",
            "href": change("core_auditentry", report_entry.pk)
            if report_entry
            else changelist("core_auditentry", action__exact=report_module.REPORT_ACTION),
            "state": state(audit_age_state(report_entry)),
        },
        {
            "task": gettext("Cashback daftari solishtiruvi"),
            "schedule": gettext("hisobot bilan"),
            "trace": never_ran
            if reconcile_entry is None
            else f"{stamp(reconcile_entry.created_at)} ({ago(reconcile_entry.created_at, now)})",
            "href": change("core_auditentry", reconcile_entry.pk)
            if reconcile_entry
            else changelist("core_auditentry", action__exact=report_module.RECONCILE_ACTION),
            "state": state(audit_age_state(reconcile_entry)),
        },
    ]


def _report_tile(now: datetime, summary: report_module.ReportSummary | None) -> dict[str, Any]:
    money_url = reverse("admin:reporting_money")
    if summary is None:
        return {
            "title": gettext("Konsistentlik hisoboti"),
            "icon": "fact_check",
            "value": "—",
            "unit": "",
            "hint": gettext("Hisobot hali hech qachon ishlamagan"),
            "href": money_url,
            "state": state(report_module.age_state(None, now)),
        }
    tile_state = worst([report_module.findings_state(summary), report_module.age_state(summary.ran_at, now)])
    return {
        "title": gettext("Konsistentlik hisoboti"),
        "icon": "fact_check",
        "value": num(summary.findings),
        "unit": ngettext("topilma", "topilma", summary.findings),
        "hint": f"{stamp(summary.ran_at)} · {ago(summary.ran_at, now)}",
        "href": money_url,
        "state": state(tile_state),
    }


# What each signal is about: a tile, a readiness row and a beat row about the same thing (Billz: its tile,
# its provider row, its delta-sync task) count as one problem in the headline.
TILE_TOPICS = ("outbox", "webhooks", "billz", "bts", "report", "jobs")
BEAT_TOPICS = ("outbox", "billz", "jobs", "bts", "report", "reconcile")
TOPIC_LABELS: dict[str, StrOrPromise] = {
    **PROVIDER_LABELS,
    "outbox": "Outbox",
    "webhooks": _("Webhooklar"),
    "billz": "Billz",
    "bts": "BTS",
    "report": _("Konsistentlik hisoboti"),
    "jobs": _("Fon vazifalari"),
    "reconcile": _("Cashback daftari solishtiruvi"),
}


def _problems(signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every topic whose worst signal warns or is critical, the critical ones first."""
    by_topic: dict[str, str] = {}
    for item in signals:
        topic = str(item["topic"])
        by_topic[topic] = worst([by_topic.get(topic, NEUTRAL), item["state"]["key"]])
    ranked = sorted((key != CRITICAL, topic) for topic, key in by_topic.items() if key in (WARN, CRITICAL))
    return [
        {"topic": topic, "label": str(TOPIC_LABELS.get(topic, topic)), "state": state(by_topic[topic])}
        for _rank, topic in ranked
    ]


def _headline(problems: list[dict[str, Any]]) -> str:
    """``Hammasi joyida`` · ``1 ta jiddiy muammo`` · ``1 ta jiddiy muammo, 2 ta ogohlantirish`` (zero parts
    left out): the same signals as the banner's colour, so the two never disagree."""
    critical = sum(1 for problem in problems if problem["state"]["key"] == CRITICAL)
    warnings = len(problems) - critical
    parts = []
    if critical:
        parts.append(
            ngettext("%(count)s ta jiddiy muammo", "%(count)s ta jiddiy muammo", critical)
            % {"count": critical}
        )
    if warnings:
        parts.append(
            ngettext("%(count)s ta ogohlantirish", "%(count)s ta ogohlantirish", warnings)
            % {"count": warnings}
        )
    return ", ".join(parts) or gettext("Hammasi joyida")


def build(now: datetime) -> dict[str, Any]:
    overdue = timedelta(minutes=int(settings.CONSISTENCY_OUTBOX_OVERDUE_MINUTES))
    topics = consistency.backlog_by_topic(now, overdue)
    rows = integration_selectors.provider_states()
    jobs = integration_selectors.latest_jobs()
    audits = reporting_selectors.latest_audits([report_module.REPORT_ACTION, report_module.RECONCILE_ACTION])
    modes = integration_modes.provider_modes()
    configured = readiness.live_configured()
    summary = report_module.summary(audits.get(report_module.REPORT_ACTION))
    outbox = _outbox(now, overdue, topics)
    webhooks = _webhooks(now)
    job_rows = [_job_row(now, kind, jobs.get(kind)) for kind in enums.JobKind.values]
    job_tile_state = worst(row["state"]["key"] for row in job_rows)
    first_job = job_rows[0]
    tiles = [
        outbox["tile"],
        webhooks["tile"],
        _billz_tile(now, rows.get("billz"), modes.get("billz") == enums.IntegrationMode.LIVE),
        _bts_tile(now, rows.get("bts"), modes.get("bts") == enums.IntegrationMode.LIVE),
        _report_tile(now, summary),
        {
            "title": gettext("Fon vazifalari"),
            "icon": "work_history",
            "value": first_job["state_text"],
            "unit": "",
            "hint": f"{first_job['kind']} · {first_job['when']} · {first_job['duration']}"
            if first_job["ran"]
            else gettext("%(job)s hali ishga tushirilmagan") % {"job": first_job["kind"]},
            "href": first_job["href"],
            "state": state(job_tile_state),
        },
    ]
    for tile, topic in zip(tiles, TILE_TOPICS, strict=True):
        tile["topic"] = topic
    readiness_rows = _readiness(now, rows, modes, configured)
    beat = _beat(now, overdue, topics, rows, modes, jobs, audits)
    for row, topic in zip(beat, BEAT_TOPICS, strict=True):
        row["topic"] = topic
    signals = [*tiles, *readiness_rows, *beat]
    overall = worst(item["state"]["key"] for item in signals)
    problems = _problems(signals)
    return {
        "generated": generated(now),
        "overall": state(overall),
        "headline": _headline(problems),
        "problems": problems,
        "tiles": tiles,
        "outbox": outbox["rows"],
        "outbox_totals": outbox["raw"],
        "webhooks": webhooks["rows"],
        "webhook_totals": webhooks["raw"],
        "jobs": job_rows,
        "readiness": readiness_rows,
        "live_count": sum(1 for row in readiness_rows if row["live"]),
        "beat": beat,
        "report": None
        if summary is None
        else {
            "ran_at": stamp(summary.ran_at),
            "findings": summary.findings,
            "failing": len(summary.failing),
            "href": reverse("admin:reporting_money"),
        },
        "thresholds": {
            "overdue_minutes": int(overdue.total_seconds() // 60),
            "webhook_critical": WEBHOOK_FAILED_CRITICAL,
            "outbox_overdue_critical": OUTBOX_OVERDUE_CRITICAL,
        },
    }


__all__ = ["build"]
