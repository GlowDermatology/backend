"""OTP rate limits (customer contract §2.1.1, §6.4): per-account blocks derived from the stored codes and the
daily / hourly counters per install, IP address and channel kept in the cache.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.db.models import Q, Sum

from glowderma.accounts.models import SmsCode
from glowderma.accounts.services.otp import channels, targets
from glowderma.core import alerts, timeutils
from glowderma.core.api.exceptions import OtpDailyLimit, OtpLocked, OtpNetworkLimit
from glowderma.core.pii import pii_ref

logger = logging.getLogger("glowderma.accounts.services")


def seconds(delta: timedelta) -> int:
    return max(1, int(delta.total_seconds() + 0.999))


def _day_start(now: datetime) -> datetime:
    return timeutils.day_bounds(timeutils.local_date(now))[0]


def to_midnight(now: datetime) -> int:
    return seconds(timeutils.day_bounds(timeutils.local_date(now))[1] - now)


def _day_tag(now: datetime) -> str:
    return timeutils.local_date(now).strftime("%Y%m%d")


def _fid_counter_key(fid: str, now: datetime) -> str:
    return f"gd:otp:fid:{pii_ref(fid)}:{_day_tag(now)}"


def _ip_counter_key(ip: str, now: datetime) -> str:
    return f"gd:otp:ip:{pii_ref(ip)}:{_day_tag(now)}"


def _email_ip_counter_key(ip: str, now: datetime) -> str:
    return f"gd:otp:email_ip:{pii_ref(ip)}:{now.astimezone(timeutils.TASHKENT).strftime('%Y%m%d%H')}"


def _global_counter_key(now: datetime, channel: str = channels.SMS_CHANNEL) -> str:
    prefix = channels.CHANNELS[channel].global_counter_prefix
    return f"{prefix}:{now.astimezone(timeutils.TASHKENT).strftime('%Y%m%d%H')}"


def _seconds_to_next_hour(now: datetime) -> int:
    local = now.astimezone(timeutils.TASHKENT)
    nxt = local.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return seconds(nxt - local)


def _counter(key: str) -> int:
    try:
        return int(cache.get(key) or 0)
    except TypeError, ValueError:
        return 0


def _incr(key: str, timeout: int) -> int:
    """Atomic increment of a Redis day/hour counter (created with ``timeout`` on first use)."""
    cache.add(key, 0, timeout=timeout)
    try:
        return int(cache.incr(key))
    except ValueError:  # expired between add and incr
        cache.add(key, 1, timeout=timeout)
        return 1


def _account_codes(target: targets.OtpTarget) -> Any:
    condition = Q()
    for each in targets.account_targets(target):
        condition |= Q(**each.lookup)
    return SmsCode.objects.filter(condition)


def phone_block(target: targets.OtpTarget, now: datetime) -> Exception | None:
    """The account-derived refusals (DB rows, they survive a cache flush): lockout, daily wrong-code cap —
    over the codes of the target and of its account's other identifier (:func:`account_targets`)."""
    lockout = timedelta(minutes=int(settings.CUSTOMER_OTP_LOCKOUT_MINUTES))
    exhausted = (
        _account_codes(target)
        .filter(exhausted_at__gt=now - lockout)
        .order_by("-exhausted_at")
        .values_list("exhausted_at", flat=True)
        .first()
    )
    if exhausted is not None:
        return OtpLocked(retry_after=seconds(exhausted + lockout - now))
    if wrong_codes_today(target, now) >= int(settings.CUSTOMER_OTP_DAILY_WRONG_LIMIT):
        return OtpLocked(retry_after=to_midnight(now))
    return None


def wrong_codes_today(identifier: str | targets.OtpTarget, now: datetime) -> int:
    target = targets.otp_target(identifier)
    return int(
        _account_codes(target)
        .filter(created_at__gte=_day_start(now))
        .aggregate(total=Sum("attempts"))["total"]
        or 0
    )


def _daily_limit(target: targets.OtpTarget, now: datetime) -> Exception:
    return OtpDailyLimit(retry_after=to_midnight(now), key=channels.CHANNELS[target.channel].daily_limit_key)


def cap_block(target: targets.OtpTarget, *, fid: str, ip: str, now: datetime) -> Exception | None:
    """The day caps: codes per target (DB), per fid and per client IP (Redis counters)."""
    if target.codes().filter(created_at__gte=_day_start(now)).count() >= int(
        settings.CUSTOMER_OTP_DAILY_LIMIT
    ) or _counter(_fid_counter_key(fid, now)) >= int(settings.CUSTOMER_OTP_DEVICE_DAILY_LIMIT):
        return _daily_limit(target, now)
    if ip and _counter(_ip_counter_key(ip, now)) >= int(settings.CUSTOMER_OTP_IP_DAILY_LIMIT):
        return OtpNetworkLimit(retry_after=to_midnight(now))
    # E-mail codes are cheap to request and share one hourly breaker: one IP may not exhaust it.
    if (
        target.is_email
        and ip
        and _counter(_email_ip_counter_key(ip, now)) >= int(settings.EMAIL_OTP_IP_HOURLY_LIMIT)
    ):
        return OtpNetworkLimit(retry_after=_seconds_to_next_hour(now))
    return None


def budget_block(now: datetime, channel: str = channels.SMS_CHANNEL) -> Exception | None:
    """The global hourly breaker of a channel: SMS → 503 ``sms_unavailable`` + ERROR ``otp.budget_tripped``
    and the alert of the same name; e-mail → 503 ``email_unavailable`` + ``otp.email_budget_tripped``, once
    per dedupe window. The alert insert belongs to the caller's transaction: ``auth/login/`` rolls its
    transaction back with the refusal (the ERROR line stays), ``auth/send-code/`` has none."""
    otp_channel = channels.CHANNELS[channel]
    limit = int(getattr(settings, otp_channel.global_limit_setting))
    if _counter(_global_counter_key(now, channel)) < limit:
        return None
    logger.error(otp_channel.budget_event + " limit=%s", limit)
    alerts.alert(otp_channel.budget_event, {"limit": limit})
    return otp_channel.unavailable(retry_after=_seconds_to_next_hour(now))


def count_issue(*, fid: str, ip: str, now: datetime, channel: str = channels.SMS_CHANNEL) -> None:
    _incr(_global_counter_key(now, channel), 3600 + 60)
    if channel == channels.EMAIL_CHANNEL and ip:
        _incr(_email_ip_counter_key(ip, now), 3600 + 60)
    _incr(_fid_counter_key(fid, now), 2 * 86400)
    if ip:
        _incr(_ip_counter_key(ip, now), 2 * 86400)


__all__ = [
    "seconds",
    "to_midnight",
    "phone_block",
    "wrong_codes_today",
    "cap_block",
    "budget_block",
    "count_issue",
]
