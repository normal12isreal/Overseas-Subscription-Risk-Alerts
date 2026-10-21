from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Purpose(StrEnum):
    BACKGROUND = "background"
    APPEAL = "appeal"
    RESPONSE = "response"


class IncidentSeed(Model):
    source_ref: str = Field(min_length=1, max_length=300)
    service: str = Field(min_length=1, max_length=100)
    account_ref: str = Field(min_length=1, max_length=150)
    notice: str = Field(min_length=1, max_length=20000)
    service_status: Literal["scheduled_deactivation", "suspended", "verification_required"]
    effective_at: AwareDatetime | None = None
    simulated: bool = True

    @property
    def event_id(self) -> str:
        # One notice can identify several accounts. Never dedupe by service alone.
        key = "\0".join((self.source_ref, self.service.casefold(), self.account_ref))
        return "evt_" + sha256(key.encode("utf-8")).hexdigest()[:20]


class EventFacts(Model):
    title: str = Field(min_length=1, max_length=160)
    summary: str = Field(min_length=1, max_length=1800)
    known_facts: list[str] = Field(max_length=8)
    unknowns: list[str] = Field(max_length=8)


class ResearchTask(Model):
    purpose: Purpose
    query: str = Field(min_length=3, max_length=250)
    goal: str = Field(min_length=1, max_length=500)


class ResearchPlan(Model):
    tasks: list[ResearchTask] = Field(min_length=3, max_length=3)

    @model_validator(mode="after")
    def check_directions(self):
        if {task.purpose for task in self.tasks} != set(Purpose):
            raise ValueError("Exactly one task for each research purpose is required")
        return self


class Source(Model):
    source_id: str
    url: str
    title: str
    published_at: str | None = None
    video_id: str | None = None


class Segment(Model):
    text: str
    start: float = Field(ge=0)
    duration: float = Field(ge=0)


class Material(Model):
    source: Source
    status: Literal["success", "unavailable", "failed", "timed_out"]
    segments: list[Segment] = Field(default_factory=list)
    text: str = ""
    note: str = ""


class Collected(Model):
    purpose: Purpose
    query: str
    status: Literal["success", "empty", "failed", "timed_out"]
    materials: list[Material] = Field(default_factory=list)
    note: str = ""


class BriefContent(Model):
    summary: str = Field(min_length=1, max_length=2200)
    actions: list[str] = Field(max_length=8)
    source_ids: list[str] = Field(max_length=15)
    limitations: list[str] = Field(max_length=10)


class BranchBrief(Model):
    purpose: Purpose
    query: str
    status: Literal["success", "empty", "failed", "timed_out"]
    content: BriefContent
    sources: list[Source] = Field(default_factory=list)


class SolutionContent(Model):
    priority: Literal["ordinary", "urgent"]
    summary: str = Field(min_length=1, max_length=2200)
    reasons: list[str] = Field(max_length=8)
    actions: list[str] = Field(min_length=1, max_length=10)
    limitations: list[str] = Field(max_length=10)


class Solution(SolutionContent):
    source_ids: list[str] = Field(max_length=30)


class Delivery(Model):
    key: str
    status: Literal["staged", "sent", "failed"]
    location: str = ""
    error: str = ""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_time(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("An explicit timezone is required")
    return result.astimezone(timezone.utc)
