# Demo Script

## Prerequisites

- Docker + Docker Compose running with Trino + PostgreSQL (see workshop setup)
- `uv` or `pip` with Python 3.12
- `meshops` CLI installed: `uv pip install -e ".[dev]"`

## 1. Verify connectivity

```bash
meshops --log-level DEBUG stress run --scenario scenarios/trino/light.yaml
```

Expected: all phases complete, results written to `stress_results.json`.

## 2. Run the dashboard-like workload

```bash
meshops stress run --scenario scenarios/trino/dashboard_like.yaml \
  --output reports/dashboard_like.json
```

## 3. Run the full high-concurrency stress test

```bash
meshops stress run --scenario scenarios/trino/high_concurrency.yaml \
  --output reports/high_concurrency.json
```

Point out in the output:
- Baseline latency per query type
- QPS plateau in the concurrency ramp
- First concurrency level with errors

## 4. Run the long-running / memory-pressure scenario

```bash
meshops stress run --scenario scenarios/trino/long_running.yaml \
  --output reports/long_running.json
```

## 5. (Future) Full mesh health check

```bash
meshops --config config/demo.yaml diagnose run
meshops --config config/demo.yaml discover run
meshops --config config/demo.yaml report run --output reports/
```
