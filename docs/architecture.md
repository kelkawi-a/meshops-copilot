# Architecture

## Overview

```
meshops-copilot
├── CLI (Click)          ← user entry point
├── Agents               ← orchestrate one or more skills
│   ├── StressAgent
│   ├── ObservabilityAgent
│   └── GovernanceAgent
├── Skills               ← atomic, testable units of work
│   ├── trino_stress     ← IMPLEMENTED
│   ├── superset_stress  ← stub
│   ├── grafana_diagnostics ← stub
│   ├── datahub_discovery   ← stub
│   ├── superset_quality    ← stub
│   └── report_writer       ← stub
├── Connectors           ← thin HTTP/SDK clients
│   ├── TrinoConnector   ← IMPLEMENTED (stdlib urllib)
│   ├── SupersetConnector
│   ├── GrafanaMCPConnector
│   ├── DataHubMCPConnector
│   ├── PrometheusConnector
│   └── KubernetesConnector
└── Core
    ├── config.py        ← YAML + env-var loading
    ├── models.py        ← shared SkillResult envelope
    ├── errors.py        ← exception hierarchy
    ├── logging.py       ← Rich-backed structured logging
    └── llm.py           ← optional OpenAI / Anthropic client
```

## Data Flow

```
CLI command
  → load_config()
  → Agent.run(scenario)
    → Skill.run()
      → Connector.execute()
      → parse + aggregate results
      → return SkillResult
  → output formatter (console / markdown / JSON)
```

## Skill Contract

Every skill:
1. Accepts configuration via its constructor (connector config, options).
2. Exposes a single `run(**kwargs) -> SkillResult` method.
3. Returns a `SkillResult` with `status`, `summary`, `details`, and `errors`.
4. Is independently testable with fixture data — no live service required for unit tests.

## Scenario YAML

Trino stress scenarios live in `scenarios/trino/`. The schema is:

```yaml
name: <string>
description: <string>
trino:
  url: <string>
  user: <string>
  timeout: <int>
queries:                  # optional — omit to use all built-ins
  - light_count
  - heavy_join
phases:
  warmup:      { enabled: bool, queries: [...] }
  baseline:    { enabled: bool, runs: int }
  concurrency_ramp: { enabled: bool, query: str, levels: [...] }
  mixed_workload:   { enabled: bool, workers_per_query: int }
  memory_pressure:  { enabled: bool, queries: [...], workers: int }
  breaking_point:   { enabled: bool, query: str, levels: [...], stop_at_error_rate: int }
```

## Adding a New Skill

1. Create `src/meshops_copilot/skills/<skill_name>/` with `skill.py`, `models.py`, and any helpers.
2. Inherit from `BaseSkill` and implement `run()`.
3. Add a connector under `connectors/` if a new service is needed.
4. Register the skill in `skills/__init__.py`.
5. Add a CLI sub-command under `cli/commands/`.
6. Add unit tests under `tests/unit/`.
