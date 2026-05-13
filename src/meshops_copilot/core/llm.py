"""LLM client wrapper (optional dependency).

Falls back gracefully when no API key is configured or when the
``openai`` / ``anthropic`` packages are not installed.

Supported providers
-------------------
- ``openai``      — OpenAI API (requires ``openai`` package)
- ``anthropic``   — Anthropic Claude API (requires ``anthropic`` package)
- ``openrouter``  — OpenRouter unified gateway (uses ``openai`` package;
                    routes to any model via ``https://openrouter.ai/api/v1``)
- ``none``        — disabled
"""

from __future__ import annotations

from meshops_copilot.core.config import LLMConfig
from meshops_copilot.core.logging import get_logger

log = get_logger(__name__)

_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class LLMClient:
    """Thin wrapper that routes to the configured LLM provider."""

    def __init__(self, cfg: LLMConfig) -> None:
        self._cfg = cfg

    def complete(self, prompt: str, system: str = "") -> str:
        """Return a completion string, or an empty string if LLM is disabled."""
        if self._cfg.provider == "none" or not self._cfg.api_key:
            log.debug("LLM provider is 'none' or no API key — skipping completion.")
            return ""

        if self._cfg.provider == "openai":
            return self._openai(prompt, system)
        if self._cfg.provider == "anthropic":
            return self._anthropic(prompt, system)
        if self._cfg.provider == "openrouter":
            return self._openrouter(prompt, system)

        log.warning("Unknown LLM provider: %s", self._cfg.provider)
        return ""

    # ── Provider implementations ───────────────────────────────────────────────

    def _openai(self, prompt: str, system: str) -> str:
        try:
            import openai  # noqa: PLC0415
        except ModuleNotFoundError:
            log.error(
                "openai package is not installed. "
                "Run: pip install openai  or  uv pip install '.[llm]'"
            )
            return ""
        try:
            client = openai.OpenAI(api_key=self._cfg.api_key)
            messages = []
            if system:
                messages.append({"role": "system", "content": system})
            messages.append({"role": "user", "content": prompt})
            resp = client.chat.completions.create(model=self._cfg.model, messages=messages)
            return resp.choices[0].message.content or ""
        except Exception as exc:  # noqa: BLE001
            log.error("OpenAI completion failed: %s", exc)
            return ""

    def _openrouter(self, prompt: str, system: str) -> str:
        """OpenRouter uses the OpenAI-compatible API at a different base URL."""
        try:
            import openai  # noqa: PLC0415
        except ModuleNotFoundError:
            log.error(
                "openai package is not installed. "
                "Run: pip install openai  or  uv pip install '.[llm]'"
            )
            return ""
        try:
            client = openai.OpenAI(
                api_key=self._cfg.api_key,
                base_url=_OPENROUTER_BASE_URL,
            )
            messages = []
            if system:
                messages.append({"role": "system", "content": system})
            messages.append({"role": "user", "content": prompt})
            resp = client.chat.completions.create(model=self._cfg.model, messages=messages)
            return resp.choices[0].message.content or ""
        except Exception as exc:  # noqa: BLE001
            log.error("OpenRouter completion failed: %s", exc)
            return ""

    def _anthropic(self, prompt: str, system: str) -> str:
        try:
            import anthropic  # noqa: PLC0415
        except ModuleNotFoundError:
            log.error(
                "anthropic package is not installed. "
                "Run: pip install anthropic  or  uv pip install '.[llm]'"
            )
            return ""
        try:
            client = anthropic.Anthropic(api_key=self._cfg.api_key)
            kwargs: dict = {
                "model": self._cfg.model,
                "max_tokens": 4096,
                "messages": [{"role": "user", "content": prompt}],
            }
            if system:
                kwargs["system"] = system
            resp = client.messages.create(**kwargs)
            return resp.content[0].text if resp.content else ""
        except Exception as exc:  # noqa: BLE001
            log.error("Anthropic completion failed: %s", exc)
            return ""
