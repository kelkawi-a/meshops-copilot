"""Observability agent — grafana_diagnostics skill (stub)."""

from __future__ import annotations

from meshops_copilot.agents.base import BaseAgent
from meshops_copilot.core.models import SkillResult


class ObservabilityAgent(BaseAgent):
    def run(self, **kwargs) -> list[SkillResult]:
        raise NotImplementedError
