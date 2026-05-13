"""Skills package — auto-registers all skills."""

from meshops_copilot.skills.trino_stress.skill import TrinoStressSkill
from meshops_copilot.skills.superset_stress.skill import SupersetStressSkill

__all__ = ["TrinoStressSkill", "SupersetStressSkill"]
