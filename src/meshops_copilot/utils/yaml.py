"""YAML loading helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def load_yaml(path: str | Path) -> Any:
    with open(path) as fh:
        return yaml.safe_load(fh)


def dump_yaml(data: Any, path: str | Path) -> None:
    with open(path, "w") as fh:
        yaml.safe_dump(data, fh, sort_keys=False)
