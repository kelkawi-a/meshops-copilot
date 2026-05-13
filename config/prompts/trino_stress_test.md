# Trino Stress Test — Analysis Prompt

You have been given the JSON output of a `trino_stress` skill run.

Analyse the results and produce a concise report covering:

1. **Baseline performance** — median and max latency per query type; highlight any query taking >5s serially.
2. **Concurrency behaviour** — at what worker count does QPS plateau? When does p95 latency exceed 3× the serial baseline?
3. **Error profile** — first concurrency level where errors appear; error rate at the breaking point.
4. **Memory pressure** — peak JVM heap utilisation; any OOM risk.
5. **Cross-catalog joins** — latency trend and whether broadcast join hints are advisable.
6. **Top 3 recommended actions** — ordered by impact, with specific config changes or query rewrites.

Be concise. Use markdown headers and bullet points. Include a one-sentence executive summary at the top.
