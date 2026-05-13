#!/usr/bin/env bash
# run-demo.sh — end-to-end demo: light check → high concurrency → report

set -euo pipefail

CONFIG="${MESHOPS_CONFIG:-config/local.yaml}"
REPORT_DIR="${MESHOPS_OUTPUT_DIR:-./reports}"

mkdir -p "$REPORT_DIR"

echo "=== MeshOps Copilot Demo ==="
echo "Config : $CONFIG"
echo "Output : $REPORT_DIR"
echo ""

echo "--- Step 1: Light connectivity check ---"
meshops --config "$CONFIG" stress run \
  --scenario scenarios/trino/light.yaml \
  --output "$REPORT_DIR/light.json"

echo ""
echo "--- Step 2: High-concurrency stress test ---"
meshops --config "$CONFIG" stress run \
  --scenario scenarios/trino/high_concurrency.yaml \
  --output "$REPORT_DIR/high_concurrency.json"

echo ""
echo "--- Done. Results in $REPORT_DIR/ ---"
