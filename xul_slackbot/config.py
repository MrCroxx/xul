from __future__ import annotations

import os
from pathlib import Path


DEFAULT_DOTENV_PATH = Path(".env")


def load_dotenv(path: str | Path = DEFAULT_DOTENV_PATH) -> dict[str, str]:
    dotenv_path = Path(path)
    if not dotenv_path.exists():
        return {}

    values: dict[str, str] = {}
    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def get_required_config_value(key: str, path: str | Path = DEFAULT_DOTENV_PATH) -> str:
    env_value = os.getenv(key, "").strip()
    if env_value:
        return env_value

    values = load_dotenv(path)
    value = values.get(key, "").strip()
    if not value:
        raise ValueError(
            f"Missing required `{key}` in environment or {Path(path)}."
        )
    return value


def get_config_value(
    key: str,
    default: str,
    path: str | Path = DEFAULT_DOTENV_PATH,
) -> str:
    env_value = os.getenv(key)
    if env_value is not None:
        stripped = env_value.strip()
        return stripped if stripped else default

    values = load_dotenv(path)
    value = values.get(key)
    if value is None:
        return default
    stripped = value.strip()
    return stripped if stripped else default
