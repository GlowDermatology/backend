import re
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.core.cache import cache
from rest_framework.authentication import BaseAuthentication, get_authorization_header
from rest_framework.request import Request

from glowderma.accounts import services, sessions, tokens
from glowderma.accounts.models import AuthSession, Customer, Device, StaffUser
from glowderma.core import audit, enums, timeutils
from glowderma.core.api.exceptions import AppUpdateRequired, AuthFailed, DeviceRequired, PasswordChanged
from glowderma.core.clientip import has_internal_secret
from glowderma.core.pii import pii_ref

# Customer contract §1.2.
FID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
APP_VERSION_PATTERN = re.compile(r"^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$")
DEVICE_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")
PLATFORMS = frozenset(enums.OrderChannel.values)
# The device's last_active_at / app_version are written at most this often (seconds).
DEVICE_BUMP_SECONDS = 300
# At most one ``auth.device_mismatch`` audit row per session per this many seconds.
MISMATCH_AUDIT_SECONDS = 3600
# Where the parsed device headers are kept on the Django request (set by the customer base views).
REQUEST_DEVICE_ATTR = "gd_device"


@dataclass(frozen=True, slots=True)
class DeviceHeaders:
    fid: str
    platform: str
    app_version: str

    @property
    def fid_ref(self) -> str:
        return pii_ref(self.fid)


def _version_tuple(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split("."))


def _store_url(platform: str) -> str:
    return str(settings.APP_STORE_URL if platform == enums.OrderChannel.IOS else settings.PLAY_STORE_URL)


def min_app_version(platform: str) -> str:
    return str(
        settings.MIN_APP_VERSION_IOS
        if platform == enums.OrderChannel.IOS
        else settings.MIN_APP_VERSION_ANDROID
    )


def parse_device_headers(request: Any, *, required: bool) -> DeviceHeaders | None:
    """Validate ``X-GD-Device-Id`` / ``X-GD-Platform`` / ``X-GD-App-Version`` (customer contract §1.2).

    Missing or malformed → 400 ``device_required`` when ``required`` (auth and C routes); on optional (O)
    routes no ``X-GD-Device-Id`` means no device (``None``), while a present one must come with valid
    companions. A version below the platform's minimum → 426 ``app_update_required`` with ``{min_version,
    store_url}``. Platform ``web`` (the website's BFF) is accepted only with a valid ``X-GD-Internal-Secret``
    (else 400 ``device_required``); its ``X-GD-App-Version`` is optional (validated when present, ``""``
    when absent) and no minimum version applies.
    """
    headers = request.headers
    fid = str(headers.get("X-GD-Device-Id", ""))
    if not fid and not required:
        return None
    platform = str(headers.get("X-GD-Platform", ""))
    version = str(headers.get("X-GD-App-Version", ""))
    if platform == enums.OrderChannel.WEB and not version:
        # The website (B-W1) has no installed app: the version header is optional and never has a floor.
        version = ""
    elif not APP_VERSION_PATTERN.fullmatch(version):
        raise DeviceRequired()
    if not FID_PATTERN.fullmatch(fid) or platform not in PLATFORMS:
        raise DeviceRequired()
    if platform == enums.OrderChannel.WEB and not has_internal_secret(getattr(request, "_request", request)):
        # only the website's server-side BFF (with the internal secret) is "web": a self-asserted web
        # platform would skip the app's minimum-version floor
        raise DeviceRequired()
    if platform == enums.OrderChannel.WEB:
        return DeviceHeaders(fid=fid, platform=platform, app_version=version)
    floor = min_app_version(platform)
    if _version_tuple(version) < _version_tuple(floor):
        raise AppUpdateRequired(details={"min_version": floor, "store_url": _store_url(platform)})
    return DeviceHeaders(fid=fid, platform=platform, app_version=version)


