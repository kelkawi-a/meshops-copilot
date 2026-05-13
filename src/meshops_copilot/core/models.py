"""Shared domain models used across skills and agents."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class SkillStatus(str, Enum):
    OK = "ok"
    DEGRADED = "degraded"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class SkillResult:
    """Generic result envelope returned by every skill."""

    skill: str
    status: SkillStatus
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    summary: str = ""
    details: dict = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def elapsed_seconds(self) -> float | None:
        if self.finished_at:
            return (self.finished_at - self.started_at).total_seconds()
        return None
