# ruff: noqa: S106 - internal secrets in fixtures are test data
"""B-W1 helpers: the user-agent reader, the local GeoIP lookup, the client user agent, and the slugs."""

from typing import Any

import maxminddb
import pytest
from django.test import RequestFactory

from glowderma.core import geoip, slugs, useragent
from glowderma.core.clientip import USER_AGENT_MAX, client_user_agent

CHROME_WINDOWS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)
SAFARI_MAC = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/18.1 Safari/605.1.15"
)
SAFARI_IPHONE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/18.1 Mobile/15E148 Safari/604.1"
)
CHROME_IPHONE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "CriOS/130.0.6723.90 Mobile/15E148 Safari/604.1"
)
EDGE_WINDOWS = f"{CHROME_WINDOWS} Edg/130.0.2849.68"
OPERA_WINDOWS = f"{CHROME_WINDOWS} OPR/114.0.0.0"
YANDEX_ANDROID = (
    "Mozilla/5.0 (Linux; Android 14; SM-A546B) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 YaBrowser/24.10.3.104.00 SA/3 Mobile Safari/537.36"
)
SAMSUNG_ANDROID = (
    "Mozilla/5.0 (Linux; Android 14; SM-S911B) AppleWebKit/537.36 (KHTML, like Gecko) "
    "SamsungBrowser/26.0 Chrome/122.0.0.0 Mobile Safari/537.36"
)
FIREFOX_LINUX = "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:132.0) Gecko/20100101 Firefox/132.0"
FIREFOX_IPHONE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "FxiOS/132.0 Mobile/15E148 Safari/605.1.15"
)
CHROME_OS = (
    "Mozilla/5.0 (X11; CrOS x86_64 14541.0.0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 "
    "Safari/537.36"
)
TELEGRAM_ANDROID = (
    "Mozilla/5.0 (Linux; Android 13; Redmi Note 12) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
    "Chrome/129.0.6668.100 Mobile Safari/537.36 Telegram-Android/11.2.2"
)


@pytest.mark.parametrize(
    ("agent", "expected"),
    [
        (CHROME_WINDOWS, "Chrome (Windows)"),
        (SAFARI_MAC, "Safari (Mac OS X)"),
        (SAFARI_IPHONE, "Safari (iOS)"),
        (CHROME_IPHONE, "Chrome (iOS)"),
        (EDGE_WINDOWS, "Edge (Windows)"),
        (OPERA_WINDOWS, "Opera (Windows)"),
        (YANDEX_ANDROID, "Yandex Browser (Android)"),
        (SAMSUNG_ANDROID, "Samsung Internet (Android)"),
        (FIREFOX_LINUX, "Firefox (Linux)"),
        (FIREFOX_IPHONE, "Firefox (iOS)"),
        (CHROME_OS, "Chrome (Chrome OS)"),
        (TELEGRAM_ANDROID, "Telegram (Android)"),
        ("curl/8.5.0", "Brauzer"),
        ("SomeBot/1.0 (Windows NT 10.0)", "Brauzer (Windows)"),
        ("", "Brauzer"),
    ],
)
def test_user_agent_labels(agent: str, expected: str) -> None:
    assert useragent.label(agent, unknown="Brauzer") == expected


def test_parse_is_bounded_and_never_fails() -> None:
    assert useragent.parse("x" * 100_000 + " Firefox/1") == useragent.UserAgent(browser="", os="")
    assert useragent.parse(None) == useragent.UserAgent(browser="", os="")  # type: ignore[arg-type]


# --- client user agent (core.clientip) ---------------------------------------------------------------------


def test_the_bff_user_agent_counts_only_with_the_internal_secret(settings: Any) -> None:
    settings.INTERNAL_API_SECRET = "s" * 40
    factory = RequestFactory()
    trusted = factory.get(
        "/", HTTP_X_GD_INTERNAL_SECRET="s" * 40, HTTP_X_GD_CLIENT_UA=SAFARI_MAC, HTTP_USER_AGENT="node"
    )
    assert client_user_agent(trusted) == SAFARI_MAC
    forged = factory.get(
        "/", HTTP_X_GD_INTERNAL_SECRET="wrong", HTTP_X_GD_CLIENT_UA=SAFARI_MAC, HTTP_USER_AGENT="okhttp/4"
    )
    assert client_user_agent(forged) == "okhttp/4"
    without_ua = factory.get("/", HTTP_X_GD_INTERNAL_SECRET="s" * 40, HTTP_USER_AGENT="node")
    assert client_user_agent(without_ua) == "node"


def test_the_user_agent_is_printable_and_bounded() -> None:
    request = RequestFactory().get("/", HTTP_USER_AGENT="a\x00b\x1bc" + "z" * 1000)
    value = client_user_agent(request)
    assert value.startswith("abc") and len(value) == USER_AGENT_MAX


# --- GeoIP --------------------------------------------------------------------------------------------------


class FakeReader:
    def __init__(self, records: dict[str, Any], *, fail: bool = False) -> None:
        self.records = records
        self.fail = fail
        self.closed = False

    def get(self, ip: str) -> Any:
        if self.fail:
            raise ValueError("corrupt")
        return self.records.get(ip)

    def close(self) -> None:
        self.closed = True


