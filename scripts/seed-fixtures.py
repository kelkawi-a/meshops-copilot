#!/usr/bin/env python3
"""seed-fixtures.py — populate tests/fixtures/ with real data from a live stack.

Usage:
    python scripts/seed-fixtures.py --trino-url http://localhost:8080
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

FIXTURES = Path(__file__).parent.parent / "tests" / "fixtures"


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed test fixtures from live services.")
    parser.add_argument("--trino-url", default="http://localhost:8080")
    args = parser.parse_args()

    print(f"Seeding fixtures from {args.trino_url} → {FIXTURES}")
    # TODO: implement fixture capture once connectors are complete
    print("seed-fixtures: not yet implemented.")


if __name__ == "__main__":
    main()