def request_device(request: Request) -> DeviceHeaders | None:
    """The device headers the customer base view parsed for this request (``None`` when absent)."""
    device = getattr(request._request, REQUEST_DEVICE_ATTR, None)
    return device if isinstance(device, DeviceHeaders) else None


def bearer_token(request: Request) -> str | None:
    header = get_authorization_header(request).split()
    if not header or header[0].lower() != b"bearer":
        return None
    if len(header) != 2:
        raise AuthFailed("not_authenticated")
    try:
        return header[1].decode("ascii")
    except UnicodeDecodeError as exc:
        raise AuthFailed("not_authenticated") from exc


class StaffJWTAuthentication(BaseAuthentication):
    def authenticate(self, request: Request) -> tuple[StaffUser, dict[str, Any]] | None:
        raw_token = bearer_token(request)
        if raw_token is None:
            return None
        claims = tokens.decode_access(raw_token, audience="staff")
        key = f"auth:sid:{claims['sid']}"
        value = cache.get(key)
        if value is None:
            session = AuthSession.objects.select_related("staff").filter(pk=claims["sid"]).first()
            if session is None:
                raise AuthFailed("SESSION_EXPIRED")
            if session.audience != "staff" or session.staff_id != int(claims["sub"]):
                raise AuthFailed("not_authenticated")
            services.check_session(session)
            remaining = session.absolute_expires_at.timestamp() - timeutils.now().timestamp()
            active = f"active:{session.absolute_expires_at.timestamp()}"
            cache.add(key, active, timeout=max(1, min(60, int(remaining))))
            value = cache.get(key, active)
        if isinstance(value, str) and value.startswith("revoked:"):
            raise AuthFailed("SESSION_REPLACED" if value == "revoked:replaced" else "SESSION_EXPIRED")
        try:
            if not isinstance(value, str) or not value.startswith("active:"):
                raise ValueError("Invalid session cache entry")
            absolute = float(value.removeprefix("active:"))
            if timeutils.now().timestamp() >= absolute:
                raise AuthFailed("SESSION_EXPIRED")
        except (ValueError, TypeError) as exc:
            raise AuthFailed("SESSION_EXPIRED") from exc
        user = StaffUser.objects.filter(pk=int(claims["sub"])).first()
        if user is None or not user.is_active:
            raise AuthFailed("SESSION_EXPIRED")
        return user, claims

    def authenticate_header(self, request: Request) -> str:
        return 'Bearer realm="api"'


def raise_revoked(reason: str) -> None:
    """The 401 of a revoked customer session (customer contract §1.2): only ``password_changed`` →
    ``password_changed``, every other reason → ``SESSION_EXPIRED`` (B-W1: sessions are no longer replaced by
    a sign-in elsewhere, so the customer audience never answers ``SESSION_REPLACED``)."""
    if reason == enums.SessionRevokeReason.PASSWORD_CHANGED:
        raise PasswordChanged()
    raise AuthFailed("SESSION_EXPIRED")


def _active_entry(session: AuthSession) -> str:
    device: Device | None = session.device
    fid_ref = pii_ref(device.fid) if device is not None else ""
    platform = device.platform if device is not None else ""
    return f"active:{session.absolute_expires_at.timestamp()}:{fid_ref}:{platform}"


