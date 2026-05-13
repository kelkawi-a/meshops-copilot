"""Unit tests for core config loading."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from meshops_copilot.core.config import load_config


def test_defaults():
    cfg = load_config()
    assert cfg.trino.url == "http://localhost:8080"
    assert cfg.trino.user == "meshops"
    assert cfg.llm.provider == "none"


def test_env_override(monkeypatch):
    monkeypatch.setenv("TRINO_URL", "http://my-trino:8080")
    monkeypatch.setenv("TRINO_USER", "tester")
    cfg = load_config()
    assert cfg.trino.url == "http://my-trino:8080"
    assert cfg.trino.user == "tester"


# ── Priority order tests ───────────────────────────────────────────────────────

def test_scenario_defaults_used_when_nothing_else_set():
    """Scenario trino: block is used when no env var is present."""
    scenario = {"trino": {"url": "http://scenario-trino:8080", "user": "scenario-user"}}
    cfg = load_config(scenario_defaults=scenario)
    assert cfg.trino.url == "http://scenario-trino:8080"
    assert cfg.trino.user == "scenario-user"


def test_env_wins_over_scenario_defaults(monkeypatch):
    """Env vars beat scenario YAML trino: block."""
    monkeypatch.setenv("TRINO_URL", "http://env-trino:8080")
    scenario = {"trino": {"url": "http://scenario-trino:8080"}}
    cfg = load_config(scenario_defaults=scenario)
    assert cfg.trino.url == "http://env-trino:8080"


# ── LLM provider / API key tests ──────────────────────────────────────────────

def test_openrouter_api_key_loaded(monkeypatch):
    """OPENROUTER_API_KEY is preferred when provider=openrouter."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-key")
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    cfg = load_config()
    assert cfg.llm.provider == "openrouter"
    assert cfg.llm.api_key == "sk-or-test-key"


def test_openrouter_key_wins_over_openai_when_provider_is_openrouter(monkeypatch):
    """When provider=openrouter, OPENROUTER_API_KEY beats OPENAI_API_KEY."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-key")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    cfg = load_config()
    assert cfg.llm.api_key == "sk-or-key"


def test_openai_key_takes_priority_over_openrouter(monkeypatch):
    """When provider=openai, OPENAI_API_KEY beats OPENROUTER_API_KEY."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-key")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    cfg = load_config()
    assert cfg.llm.api_key == "sk-openai-key"
