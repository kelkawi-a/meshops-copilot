"""Unit test configuration.

Prevents ``load_dotenv`` from reading the real ``.env`` file during unit
tests so that assertions about defaults and YAML overrides are not
contaminated by local credentials.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _no_dotenv():
    """Patch load_dotenv to a no-op for every unit test."""
    with patch("meshops_copilot.core.config.load_dotenv"):
        yield
