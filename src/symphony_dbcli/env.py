from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_LOCAL_ENV_FILE = ".symphony/github-app.env"


@dataclass(frozen=True)
class EnvLoadResult:
    path: Path
    loaded: tuple[str, ...]
    skipped: tuple[str, ...]


def load_local_env(path: str | Path = DEFAULT_LOCAL_ENV_FILE) -> EnvLoadResult:
    env_path = Path(path)
    if not env_path.exists():
        return EnvLoadResult(path=env_path, loaded=(), skipped=())

    loaded: list[str] = []
    skipped: list[str] = []
    for key, value in parse_env_file(env_path).items():
        if key in os.environ:
            skipped.append(key)
            continue
        os.environ[key] = value
        loaded.append(key)
    return EnvLoadResult(path=env_path, loaded=tuple(loaded), skipped=tuple(skipped))


def parse_env_file(path: str | Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        parsed = _parse_env_line(line)
        if parsed is None:
            continue
        key, value = parsed
        values[key] = value
    return values


def _parse_env_line(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    key, raw_value = stripped.split("=", 1)
    key = key.strip()
    value = raw_value.strip()
    if not key or not value:
        return None
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        value = value[1:-1]
    return key, value
