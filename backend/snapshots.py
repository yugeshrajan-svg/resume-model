"""Persist non-secret engine settings; reject execution by incompatible code rather than silently changing scores."""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from importlib.metadata import version
from pathlib import Path
from typing import Any

from pydantic import AliasChoices

from engine.config import Settings

_SECRETS = {"azure_openai_api_key", "azure_docintel_key", "api_key"}


class SnapshotMismatch(RuntimeError):
    """The worker cannot execute this run's frozen engine version."""


@lru_cache(maxsize=1)
def engine_fingerprint() -> str:
    root = Path(__file__).resolve().parents[1]
    paths = [
        *sorted((root / "engine").glob("*.py")),
        *sorted((root / "engine").glob("*.json")),
        root / "backend/services/screening.py",
        root / "backend/snapshots.py",
    ]
    parts = {
        str(path.relative_to(root)).replace("\\", "/"): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths
    }
    parts["dependencies"] = {name: version(name) for name in ("numpy", "scikit-learn", "pydantic", "openai")}
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()


def freeze_settings(settings: Settings) -> dict[str, Any]:
    data = settings.model_dump(mode="json", exclude=_SECRETS)
    data["extraction_mode"] = settings.resolved_extraction_mode
    data["semantic_backend"] = settings.resolved_semantic_backend
    return data


def restore_settings(saved: dict[str, Any], current: Settings) -> Settings:
    # Explicit constructor aliases prevent BaseSettings from filling saved values from the worker environment.
    data = {**current.model_dump(), **saved}
    aliased = {}
    for name, field in Settings.model_fields.items():
        alias = field.validation_alias
        key = alias.choices[0] if isinstance(alias, AliasChoices) else alias or name
        aliased[key] = data[name]
    return Settings(_env_file=None, **aliased)


def validate_snapshot(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    if not snapshot or snapshot.get("format") != 1 or snapshot.get("fingerprint") != engine_fingerprint():
        raise SnapshotMismatch("Create a new run or use the original worker version")
    return snapshot
