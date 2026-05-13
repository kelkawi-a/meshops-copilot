"""Skills package — auto-registers all skills."""

from meshops_copilot.skills.trino_stress.skill import TrinoStressSkill
from meshops_copilot.skills.superset_stress.skill import SupersetStressSkill
from meshops_copilot.skills.noisy_neighbor.skill import NoisyNeighborSkill

__all__ = ["TrinoStressSkill", "SupersetStressSkill", "NoisyNeighborSkill"]
