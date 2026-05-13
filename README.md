# MeshOps Copilot

AI-assisted data mesh operations — stress testing, observability diagnostics, data discovery, and reporting.

## Installation

Requires Python 3.12 and [uv](https://github.com/astral-sh/uv) (or pip).

```bash
git clone https://github.com/your-org/meshops-copilot
cd meshops-copilot
uv venv
uv pip install -e .           # runtime only
uv pip install -e ".[dev]"    # includes pytest, ruff, mypy
```

---

## Configuration

Configuration is loaded in priority order: **CLI flags → environment variables → config YAML → defaults**.

### `.env` file (recommended)

Copy the example and fill in your values — the file is loaded automatically on every `meshops` invocation:

```bash
cp .env.example .env
$EDITOR .env
```

Variables already exported in your shell always take precedence over the file, so you can override individual values with `export` without editing it.

### Config YAML (optional)

For per-environment settings, copy the example config and edit it:

```bash
cp config/local.example.yaml config/local.yaml
$EDITOR config/local.yaml
```

Pass it with the `--config` flag **before** the subcommand — it is a root-level flag that applies to all commands:

```bash
# Trino stress test using local.yaml for connection details
meshops --config config/local.yaml stress run --scenario scenarios/trino/light.yaml

# Superset stress test
meshops --config config/local.yaml stress superset --scenario scenarios/superset/workshop.yaml

# Any other subcommand follows the same pattern
meshops --config config/local.yaml diagnose run
meshops --config config/local.yaml report run --output reports/
```

Alternatively, point at the file with an environment variable so you never have to type the flag:

```bash
export MESHOPS_CONFIG=config/local.yaml
meshops stress run --scenario scenarios/trino/light.yaml
```

The YAML is merged with lower priority than environment variables, so you can keep shared settings in `local.yaml` and still override individual values inline or via `.env`.

### Environment variable reference

| Variable | Description |
|---|---|
| `TRINO_URL` | Trino coordinator URL |
| `TRINO_USER` | Trino username |
| `TRINO_PASSWORD` | Trino password (Basic Auth) |
| `SUPERSET_URL` | Superset base URL |
| `SUPERSET_USER` | Superset username |
| `SUPERSET_PASSWORD` | Superset password |
| `SUPERSET_DISCOVERY_ENABLED` | `true` / `false` — enable/disable live chart discovery (default `true`) |
| `SUPERSET_DISCOVERY_MAX_CHARTS` | Max charts to fetch during discovery (default `500`) |
| `MESHOPS_CONFIG` | Path to config YAML |
| `MESHOPS_OUTPUT_DIR` | Directory for output files (default: `./reports`) |
| `MESHOPS_LOG_LEVEL` | `DEBUG` / `INFO` / `WARNING` (default: `INFO`) |
| `OPENAI_API_KEY` | OpenAI key for LLM-enriched reports (optional) |
| `ANTHROPIC_API_KEY` | Anthropic key for LLM-enriched reports (optional) |

See `.env.example` for the full list and `docs/configuration.md` for detailed explanations.

---

## Skills

### `trino_stress` — Trino stress tester

Runs a multi-phase load test against any Trino deployment. By default, the skill **dynamically discovers the target schema** via `information_schema`, detects join relationships, and synthesises appropriate queries — no prior knowledge of the schema required.

#### Quick start

```bash
# Dynamic discovery — works against any Trino deployment
meshops stress run --scenario scenarios/trino/discovery.yaml

# Workshop schema (hardcoded queries for the local demo cluster)
meshops stress run --scenario scenarios/trino/high_concurrency.yaml
```

#### Authentication

```bash
# Recommended: password via environment variable
export TRINO_PASSWORD=secret
meshops stress run --scenario scenarios/trino/discovery.yaml

# Or inline (appears in shell history — use with care)
meshops stress run --scenario scenarios/trino/light.yaml \
  --url https://my-trino:8443 --user alice --password secret

# Self-signed TLS certificate
meshops stress run --scenario scenarios/trino/discovery.yaml \
  --url https://my-trino:8443 --no-verify-ssl
```

#### Connection override flags

| Flag | Env var | Description |
|---|---|---|
| `--url` | `TRINO_URL` | Coordinator URL |
| `--user` | `TRINO_USER` | Username |
| `--password` | `TRINO_PASSWORD` | Password for Basic Auth |
| `--no-verify-ssl` | — | Disable TLS certificate verification |

#### Scenario YAML

All connection details and phase config can also live in the scenario file:

```yaml
# scenarios/trino/my_cluster.yaml
name: my_cluster
trino:
  url: https://my-trino.company.com:8443
  user: alice
  password: "${TRINO_PASSWORD}"   # resolved from env at runtime
  verify_ssl: true
  timeout: 300

# Omit 'queries:' to trigger dynamic schema discovery
discovery:
  enabled: true
  max_tables: 50

phases:
  baseline:    { enabled: true, runs: 5 }
  concurrency_ramp: { enabled: true, query: heavy_join, levels: [1,2,4,8,16,32] }
  breaking_point:   { enabled: true, query: heavy_join, levels: [32,48,64,96] }
```

```bash
meshops stress run --scenario scenarios/trino/my_cluster.yaml
```

#### Built-in scenarios

| Scenario | Description |
|---|---|
| `scenarios/trino/light.yaml` | Connectivity check — baseline only, two queries |
| `scenarios/trino/dashboard_like.yaml` | Simulates BI dashboard refresh patterns |
| `scenarios/trino/high_concurrency.yaml` | Full ramp + breaking point (uses discovery) |
| `scenarios/trino/long_running.yaml` | Memory and latency under sustained heavy queries |
| `scenarios/trino/discovery.yaml` | Fully dynamic — adapts to any schema |

#### Test phases

| Phase | What it measures |
|---|---|
| Warmup | JIT warm-up (results discarded) |
| Baseline | Serial latency per query type (5 runs) |
| Concurrency ramp | QPS and latency as workers scale 1 → 32 |
| Mixed workload | All query types running simultaneously |
| Memory pressure | Peak heap usage under concurrent heavy queries |
| Breaking point | Pushes concurrency to failure to find the error threshold |

#### Output

Results are written to `stress_results.json` by default. Override with `--output`:

```bash
meshops stress run --scenario scenarios/trino/high_concurrency.yaml \
  --output reports/my_run.json
```

---

### `superset_stress` — Superset dashboard stress tester

Stress-tests Superset by firing concurrent `POST /api/v1/chart/data` requests
across all charts in one or more dashboards. Measures per-chart serial latency
(baseline), RPS and p50/p95/p99 under concurrency ramp, and the concurrency
level where errors first appear (breaking point).

#### Quick start

```bash
# Full test — all 27 workshop charts, default phases
meshops stress superset --scenario scenarios/superset/workshop.yaml

# Concurrency-focused — slowest 3 charts only, higher worker levels
meshops stress superset --scenario scenarios/superset/concurrency.yaml

# Write results to a custom path
meshops stress superset --scenario scenarios/superset/workshop.yaml \
  --output reports/superset_run.json
```

#### Connection details

Connection details default to `http://localhost:8088` / `admin` / `admin`.
Override via `.env`, environment variables, config YAML, or the scenario's `superset:` block — in that priority order.

**`.env` file (recommended)**

```bash
SUPERSET_URL=https://superset.company.com
SUPERSET_USER=analyst
SUPERSET_PASSWORD=secret
```

Place this in your project root (or any parent directory). The file is loaded automatically on every `meshops` invocation. Variables already exported in your shell always win over the file.

| Env var | Description |
|---|---|
| `SUPERSET_URL` | Superset base URL |
| `SUPERSET_USER` | Username |
| `SUPERSET_PASSWORD` | Password |
| `SUPERSET_DISCOVERY_ENABLED` | `true` / `false` — enable/disable live chart discovery (default `true`) |
| `SUPERSET_DISCOVERY_MAX_CHARTS` | Max charts to fetch during discovery (default `500`) |

You can also set them inline in the scenario's `superset:` block (lowest priority, useful for committing non-secret defaults):

```yaml
# scenarios/superset/my_instance.yaml
superset:
  url: https://superset.company.com
  user: analyst
  password: "${SUPERSET_PASSWORD}"   # still resolved from env at runtime
```

#### Discovery mode

When no `charts:` key is present and `discovery: enabled: true` (the default), the skill
queries the live Superset API, enumerates all charts across all dashboards, builds a
`query_context` for each one, and uses the result as the chart catalogue — no prior
knowledge of the schema required.

```bash
# Auto-discover all charts on any Superset instance, then stress-test them
meshops stress superset --scenario scenarios/superset/discovery.yaml

# Or pass connection details inline without a scenario file
meshops stress superset --scenario scenarios/superset/discovery.yaml \
  --scenario scenarios/superset/discovery.yaml   # reuse discovery.yaml as template
```

To use discovery against a different Superset instance, create a minimal scenario file:

```yaml
# scenarios/superset/my_instance.yaml
name: my_instance
superset:
  url: https://superset.company.com
  user: analyst
  password: "${SUPERSET_PASSWORD}"

discovery:
  enabled: true
  max_charts: 100   # cap to keep the run manageable (default 500)

phases:
  baseline:  { enabled: true, runs: 2 }
  concurrency_ramp:
    enabled: true
    levels: [1, 2, 4, 8]
    # 'chart' omitted → auto-selected as slowest chart from baseline
  breaking_point: { enabled: false }
```

```bash
meshops stress superset --scenario scenarios/superset/my_instance.yaml
```

To skip discovery and use the built-in 27-chart workshop catalogue instead, set
`discovery: enabled: false` (or provide an explicit `charts:` / `dashboard_id:` key).

#### Chart resolution priority

| Priority | Trigger | Source label |
|---|---|---|
| 1 | `charts:`, `charts_file:`, or `dashboard_id:` present | `scenario` / `file` / `dashboard` |
| 2 | Neither of the above **and** `discovery: enabled: true` | `discovered` |
| 3 | Discovery disabled or returns no usable charts | `builtin` (27 workshop charts) |

#### Scenario YAML

```yaml
name: my_scenario
superset:
  url: http://localhost:8088

# Chart selection (pick one approach):
#   charts: [name, …]          list of built-in chart keys
#   charts_file: path          load a JSON catalogue file
#   dashboard_id: 1            restrict built-ins to one dashboard
#                              (1=eCommerce, 2=Analytics, 3=CRM)
#   discovery: enabled: true   query the live API (default when no charts: key)
#   discovery: enabled: false  skip discovery, fall back to 27 built-in charts

phases:
  warmup:    { enabled: true }
  baseline:  { enabled: true, runs: 3 }
  concurrency_ramp:
    enabled: true
    chart: daily_active_sessions   # omit to auto-select slowest from baseline
    levels: [1, 2, 4, 8, 16]
  breaking_point:
    enabled: true
    chart: daily_active_sessions
    levels: [16, 24, 32, 48]
    stop_at_error_rate: 50
```

#### Built-in scenarios

| Scenario | Description |
|---|---|
| `scenarios/superset/workshop.yaml` | All 27 workshop charts, full phase set (built-in catalogue) |
| `scenarios/superset/concurrency.yaml` | Slowest 3 charts, aggressive concurrency ramp |
| `scenarios/superset/discovery.yaml` | Discovery mode — adapts to any Superset instance |

#### Output

Results are written to `superset_stress_results.json` by default. Override with `--output`.

---

### `grafana_diagnostics` — Prometheus metrics analysis via Grafana MCP

Analyses Prometheus metrics through the Grafana MCP server to diagnose CPU saturation, memory pressure, query bottlenecks, network and disk I/O issues across the data-mesh platform.

#### Prerequisites

1. A running Grafana instance (v9.0+) with at least one Prometheus datasource configured.
2. A Grafana service account token with `datasources:read` and `datasources:query` permissions.
3. [uv](https://docs.astral.sh/uv/getting-started/installation/) installed (used to run the MCP server via `uvx`).
4. [OpenCode](https://opencode.ai) installed.

#### Setup

Set the Grafana connection variables in your `.env` file (or export them):

```bash
GRAFANA_URL=http://localhost:3000
GRAFANA_TOKEN=glsa_xxxxxxxxxxxxxxxxxxxx
```

The project includes an `opencode.json` that configures the [Grafana MCP server](https://github.com/grafana/mcp-grafana) automatically. It reads `GRAFANA_URL` and `GRAFANA_TOKEN` from your environment.

#### Usage with OpenCode

Start OpenCode in the project root:

```bash
cd meshops-copilot
opencode
```

Then use the skill by asking the agent to analyse Prometheus metrics:

```
Analyse Prometheus metrics for the last hour and identify the top bottlenecks
```

```
Use the prometheus-analysis skill to diagnose CPU and memory pressure across all pods
```

```
Run a diagnostics report on query queue depth and network I/O from Grafana
```

The agent will automatically load the `prometheus-analysis` skill, which walks it through:

1. Discovering available Prometheus datasources via `grafana_list_datasources`
2. Listing available metric names and label dimensions
3. Executing PromQL queries for CPU, memory, query queue, network, and disk I/O
4. Computing latency histogram percentiles (p50/p90/p95/p99)
5. Producing a structured report with a bottleneck summary and recommended actions

#### CLI (planned)

The programmatic `meshops diagnose run` command is not yet implemented. For now, use the OpenCode skill workflow above.

```bash
# Coming soon
meshops diagnose run
```

---

### `datahub_discovery` — Data product and governance discovery

> **Status: not yet implemented.**

Planned: search DataHub for candidate data products, golden reports, and duplicate dashboards. Scores assets by usage, ownership completeness, and downstream adoption.

```bash
# Coming soon
meshops discover run
```

---

### `superset_quality` — Dashboard quality analysis

> **Status: not yet implemented.**

Planned: lint Superset dashboards for anti-patterns (missing filters, unbounded queries, excessive chart count) and detect noisy-neighbour charts that consume disproportionate query resources.

```bash
# Coming soon
meshops diagnose run
```

---

### `report_writer` — MeshOps Copilot Report generation

> **Status: not yet implemented.**

Planned: compile results from all skills into the structured report below, optionally enriched with LLM-generated narrative using the configured provider (`OPENAI_API_KEY` / `ANTHROPIC_API_KEY`).

```bash
# Coming soon
meshops report run --output reports/
```

---

## Development

```bash
make install-dev   # install with dev dependencies
make test          # run unit tests
make lint          # ruff check
make fmt           # ruff format
make typecheck     # mypy
```

Adding a new skill: see `docs/architecture.md`.
