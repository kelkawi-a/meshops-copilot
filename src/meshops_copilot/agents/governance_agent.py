"""Governance agent — datahub_discovery and superset_quality skills (stub)."""

from __future__ import annotations

from meshops_copilot.agents.base import BaseAgent
from meshops_copilot.core.models import SkillResult


class GovernanceAgent(BaseAgent):
    def run(self, **kwargs) -> list[SkillResult]:
        raise NotImplementedError
