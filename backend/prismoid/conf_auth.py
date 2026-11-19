# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The auth group (sessions, signup, the auth doors' burst buckets, argon2id, password resets) and
the hosted boot's check for the dev compose's well-known credentials.

`prismoid.conf` composes these into `settings` (`settings.auth`) and every caller reads them there.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from pydantic_settings import BaseSettings

from prismoid.conf_env import cfg

if TYPE_CHECKING:  # the root tree composes this module; the check reads the tree it is handed
    from prismoid.conf import Settings


class AuthSettings(BaseSettings):
    """Opaque, revocable, hashed. No JWT - statelessness defeats the kill switch."""

    model_config = cfg("PRISMOID_AUTH_")

    # Open signup. Staging and production read it as False unless the environment names it, so a
    # fresh hosted box is invite-only until its operator opts in (`Settings._signup_closed_outside_dev`).
    signup_enabled: bool = True
    session_ttl_days: int = 30

    # The auth doors' burst buckets (`operations/throttle`): a Postgres row per bucket, so every
    # replica and a restart count the same one-minute window. Login is counted per account as seen
    # from one client address AND per client address (across accounts); the reset request per
    # account (the normalized address, from any client) AND per client address; signup per client
    # address and per address signing up; the reset completion per client address. Past a
    # bucket's line the door answers 429 with Retry-After.
    login_rate_limit_per_minute: int = 10
    login_ip_rate_limit_per_minute: int = 60
    signup_rate_limit_per_minute: int = 10
    reset_rate_limit_per_minute: int = 10
    # Backoff on consecutive failed logins: from the Nth failure a bucket refuses every attempt for
    # base * 2^(failures - N) seconds, capped. A success clears the account's count from that
    # address, a password reset clears it from every address, and a quiet hour forgets it. The
    # address threshold is higher: one office behind one NAT is many people.
    login_backoff_after_failures: int = 5
    login_ip_backoff_after_failures: int = 30
    login_backoff_base_seconds: int = 30
    login_backoff_max_seconds: int = 900

    # argon2id parameters (OWASP first choice), tuned against the <200 ms login budget. Measured:
    # t=2 / 64 MiB yields ~95–128 ms worst-case per verify - strong (64 MiB defeats commodity GPU cracking)
    # and comfortably under 200 ms. The tuning decision is to STAY here, not to push higher: raising
    # time_cost to 3 would spend ~150 ms and risk breaching the budget under load spikes for a marginal
    # attacker cost, and 64 MiB already exceeds the OWASP floor. The headroom is deliberate, not slack.
    argon2_time_cost: int = 2
    argon2_memory_cost_kib: int = 65536
    argon2_parallelism: int = 1

    # Forgotten passwords. The emailed `cr_` code is single-use and short-lived: long
    # enough to walk to another tab and paste it, short enough that a forgotten inbox is not a
    # standing skeleton key. The per-hour cap is a durable per-user throttle on outbound mail -
    # a reset email an attacker can trigger for free is a nuisance cannon aimed at your users.
    password_reset_ttl_minutes: int = 15
    password_reset_max_per_hour: int = 5

    # The emailed `ev_` code that proves an address (ADR-086): a day to find the mail, and the same
    # durable per-user cap on outbound mail as the reset.
    email_verification_ttl_minutes: int = 1440
    email_verification_max_per_hour: int = 5


# The dev compose's credentials (backend/deploy/compose.yaml): MinIO's root password, Postgres's
# password, the egress proxy's control secret.
_DEV_S3_SECRET = "prismoid1234"
_DEV_DB_PASSWORD = "prismoid"
_DEV_EGRESS_SECRET = "dev-egress-control-secret"


def close_signup_outside_dev(s: Settings) -> None:
    """In staging and production `PRISMOID_AUTH_SIGNUP_ENABLED` must be set to open signup; unset reads
    as False. Signup does not verify an address, so open signup on a fresh public box is a
    stranger's door."""
    if s.environment in ("staging", "prod") and "signup_enabled" not in s.auth.model_fields_set:
        s.auth.signup_enabled = False


def refuse_dev_secrets(s: Settings) -> None:
    """The dev compose's well-known credentials are public the moment the repo is: staging and
    production refuse to boot on any of them, naming the settings and never a value."""
    if s.environment not in ("staging", "prod"):
        return
    if found := dev_secrets_in_use(s):
        raise ValueError(
            f"refused with PRISMOID_ENV={s.environment}: {', '.join(found)} still carry the dev default "
            "(or are empty). Set real secrets for them."
        )


def dev_secrets_in_use(s: Settings) -> list[str]:
    """The settings that still hold a dev default (or nothing) where the deployment uses them,
    by name."""
    found: list[str] = []
    worker = s.worker
    if worker.storage_backend == "s3" and worker.s3_secret_key.get_secret_value() == _DEV_S3_SECRET:
        found.append("PRISMOID_WORKER_S3_SECRET_KEY")
    for name, url in (
        ("PRISMOID_API_DATABASE_URL", s.api.database_url),
        ("PRISMOID_WORKER_DATABASE_URL", worker.database_url),
    ):
        if urlsplit(url).password == _DEV_DB_PASSWORD:
            found.append(name)
    # The frame secrets bind only a process told which frames to make: the shipped compose names the
    # driver on the worker alone, and the API (which makes no frames) leaves it at its default.
    driver = worker.frame_driver if "frame_driver" in worker.model_fields_set else None
    if driver == "docker" and s.egress_control_secret.get_secret_value() in ("", _DEV_EGRESS_SECRET):
        found.append("PRISMOID_EGRESS_CONTROL_SECRET")
    if driver == "sandbar" and not worker.sandbar_token.get_secret_value():
        found.append("PRISMOID_WORKER_SANDBAR_TOKEN")
    return found
