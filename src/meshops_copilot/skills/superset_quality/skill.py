"""SupersetQualitySkill — stub."""

from __future__ import annotations

from meshops_copilot.core.models import SkillResult
from meshops_copilot.skills.base import BaseSkill


class SupersetQualitySkill(BaseSkill):
    """Lint Superset dashboards and detect noisy-neighbour patterns."""

    name = "superset_quality"

    def run(self, **kwargs) -> SkillResult:
        raise NotImplementedError("SupersetQualitySkill is not yet implemented.")
