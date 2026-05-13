#!/usr/bin/env python3
"""generate-report.py — compile skill JSON outputs into a MeshOps Copilot Report.

Usage:
    python scripts/generate-report.py --input reports/ --output reports/report.md
"""

from __future__ import annotations

import argparse

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="reports/")
    parser.add_argument("--output", default="reports/report.md")
    args = parser.parse_args()
    # TODO: implement once report_writer skill is complete
    print(f"generate-report: not yet implemented. (input={args.input}, output={args.output})")

if __name__ == "__main__":
    main()
