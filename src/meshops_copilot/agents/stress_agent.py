"""Stress-testing agent — runs trino_stress and superset_stress skills."""

from __future__ import annotations

from meshops_copilot.agents.base import BaseAgent
from meshops_copilot.core.models import SkillResult
from meshops_copilot.skills.trino_stress.skill import TrinoStressSkill


class StressAgent(BaseAgent):
    """Runs stress skills and aggregates their results."""

    def run(self, scenario_path: str | None = None, **kwargs) -> list[SkillResult]:
        trino_skill = TrinoStressSkill(self.cfg.trino)
        return [trino_skill.run(scenario_path=scenario_path)]
