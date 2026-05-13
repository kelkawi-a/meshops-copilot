"""Abstract base skill."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone

from meshops_copilot.core.models import SkillResult, SkillStatus


class BaseSkill(ABC):
    """Every skill inherits from this class and implements ``run()``."""

    #: Human-readable name used in logs and reports.
    name: str = "base"

    @abstractmethod
    def run(self, **kwargs) -> SkillResult:
        """Execute the skill and return a result envelope."""

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _ok(self, summary: str = "", details: dict | None = None) -> SkillResult:
        return SkillResult(
            skill=self.name,
            status=SkillStatus.OK,
            finished_at=datetime.now(timezone.utc),
            summary=summary,
            details=details or {},
        )

    def _failed(self, errors: list[str], details: dict | None = None) -> SkillResult:
        return SkillResult(
            skill=self.name,
            status=SkillStatus.FAILED,
            finished_at=datetime.now(timezone.utc),
            errors=errors,
            details=details or {},
        )

    def _degraded(self, summary: str, errors: list[str], details: dict | None = None) -> SkillResult:
        return SkillResult(
            skill=self.name,
            status=SkillStatus.DEGRADED,
            finished_at=datetime.now(timezone.utc),
            summary=summary,
            errors=errors,
            details=details or {},
        )
