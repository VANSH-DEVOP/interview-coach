"""The production configuration guard.

Every check here is for a value that is *correct* in development and dangerous
in production, and whose failure mode is silent. That is the bar: a wrong value
producing an obvious error needs no guard, because the error is the guard.

The model is `EMAIL_BACKEND='log'`, refused in production since long before this
existed -- it publishes password-reset links to anyone who can read the logs
while every flow still reports success.
"""

import pytest

from app.core.config import Settings
from app.core.startup_checks import production_problems, verify_production_config


def settings(**overrides) -> Settings:
    """A configuration that would pass, before the test breaks one thing."""
    safe = {
        "ENVIRONMENT": "production",
        "DEBUG": False,
        "JWT_SECRET_KEY": "x" * 64,
        "POSTGRES_PASSWORD": "a-generated-value",
        "FRONTEND_BASE_URL": "https://interviewpilot.example",
        "CORS_ORIGINS": ["https://interviewpilot.example"],
    }
    return Settings(_env_file=None, **{**safe, **overrides})


def test_a_valid_production_configuration_passes() -> None:
    verify_production_config(settings())
    assert production_problems(settings()) == []


def test_development_is_never_checked() -> None:
    """Every one of these values is the right one to have on a laptop, so the
    guard must be a no-op there rather than something to work around."""
    verify_production_config(Settings(_env_file=None, ENVIRONMENT="local"))


# -- What it refuses --------------------------------------------------------------


def test_the_development_signing_key_is_refused() -> None:
    """It is in the repository. Anyone who has read it can mint a token for any
    account, and nothing about the running application would look wrong."""
    problems = production_problems(
        settings(JWT_SECRET_KEY="insecure-local-dev-key-override-me")
    )

    assert any("JWT_SECRET_KEY" in problem for problem in problems)
    assert any("openssl rand" in problem for problem in problems)


def test_a_short_signing_key_is_refused() -> None:
    """Recoverable offline from a single token, after which any account can be
    forged."""
    assert any("at least" in problem for problem in production_problems(settings(JWT_SECRET_KEY="short")))


@pytest.mark.parametrize("password", ["interviewpilot", "postgres", "changeme", "123456"])
def test_well_known_database_passwords_are_refused(password) -> None:
    assert any(
        "POSTGRES_PASSWORD" in problem
        for problem in production_problems(settings(POSTGRES_PASSWORD=password))
    )


@pytest.mark.parametrize(
    "url", ["http://localhost:3000", "http://127.0.0.1:3000", "http://0.0.0.0:3000"]
)
def test_a_localhost_frontend_url_is_refused(url) -> None:
    """Reset and verification links are built from it and are opened outside the
    browser session, so a localhost value makes account recovery impossible for
    every user -- while the application reports every reset as sent."""
    problems = production_problems(settings(FRONTEND_BASE_URL=url))

    assert any("FRONTEND_BASE_URL" in problem for problem in problems)
    assert any("recovery impossible" in problem for problem in problems)


def test_a_localhost_cors_origin_is_refused() -> None:
    assert any(
        "CORS_ORIGINS" in problem
        for problem in production_problems(settings(CORS_ORIGINS=["http://localhost:3000"]))
    )


def test_debug_is_refused() -> None:
    assert any("DEBUG" in problem for problem in production_problems(settings(DEBUG=True)))


def test_a_plain_http_frontend_is_refused() -> None:
    """The one that is hardest to diagnose from the symptom. Session cookies are
    Secure in production, and a browser silently discards those over plain HTTP:
    login appears to succeed and every request after it is unauthenticated."""
    problems = production_problems(settings(FRONTEND_BASE_URL="http://interviewpilot.example"))

    assert any("Secure" in problem and "HTTPS" in problem for problem in problems)


# -- How it reports ---------------------------------------------------------------


def test_every_problem_is_reported_at_once() -> None:
    """Failing on the first would mean fix, redeploy, discover the next -- and
    for a production deploy that is minutes each, and an easy way to stop
    halfway with a half-secured configuration."""
    with pytest.raises(ValueError) as raised:
        verify_production_config(
            settings(
                JWT_SECRET_KEY="insecure-local-dev-key-override-me",
                POSTGRES_PASSWORD="postgres",
                DEBUG=True,
                FRONTEND_BASE_URL="http://localhost:3000",
                CORS_ORIGINS=["http://localhost:3000"],
            )
        )

    message = str(raised.value)
    for expected in (
        "JWT_SECRET_KEY",
        "POSTGRES_PASSWORD",
        "DEBUG",
        "FRONTEND_BASE_URL",
        "CORS_ORIGINS",
    ):
        assert expected in message
    assert "5 unsafe setting(s)" in message


def test_it_raises_rather_than_warns() -> None:
    """A warning at boot is a line in a log nobody reads, and every one of these
    leaves the application apparently working."""
    with pytest.raises(ValueError):
        verify_production_config(settings(DEBUG=True))
