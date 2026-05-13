"""Abstract base agent."""

from __future__ import annotations

from abc import ABC, abstractmethod

from meshops_copilot.core.config import MeshOpsConfig
from meshops_copilot.core.models import SkillResult


class BaseAgent(ABC):
    """All agents inherit from this class."""

    def __init__(self, cfg: MeshOpsConfig) -> None:
        self.cfg = cfg

    @abstractmethod
    def run(self, **kwargs) -> list[SkillResult]:
        """Execute the agent and return a list of skill results."""
