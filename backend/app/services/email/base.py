"""EmailSender abstraction.

Business logic depends ONLY on this interface. Backends (a logger today, SMTP
for anything real) implement it, and switching is a configuration change
(`EMAIL_BACKEND`), not a code change.

Contract notes:
- `to` is a single recipient. Every message this application sends is
  transactional and addressed to one person; there is no bulk path, and adding
  one later should be a deliberate decision rather than a list that quietly
  grows.
- Backends raise `EmailError` on provider failure, so callers never handle
  provider-specific exceptions.
- Sending is best-effort from the caller's perspective. Whether a failure
  should surface to the user is a decision for each flow, not for the transport
  -- password reset deliberately swallows it (see auth_service), because
  reporting a delivery failure tells an anonymous caller that the address
  exists.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass


class EmailError(RuntimeError):
    """A backend could not send the message."""


@dataclass(frozen=True, slots=True)
class EmailMessage:
    to: str
    subject: str
    # Plain text only. Every message here is a short sentence and a link;
    # multipart HTML would add a templating layer, a second body to keep in
    # sync, and a much better chance of landing in spam.
    body: str


class EmailSender(ABC):
    """Abstract transactional email transport. One instance per process."""

    @abstractmethod
    async def send(self, message: EmailMessage) -> None:
        """Deliver the message, or raise EmailError."""
