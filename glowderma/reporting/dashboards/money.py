"""«Pul solishtirish» — money reconciliation: the latest nightly K1–K6 report next to the same checks run now.

The report is read from its audit row (never run here); the live column runs the report's own check
selectors (:func:`glowderma.reporting.tasks.checks`: one indexed statement each, count + ids, under a short
statement timeout) so every number links to exactly the rows behind it. Loyalty: the policy in force and the
last ledger reconciliation (its audit row). Statements: 13 checks (in one savepoint under a statement
timeout) + 1 audit + 2 policy.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import urlencode

from django.urls import reverse

from glowderma.core.consistency import Finding
from glowderma.loyalty import selectors as loyalty_selectors
from glowderma.reporting import selectors as reporting_selectors
from glowderma.reporting import tasks
from glowderma.reporting.dashboards import (
    CRITICAL,
    LINK_IDS,
    NEUTRAL,
    OK,
    WARN,
    ago,
    capped,
    change,
    changelist,
    generated,
    ids_link,
    num,
    som,
    stamp,
    state,
    worst,
)
from glowderma.reporting.dashboards import report as report_module

# A live check statement is cut off after this long (the nightly run allows 60 s; a page must stay quick).
LIVE_TIMEOUT_MS = 5_000
# How many sample ids a row shows inline (the row's link lists up to LINK_IDS).
SAMPLE_IDS = 5
LOYALTY_CHECK = report_module.LOYALTY_CHECK


CHECKS = report_module.CHECKS
SKIPPED_LABELS = {tasks.BILLZ_NOT_LIVE: "Billz sinov rejimida — tekshirilmadi"}
# What to do about a row that found something (the rows that need a person, in plain words).
GUIDANCE: dict[str, str] = {
    "k2_payment_pending_stuck": (
        "Har bir to‘lovni oching va provayder kabinetida (Click / Payme) tranzaksiya ID bo‘yicha holatini "
        "tekshiring. Pul yechilgan bo‘lsa — buyurtma raqamini texnik guruhga yuboring; yechilmagan bo‘lsa, "
        "mijoz bilan bog‘lanib, qayta to‘lashni so‘rang."
    ),
    "k2_click_paid_without_paydoc": (
        "Click kabinetida to‘lovning fiskal cheki (paydoc) chiqqanini tekshiring; chiqmagan bo‘lsa, texnik "
        "guruhga to‘lov raqamini yuboring — chek bo‘lmasa, qaytarishni avtomatik yuborib bo‘lmaydi."
    ),
    "k3_refund_open": (
        "Qaytarish uzoq ochiq turibdi: uni oching, holati va «oxirgi xato»ni ko‘ring. Holati noma’lum yoki "
        "qo‘lda kiritish kerak bo‘lsa, provayder kabinetida pul qaytganini tekshiring va boshqaruv panelida "
        "(To‘lovlar → Qaytarishlar) uni hal qilingan deb belgilang."
    ),
    "k3_refund_failed": (
        "Qaytarish provayderda rad etilgan: «xato kodi»ni ko‘ring, so‘ng boshqaruv panelida (To‘lovlar → "
        "Qaytarishlar) qayta yuboring yoki pulni qo‘lda qaytarib, «qo‘lda qaytarildi» deb belgilang."
    ),
}
# The checks of the ledger reconciliation (glowderma.loyalty.tasks.CHECKS) in Uzbek; an unknown name is shown
# as it is.
RECONCILE_CHECK_LABELS = {
    "ledger_sum": "Balans = daftar yozuvlari yig‘indisi",
    "last_balance_after": "Balans = oxirgi yozuvdan keyingi balans",
    "chain": "Yozuvlar zanjiri (har bir yozuvdan keyingi balans)",
    "purchase_links": "Xarid ↔ hisoblangan cashback yozuvi",
    "captured_holds": "Yechilgan band ↔ sarflash yozuvi",
}


def _live(now: datetime) -> dict[str, Finding | None]:
    """Every K1–K6 check now, in one savepoint whose statements are cut off after :data:`LIVE_TIMEOUT_MS`
    (:func:`tasks.guarded`); when any of them fails or times out (logged), every live cell reads «Xato» and
    the page still renders."""
    planned = tasks.checks(now, limit=LINK_IDS)
    results = tasks.guarded(
        "ops_console_live", lambda: {name: run() for name, run in planned}, timeout_ms=LIVE_TIMEOUT_MS
    )
    if results is None:
        return dict.fromkeys((name for name, _run in planned), None)
    return dict(results)


def _samples(model: str, ids: list[int]) -> list[dict[str, str]]:
    if not model:
        return []
    return [{"id": f"#{pk}", "href": change(model, pk)} for pk in ids[:SAMPLE_IDS]]


def _rows(
    summary: report_module.ReportSummary | None, live: dict[str, Finding | None]
) -> list[dict[str, Any]]:
    rows = []
    for name, info in CHECKS.items():
        reported = summary.checks.get(name) if summary else None
        finding = live.get(name)
        if name == LOYALTY_CHECK:
            live_cell: dict[str, Any] = {"text": "Faqat tunda", "href": None, "count": None}
        elif finding is None:
            live_cell = {"text": "Xato", "href": None, "count": None}
        elif finding.skipped:
            live_cell = {"text": "—", "href": None, "count": 0, "note": SKIPPED_LABELS.get(finding.skipped)}
        else:
            live_cell = {
                "text": num(finding.count),
                "href": ids_link(info.model, finding.ids) if finding.count else None,
                "count": finding.count,
                "capped": capped(finding.count, finding.ids),
            }
        if reported is None:
            report_cell: dict[str, Any] = {"text": "—", "href": None, "count": None, "samples": []}
        elif reported.error:
            report_cell = {"text": "Xato", "href": None, "count": None, "samples": [], "note": reported.error}
        else:
            report_cell = {
                "text": num(reported.count),
                "href": ids_link(info.model, reported.ids) if reported.count and info.model else None,
                "count": reported.count,
                "samples": _samples(info.model, reported.ids),
                "capped": capped(reported.count, reported.ids) if info.model else None,
                "more": max(reported.count - min(len(reported.ids), SAMPLE_IDS), 0),
                "note": SKIPPED_LABELS.get(reported.skipped) if reported.skipped else None,
            }
        current = live_cell["count"] if live_cell["count"] is not None else report_cell["count"]
        if live_cell["text"] == "Xato":
            row_state = CRITICAL
        elif current is None:
            row_state = CRITICAL if report_cell["text"] == "Xato" else NEUTRAL
        else:
            row_state = info.severity if current else OK
        rows.append(
            {
                "name": name,
                "code": info.code,
                "label": info.label,
                "guidance": GUIDANCE.get(name, "") if row_state in (WARN, CRITICAL) else "",
                "report": report_cell,
                "live": live_cell,
                "state": state(row_state),
            }
        )
    return rows


def _report_tile(now: datetime, summary: report_module.ReportSummary | None) -> dict[str, Any]:
    if summary is None:
        return {
            "title": "Oxirgi tungi hisobot",
            "icon": "fact_check",
            "value": "Yo‘q",
            "unit": "",
            "hint": "Hisobot hali hech qachon ishlamagan (har kuni 03:40)",
            "href": changelist("core_auditentry", action__exact=report_module.REPORT_ACTION),
            "state": state(report_module.age_state(None, now)),
        }
    stale = summary.stale(now)
    tile_state = worst([report_module.findings_state(summary), report_module.age_state(summary.ran_at, now)])
    return {
        "title": "Oxirgi tungi hisobot",
        "icon": "fact_check",
        "value": stamp(summary.ran_at),
        "unit": "",
        "hint": (f"Eskirgan: {ago(summary.ran_at, now)} · " if stale else f"{ago(summary.ran_at, now)} · ")
        + f"{num(summary.findings)} topilma",
        "href": change("core_auditentry", summary.entry_id),
        "state": state(tile_state),
    }


def _policy(now: datetime) -> dict[str, Any]:
    policy = loyalty_selectors.active_policy(now)
    if policy is None:
        return {
            "tile": {
                "title": "Cashback siyosati",
                "icon": "policy",
                "value": "Yo‘q",
                "unit": "",
                "hint": "Amaldagi siyosat yo‘q — xaridlar cashback olmaydi",
                "href": changelist("loyalty_cashbackpolicy"),
                "state": state(CRITICAL),
            },
            "tiers": [],
        }
    tiers = sorted(policy.tiers.all(), key=lambda tier: tier.from_amount)
    return {
        "tile": {
            "title": "Cashback siyosati",
            "icon": "policy",
            "value": f"v{policy.version}",
            "unit": "amalda",
            "hint": f"{stamp(policy.effective_from)} dan · {len(tiers)} daraja",
            "href": change("loyalty_cashbackpolicy", policy.pk),
            "state": state(OK),
        },
        "tiers": [
            {
                "range": f"{som(tier.from_amount)} – {som(tier.to_amount) if tier.to_amount else '∞'}",
                "percent": f"{tier.percent.normalize():f}%",
            }
            for tier in tiers
        ],
    }


def _reconcile(now: datetime, entry: Any) -> dict[str, Any]:
    mismatch_link = changelist("core_auditentry", action__exact=report_module.RECONCILE_MISMATCH_ACTION)
    if entry is None:
        return {
            "title": "Daftar solishtiruvi",
            "icon": "balance",
            "value": "—",
            "unit": "",
            "hint": "Hali ishlamagan (tungi hisobot bilan ishlaydi)",
            "href": changelist("core_auditentry", action__exact=report_module.RECONCILE_ACTION),
            "state": state(report_module.age_state(None, now)),
            "checks": [],
        }
    after = entry.after if isinstance(entry.after, dict) else {}
    mismatches = int(after.get("mismatches") or 0)
    negative = int(after.get("negative_balances") or 0)
    accounts = int(after.get("accounts") or 0)
    checks = after.get("checks")
    if not isinstance(checks, dict):
        checks = {}
    tile_state = worst(
        [
            CRITICAL if mismatches else OK,
            WARN if negative else OK,
            report_module.age_state(entry.created_at, now),
        ]
    )
    return {
        "title": "Daftar solishtiruvi",
        "icon": "balance",
        "value": num(mismatches),
        "unit": "nomuvofiqlik",
        "hint": f"{num(accounts)} hisob · {num(negative)} manfiy balans · {ago(entry.created_at, now)}",
        "href": mismatch_link if mismatches else change("core_auditentry", entry.pk),
        "state": state(tile_state),
        "checks": [
            {
                "name": str(name),
                "label": RECONCILE_CHECK_LABELS.get(str(name), str(name)),
                "count": num(int(count or 0)),
                "raw": int(count or 0),
            }
            for name, count in checks.items()
        ],
    }


def build(now: datetime) -> dict[str, Any]:
    audits = reporting_selectors.latest_audits([report_module.REPORT_ACTION, report_module.RECONCILE_ACTION])
    summary = report_module.summary(audits.get(report_module.REPORT_ACTION))
    live = _live(now)
    rows = _rows(summary, live)
    live_total = sum(
        finding.count for finding in live.values() if finding is not None and not finding.skipped
    )
    live_state = worst(row["state"]["key"] for row in rows if row["name"] != LOYALTY_CHECK)
    policy = _policy(now)
    reconcile = _reconcile(now, audits.get(report_module.RECONCILE_ACTION))
    stuck = live.get("k2_payment_pending_stuck")
    tiles = [
        _report_tile(now, summary),
        {
            "title": "Hozirgi topilmalar",
            "icon": "radar",
            "value": num(live_total),
            "unit": "qator",
            "hint": "K1–K6 hozir qayta tekshirildi",
            "href": "#checks",
            "state": state(live_state if live_total else OK),
        },
        {
            "title": "Osilib qolgan to‘lovlar",
            "icon": "hourglass_top",
            "value": num(stuck.count) if stuck else "—",
            "unit": "ta to‘lov",
            "hint": "Band muddati o‘tgan, tranzaksiyasi ochiq",
            "href": ids_link("payments_payment", stuck.ids) if stuck and stuck.count else None,
            "note": capped(stuck.count, stuck.ids) if stuck else None,
            "state": state(CRITICAL if stuck and stuck.count else OK if stuck else CRITICAL),
        },
        policy["tile"],
        {key: value for key, value in reconcile.items() if key != "checks"},
    ]
    return {
        "generated": generated(now),
        "tiles": tiles,
        "rows": rows,
        "report": None
        if summary is None
        else {
            "ran_at": stamp(summary.ran_at),
            "ago": ago(summary.ran_at, now),
            "stale": summary.stale(now),
            "findings": summary.findings,
            "href": change("core_auditentry", summary.entry_id),
        },
        "live_total": live_total,
        "policy_tiers": policy["tiers"],
        "reconcile_checks": reconcile["checks"],
        "ops_url": reverse("admin:index"),
        # the «Hisobotni hozir ishga tushirish» action of the audit log (its confirm step; managers only)
        "run_report_url": f"{reverse('admin:core_auditentry_run_consistency_report')}?"
        + urlencode({"next": reverse("admin:reporting_money")}),
    }


__all__ = ["CHECKS", "build"]
