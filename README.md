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

You can also set non-secret defaults inline in the scenario's `superset:` block (lowest priority — `.env` and env vars always win):

```yaml
# scenarios/superset/my_instance.yaml
superset:
  url: https://superset.company.com
  user: analyst
  # Omit password here — set SUPERSET_PASSWORD in .env instead
```

#### Discovery mode

When no `charts:` key is present and `discovery: enabled: true` (the default), the skill
queries the live Superset API, enumerates all charts across all dashboards, builds a
`query_context` for each one, and uses the result as the chart catalogue — no prior
knowledge of the schema required.

```bash
# Auto-discover all charts on any Superset instance, then stress-test them
meshops stress superset --scenario scenarios/superset/discovery.yaml
```

To use discovery against a different Superset instance, create a minimal scenario file:

```yaml
# scenarios/superset/my_instance.yaml
name: my_instance
superset:
  url: https://superset.company.com
  user: analyst
  # Set SUPERSET_PASSWORD in .env — it takes priority over this block

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

#### Test phases

| Phase | What it measures |
|---|---|
| Warmup | Cache warm-up — fires nominated charts once (results discarded) |
| Baseline | Serial latency per chart (configurable runs, default 3) |
| Concurrency ramp | RPS, p50/p95/p99 as workers scale through configurable levels |
| Breaking point | Pushes concurrency until the error rate exceeds a threshold |

The benchmark chart for concurrency ramp and breaking point is auto-selected as
the slowest chart from the baseline phase (by median latency) unless explicitly
set with `chart:` in the scenario YAML.

#### Output

Results are written to `superset_stress_results.json` by default. Override with `--output`:

```bash
meshops stress superset --scenario scenarios/superset/workshop.yaml \
  --output reports/superset_run.json
```

---

### `grafana_diagnostics` — Prometheus metrics and Loki log analysis

Analyses Prometheus metrics **and** Loki logs to diagnose performance issues across the data-mesh platform. Automatically discovers application-specific metrics for the target component (e.g. `superset_query_duration_seconds`, `trino_execution_time_seconds`) rather than relying on generic container metrics alone. Accepts natural-language queries.

#### Prerequisites

1. A Grafana instance with Prometheus and/or Loki datasources (or direct URLs).
2. A Grafana service account token (recommended — auto-discovers datasources and proxies queries).
3. (Optional) An LLM API key for natural-language query interpretation and answer synthesis.

#### Setup

Set the connection variables in your `.env` file (or export them):

```bash
# Grafana (recommended — auto-discovers Prometheus + Loki datasources)
GRAFANA_URL=https://grafana.company.com
GRAFANA_TOKEN=glsa_xxxxxxxxxxxxxxxxxxxx

# Or direct Prometheus URL (fallback when Grafana is not available)
PROMETHEUS_URL=http://localhost:9090

# Optional — enables natural-language query interpretation
OPENAI_API_KEY=sk-...
LLM_PROVIDER=openai
LLM_MODEL=gpt-4o
```

#### Quick start

```bash
# Ask about a specific component — discovers its metrics + queries logs:
meshops diagnose run "why was superset slow between 10:30 and 11:00"

# Focus on specific concerns:
meshops diagnose run "check CPU and memory pressure on trino pods"

# Look for errors:
meshops diagnose run "are there any errors in superset logs in the last 30 minutes"

# Full diagnostic sweep (no LLM needed — runs all categories over the last hour):
meshops diagnose run

