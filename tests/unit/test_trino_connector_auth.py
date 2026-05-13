"""Unit tests for TrinoConnector authentication header building."""

from __future__ import annotations

import base64

from meshops_copilot.connectors.trino import TrinoConnector


def test_no_auth_headers():
    conn = TrinoConnector(url="http://trino:8080", user="alice")
    h = conn._headers()
    assert h["X-Trino-User"] == "alice"
    assert "Authorization" not in h


def test_basic_auth_header_present():
    conn = TrinoConnector(url="http://trino:8080", user="alice", password="s3cr3t")
    h = conn._headers()
    assert "Authorization" in h
    assert h["Authorization"].startswith("Basic ")


def test_basic_auth_header_correct_encoding():
    conn = TrinoConnector(url="http://trino:8080", user="alice", password="s3cr3t")
    h = conn._headers()
    expected = base64.b64encode(b"alice:s3cr3t").decode()
    assert h["Authorization"] == f"Basic {expected}"


def test_basic_auth_includes_trino_user():
    """X-Trino-User must be set even when Basic Auth is used."""
    conn = TrinoConnector(url="http://trino:8080", user="alice", password="s3cr3t")
    h = conn._headers()
    assert h["X-Trino-User"] == "alice"


def test_content_type_included_when_requested():
    conn = TrinoConnector(url="http://trino:8080", user="alice")
    h = conn._headers(content_type="text/plain")
    assert h["Content-Type"] == "text/plain"


def test_content_type_absent_when_not_requested():
    conn = TrinoConnector(url="http://trino:8080", user="alice")
    h = conn._headers()
    assert "Content-Type" not in h


def test_url_trailing_slash_stripped():
    conn = TrinoConnector(url="http://trino:8080/", user="alice")
    assert conn.url == "http://trino:8080"


def test_verify_ssl_default_true():
    conn = TrinoConnector(url="https://trino:8443", user="alice")
    assert conn._ssl_ctx is None  # None = use default verified context


def test_verify_ssl_false_creates_context():
    conn = TrinoConnector(url="https://trino:8443", user="alice", verify_ssl=False)
    assert conn._ssl_ctx is not None
