# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Outbound product email (ADR-031) - console in dev, SMTP when configured.

A sibling of `storage.py` in the layer contract: an infrastructure adapter that `operations/`
composes and nothing below it may import. Two backends, chosen by `PRISMOID_EMAIL_BACKEND`:

* **console** (the dev default) - the rendered message prints to the API process stdout, boxed
  and unmistakable. That IS the delivery mechanism, not logging: locally there is no relay, and
  a reset code has to land somewhere the developer can read it. A deliberate, scoped
  exception recorded in ADR-031 - structured logs stay scrubbed (`cr_…` matches the Logfire
  pattern), and nothing here ever writes plaintext to the database.
* **smtp** - stdlib `smtplib` (no new dependency) in a worker thread; STARTTLS by
  default, credentials from settings, never logged.

`send()` never raises: a failed courtesy email must not fail the request that occasioned it.
The caller gets a bool; the log gets the reason, with the recipient masked - an address is PII.
"""

from __future__ import annotations

import asyncio
import smtplib
import ssl
from email.message import EmailMessage

import logfire

from prismoid.conf import settings


def mask_recipient(to: str) -> str:
    """`ana.lopes@example.com` → `a***@example.com` - enough to debug, too little to harvest."""
    local, _, domain = to.partition("@")
    return f"{local[:1]}***@{domain}" if domain else "***"


def _render_console(to: str, subject: str, text: str, *, sender: str | None = None) -> str:
    body = "\n".join(f"│ {line}" for line in text.splitlines())
    return "\n".join(
        [
            "┌─ ✉  outbound email - console backend, nothing left this machine ─────────",
            f"│ To:      {to}",
            f"│ From:    {sender or settings.email.from_address}",
            f"│ Subject: {subject}",
            "│",
            body,
            "└───────────────────────────────────────────────────────────────────────────",
        ]
    )


def _send_smtp(msg: EmailMessage) -> None:
    """Blocking on purpose - runs in `asyncio.to_thread`, never on the event loop."""
    cfg = settings.email
    with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=cfg.smtp_timeout_seconds) as smtp:
        if cfg.smtp_starttls:
            smtp.starttls(context=ssl.create_default_context())
        if cfg.smtp_username:
            smtp.login(cfg.smtp_username, cfg.smtp_password.get_secret_value())
        smtp.send_message(msg)


async def send(
    *,
    to: str,
    subject: str,
    text: str,
    from_address: str | None = None,
    reply_to: str | None = None,
    headers: dict[str, str] | None = None,
) -> bool:
    """Deliver one plain-text message. True = handed to the backend; False = logged and dropped.

    The whole body of each backend is guarded, not just the wire call: `except Exception` is the
    contract, because the failures that slipped the first, narrower net were exactly the exotic
    ones - a non-UTF-8 stdout choking on the box glyphs, a torn pipe under nohup, a ValueError
    from a header, a RuntimeError from `to_thread` at shutdown - and any of them raising here
    fails (or oracles) a request that already did its real work.

    `from_address`/`reply_to`/`headers`: a bot channel replies AS the bot - its
    own address on the bots subdomain, threading headers riding along - while the product's
    own mail keeps the plain default. Nothing else changed: console still prints, smtp still
    threads out.
    """
    backend = settings.email.backend
    sender = from_address or settings.email.from_address
    if backend == "console":
        # stdout is the inbox in dev. flush=True is load-bearing: `just dev` nohups
        # the API into a log file, which makes stdout block-buffered - without the flush the
        # code the developer is watching for sits invisible in a 4 KB buffer.
        try:
            print(_render_console(to, subject, text, sender=sender), flush=True)
        except Exception as exc:
            logfire.error("console email to {to} failed: {exc}", to=mask_recipient(to), exc=str(exc))
            return False
        return True
    if backend == "smtp":
        try:
            msg = EmailMessage()
            msg["From"] = sender
            msg["To"] = to
            msg["Subject"] = subject
            if reply_to:
                msg["Reply-To"] = reply_to
            for key, value in (headers or {}).items():
                msg[key] = value
            msg.set_content(text)
            await asyncio.to_thread(_send_smtp, msg)
        except Exception as exc:
            logfire.error("email to {to} failed: {exc}", to=mask_recipient(to), exc=str(exc))
            return False
        return True
    logfire.error(
        "unknown PRISMOID_EMAIL_BACKEND {backend!r} - email to {to} dropped",
        backend=backend,
        to=mask_recipient(to),
    )
    return False