class CustomerJWTAuthentication(BaseAuthentication):
    """Customer access JWT (``aud = customer``, customer contract §1.2, §1.9, §2.1.6).

    The session state comes from ``auth:sid:{sid}``: ``active:{abs_expiry}:{pii_ref(fid)}:{platform}``, filled
    with ``cache.add`` only (never rewritten, so no write can overwrite a ``revoked:<reason>`` tombstone
    written on commit by a revocation), which lets the device and platform checks run without a query. The
    first request carrying the session's current ``rot`` confirms the rotation with one conditional UPDATE,
    remembered by the add-only flag ``auth:sidrot:{sid}:{rot}``. ``last_active_at`` / ``app_version`` of the
    device and the session's activity (``last_used_at``, user agent, IP and its location — B-W1) are bumped
    at most every five minutes.
    """

    def authenticate(self, request: Request) -> tuple[Customer, dict[str, Any]] | None:
        raw_token = bearer_token(request)
        if raw_token is None:
            if get_authorization_header(request):  # Basic, Api-Key, …: present but not a customer bearer
                raise AuthFailed("not_authenticated")
            return None
        claims = tokens.decode_access(raw_token, audience="customer")
        sid = claims["sid"]
        key = f"auth:sid:{sid}"
        value = cache.get(key)
        if value is None:
            session = AuthSession.objects.select_related("device").filter(pk=sid).first()
            if session is None:
                raise AuthFailed("SESSION_EXPIRED")
            if session.audience != "customer" or session.customer_id != int(claims["sub"]):
                raise AuthFailed("not_authenticated")
            if session.revoked_at is not None:
                raise_revoked(session.revoke_reason)
            remaining = session.absolute_expires_at.timestamp() - timeutils.now().timestamp()
            if remaining <= 0:
                raise AuthFailed("SESSION_EXPIRED")
            active = _active_entry(session)
            cache.add(key, active, timeout=max(1, min(60, int(remaining))))
            value = cache.get(key, active)
        if isinstance(value, str) and value.startswith("revoked:"):
            raise_revoked(value.removeprefix("revoked:"))
        try:
            if not isinstance(value, str) or not value.startswith("active:"):
                raise ValueError("Invalid session cache entry")
            absolute, fid_ref, platform = value.removeprefix("active:").rsplit(":", 2)
            if timeutils.now().timestamp() >= float(absolute):
                raise AuthFailed("SESSION_EXPIRED")
        except (ValueError, TypeError) as exc:
            raise AuthFailed("SESSION_EXPIRED") from exc
        customer = Customer.objects.filter(pk=int(claims["sub"])).first()
        if customer is None or customer.status != enums.CustomerStatus.ACTIVE:
            raise AuthFailed("SESSION_EXPIRED")
        device = request_device(request) or parse_device_headers(request, required=False)
        if device is None or device.fid_ref != fid_ref or device.platform != platform:
            if (
                device is not None
                and device.fid_ref != fid_ref
                and cache.add(f"auth:mismatch:{sid}", 1, timeout=MISMATCH_AUDIT_SECONDS)
            ):
                audit.record(
                    action="auth.device_mismatch",
                    entity_type="customer",
                    entity_id=str(customer.pk),
                    actor=customer,
                    request=request,
                    after={"sid": sid},
                )
            raise AuthFailed("not_authenticated")
        self._confirm_rotation(sid, int(claims["rot"]))
        if cache.add(f"auth:devbump:{sid}", 1, timeout=DEVICE_BUMP_SECONDS):
            Device.objects.filter(sessions__pk=sid).update(
                last_active_at=timeutils.now(), app_version=device.app_version, updated_at=timeutils.now()
            )
            user_agent, ip = sessions.activity_facts(request, platform=device.platform)
            sessions.touch_session(sid, user_agent=user_agent, ip=ip)
        return customer, claims

    @staticmethod
    def _confirm_rotation(sid: str, rot: int) -> None:
        flag = f"auth:sidrot:{sid}:{rot}"
        if cache.get(flag) is not None:
            return
        AuthSession.objects.filter(pk=sid, rotation=rot, rotation_confirmed=False).update(
            rotation_confirmed=True
        )
        cache.add(flag, 1, timeout=int(settings.CUSTOMER_ACCESS_TTL))

    def authenticate_header(self, request: Request) -> str:
        return 'Bearer realm="api"'


# The optional-auth (O) routes use the same class: no ``Authorization`` header → anonymous (``AllowAny`` lets
# the request through); a present header must be a valid customer access token (an invalid one, or another
# scheme, is a 401, never silently anonymous). One class keeps one OpenAPI security component
# (``CustomerJWT``).
OptionalCustomerAuthentication = CustomerJWTAuthentication
