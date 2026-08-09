"""Email backend that writes to the log instead of sending.

The default, and what CI and local development run on. It makes every flow
fully exercisable -- you copy the reset link out of the log and use it -- with
no credentials, no network, and no chance of mailing a real person from a test.

It is also a liability in production, because the whole point is that it prints
the contents of a message only the recipient should be able to read: reset
links, verification links. `get_email_sender` refuses to hand this back in a
production environment for exactly that reason.

Named `console` rather than `logging`: a module called `logging.py` inside a
package shadows the standard library for the package's own `__init__.py`, since
importing a submodule binds it as an attribute of the parent -- and the
parent's namespace *is* `__init__.py`. `import logging` there then resolves to
this file and fails on `getLogger`.
"""

import logging

from app.services.email.base import EmailMessage, EmailSender

logger = logging.getLogger(__name__)


class LoggingEmailSender(EmailSender):
    async def send(self, message: EmailMessage) -> None:
        logger.info(
            "EMAIL (not sent -- logging backend)\n"
            "  To:      %s\n"
            "  Subject: %s\n"
            "%s",
            message.to,
            message.subject,
            message.body,
        )
