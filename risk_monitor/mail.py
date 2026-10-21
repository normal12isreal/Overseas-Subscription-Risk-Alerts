"""Mailbox input and risk detector contracts, independent of Gmail transport."""

from datetime import datetime, timedelta
from hashlib import sha256
from typing import Literal

from pydantic import AwareDatetime, Field

from .domain import Model


class MailMessage(Model):
    mailbox: str  # Hash of the authenticated account, not the email address.
    message_id: str
    received_at: AwareDatetime
    sender: str
    subject: str
    body: str
    truncated: bool = False

    @property
    def fingerprint(self) -> str:
        # Different messages with identical sender/subject/content are duplicates.
        # Account boundary is retained in the DB key; no fuzzy semantic merging.
        text = "\0".join((self.sender.strip().casefold(), self.subject.strip(), self.body))
        return sha256(text.encode("utf-8")).hexdigest()


class RiskCandidate(Model):
    service: str = Field(min_length=1, max_length=100)
    account_ref: str = Field(default="unspecified", min_length=1, max_length=150)
    service_status: Literal["scheduled_deactivation", "suspended", "cancelled", "verification_required"]
    effective_at: AwareDatetime | None = None
    evidence: str = Field(min_length=5, max_length=1200)


class MailAnalysis(Model):
    # One email may refer to several independent services/accounts.
    risks: list[RiskCandidate] = Field(max_length=10)


def window_start(now: datetime) -> datetime:
    return now - timedelta(hours=2)
