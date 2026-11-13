"""Notifications by email (design §10): optional, plain text, never blocking."""

from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from exradius_desk.settings import Smtp

log = logging.getLogger(__name__)

BODY_LIMIT = 2000


@dataclass(frozen=True)
class Mail:
    to: str
    name: str
    subject: str
    body: str


def cut(text: str, limit: int = BODY_LIMIT) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


class Mailer:
    def __init__(self, smtp: Smtp | None, on_failure=None):
        self.smtp = smtp
        self.on_failure = on_failure
        """Called with the reason when a message could not be sent (for the bridge status)."""
        self._work: set[asyncio.Task] = set()

    @property
    def enabled(self) -> bool:
        return self.smtp is not None

    def send(self, mails: list[Mail]) -> None:
        """In the background, after the caller's commit."""
        if not self.enabled or not mails:
            return
        task = asyncio.create_task(self._send(mails))
        self._work.add(task)
        task.add_done_callback(self._work.discard)

    async def idle(self) -> None:
        while self._work:
            await asyncio.gather(*list(self._work), return_exceptions=True)

    async def _send(self, mails: list[Mail]) -> None:
        try:
            await asyncio.to_thread(self._deliver, mails)
        except Exception as exc:  # noqa: BLE001 - logged and reported, never retried
            reason = f"mail could not be sent through {self.smtp.host}: {exc or type(exc).__name__}"
            log.warning("%s", reason)
            if self.on_failure is not None:
                self.on_failure(reason)

    def _deliver(self, mails: list[Mail]) -> None:
        smtp = self.smtp
        assert smtp is not None
        context = ssl.create_default_context()
        if smtp.tls == "tls":
            client: smtplib.SMTP = smtplib.SMTP_SSL(smtp.host, smtp.port, timeout=30, context=context)
        else:
            client = smtplib.SMTP(smtp.host, smtp.port, timeout=30)
        with client:
            if smtp.tls == "starttls":
                client.starttls(context=context)
            if smtp.username:
                client.login(smtp.username, smtp.password or "")
            for mail in mails:
                message = EmailMessage()
                message["From"] = smtp.sender
                message["To"] = formataddr((mail.name, mail.to))
                message["Subject"] = mail.subject
                message["Message-ID"] = make_msgid(domain="exradius-desk")
                message.set_content(mail.body)
                client.send_message(message)
