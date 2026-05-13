"""Integration test: TrinoConnector against a live cluster.

Requires TRINO_URL to point to a running Trino instance.
Skip automatically if the cluster is unreachable.
"""

from __future__ import annotations

import os

import pytest

from meshops_copilot.connectors.trino import TrinoConnector


TRINO_URL = os.getenv("TRINO_URL", "http://localhost:8080")


def _reachable() -> bool:
    import urllib.request

    try:
        urllib.request.urlopen(f"{TRINO_URL}/v1/info", timeout=3)
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _reachable(), reason="Trino cluster not reachable")
def test_simple_query():
    conn = TrinoConnector(url=TRINO_URL, user="test")
    elapsed, stats, error = conn.execute("SELECT 1")
    assert error is None
    assert elapsed is not None and elapsed > 0


@pytest.mark.skipif(not _reachable(), reason="Trino cluster not reachable")
def test_cluster_stats():
    conn = TrinoConnector(url=TRINO_URL, user="test")
    stats = conn.cluster_stats()
    assert isinstance(stats, dict)
