"""Configuration that must not reach production, refused at startup.

Every check here is for a setting that is *correct* in development and
dangerous in production, and whose failure mode is silent. That is the bar for
being in this file: a wrong value that produces an obvious error does not need a
guard, because the error is the guard.

The model is `EMAIL_BACKEND='log'`, which has been refused in production since
before this module existed -- it publishes password-reset links to anyone who
can read the logs while every flow still reports success. The checks below are
the same shape: a default that is harmless on a laptop and a hole on the
internet.

**All problems are reported at once.** Failing on the first would mean fixing
one, redeploying, and discovering the next -- which for a production deploy is
several minutes each and an easy way to give up halfway with a half-secured
configuration.

Fatal rather than a warning, for the same reason as the email one: a warning at
boot is a line in a log nobody reads, and every one of these leaves the
application apparently working.
"""

from __future__ import annotations

from app.core.config import Settings

# Values shipped in .env.example and docker-compose defaults. Anything still
# holding one of these in production was never configured, as opposed to
# configured badly.
_DEFAULT_PASSWORDS = {"interviewpilot", "postgres", "password", "changeme", "123456"}

_INSECURE_JWT_PREFIX = "insecure-local-dev-key"

# A JWT signing key shorter than this is brute-forceable offline: an attacker
# with one token can recover the key and then mint their own, for any account.
_MIN_JWT_KEY_LENGTH = 32


def _local_host(url: str) -> bool:
    lowered = url.lower()
    return any(host in lowered for host in ("localhost", "127.0.0.1", "0.0.0.0", "::1"))


def production_problems(settings: Settings) -> list[str]:
    """Every reason this configuration must not serve production traffic.

    Separate from the raising wrapper so it can be tested, and so a deployment
    script can ask the question without catching an exception.
    """
    problems: list[str] = []

    key = settings.JWT_SECRET_KEY or ""
    if key.startswith(_INSECURE_JWT_PREFIX):
        problems.append(
            "JWT_SECRET_KEY is still the development default. Anyone who has read "
            "this repository can mint a token for any account. "
            "Generate one with: openssl rand -hex 32"
        )
    elif len(key) < _MIN_JWT_KEY_LENGTH:
        problems.append(
            f"JWT_SECRET_KEY is {len(key)} characters; use at least "
            f"{_MIN_JWT_KEY_LENGTH}. A short key can be recovered offline from a "
            "single token, after which any account can be forged."
        )

    if (settings.POSTGRES_PASSWORD or "") in _DEFAULT_PASSWORDS:
        problems.append(
            "POSTGRES_PASSWORD is a well-known default. Set a generated value."
        )

    # Reset and verification links are built from this, and they are opened
    # outside the browser session -- a localhost value does not merely look
    # wrong, it makes account recovery impossible.
    if _local_host(settings.FRONTEND_BASE_URL):
        problems.append(
            f"FRONTEND_BASE_URL points at {settings.FRONTEND_BASE_URL!r}. Password "
            "reset and email verification links are built from it and are opened "
            "outside the browser session, so a localhost value makes account "
            "recovery impossible for every user."
        )

    if any(_local_host(origin) for origin in settings.CORS_ORIGINS):
        problems.append(
            f"CORS_ORIGINS still contains a localhost entry ({settings.CORS_ORIGINS}). "
            "Set it to the deployed frontend's origin."
        )

    if settings.DEBUG:
        problems.append(
            "DEBUG is on. Set DEBUG=false; debug output belongs to development."
        )

    # The BFF sets Secure cookies, which a browser discards over plain HTTP on
    # anything but localhost. The symptom is not an error: login appears to
    # succeed and every request afterwards is unauthenticated.
    if settings.FRONTEND_BASE_URL.startswith("http://") and not _local_host(
        settings.FRONTEND_BASE_URL
    ):
        problems.append(
            "FRONTEND_BASE_URL is http://. Session cookies are marked Secure in "
            "production, and a browser silently discards those over plain HTTP -- "
            "login will appear to succeed and every request after it will be "
            "unauthenticated. Serve the frontend over HTTPS."
        )

    return problems


def verify_production_config(settings: Settings) -> None:
    """Raise if this configuration must not serve production traffic.

    A no-op outside production, where every one of these values is the right
    one to have.
    """
    if settings.ENVIRONMENT != "production":
        return

    problems = production_problems(settings)
    if not problems:
        return

    listed = "\n".join(f"  - {problem}" for problem in problems)
    raise ValueError(
        f"Refusing to start in production with {len(problems)} unsafe "
        f"setting(s):\n{listed}\n"
        "Each of these is safe in development and a hole in production, and "
        "none of them would have produced a visible error at runtime."
    )
