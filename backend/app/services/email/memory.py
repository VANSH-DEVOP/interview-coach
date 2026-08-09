"""In-memory email backend.

Lives in the application package rather than the test suite because it is part
of the seam's contract: asserting that a flow sent the right message to the
right address is how every email-sending feature is tested, and each of those
tests would otherwise reimplement this.

Never selectable through `EMAIL_BACKEND` -- it is injected directly by tests.
"""

from app.services.email.base import EmailMessage, EmailSender


class RecordingEmailSender(EmailSender):
    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []

    async def send(self, message: EmailMessage) -> None:
        self.sent.append(message)

    def last_to(self, recipient: str) -> EmailMessage | None:
        """The most recent message to an address, or None.

        Most recent rather than first: a resend should be what an assertion
        sees, otherwise tests silently check the stale message.
        """
        for message in reversed(self.sent):
            if message.to == recipient:
                return message
        return None

    def clear(self) -> None:
        self.sent.clear()
