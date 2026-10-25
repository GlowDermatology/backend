import pytest

from glowderma.integrations.oauth.fake import FakeFacebook, FakeGoogle
from glowderma.integrations.oauth.ports import OAuthExchangeError, OAuthProvider, SocialClaims

VERIFIER = "a" * 43
REDIRECT = "http://localhost:3000/admin/auth/callback"


def exchange(
    provider: OAuthProvider, code: str, *, nonce: str | None = "nonce-42", verifier: str = VERIFIER
) -> SocialClaims:
    return provider.exchange(code=code, code_verifier=verifier, redirect_uri=REDIRECT, nonce=nonce)


def test_fake_google_success_codes() -> None:
    claims = exchange(FakeGoogle(), "fake:42:ali@gmail.com")
    assert claims == SocialClaims(
        provider="google", subject="42", email="ali@gmail.com", email_verified=True, hd=None
    )
    with_hd = exchange(FakeGoogle(), "fake:42:ali@glowderma.uz:glowderma.uz")
    assert with_hd.hd == "glowderma.uz"
    unverified = exchange(FakeGoogle(), "fake-unverified:42:ali@gmail.com")
    assert unverified.email_verified is False


@pytest.mark.parametrize(
    "code,kind",
    [
        ("fake-fail", "exchange_failed"),
        ("fake-down", "unavailable"),
        ("garbage", "exchange_failed"),
        ("fake:", "exchange_failed"),
        ("fake::x@y.uz", "exchange_failed"),
        ("fake:1:x@y.uz:hd:extra", "exchange_failed"),
        ("real-looking-code", "exchange_failed"),
    ],
)
def test_fake_failure_codes(code: str, kind: str) -> None:
    for provider in (FakeGoogle(), FakeFacebook()):
        with pytest.raises(OAuthExchangeError) as info:
            exchange(provider, code, nonce="*")
        assert info.value.kind == kind


@pytest.mark.parametrize("verifier", ["a" * 42, "a" * 129, ""])
def test_fake_rejects_pkce_verifier_outside_43_128(verifier: str) -> None:
    for provider in (FakeGoogle(), FakeFacebook()):
        with pytest.raises(OAuthExchangeError) as info:
            exchange(provider, "fake:42:ali@gmail.com", nonce="*", verifier=verifier)
        assert info.value.kind == "exchange_failed"
    assert exchange(FakeGoogle(), "fake:42:ali@gmail.com", verifier="b" * 128).subject == "42"


def test_fake_google_nonce_must_match_subject_or_be_wildcard() -> None:
    assert exchange(FakeGoogle(), "fake:42:ali@gmail.com", nonce="nonce-42").subject == "42"
    assert exchange(FakeGoogle(), "fake:42:ali@gmail.com", nonce="*").subject == "42"
    for nonce in ("nonce-41", "", None):
        with pytest.raises(OAuthExchangeError) as info:
            exchange(FakeGoogle(), "fake:42:ali@gmail.com", nonce=nonce)
        assert info.value.kind == "invalid_token"


def test_fake_facebook_ignores_nonce_and_never_verifies_email() -> None:
    for nonce in (None, "", "anything"):
        claims = exchange(FakeFacebook(), "fake:777:malika@example.com:ignored.uz", nonce=nonce)
        assert claims == SocialClaims(
            provider="facebook", subject="777", email="malika@example.com", email_verified=False, hd=None
        )


def test_claims_are_frozen() -> None:
    claims = exchange(FakeGoogle(), "fake:42:ali@gmail.com")
    with pytest.raises(AttributeError):
        claims.email = "other@gmail.com"  # type: ignore[misc]