TASHKENT = {
    "city": {"names": {"en": "Tashkent", "ru": "Ташкент"}},
    "subdivisions": [{"names": {"en": "Tashkent"}}],
    "country": {"iso_code": "UZ", "names": {"en": "Uzbekistan"}},
}


@pytest.fixture
def geo_db(settings: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    geoip.reset()
    settings.GEOIP_CITY_DB = "/srv/geoip/city.mmdb"
    reader = FakeReader({"84.54.70.10": TASHKENT, "2.2.2.2": {"country": {"iso_code": "DE"}}})
    opened: list[str] = []

    def open_database(path: str) -> FakeReader:
        opened.append(path)
        return reader

    monkeypatch.setattr(maxminddb, "open_database", open_database)
    yield opened
    geoip.reset()


def test_locate_formats_city_region_country(geo_db: list[str]) -> None:
    assert geoip.locate("84.54.70.10") == "Tashkent, Tashkent, UZ"
    assert geoip.locate("84.54.70.10", "ru") == "Ташкент, Tashkent, UZ"
    assert geoip.locate("2.2.2.2") == "DE"
    assert geoip.locate("9.9.9.9") == ""  # not in the database
    assert geo_db == ["/srv/geoip/city.mmdb"]  # opened once, then shared


@pytest.mark.parametrize("ip", ["10.0.0.1", "127.0.0.1", "192.168.1.5", "::1", "not-an-ip", ""])
def test_private_and_malformed_addresses_are_never_looked_up(geo_db: list[str], ip: str) -> None:
    assert geoip.locate(ip) == ""
    assert geo_db == []


def test_no_database_means_no_location(settings: Any) -> None:
    geoip.reset()
    settings.GEOIP_CITY_DB = ""
    assert geoip.locate("84.54.70.10") == ""


def test_an_unreadable_database_is_tried_once_and_logged(settings: Any, caplog: Any, tmp_path: Any) -> None:
    geoip.reset()
    settings.GEOIP_CITY_DB = str(tmp_path / "missing.mmdb")
    assert geoip.locate("84.54.70.10") == ""
    assert geoip.locate("84.54.70.11") == ""
    assert caplog.text.count("geoip.unavailable") == 1
    geoip.reset()


def test_a_failing_lookup_is_empty(settings: Any, monkeypatch: pytest.MonkeyPatch, caplog: Any) -> None:
    geoip.reset()
    settings.GEOIP_CITY_DB = "/srv/geoip/broken.mmdb"
    monkeypatch.setattr(maxminddb, "open_database", lambda path: FakeReader({}, fail=True))
    assert geoip.locate("84.54.70.10") == ""
    assert "geoip.lookup_failed" in caplog.text
    geoip.reset()


def test_describe_tolerates_odd_records() -> None:
    assert geoip.describe(None) == ""
    assert geoip.describe({"city": "Tashkent", "subdivisions": "x", "country": []}) == ""
    assert geoip.describe({"city": {"names": {"de": "Taschkent"}}}) == ""
    assert geoip.describe({"city": {"names": {"en": "A" * 200}}}) == "A" * geoip.MAX_LENGTH


# --- slugs --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Ko‘pikli yuvish geli 150 ml", "kopikli-yuvish-geli-150-ml"),
        ("Koʻpik gʻamxoʻr O'zbekiston", "kopik-gamxor-ozbekiston"),
        ("Сыворотка с гиалуроновой кислотой", "syvorotka-s-gialuronovoy-kislotoy"),
        ("Ёлка, щётка, Юлия, Шахноза, Чой", "yolka-shyotka-yuliya-shaxnoza-choy"),
        ("Қовун ғишт ҳаво ўрик", "qovun-gisht-havo-orik"),
        ("Crème Brûlée — SPF 50+", "creme-brulee-spf-50"),
        ("  --  ", ""),
        ("Dr. Malika Karimova", "dr-malika-karimova"),
    ],
)
def test_slugify_uz(text: str, expected: str) -> None:
    assert slugs.slugify_uz(text) == expected


def test_slugify_cuts_at_a_word_boundary() -> None:
    assert slugs.slugify_uz("alpha beta gamma", max_length=12) == "alpha-beta"
    assert slugs.slugify_uz("abcdefghijklmnop", max_length=5) == "abcde"


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ("serum-30-ml", True),
        ("a", True),
        ("12345", False),  # only digits: an id
        ("Serum", False),
        ("serum--30", False),
        ("-serum", False),
        ("serum_30", False),
        ("", False),
        ("a" * 141, False),
    ],
)
def test_is_valid_slug(value: str, valid: bool) -> None:
    assert slugs.is_valid_slug(value, max_length=140) is valid


def test_candidates_keep_the_suffix_within_the_limit() -> None:
    assert slugs.candidate("serum", 1, max_length=10) == "serum"
    assert slugs.candidate("serum", 2, max_length=10) == "serum-2"
    assert slugs.candidate("abcdefghij", 12, max_length=10) == "abcdefg-12"