# Write results to JSON:
meshops diagnose run "is there disk spill happening?" --output reports/diagnose.json
```

#### How it works

1. **Query interpretation** — the LLM extracts structured parameters (time window, component, categories) from your question. Without an LLM, keyword-based heuristics are used.
2. **Metric discovery** — queries Prometheus for all metric names related to the target component, classifies them into categories (CPU, memory, latency, errors, throughput, queue, network, disk), and generates targeted PromQL queries from what's actually available.
3. **Log analysis** — queries Loki (via Grafana proxy) for error and warning log lines matching the target component, computes error rates, and extracts log patterns.
4. **Bottleneck detection** — ranks issues by severity (Critical / High / Medium) from both metrics and logs.
5. **LLM synthesis** — synthesises the combined metric and log data into a concise answer.

#### Metric categories

| Category | What it finds |
|---|---|
| `cpu` | Container/process CPU usage, throttling |
| `memory` | Working set, heap, GC pauses, OOM events |
| `latency` | Request/query duration histograms (p95, avg) |
| `errors` | Error counters, HTTP 5xx rates |
| `throughput` | Request/query rates, rows processed |
| `queue` | Queue depth, in-flight queries, thread pools |
| `network` | Network receive/transmit rates, errors |
| `disk` | Disk read/write IOPS and throughput |

The tool discovers which of these exist for the target component rather than assuming fixed metric names.

#### CLI options

| Flag | Description |
|---|---|
| `QUERY` (positional) | Natural-language question (optional) |
| `--output PATH` | Write JSON results to a file |
| `--namespace REGEX` | Kubernetes namespace filter (default: all) |
| `--window MINUTES` | Analysis window override (default: 60 or auto-detected) |
| `--url URL` | Prometheus URL override |

#### Without an LLM

The tool works without an LLM configured — it runs all diagnostic categories over the last hour and prints the bottleneck table directly. The LLM adds:
- Smart time-window extraction from your question
- Component targeting (e.g. "superset" → filters metrics and logs)
- Natural-language answer synthesis

```bash
# No LLM required — runs a full sweep:
meshops diagnose run

# Filter by namespace without LLM:
meshops diagnose run --namespace "superset.*" --window 30
```

---

### `noisy_neighbor` — Superset noisy-neighbor detector

Identifies dashboards, charts, users, and databases that cause disproportionate
Trino query load relative to their share of Superset activity.

Example finding:
> Dashboard "Sales Pipeline" accounts for 4% of views but 38% of Trino query time (noise ratio: 9.5x)

#### How it works

1. **Collect** — fetches Superset query history (`/api/v1/query/`), activity logs
   (`/api/v1/log/`), and Trino query stats (`system.runtime.queries`).
2. **Correlate** — joins Superset queries to Trino executions via the `tracking_url`
   (contains the Trino `query_id`). Uncorrelated Trino queries with
   `source = 'Apache Superset'` are still included for user-level analysis.
3. **Analyze** — computes a **noise ratio** per entity per dimension:
   `noise_ratio = cost_share / activity_share`. A ratio of 1.0 is proportionate;
   ≥ 1.5 is moderate; ≥ 3.0 is critical.

#### Dimensions

| Dimension | Activity metric | Cost metric |
|---|---|---|
| `user` | Query count | Trino query duration |
| `database` | Query count per Superset database | Trino query duration |
| `dashboard` | View count (from log) | Log duration_ms |
| `chart` | Render count (from log) | Log duration_ms |
| `time_of_day` | Hourly query count | Trino query duration per hour |

#### Quick start

```bash
# Analyze the last 7 days (default)
meshops diagnose noisy-neighbor

# Analyze the last 24 hours only
meshops diagnose noisy-neighbor --lookback 24

# Write results to a custom path
meshops diagnose noisy-neighbor --output reports/noisy.json
```

#### Configuration

Uses the same `.env` / config YAML for connection details:

```bash
SUPERSET_URL=https://superset.company.com
SUPERSET_USER=analyst
SUPERSET_PASSWORD=secret
TRINO_URL=https://trino.company.com
TRINO_USER=meshops
TRINO_PASSWORD=secret
```

#### CLI options

| Flag | Description |
|---|---|
| `--lookback HOURS` | How many hours of history to analyze (default: 168 = 7 days) |
| `--output PATH` | Write JSON results to a file (default: `noisy_neighbor_results.json`) |

#### Output

Results are saved as JSON and printed as Rich tables to the terminal. Each
dimension table shows entity name, activity count/share, cost in seconds/share,
noise ratio, and severity classification.

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
