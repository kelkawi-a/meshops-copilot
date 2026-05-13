"""Unit tests for report_writer.markdown and ReportWriterSkill."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from meshops_copilot.skills.report_writer.markdown import (
    build_llm_prompt,
    format_stress_report,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _minimal_results() -> dict:
    return {
        "target": "https://trino.example.com",
        "scenario": "test",
        "query_source": "builtin",
        "discovered_tables": [],
        "generated_queries": {},
        "baseline": {
            "light_count": {"times": [0.5, 0.6, 0.55], "errors": [], "peak_mem_mb": 128.0},
            "heavy_join":  {"times": [2.1, 2.4, 2.2],  "errors": [], "peak_mem_mb": 512.0},
        },
        "concurrency": {
            "1":  {"completed": 5, "errors": 0, "qps": 2.1, "p50": 0.5, "p95": 0.6, "p99": 0.7, "wall": 2.4},
            "4":  {"completed": 5, "errors": 0, "qps": 5.8, "p50": 0.7, "p95": 1.1, "p99": 1.4, "wall": 0.9},
            "8":  {"completed": 4, "errors": 1, "qps": 6.2, "p50": 1.2, "p95": 2.1, "p99": 3.0, "wall": 0.8},
            "16": {"completed": 2, "errors": 3, "qps": 3.1, "p50": 4.5, "p95": 8.0, "p99": 9.0, "wall": 1.6},
        },
        "breaking": {
            "16": {"completed": 3, "errors": 2, "qps": 3.0, "p50": 4.0, "p99": 9.0, "wall": 1.5},
            "32": {"completed": 1, "errors": 4, "qps": 1.2, "p50": 8.0, "p99": 15.0, "wall": 4.1},
        },
        "memory": {},
        "mixed": {
            "wall": 12.4,
            "timings": {
                "light_count": [0.4, 0.5],
                "heavy_join":  [2.1, 2.3],
            },
            "errors": {"heavy_join": []},
            "docker_mid": {},
            "docker_end": {},
            "cluster": {},
        },
    }


# ── format_stress_report ──────────────────────────────────────────────────────

def test_format_stress_report_contains_target():
    md = format_stress_report(_minimal_results())
    assert "trino.example.com" in md


def test_format_stress_report_contains_baseline_queries():
    md = format_stress_report(_minimal_results())
    assert "light_count" in md
    assert "heavy_join" in md


def test_format_stress_report_baseline_medians():
    md = format_stress_report(_minimal_results())
    # median of [0.5, 0.6, 0.55] = 0.55
    assert "0.55s" in md


def test_format_stress_report_concurrency_section():
    md = format_stress_report(_minimal_results())
    assert "Concurrency Ramp" in md
    assert "QPS" in md


def test_format_stress_report_breaking_section():
    md = format_stress_report(_minimal_results())
    assert "Breaking Point" in md


def test_format_stress_report_breaking_threshold_detected():
    """Breaking point annotation appears when ≥50% error rate is detected."""
    md = format_stress_report(_minimal_results())
    # 32 workers: 4 errors / 5 total = 80% → should be annotated
    assert "32 concurrent workers" in md


def test_format_stress_report_llm_narrative_embedded():
    md = format_stress_report(_minimal_results(), llm_narrative="Great cluster, very fast.")
    assert "Analysis" in md
    assert "Great cluster, very fast." in md


def test_format_stress_report_mixed_section():
    md = format_stress_report(_minimal_results())
    assert "Mixed Workload" in md
    assert "light_count" in md
    assert "12.40s" in md  # wall time


def test_format_stress_report_empty_phases_ok():
    """Report renders without error when optional phases are absent."""
    data = {
        "target": "http://localhost:8080",
        "scenario": "minimal",
        "query_source": "explicit",
        "baseline": {"q": {"times": [1.0], "errors": [], "peak_mem_mb": 64.0}},
        "concurrency": {},
        "breaking": {},
        "memory": {},
        "mixed": {},
    }
    md = format_stress_report(data)
    assert "Baseline" in md
    assert "Concurrency" not in md


# ── build_llm_prompt ──────────────────────────────────────────────────────────

def test_build_llm_prompt_contains_target():
    prompt = build_llm_prompt(_minimal_results())
    assert "trino.example.com" in prompt


def test_build_llm_prompt_contains_baseline_numbers():
    prompt = build_llm_prompt(_minimal_results())
    assert "light_count" in prompt
    assert "heavy_join" in prompt


def test_build_llm_prompt_contains_concurrency():
    prompt = build_llm_prompt(_minimal_results())
    assert "Concurrency ramp" in prompt


def test_build_llm_prompt_contains_instructions():
    prompt = build_llm_prompt(_minimal_results())
    assert "actionable" in prompt or "recommendations" in prompt.lower()


# ── ReportWriterSkill ─────────────────────────────────────────────────────────

def test_report_writer_missing_file(tmp_path):
    from meshops_copilot.core.config import MeshOpsConfig
    from meshops_copilot.skills.report_writer.skill import ReportWriterSkill

    skill = ReportWriterSkill(MeshOpsConfig(), output_dir=tmp_path)
    result = skill.run(results_files=["nonexistent.json"])
    assert result.status.value == "failed"
    assert any("not found" in e for e in result.errors)


def test_report_writer_writes_markdown(tmp_path):
    from meshops_copilot.core.config import MeshOpsConfig
    from meshops_copilot.skills.report_writer.skill import ReportWriterSkill

    results_file = tmp_path / "stress_results.json"
    results_file.write_text(json.dumps(_minimal_results()))

    skill = ReportWriterSkill(MeshOpsConfig(), output_dir=tmp_path / "reports")
    result = skill.run(results_files=[str(results_file)], no_llm=True)

    assert result.status.value == "ok"
    assert (tmp_path / "reports" / "report.md").exists()
    content = (tmp_path / "reports" / "report.md").read_text()
    assert "trino.example.com" in content
    assert "Baseline" in content


def test_report_writer_skips_llm_when_no_key(tmp_path):
    from meshops_copilot.core.config import MeshOpsConfig
    from meshops_copilot.skills.report_writer.skill import ReportWriterSkill

    results_file = tmp_path / "stress_results.json"
    results_file.write_text(json.dumps(_minimal_results()))

    cfg = MeshOpsConfig()
    cfg.llm.provider = "openai"
    cfg.llm.api_key = ""  # no key

    skill = ReportWriterSkill(cfg, output_dir=tmp_path / "reports")
    result = skill.run(results_files=[str(results_file)])
    # Should succeed without LLM
    assert result.status.value == "ok"


def test_report_writer_calls_llm_when_configured(tmp_path):
    from meshops_copilot.core.config import MeshOpsConfig
    from meshops_copilot.skills.report_writer.skill import ReportWriterSkill

    results_file = tmp_path / "stress_results.json"
    results_file.write_text(json.dumps(_minimal_results()))

    cfg = MeshOpsConfig()
    cfg.llm.provider = "openai"
    cfg.llm.api_key = "sk-test"

    with patch("meshops_copilot.skills.report_writer.skill.LLMClient") as MockLLM:
        MockLLM.return_value.complete.return_value = "Cluster looks healthy."
        skill = ReportWriterSkill(cfg, output_dir=tmp_path / "reports")
        result = skill.run(results_files=[str(results_file)])

    assert result.status.value == "ok"
    content = (tmp_path / "reports" / "report.md").read_text()
    assert "Cluster looks healthy." in content
    MockLLM.return_value.complete.assert_called_once()


def test_report_writer_calls_llm_openrouter(tmp_path):
    from meshops_copilot.core.config import MeshOpsConfig
    from meshops_copilot.skills.report_writer.skill import ReportWriterSkill

    results_file = tmp_path / "stress_results.json"
    results_file.write_text(json.dumps(_minimal_results()))

    cfg = MeshOpsConfig()
    cfg.llm.provider = "openrouter"
    cfg.llm.api_key = "sk-or-test"
    cfg.llm.model = "openai/gpt-4o"

    with patch("meshops_copilot.skills.report_writer.skill.LLMClient") as MockLLM:
        MockLLM.return_value.complete.return_value = "OpenRouter analysis here."
        skill = ReportWriterSkill(cfg, output_dir=tmp_path / "reports")
        result = skill.run(results_files=[str(results_file)])

    assert result.status.value == "ok"
    content = (tmp_path / "reports" / "report.md").read_text()
    assert "OpenRouter analysis here." in content
    MockLLM.return_value.complete.assert_called_once()
