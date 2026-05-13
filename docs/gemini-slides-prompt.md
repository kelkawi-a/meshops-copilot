# Gemini Slides Prompt — meshops-copilot

> Paste everything below the horizontal rule directly into Gemini.

---

Create a slide deck for the following data platform engineering project. Use a clean, technical but accessible style. Each slide should have a title, 3–5 bullet points, and a one-line speaker note.

---

## Project: meshops-copilot

A Python CLI tool (`meshops`) that brings AI-assisted intelligence to data mesh operations. It connects to live infrastructure — DataHub, Superset, Trino, Grafana — via MCP (Model Context Protocol) and REST APIs, collects signals, scores them with weighted heuristics, and produces actionable Markdown reports. An optional LLM layer (OpenAI / Anthropic / OpenRouter) adds natural-language justifications and executive summaries. All skills are read-only against production systems; outputs are local report files only.

**Tech stack:** Python 3.12, `uv`/`hatchling`, `mcp` SDK, `rich`, `click`, stdlib `urllib` only for HTTP (no heavy clients).

---

## Skills

### 1. Trino Stress Tester
Runs configurable workload scenarios (baseline, concurrency ramp, breaking-point search) against a Trino cluster using raw HTTP. Discovers schema automatically and generates queries from it. Measures p50/p95 latency, failure rates, and the concurrency level at which the cluster degrades. Outputs a full performance report with an optional LLM narrative.

### 2. LLM Report Writer
Assembles structured Markdown reports from stress-test and diagnostic JSON artefacts using Jinja2 templates. Calls the configured LLM to produce an executive summary and bottleneck narrative. Supports OpenAI, Anthropic, and OpenRouter interchangeably via a single `LLMClient` abstraction.

### 3. Data Product Candidate Discovery
Connects to DataHub via MCP, searches datasets, and scores each one across eight signals: query volume, unique users, downstream dashboard/dataset count, ownership, team coverage, description presence, and schema richness. Ranks datasets as data product candidates and writes a scored report. LLM justifications are generated in a single batched API call. Entity metadata is fetched in one batched MCP call; usage and lineage are opt-in flags.

### 4. Golden Report Finder
Scores Superset dashboards for "golden" candidacy using signals from the Superset REST API: view counts, certification status, owner coverage, description quality, chart count, error rate, and load performance. Detects near-duplicate dashboards via chart-set Jaccard similarity. Recommends which dashboards should be promoted as the single source of truth for a business area.

### 5. Noisy Neighbor Detector
Correlates Superset dashboard activity logs with Trino query execution records to identify dashboards, users, or time windows that consume a disproportionate share of shared query capacity. Produces a ranked list of offenders across four dimensions (user, database, dashboard, time-of-day) with severity scores and correlated cost estimates.

### 6. Duplicate Dashboard & Metric Detector
Fetches all dashboards from DataHub MCP and compares them pairwise across four signals: name similarity (`difflib`), chart-set Jaccard overlap, upstream dataset-set Jaccard overlap (opt-in lineage), and shared business glossary terms. An optional fifth signal uses SHA-256 fingerprints of normalised Superset `query_context` JSON for SQL-level comparison. Overlapping pairs are clustered into consolidation groups via union-find so transitive duplicates (A≈B, B≈C) surface as a single group. Each group gets a heuristic "keep/deprecate" recommendation and an optional LLM consolidation note. Fully read-only — no changes are made to DataHub or Superset.

---

## Suggested slide structure

1. Title slide
2. Problem statement — why data mesh operations are hard to govern at scale
3. Solution overview / architecture diagram description
4. One slide per skill (6 slides)
5. How skills compose into workflows
6. LLM integration layer
7. What's next / roadmap
