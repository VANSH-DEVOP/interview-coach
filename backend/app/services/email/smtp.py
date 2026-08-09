"""SMTP email backend.

One class covers every provider worth using -- SES, SendGrid, Mailgun,
Postmark, Gmail -- because they all speak SMTP. Changing provider is host, port
and credentials in the environment; no code, no new dependency, no redeploy of
anything but config. That portability is the reason this is the real backend
rather than a hand-written REST client per vendor.

The trade is coarser errors. A REST API tells you *which* recipient was
rejected and why; SMTP gives you a status code and a string. Since every
message here is a single transactional email whose failure is logged and
retried by the user, that is an acceptable loss.
"""

import logging

import aiosmtplib

from app.services.email.base import EmailError, EmailMessage, EmailSender

logger = logging.getLogger(__name__)


class SmtpEmailSender(EmailSender):
    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str | None,
        password: str | None,
        sender: str,
        use_tls: bool,
        start_tls: bool,
        timeout: int,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._sender = sender
        self._use_tls = use_tls
        self._start_tls = start_tls
        self._timeout = timeout

    async def send(self, message: EmailMessage) -> None:
        from email.message import EmailMessage as MimeMessage

        # Construction is inside the try on purpose. Built with the stdlib
        # rather than by string concatenation so a newline in a subject or
        # address cannot inject headers -- and the stdlib *refuses* those
        # outright rather than encoding them, raising ValueError. That is the
        # behaviour we want, but it has to leave here as EmailError like every
        # other failure, not as an unhandled exception from a mail backend.
        try:
            mime = MimeMessage()
            mime["From"] = self._sender
            mime["To"] = message.to
            mime["Subject"] = message.subject
            mime.set_content(message.body)

            await aiosmtplib.send(
                mime,
                hostname=self._host,
                port=self._port,
                username=self._username or None,
                password=self._password or None,
                # Implicit TLS (port 465) and STARTTLS (587) are mutually
                # exclusive; aiosmtplib rejects both being set.
                use_tls=self._use_tls,
                start_tls=self._start_tls or None,
                timeout=self._timeout,
            )
        except Exception as exc:  # noqa: BLE001 - aiosmtplib raises several types
            # The recipient is logged, the body is not: it contains the very
            # secret the email exists to deliver.
            logger.warning("SMTP delivery to %s failed.", message.to, exc_info=exc)
            raise EmailError(f"Could not send email via {self._host}: {exc}") from exc
