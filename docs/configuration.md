# Configuration

## Sources (priority order)

1. Environment variables (highest)
2. Config YAML (`--config` flag or `MESHOPS_CONFIG` env var)
3. Built-in defaults (lowest)

## Config YAML

Copy `config/local.example.yaml` to `config/local.yaml` and edit as needed.
The file is gitignored.

```bash
cp config/local.example.yaml config/local.yaml
meshops --config config/local.yaml stress run --scenario scenarios/trino/light.yaml
```

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `TRINO_URL` | `http://localhost:8080` | Trino coordinator URL |
| `TRINO_USER` | `meshops` | Trino username |
| `SUPERSET_URL` | `http://localhost:8088` | Superset URL |
| `SUPERSET_USER` | `admin` | Superset username |
| `SUPERSET_PASSWORD` | `admin` | Superset password |
| `GRAFANA_URL` | `http://localhost:3000` | Grafana URL |
| `GRAFANA_TOKEN` | _(empty)_ | Grafana service account token |
| `DATAHUB_GMS_URL` | `http://localhost:8080` | DataHub GMS URL |
| `DATAHUB_TOKEN` | _(empty)_ | DataHub personal access token |
| `PROMETHEUS_URL` | `http://localhost:9090` | Prometheus URL |
| `LLM_PROVIDER` | `none` | `openai` \| `anthropic` \| `none` |
| `LLM_MODEL` | `gpt-4o` | Model name |
| `OPENAI_API_KEY` | _(empty)_ | OpenAI API key |
| `ANTHROPIC_API_KEY` | _(empty)_ | Anthropic API key |
| `MESHOPS_OUTPUT_DIR` | `./reports` | Directory for output files |
| `MESHOPS_LOG_LEVEL` | `INFO` | `DEBUG` \| `INFO` \| `WARNING` \| `ERROR` |
| `MESHOPS_CONFIG` | _(empty)_ | Path to config YAML |

## Scenario-level overrides

Any `trino:` block in a scenario YAML overrides the global config for that run:

```yaml
trino:
  url: http://my-trino:8080
  user: perf-test
  timeout: 300
```
