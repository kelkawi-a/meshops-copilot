"""Custom exception hierarchy."""

from __future__ import annotations


class MeshOpsError(Exception):
    """Base exception for all meshops-copilot errors."""


class ConfigError(MeshOpsError):
    """Invalid or missing configuration."""


class ConnectorError(MeshOpsError):
    """Could not reach a remote service (Trino, Superset, etc.)."""


class SkillError(MeshOpsError):
    """A skill failed to execute."""


class ScenarioError(MeshOpsError):
    """A scenario YAML is malformed or references unknown queries."""
