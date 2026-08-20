"""Structured logging with secret scrubbing in the formatter.

Scrubbing lives in the formatter, not at call sites: an unhandled httpx exception will
otherwise print the full request including headers, and no amount of care at the places
we *do* log defends against the places we don't.
"""

from __future__ import annotations

import logging
import re
import sys

REDACTED = "***REDACTED***"

# Short values (a page size, "1", "true") would turn log lines into noise if redacted
# globally, and are not credentials worth protecting. The floor applies only to values
# read from a named env var — see `register_secret`.
MIN_SECRET_LENGTH = 8

# Vars named like a credential are exempt from the floor: a short dev password is exactly
# the kind that gets reused somewhere that matters.
CREDENTIAL_NAME_RE = re.compile(r"PASSWORD|PASS|TOKEN|SECRET|KEY", re.IGNORECASE)

_secrets: set[str] = set()


def register_secret(value: str, *, name: str | None = None) -> None:
    """Mark a value as never printable.

    `name` is the env var it came from, when it came from one. Values registered without a
    name are unconditional — the `login_token` bearer never passes through the environment
    and is the single most likely thing to leak.
    """
    if not value:
        return
    below_floor = len(value) < MIN_SECRET_LENGTH
    if name is not None and below_floor and not CREDENTIAL_NAME_RE.search(name):
        return
    _secrets.add(value)


def forget_secrets() -> None:
    """Test hook; the process otherwise keeps registered secrets for its lifetime."""
    _secrets.clear()


def scrub(text: str) -> str:
    for secret in _secrets:
        text = text.replace(secret, REDACTED)
    return text


class ScrubbingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return scrub(super().format(record))


def configure_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(ScrubbingFormatter("%(asctime)s %(levelname)-7s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
