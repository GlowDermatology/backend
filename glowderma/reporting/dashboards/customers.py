"""«Mijozlar, kirish va sodiqlik» — sign-ups by method (30 days), phone coverage of hybrid accounts, open
sessions by platform, one-time codes and lockouts, cashback issued vs redeemed, the tier distribution and
today's clinic day with the schedule conflicts ahead.

Statements: sign-ups 1, phone coverage 1, sessions 1, OTP 1, staff login failures 1, cashback 1, policy 2 +
tier distribution 1, today's appointments 1, conflicts 4.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any

from glowderma.accounts import selectors as account_selectors
from glowderma.appointments import selectors as appointment_selectors
from glowderma.core import timeutils
from glowderma.core.models import AuditEntry
from glowderma.loyalty import selectors as loyalty_selectors
from glowderma.reporting.dashboards import (
    NEUTRAL,
    OK,
    WARN,
    by_count,
    change,
    changelist,
    clock,
    generated,
    iso,
    num,
    percent,
    som,
    som_short,
    stamp,
    state,
)

SIGNUP_DAYS = 30
CASHBACK_DAYS = 30
CONFLICT_DAYS = 14
# Lockouts (a code's wrong attempts reached the limit) in the last 24 h: one is a typo, many an attack.
LOCKOUTS_WARN = 1
LOCKOUTS_CRITICAL = 20
STAFF_LOGIN_FAILED = "auth.login_failed"
STAFF_FAILURES_WARN = 5
SOCIAL = "social"
SIGNUP_SERIES: tuple[tuple[str, str], ...] = (
    (account_selectors.SIGNUP_PHONE, "Telefon"),
    (account_selectors.SIGNUP_EMAIL, "Email"),
    (SOCIAL, "Ijtimoiy tarmoq"),
)
SOCIAL_LABELS = {"google": "Google", "facebook": "Facebook", "apple": "Apple"}
PLATFORMS: tuple[tuple[str, str], ...] = (
    ("web", "Veb-sayt"),
    ("ios", "iOS"),
    ("android", "Android"),
    (account_selectors.NO_DEVICE, "Qurilmasiz"),
)
OTP_PURPOSES = {
    "signup": "Ro‘yxatdan o‘tish",
    "reset": "Parolni tiklash",
    "new_device": "Yangi qurilma",
    "social_link": "Ijtimoiy bog‘lash",
    "verify_email": "Emailni tasdiqlash",
    "attach_phone": "Telefon qo‘shish",
}
OTP_CHANNELS = {account_selectors.OTP_SMS: "SMS", account_selectors.OTP_EMAIL: "Email"}
APPOINTMENT_STATUS = {
    "requested": "So‘rov",
    "confirmed": "Tasdiqlangan",
    "attended": "Kelgan",
    "rejected": "Rad etilgan",
    "cancelled": "Bekor qilingan",
}


def _signups(now: datetime) -> dict[str, Any]:
    today = timeutils.local_date(now)
    first = today - timedelta(days=SIGNUP_DAYS - 1)
    start, end = timeutils.day_bounds(first)[0], timeutils.day_bounds(today)[1]
    rows = account_selectors.signups_by_day_and_method(start, end)
    calendar = [first + timedelta(days=offset) for offset in range(SIGNUP_DAYS)]

    def series_of(day: date, key: str) -> int:
        if key == SOCIAL:
            return sum(n for (when, method), n in rows.items() if when == day and method in SOCIAL_LABELS)
        return rows.get((day, key), 0)

    series = {key: [series_of(day, key) for day in calendar] for key, _label in SIGNUP_SERIES}
    total = sum(rows.values())
    window = changelist("accounts_customer", registered_at__gte=iso(start), registered_at__lt=iso(end))
    by_method = []
    for key, label in SIGNUP_SERIES:
        count = sum(series[key])
        by_method.append(
            {"label": label, "count": num(count), "raw": count, "percent": percent(count, total)}
        )
    providers = [
        {
            "label": label,
            "count": num(sum(n for (_day, method), n in rows.items() if method == key)),
            "href": changelist(
                "accounts_customersocialidentity", provider__exact=key, created_at__gte=iso(start)
            ),
        }
        for key, label in SOCIAL_LABELS.items()
    ]
    datasets = [
        {
            "label": label,
            "data": series[key],
            "backgroundColor": f"var(--gd-method-{index})",
            "gdColor": f"--gd-method-{index}",
            "borderColor": "var(--gd-surface)",
            "borderWidth": {"top": 2, "right": 0, "bottom": 0, "left": 0},
            "borderSkipped": False,
            "borderRadius": 2,
            "maxBarThickness": 26,
            "stack": "signups",
        }
        for index, (key, label) in enumerate(SIGNUP_SERIES, start=1)
    ]
    options = {
        "animation": False,
        "responsive": True,
        "maintainAspectRatio": False,
        "interaction": {"mode": "index", "intersect": False},
        "plugins": {
            "legend": {
                "display": True,
                "position": "top",
                "align": "end",
                "labels": {
                    "boxWidth": 8,
                    "boxHeight": 8,
                    "usePointStyle": True,
                    "pointStyle": "circle",
                    "color": "#9ca3af",
                },
            },
            "tooltip": {"enabled": True},
        },
        "scales": {
            "x": {
                "stacked": True,
                "grid": {"display": False},
                "border": {"display": False},
                "ticks": {"color": "#9ca3af", "maxTicksLimit": 10, "autoSkip": True, "maxRotation": 0},
            },
            "y": {
                "stacked": True,
                "beginAtZero": True,
                "border": {"display": False},
                "grid": {"display": True},
                "ticks": {"color": "#9ca3af", "maxTicksLimit": 5, "precision": 0},
            },
        },
    }
    return {
        "total": total,
        "href": window,
        "methods": by_method,
        "providers": providers,
        "chart": {
            "data": json.dumps(
                {"labels": [day.strftime("%d.%m") for day in calendar], "datasets": datasets},
                ensure_ascii=False,
            ),
            "options": json.dumps(options),
        },
    }


def _otp(now: datetime) -> dict[str, Any]:
    rows = account_selectors.otp_volume(now)
    week_start = iso(now - timedelta(days=7))
    day_start = iso(now - timedelta(days=1))

    def channel_filter(channel: str) -> dict[str, str]:
        # SMS codes carry the phone (E.164, "+998…"), e-mail codes the address.
        return (
            {"phone__startswith": "+"} if channel == account_selectors.OTP_SMS else {"email__contains": "@"}
        )

    table = [
        {
            "channel": OTP_CHANNELS.get(row.channel, row.channel),
            "purpose": OTP_PURPOSES.get(row.purpose, row.purpose),
            "day": num(row.last_day),
            "week": num(row.last_week),
            "verified": f"{percent(row.verified_week, row.last_week)}%",
            "locked": num(row.locked_week),
            "href": changelist(
                "accounts_smscode",
                purpose__exact=row.purpose,
                created_at__gte=week_start,
                **channel_filter(row.channel),
            ),
            "locked_href": changelist(
                "accounts_smscode",
                purpose__exact=row.purpose,
                exhausted_at__gte=week_start,
                **channel_filter(row.channel),
            )
            if row.locked_week
            else None,
        }
        for row in rows
    ]
    totals = {
        channel: {
            "day": sum(row.last_day for row in rows if row.channel == channel),
            "week": sum(row.last_week for row in rows if row.channel == channel),
        }
        for channel in OTP_CHANNELS
    }
    locked_day = sum(row.locked_day for row in rows)
    return {
        "rows": table,
        "totals": totals,
        "locked_day": locked_day,
        "locked_href": changelist("accounts_smscode", exhausted_at__gte=day_start),
        "sms_href": changelist("accounts_smscode", created_at__gte=day_start, phone__startswith="+"),
    }


def _cashback(now: datetime) -> dict[str, Any]:
    start = now - timedelta(days=CASHBACK_DAYS)
    flow = loyalty_selectors.cashback_flow(start, now)
    policy = loyalty_selectors.active_policy(now)
    tiers = loyalty_selectors.tier_distribution(policy)
    covered_total = sum(bucket.customers for bucket in tiers.tiers) + tiers.uncovered
    biggest = max([flow.issued, flow.redeemed, 1])
    return {
        "issued": som(flow.issued),
        "redeemed": som(flow.redeemed),
        "reversed": som(flow.reversed),
        "adjusted": som(flow.adjusted),
        "issued_pct": percent(flow.issued, biggest),
        "redeemed_pct": percent(flow.redeemed, biggest),
        "net": som(flow.issued - flow.redeemed + flow.reversed + flow.adjusted),
        "raw": {"issued": flow.issued, "redeemed": flow.redeemed},
        "issued_href": changelist("loyalty_ledgerentry", kind__exact="credit", at__gte=iso(start)),
        "redeemed_href": changelist("loyalty_ledgerentry", kind__exact="debit", at__gte=iso(start)),
        "policy_version": tiers.policy_version,
        "policy_href": change("loyalty_cashbackpolicy", policy.pk)
        if policy is not None
        else changelist("loyalty_cashbackpolicy"),
        "tiers": [
            {
                "label": f"{bucket.percent.normalize():f}%",
                "range": f"{som_short(bucket.from_amount)} – "
                + (som_short(bucket.to_amount) if bucket.to_amount is not None else "∞"),
                "customers": num(bucket.customers),
                "raw": bucket.customers,
                "percent": percent(bucket.customers, covered_total),
            }
            for bucket in tiers.tiers
        ],
        "uncovered": num(tiers.uncovered),
        "uncovered_raw": tiers.uncovered,
    }


def _clinic(now: datetime) -> dict[str, Any]:
    today = timeutils.local_date(now)
    start, end = timeutils.day_bounds(today)
    counts = appointment_selectors.day_status_counts(today)
    conflicts = appointment_selectors.upcoming_conflicts(now, days=CONFLICT_DAYS)
    return {
        "total": sum(counts.values()),
        "href": changelist("appointments_appointment", start__gte=iso(start), start__lt=iso(end)),
        "statuses": [
            {
                "label": APPOINTMENT_STATUS.get(status, status),
                "count": num(count),
                "href": changelist(
                    "appointments_appointment",
                    status__exact=status,
                    start__gte=iso(start),
                    start__lt=iso(end),
                ),
            }
            for status, count in counts.items()
            if count
        ],
        "conflicts": [
            {
                "id": appointment.pk,
                "when": f"{stamp(appointment.start)}–{clock(appointment.end)}",
                "professional": str(appointment.professional.name),
                "service": str(appointment.service.name_uz) if appointment.service_id else "—",
                "status": APPOINTMENT_STATUS.get(appointment.status, appointment.status),
                "href": change("appointments_appointment", appointment.pk),
            }
            for appointment in conflicts
        ],
    }


def build(now: datetime) -> dict[str, Any]:
    signups = _signups(now)
    coverage = account_selectors.phone_coverage()
    sessions = account_selectors.open_customer_sessions_by_platform(now)
    otp = _otp(now)
    staff_failures = AuditEntry.objects.filter(
        action=STAFF_LOGIN_FAILED, created_at__gte=now - timedelta(days=1)
    ).count()
    cashback = _cashback(now)
    clinic = _clinic(now)
    session_total = sum(sessions.values())
    open_sessions = changelist(
        "accounts_authsession",
        audience__exact="customer",
        revoked_at__isnull="True",
        absolute_expires_at__gt=iso(now),
    )
    platform_rows = [
        {
            "label": label,
            "count": num(sessions.get(key, 0)),
            "raw": sessions.get(key, 0),
            "percent": percent(sessions.get(key, 0), session_total),
            "href": changelist("accounts_device", platform__exact=key, is_active__exact="1")
            if key != account_selectors.NO_DEVICE
            else open_sessions,
        }
        for key, label in PLATFORMS
        if key != account_selectors.NO_DEVICE or sessions.get(key)
    ]
    conflict_count = len(clinic["conflicts"])
    tiles = [
        {
            "title": f"Yangi mijozlar · {SIGNUP_DAYS} kun",
            "icon": "person_add",
            "value": num(signups["total"]),
            "unit": "ta",
            "hint": " · ".join(f"{row['label']} {row['count']}" for row in signups["methods"]),
            "href": signups["href"],
            "state": state(NEUTRAL),
        },
        {
            "title": "Telefonsiz akkauntlar",
            "icon": "phone_disabled",
            "value": num(coverage.phoneless),
            "unit": "faol",
            "hint": f"{num(coverage.attached_later)} tasi keyin telefon qo‘shgan",
            "href": changelist("accounts_customer", phone__isnull="True", status__exact="active"),
            "state": state(NEUTRAL),
        },
        {
            "title": "Ochiq sessiyalar",
            "icon": "devices",
            "value": num(session_total),
            "unit": "mijoz",
            "hint": " · ".join(f"{row['label']} {row['count']}" for row in platform_rows) or "Sessiya yo‘q",
            "href": open_sessions,
            "state": state(NEUTRAL),
        },
        {
            "title": "OTP · 24 soat",
            "icon": "sms",
            "value": num(otp["totals"][account_selectors.OTP_SMS]["day"]),
            "unit": "SMS",
            "hint": f"{num(otp['totals'][account_selectors.OTP_EMAIL]['day'])} email · "
            f"{num(otp['locked_day'])} bloklangan",
            "href": otp["sms_href"],
            "state": state(by_count(otp["locked_day"], warn=LOCKOUTS_WARN, critical=LOCKOUTS_CRITICAL)),
        },
        {
            "title": "Bugungi qabullar",
            "icon": "event_available",
            "value": num(clinic["total"]),
            "unit": "ta",
            "hint": f"{num(conflict_count)} ta jadval ziddiyati ({CONFLICT_DAYS} kun)",
            "href": clinic["href"],
            "state": state(WARN if conflict_count else OK),
        },
        {
            "title": "Xodim kirish xatolari",
            "icon": "gpp_maybe",
            "value": num(staff_failures),
            "unit": "24 soat",
            "hint": "Noto‘g‘ri parol yoki bloklangan urinish",
            "href": changelist(
                "core_auditentry",
                action__exact=STAFF_LOGIN_FAILED,
                created_at__gte=iso(now - timedelta(days=1)),
            ),
            "state": state(by_count(staff_failures, warn=STAFF_FAILURES_WARN)),
        },
    ]
    return {
        "generated": generated(now),
        "tiles": tiles,
        "signups": signups,
        "coverage": {
            "phoneless": num(coverage.phoneless),
            "attached": num(coverage.attached_later),
            "active": num(coverage.active),
            "phoneless_pct": percent(coverage.phoneless, coverage.active),
            "attached_href": changelist(
                "core_auditentry", action__exact=account_selectors.PHONE_ATTACHED_ACTION
            ),
        },
        "platforms": platform_rows,
        "session_total": num(session_total),
        "otp": otp,
        "cashback": cashback,
        "clinic": clinic,
        "raw": {
            "signups": signups["total"],
            "phoneless": coverage.phoneless,
            "attached": coverage.attached_later,
            "sessions": sessions,
            "staff_failures": staff_failures,
            "conflicts": conflict_count,
            "appointments_today": clinic["total"],
        },
    }


__all__ = ["build"]
