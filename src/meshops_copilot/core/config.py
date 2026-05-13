"""Central configuration loading.

Priority (highest → lowest):
  1. Environment variables  (including those loaded from a ``.env`` file)
  2. Scenario defaults (only for stress-test commands)
  3. Built-in dataclass defaults

A ``.env`` file in the current working directory (or any parent) is loaded
automatically via ``python-dotenv`` before env vars are read.  Variables
already present in the shell environment take precedence over the file, so
you can always override with ``export`` without editing the file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


@dataclass
class TrinoConfig:
    url: str = "http://localhost:8080"
    user: str = "meshops"
    password: str = ""          # empty = no auth; set for password-protected clusters
    verify_ssl: bool = True     # set False for self-signed TLS certificates
    timeout: int = 180
    results_file: str = "stress_results.json"


@dataclass
class SupersetConfig:
    url: str = "http://localhost:8088"
    user: str = "admin"
    password: str = "admin"
    discovery_enabled: bool = True    # False → skip live API scan, use built-in catalogue
    discovery_max_charts: int = 500   # cap on charts fetched during discovery


@dataclass
class GrafanaConfig:
    url: str = "http://localhost:3000"
    token: str = ""


@dataclass
class DataHubConfig:
    gms_url: str = "http://localhost:8080"
    token: str = ""


@dataclass
class PrometheusConfig:
    url: str = "http://localhost:9090"


@dataclass
class LLMConfig:
    provider: str = "none"          # openai | anthropic | openrouter | none
    model: str = "gpt-4o"
    api_key: str = ""


@dataclass
class OutputConfig:
    dir: Path = field(default_factory=lambda: Path("./reports"))
    log_level: str = "INFO"


@dataclass
class MeshOpsConfig:
    trino: TrinoConfig = field(default_factory=TrinoConfig)
    superset: SupersetConfig = field(default_factory=SupersetConfig)
    grafana: GrafanaConfig = field(default_factory=GrafanaConfig)
    datahub: DataHubConfig = field(default_factory=DataHubConfig)
    prometheus: PrometheusConfig = field(default_factory=PrometheusConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    output: OutputConfig = field(default_factory=OutputConfig)


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def load_config(
    scenario_defaults: dict[str, Any] | None = None,
) -> MeshOpsConfig:
    """Load config from environment variables and ``.env`` file.

    Priority (highest → lowest):
      1. Environment variables / .env file
      2. Scenario defaults (``scenario_defaults``) — optional, used by
         stress-test commands to inject connection settings from scenario
         YAML files.
      3. Built-in class-level defaults

    Searches for a ``.env`` file starting from the current directory up to
    the filesystem root and loads it before reading any env vars.  Variables
    already exported in the shell are never overwritten by the file.
    """
    # Load .env before reading os.environ so all vars are available below.
    # override=False means real shell exports always win over the file.
    load_dotenv(override=False)

    sd = scenario_defaults or {}
    cfg = MeshOpsConfig()

    # ── Trino ──────────────────────────────────────────────────────────────────
    st = sd.get("trino", {})
    cfg.trino.url = _env("TRINO_URL", st.get("url", cfg.trino.url))
    cfg.trino.user = _env("TRINO_USER", st.get("user", cfg.trino.user))
    cfg.trino.password = _env("TRINO_PASSWORD", st.get("password", cfg.trino.password))
    cfg.trino.timeout = int(_env("TRINO_TIMEOUT", str(st.get("timeout", cfg.trino.timeout))))

    # ── Superset ───────────────────────────────────────────────────────────────
    ss = sd.get("superset", {})
    cfg.superset.url = _env("SUPERSET_URL", ss.get("url", cfg.superset.url))
    cfg.superset.user = _env("SUPERSET_USER", ss.get("user", cfg.superset.user))
    cfg.superset.password = _env("SUPERSET_PASSWORD", ss.get("password", cfg.superset.password))
    _disc_enabled_env = _env("SUPERSET_DISCOVERY_ENABLED")
    if _disc_enabled_env:
        cfg.superset.discovery_enabled = _disc_enabled_env.lower() not in ("false", "0", "no")
    _disc_max_env = _env("SUPERSET_DISCOVERY_MAX_CHARTS")
    if _disc_max_env:
        cfg.superset.discovery_max_charts = int(_disc_max_env)

    # ── Grafana ────────────────────────────────────────────────────────────────
    cfg.grafana.url = _env("GRAFANA_URL", cfg.grafana.url)
    cfg.grafana.token = _env("GRAFANA_TOKEN", cfg.grafana.token)

    # ── DataHub ────────────────────────────────────────────────────────────────
    cfg.datahub.gms_url = _env("DATAHUB_GMS_URL", cfg.datahub.gms_url)
    cfg.datahub.token = (
        _env("DATAHUB_GMS_TOKEN", None)          # SDK's canonical name
        or _env("DATAHUB_TOKEN", None)           # alias used in .env
        or cfg.datahub.token
    )

    # ── Prometheus ─────────────────────────────────────────────────────────────
    cfg.prometheus.url = _env("PROMETHEUS_URL", cfg.prometheus.url)

    # ── LLM ────────────────────────────────────────────────────────────────────
    cfg.llm.provider = _env("LLM_PROVIDER", cfg.llm.provider)
    cfg.llm.model = _env("LLM_MODEL", cfg.llm.model)
    # Prefer the key that matches the chosen provider; fall back to the others
    # so a single LLM_API_KEY or any provider key works as a catch-all.
    if cfg.llm.provider == "openrouter":
        cfg.llm.api_key = (
            _env("OPENROUTER_API_KEY")
            or _env("OPENAI_API_KEY")
            or _env("ANTHROPIC_API_KEY")
        )
    elif cfg.llm.provider == "anthropic":
        cfg.llm.api_key = (
            _env("ANTHROPIC_API_KEY")
            or _env("OPENAI_API_KEY")
            or _env("OPENROUTER_API_KEY")
        )
    else:  # openai or unknown
        cfg.llm.api_key = (
            _env("OPENAI_API_KEY")
            or _env("ANTHROPIC_API_KEY")
            or _env("OPENROUTER_API_KEY")
        )

    # ── Output ─────────────────────────────────────────────────────────────────
    cfg.output.dir = Path(_env("MESHOPS_OUTPUT_DIR", str(cfg.output.dir)))
    cfg.output.log_level = _env("MESHOPS_LOG_LEVEL", cfg.output.log_level)

    return cfg
