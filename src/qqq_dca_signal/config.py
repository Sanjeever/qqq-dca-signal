from __future__ import annotations

import os
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml


SECRET_KEYS = {"api_key", "token", "tokens", "key", "keys", "apikey", "password", "secret"}
ENV_PATTERN = re.compile(r"^\$\{([A-Z0-9_]+)\}$")


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_config(config_path: Path | None = None) -> dict[str, Any]:
    root = project_root()
    default_path = root / "config.example.yaml"
    with default_path.open("r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}

    path = config_path or root / "config.yaml"
    if path.exists():
        with path.open("r", encoding="utf-8") as f:
            override = yaml.safe_load(f) or {}
        config = merge_dict(config, override)

    return resolve_env(config)


def merge_dict(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_dict(result[key], value)
        else:
            result[key] = value
    return result


def resolve_env(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: resolve_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_env(v) for v in value]
    if isinstance(value, str):
        match = ENV_PATTERN.match(value)
        if match:
            return os.environ.get(match.group(1), "")
    return value


def mask_secrets(value: Any, parent_key: str = "") -> Any:
    if isinstance(value, dict):
        return {k: mask_secrets(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [mask_secrets(v, parent_key) for v in value]
    if parent_key.lower() in SECRET_KEYS and value:
        return "********"
    return value


def resolve_project_path(path_value: str | Path) -> Path:
    path = Path(path_value).expanduser()
    if path.is_absolute():
        return path
    return project_root() / path
