"""Top-level orchestration agent — runs all configured skills in sequence."""

from __future__ import annotations

from meshops_copilot.agents.base import BaseAgent
from meshops_copilot.core.models import SkillResult


class MeshOpsAgent(BaseAgent):
    """Orchestrates the full mesh health-check workflow."""

    def run(self, **kwargs) -> list[SkillResult]:
        # TODO: fan out to stress, observability, and governance agents
        raise NotImplementedError
