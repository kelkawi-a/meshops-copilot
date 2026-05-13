# Skills Reference

## trino_stress

**Status:** Implemented

Runs a multi-phase load test against a Trino cluster using the v1 REST API.

### Phases

| Phase | Description |
|---|---|
| `warmup` | Discarded runs to warm the JIT |
| `baseline` | Serial execution, N runs per query type |
| `concurrency_ramp` | Increase concurrent workers on a single query |
| `mixed_workload` | All query types running concurrently |
| `memory_pressure` | High-memory queries at fixed concurrency |
| `breaking_point` | Push concurrency until errors appear |

### Usage

```bash
meshops stress run --scenario scenarios/trino/high_concurrency.yaml
```

### Scenario keys

See `docs/architecture.md` → Scenario YAML.

---

## superset_stress

**Status:** Stub — not yet implemented.

Planned: simulate Superset dashboard refresh cycles by hitting the chart data API concurrently.

---

## grafana_diagnostics

**Status:** Stub — not yet implemented.

Planned: query Prometheus via the Grafana MCP connector, detect CPU/memory/IO bottlenecks.

---

## datahub_discovery

**Status:** Stub — not yet implemented.

Planned: search DataHub for data products, golden reports, and duplicate dashboards.

---

## superset_quality

**Status:** Stub — not yet implemented.

Planned: lint Superset dashboards for anti-patterns; detect noisy-neighbour charts.

---

## report_writer

**Status:** Stub — not yet implemented.

Planned: compile all skill results into the MeshOps Copilot Report (Markdown + JSON).
