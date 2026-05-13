"""DataHubDiscoverySkill — stub."""

from __future__ import annotations

from meshops_copilot.core.models import SkillResult
from meshops_copilot.skills.base import BaseSkill


class DataHubDiscoverySkill(BaseSkill):
    """Discover data products, golden reports, and duplicate dashboards via DataHub."""

    name = "datahub_discovery"

    def run(self, **kwargs) -> SkillResult:
        raise NotImplementedError("DataHubDiscoverySkill is not yet implemented.")
