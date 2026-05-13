# Superset Stress Test — Analysis Prompt

You have been given the JSON output of a `superset_stress` skill run.

Analyse the results and produce a concise report covering:

1. **Baseline performance** — median and max latency per chart; flag any chart taking >5 s serially. Group by dashboard (eCommerce=charts 1–8, Analytics=charts 9–16, CRM=charts 17–27).
2. **Concurrency behaviour** — at what worker count does RPS plateau? When does p95 latency exceed 3× the serial median?
3. **Breaking point** — first concurrency level where errors appear; error rate at peak workers tested.
4. **Bottleneck identification** — correlate Superset CPU/memory peaks with latency spikes; distinguish Superset-side from Trino-side delays.
5. **Slowest charts** — list the top 3 charts by median latency and explain the likely cause (full-table scan, cross-catalog join, large result set, etc.).
6. **Top 3 recommended actions** — ordered by impact, with specific changes (e.g. add Redis cache, switch to Gunicorn, add Postgres index).

Be concise. Use markdown headers and bullet points. Include a one-sentence executive summary at the top.
